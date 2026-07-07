"""Tests for token_self_refresh (2026-07-02, GH #13 follow-up).

Background: `token_stale` (an inactive account's stored access token aged
out, refresh token still valid) previously always fell through to a benign
display flag — `cus` never attempted to actually refresh it, because doing
so meant talking to Anthropic's undocumented OAuth token endpoint. That
endpoint (URL + client_id) was extracted directly from the installed
`claude` CLI binary's own OAuth client config via `strings` — it's the exact
request the CLI itself makes to silently refresh a live account's token.

`_refresh_account_token` attempts that refresh before `poll_account_usage`
gives up and flags `token_stale`. It is contractually "fail silent": ANY
failure (disabled, missing creds, network error, malformed response) must
return False and change nothing on disk — the caller's fallback to the
pre-existing token_stale behavior is what makes this safe to ship against an
undocumented endpoint.

Run standalone:  python3 tests/test_token_self_refresh.py
Or under pytest: pytest tests/test_token_self_refresh.py
"""

import json
import sys
import tempfile
import time
import urllib.error
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import cus  # noqa: E402


class _FakeResponse:
    """Minimal stand-in for the object `with urllib.request.urlopen(...) as resp:`
    binds — just enough of the context-manager + .read() protocol our code uses."""

    def __init__(self, payload: bytes):
        self._payload = payload

    def read(self, *_args):
        return self._payload

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class _Env:
    """Throwaway ~/claude-accounts/ + state.json + config.yaml, with cus's
    module-level path constants repointed. Plain setattr + restore() so this
    also runs standalone without pytest fixtures."""

    def __init__(self, config_yaml: str | None = None):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self._saved = {k: getattr(cus, k) for k in
                       ("ACCOUNTS_DIR", "STATE_JSON", "CONFIG_YAML", "CREDS_JSON")}
        cus.ACCOUNTS_DIR = self.root / "claude-accounts"
        cus.ACCOUNTS_DIR.mkdir()
        cus.STATE_JSON = self.root / "state.json"
        cus.STATE_JSON.write_text(json.dumps({"active": "other", "accounts": {}, "swap_history": []}))
        cus.CREDS_JSON = self.root / "live-credentials.json"  # never the active account in these tests
        config_path = self.root / "config.yaml"
        if config_yaml is not None:
            config_path.write_text(config_yaml)
        cus.CONFIG_YAML = config_path

    def write_creds(self, account: str, oauth: dict) -> Path:
        d = cus.ACCOUNTS_DIR / f"account-{account}"
        d.mkdir(parents=True, exist_ok=True)
        p = d / ".credentials.json"
        p.write_text(json.dumps({"claudeAiOauth": oauth}))
        return p

    def read_creds(self, account: str) -> dict:
        p = cus.ACCOUNTS_DIR / f"account-{account}" / ".credentials.json"
        return json.loads(p.read_text())

    def restore(self):
        for k, v in self._saved.items():
            setattr(cus, k, v)
        self._tmp.cleanup()


def _stub_urlopen(fn):
    """Swap cus.urllib.request.urlopen for `fn`; returns a restore callback."""
    original = cus.urllib.request.urlopen
    cus.urllib.request.urlopen = fn
    return lambda: setattr(cus.urllib.request, "urlopen", original)


def _stale_oauth(**extra) -> dict:
    """A refreshToken-bearing oauth block whose accessToken expired 40 min ago."""
    oauth = {
        "accessToken": "old-access-token",
        "refreshToken": "old-refresh-token",
        "expiresAt": int(time.time() * 1000) - 40 * 60 * 1000,
        "scopes": ["user:inference", "user:profile"],
    }
    oauth.update(extra)
    return oauth


# --- _refresh_account_token, isolated ---------------------------------------


def test_refresh_success_updates_creds_and_backs_up():
    env = _Env()
    calls = []
    try:
        env.write_creds("a", _stale_oauth())

        def fake_urlopen(req, timeout=None):
            calls.append(req.full_url)
            payload = json.dumps({
                "access_token": "new-access-token",
                "refresh_token": "new-refresh-token",
                "expires_in": 3600,
                "scope": "user:inference user:profile",
            }).encode()
            return _FakeResponse(payload)

        unstub = _stub_urlopen(fake_urlopen)
        try:
            ok = cus._refresh_account_token("a")
        finally:
            unstub()

        assert ok is True
        assert calls == [cus.OAUTH_TOKEN_URL]
        creds = env.read_creds("a")
        oauth = creds["claudeAiOauth"]
        assert oauth["accessToken"] == "new-access-token"
        assert oauth["refreshToken"] == "new-refresh-token"
        assert oauth["expiresAt"] > int(time.time() * 1000) + 3500 * 1000
        assert oauth["scopes"] == ["user:inference", "user:profile"]
        backups = list((cus.ACCOUNTS_DIR / "account-a").glob(".credentials.json.bak.*"))
        assert len(backups) == 1, "old creds must be backed up before overwrite (GH #79 discipline)"
    finally:
        env.restore()


