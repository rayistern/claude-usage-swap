"""Tests for GH #68 — `cus poll` must label a benign stale access token as
TOKEN_STALE, not "ERROR".

Background: poll_account_usage() classifies an aged-out stored access token
(inactive account, refresh token still valid) as `token_stale` — a benign,
self-healing condition (GH #13). It ALSO sets raw["error"] with a descriptive
string for --json consumers. The `poll` command used to check only
`token_expired` before the generic `raw["error"]` branch, so token_stale fell
through and printed a scary `ERROR:` line. `force-poll` already had the
friendly TOKEN_STALE branch; these tests pin the two commands to matching
behavior so they can't drift apart again.

Run standalone:  python3 tests/test_poll_token_stale.py
Or under pytest: pytest tests/test_poll_token_stale.py
"""

import json
import sys
import tempfile
from pathlib import Path

from click.testing import CliRunner

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import cus  # noqa: E402


def _stale_usage() -> "cus.AccountUsage":
    """AccountUsage exactly as poll_account_usage returns it for a stale
    stored token: token_stale set AND raw["error"] populated. The raw error
    text being present is the whole point — it's what used to leak into the
    ERROR: branch."""
    u = cus.AccountUsage.empty()
    u.token_stale = True
    u.raw = {
        "error": "stored access token expired (refresh token still valid)",
        "expired_minutes_ago": 42,
    }
    return u


def _expired_usage() -> "cus.AccountUsage":
    u = cus.AccountUsage.empty()
    u.token_expired = True
    u.raw = {"error": "HTTP 401 despite non-expired stored token: ..."}
    return u


def _error_usage() -> "cus.AccountUsage":
    u = cus.AccountUsage.empty()
    u.raw = {"error": "URLError: something transport-y"}
    return u


class _Env:
    """Point cus's module-level path constants at a throwaway state.json and
    stub the network poll. Plain setattr (same style as the other test files)
    so the file still runs standalone without pytest fixtures; restore() puts
    everything back so tests can't leak into each other."""

    def __init__(self, usage: "cus.AccountUsage"):
        self._saved = {
            "STATE_JSON": cus.STATE_JSON,
            "poll_account_usage": cus.poll_account_usage,
        }
        self._tmp = tempfile.TemporaryDirectory()
        state_path = Path(self._tmp.name) / "state.json"
        state_path.write_text(json.dumps({
            "active": "a",
            "accounts": {"a": {"current_5h_pct": 10, "current_7d_pct": 5}},
            "swap_history": [],
        }))
        cus.STATE_JSON = state_path
        cus.poll_account_usage = lambda name: usage

    def restore(self):
        for k, v in self._saved.items():
            setattr(cus, k, v)
        self._tmp.cleanup()


def test_poll_labels_stale_token_as_token_stale_not_error():
    """THE #68 fix: benign staleness must not read as a failure."""
    env = _Env(_stale_usage())
    try:
        result = CliRunner().invoke(cus.poll, ["--no-write"])
        assert result.exit_code == 0, result.output
        assert "TOKEN_STALE" in result.output
        assert "42m ago" in result.output           # surfaces how stale
        assert "swap-eligible" in result.output      # preempts the "broken?" worry
        assert "ERROR" not in result.output
    finally:
        env.restore()


def test_poll_and_force_poll_agree_on_stale_wording():
    """Regression guard: both commands must present token_stale as benign.
    If someone edits one and not the other, this fails."""
    env = _Env(_stale_usage())
    try:
        poll_out = CliRunner().invoke(cus.poll, ["--no-write"]).output
        force_out = CliRunner().invoke(cus.force_poll, ["a"]).output
        for out in (poll_out, force_out):
            assert "TOKEN_STALE" in out
            assert "refresh token still valid" in out
            assert "ERROR" not in out
    finally:
        env.restore()


def test_poll_still_reports_real_token_expiry():
    """token_expired (refresh-token-level failure) is a REAL problem and must
    keep its loud label — the #68 fix must not soften it."""
    env = _Env(_expired_usage())
    try:
        result = CliRunner().invoke(cus.poll, ["--no-write"])
        assert "TOKEN EXPIRED" in result.output
        assert "TOKEN_STALE" not in result.output
    finally:
        env.restore()


def test_poll_still_reports_generic_errors():
    """Transport/parse failures keep the ERROR label — only the benign
    token_stale case was mislabeled."""
    env = _Env(_error_usage())
    try:
        result = CliRunner().invoke(cus.poll, ["--no-write"])
        assert "ERROR: URLError" in result.output
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