def test_refresh_preserves_refresh_token_when_response_omits_it():
    """Some refresh responses don't rotate the refresh token — keep the old one."""
    env = _Env()
    try:
        env.write_creds("a", _stale_oauth())

        def fake_urlopen(req, timeout=None):
            payload = json.dumps({"access_token": "new-access-token", "expires_in": 3600}).encode()
            return _FakeResponse(payload)

        unstub = _stub_urlopen(fake_urlopen)
        try:
            ok = cus._refresh_account_token("a")
        finally:
            unstub()

        assert ok is True
        assert env.read_creds("a")["claudeAiOauth"]["refreshToken"] == "old-refresh-token"
    finally:
        env.restore()


def test_refresh_disabled_via_config_never_calls_network():
    env = _Env(config_yaml="token_self_refresh:\n  enabled: false\n")
    calls = []
    try:
        env.write_creds("a", _stale_oauth())
        unstub = _stub_urlopen(lambda req, timeout=None: calls.append(1) or _FakeResponse(b"{}"))
        try:
            ok = cus._refresh_account_token("a")
        finally:
            unstub()
        assert ok is False
        assert calls == []
        assert env.read_creds("a")["claudeAiOauth"]["accessToken"] == "old-access-token"
    finally:
        env.restore()


def test_refresh_no_creds_file():
    env = _Env()
    try:
        assert cus._refresh_account_token("nonexistent") is False
    finally:
        env.restore()


def test_refresh_missing_refresh_token_never_calls_network():
    env = _Env()
    calls = []
    try:
        env.write_creds("a", {"accessToken": "x", "expiresAt": 0})  # no refreshToken key
        unstub = _stub_urlopen(lambda req, timeout=None: calls.append(1) or _FakeResponse(b"{}"))
        try:
            ok = cus._refresh_account_token("a")
        finally:
            unstub()
        assert ok is False
        assert calls == []
    finally:
        env.restore()


def test_refresh_http_error_falls_back_without_touching_creds():
    env = _Env()
    try:
        env.write_creds("a", _stale_oauth())

        def raise_http_error(req, timeout=None):
            raise urllib.error.HTTPError(req.full_url, 400, "Bad Request", {}, None)

        unstub = _stub_urlopen(raise_http_error)
        try:
            ok = cus._refresh_account_token("a")
        finally:
            unstub()
        assert ok is False
        assert env.read_creds("a")["claudeAiOauth"]["accessToken"] == "old-access-token"
    finally:
        env.restore()


def test_refresh_malformed_response_falls_back():
    env = _Env()
    try:
        env.write_creds("a", _stale_oauth())
        # Missing access_token entirely.
        unstub = _stub_urlopen(lambda req, timeout=None: _FakeResponse(json.dumps({"expires_in": 3600}).encode()))
        try:
            ok = cus._refresh_account_token("a")
        finally:
            unstub()
        assert ok is False
        assert env.read_creds("a")["claudeAiOauth"]["accessToken"] == "old-access-token"
    finally:
        env.restore()


# --- poll_account_usage integration ------------------------------------------


def test_poll_account_usage_self_refresh_success_falls_through_to_normal_poll():
    """A successful self-refresh must not short-circuit to token_stale — it
    should re-read the fresh token and complete a normal usage poll."""
    env = _Env()
    try:
        env.write_creds("a", _stale_oauth())

        def fake_urlopen(req, timeout=None):
            if req.full_url == cus.OAUTH_TOKEN_URL:
                return _FakeResponse(json.dumps({
                    "access_token": "new-access-token",
                    "refresh_token": "new-refresh-token",
                    "expires_in": 3600,
                }).encode())
            assert req.full_url == cus.USAGE_API_URL
            assert req.headers.get("Authorization") == "Bearer new-access-token"
            return _FakeResponse(json.dumps({
                "five_hour": {"utilization": 12.0, "resets_at": None},
                "seven_day": {"utilization": 5.0, "resets_at": None},
            }).encode())

        unstub = _stub_urlopen(fake_urlopen)
        try:
            usage = cus.poll_account_usage("a")
        finally:
            unstub()

        assert usage.token_stale is False
        assert usage.five_hour.utilization == 12.0
    finally:
        env.restore()


def test_poll_account_usage_self_refresh_failure_still_flags_token_stale():
    """Refresh fails → identical to pre-2026-07-02 behavior: token_stale with
    an accurate expired_minutes_ago, and the usage endpoint is never hit."""
    env = _Env()
    calls = []
    try:
        env.write_creds("a", _stale_oauth())

        def raise_http_error(req, timeout=None):
            calls.append(req.full_url)
            raise urllib.error.HTTPError(req.full_url, 400, "Bad Request", {}, None)

        unstub = _stub_urlopen(raise_http_error)
        try:
            usage = cus.poll_account_usage("a")
        finally:
            unstub()

        assert usage.token_stale is True
        assert usage.raw["expired_minutes_ago"] >= 39  # matches _stale_oauth's ~40min staleness
        assert calls == [cus.OAUTH_TOKEN_URL], "usage endpoint must not be hit after a failed refresh"
    finally:
        env.restore()


def _run_all() -> int:
    import types
    tests = [v for k, v in globals().items()
             if k.startswith("test_") and isinstance(v, types.FunctionType)]
    failed = 0
    for t in tests:
        try:
            t()
            print(f"PASS  {t.__name__}")
        except Exception as e:  # noqa: BLE001
            failed += 1
            print(f"FAIL  {t.__name__}: {e}")
    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(_run_all())
