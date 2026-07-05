"""Tests for the 2026-07-05 "stale per-model usage shown as authoritative" bug cluster.

Incident: an operator (and an agent) trusted `03 Fable=100%` and moved a live
Fable session off account 03 — but 03 was `token_stale`, so that 100% was a
STALE cached per-model value; 03 actually had Fable headroom. The move
interrupted live work and burned usage. Three defects fed it:

  Bug 1 — per-model weekly values were printed BARE (no `~`, no `?`) in
    `cus status` (the `└ 7d by model:` line) and in `cus sessions`
    (`_session_binding`'s premium-gate verdict), so a stale number looked
    current even while the aggregate 5h/7d were correctly marked stale.
  Bug 2 — target selection (`_max_model_weekly_from_acct`, read by
    `pick_swap_target` and the hard-cap anti-pingpong guard) trusted the cached
    per-model dict, so a STALE per-model reading could refuse an account as a
    swap target. (The swap-AWAY force reads FRESH usage and was already safe.)
  Bug 3 — a `token_stale` account with a VALID refresh token was polled only on
    the slow inactive cadence, so its self-refresh preflight ran rarely and the
    stale cached % sat for hours instead of being reconfirmed promptly.

Run standalone:  python3 tests/test_stale_per_model_reading.py
Or under pytest: pytest tests/test_stale_per_model_reading.py
"""

import json
import sys
import tempfile
import time
import urllib.error
from datetime import datetime, timedelta, timezone
from pathlib import Path

from click.testing import CliRunner

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import cus  # noqa: E402


# ==========================================================================
# Shared fixtures
# ==========================================================================

class _FakeResponse:
    """Context-manager stand-in for urllib.request.urlopen's return value."""

    def __init__(self, payload: bytes):
        self._payload = payload

    def read(self, *_args):
        return self._payload

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _stub_urlopen(fn):
    original = cus.urllib.request.urlopen
    cus.urllib.request.urlopen = fn
    return lambda: setattr(cus.urllib.request, "urlopen", original)


def _gate_config(**over) -> dict:
    """DEFAULT_CONFIG with the per-model weekly gate ON at cap 97%, mirroring
    the live setup that surfaced the incident."""
    base = cus.deep_merge(cus.DEFAULT_CONFIG,
                          {"per_model_weekly": {"gate_enabled": True, "cap_pct": 97}})
    return cus.deep_merge(base, over) if over else base


class _StatusEnv:
    """Throwaway state.json + config.yaml with cus's path constants repointed,
    and a quiet diagnose()/find_live_sessions() so the status render path is
    exercised in isolation. Mirrors tests/test_token_stale_5h_display.py."""

    def __init__(self, acct: dict, color: bool = False):
        self._tmp = tempfile.TemporaryDirectory()
        root = Path(self._tmp.name)
        state_path = root / "state.json"
        state_path.write_text(json.dumps(
            {"active": "a", "accounts": {"a": acct}, "swap_history": []}))
        config_path = root / "config.yaml"
        config_path.write_text(f"statusline:\n  color: {'true' if color else 'false'}\n")
        self._saved = {k: getattr(cus, k) for k in
                       ("STATE_JSON", "CONFIG_YAML", "diagnose", "find_live_sessions")}
        cus.STATE_JSON = state_path
        cus.CONFIG_YAML = config_path
        cus.diagnose = lambda state, config: []
        cus.find_live_sessions = lambda *a, **k: []

    def restore(self):
        for k, v in self._saved.items():
            setattr(cus, k, v)
        self._tmp.cleanup()


class _PollEnv:
    """Throwaway ~/claude-accounts + state.json + config.yaml for the poll path.
    Mirrors tests/test_token_self_refresh.py's env."""

    def __init__(self, config_yaml: str, state: dict):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self._saved = {k: getattr(cus, k) for k in
                       ("ACCOUNTS_DIR", "STATE_JSON", "CONFIG_YAML", "CREDS_JSON")}
        cus.ACCOUNTS_DIR = self.root / "claude-accounts"
        cus.ACCOUNTS_DIR.mkdir()
        cus.STATE_JSON = self.root / "state.json"
        cus.STATE_JSON.write_text(json.dumps(state))
        cus.CREDS_JSON = self.root / "live-credentials.json"  # never the test account
        config_path = self.root / "config.yaml"
        config_path.write_text(config_yaml)
        cus.CONFIG_YAML = config_path

    def write_creds(self, account: str, oauth: dict) -> Path:
        d = cus.ACCOUNTS_DIR / f"account-{account}"
        d.mkdir(parents=True, exist_ok=True)
        p = d / ".credentials.json"
        p.write_text(json.dumps({"claudeAiOauth": oauth}))
        return p

    def restore(self):
        for k, v in self._saved.items():
            setattr(cus, k, v)
        self._tmp.cleanup()


def _iso(dt: datetime) -> str:
    return dt.isoformat()


# ==========================================================================
# (a) A token_stale account renders per-model with a stale marker
#     — in `cus status` AND in `cus sessions` (_session_binding)
# ==========================================================================

def test_status_marks_per_model_stale_under_token_stale():
    acct = {
        "current_5h_pct": 82,
        "current_7d_pct": 63,
        "next_swap_at_pct": 90,
        "token_stale": True,
        "per_model_weekly_pct": {"Fable": 100.0},
    }
    env = _StatusEnv(acct)
    try:
        out = CliRunner().invoke(cus.status).output
        model_lines = [l for l in out.splitlines() if "by model" in l]
        assert len(model_lines) == 1, out
        # Stale marker present; bare authoritative "Fable=100%" must NOT appear.
        assert "Fable=100%~" in model_lines[0], model_lines[0]
        assert "Fable=100% " not in (model_lines[0] + " ")
    finally:
        env.restore()


def test_session_binding_does_not_hard_block_on_stale_per_model():
    """A token_stale premium lane whose cached Fable=100% would trip the gate
    must NOT read as a hard 'premium gate' block — the value is unconfirmed."""
    # 5h/7d have headroom (so no ladder/hard-wall trip preempts the gate) — the
    # ONLY thing that would block is the stale Fable=100%, which must not.
    acct = {"current_5h_pct": 10.0, "current_7d_pct": 5.0,
            "token_stale": True, "per_model_weekly_pct": {"Fable": 100.0}}
    sev, txt = cus._session_binding(acct, "premium", _gate_config())
    assert sev != "blocked", (sev, txt)
    assert "premium gate" not in txt, txt
    assert "~" in txt or "stale" in txt, txt


# ==========================================================================
# (b) A fresh account renders per-model normally (no stale marker, gate binds)
# ==========================================================================

def test_status_renders_fresh_per_model_bare():
    acct = {
        "current_5h_pct": 40,
        "current_7d_pct": 63,
        "next_swap_at_pct": 90,
        "per_model_weekly_pct": {"Fable": 100.0},
    }
    env = _StatusEnv(acct)
    try:
        out = CliRunner().invoke(cus.status).output
        model_lines = [l for l in out.splitlines() if "by model" in l]
        assert len(model_lines) == 1, out
        assert "Fable=100%" in model_lines[0]
        assert "Fable=100%~" not in model_lines[0]  # no stale marker on fresh data
    finally:
        env.restore()


def test_session_binding_still_blocks_fresh_premium_over_cap():
    """Unchanged behavior: a FRESH premium lane over the model cap is blocked."""
    acct = {"current_5h_pct": 10.0, "current_7d_pct": 5.0,
            "per_model_weekly_pct": {"Fable": 98.0}}
    sev, txt = cus._session_binding(acct, "premium", _gate_config())
    assert sev == "blocked", (sev, txt)
    assert "Fable" in txt and "premium gate" in txt, txt


# ==========================================================================
# (c) The decision path does not force / refuse a swap on a stale per-model value
# ==========================================================================

def test_max_model_weekly_from_acct_treats_stale_as_unknown():
    cfg = _gate_config()
    fresh = {"current_7d_pct": 50.0, "per_model_weekly_pct": {"Fable": 100.0}}
    stale = {"token_stale": True, "current_7d_pct": 50.0,
             "per_model_weekly_pct": {"Fable": 100.0}}
    # Fresh account: the cap is real and folded in.
    assert cus._max_model_weekly_from_acct(fresh, cfg) == 100.0
    # Stale account: unknown → 0.0, so it neither excludes a target nor forces a hold.
    assert cus._max_model_weekly_from_acct(stale, cfg) == 0.0
    # Any un-observable flag counts, not just token_stale.
    for flag in ("rate_limited", "token_expired", "poll_error"):
        acct = {flag: True, "current_7d_pct": 50.0, "per_model_weekly_pct": {"Fable": 100.0}}
        assert cus._max_model_weekly_from_acct(acct, cfg) == 0.0, flag


def test_swap_away_force_reads_fresh_usage_not_cached_dict():
    """Evidence that the swap-AWAY force was already safe: a token_stale
    account's fresh AccountUsage this cycle is empty, so the swap-away signal
    (_max_model_weekly_from_usage) is 0.0 and can never force a lane off it."""
    u = cus.AccountUsage.empty()      # what poll_account_usage returns for token_stale
    u.token_stale = True
    assert cus._max_model_weekly_from_usage(u, _gate_config()) == 0.0


def test_pick_swap_target_not_refused_on_stale_per_model():
    """A stale-Fable=100% spare is NOT refused as a target on the stale reading
    (pick returns it cleanly), while a FRESH Fable=100% spare IS marked degraded
    (over the model cap). Isolates Bug 2's target-selection error."""
    cfg = _gate_config()

    def _state_with_spare(spare_acct: dict) -> dict:
        return {
            "active": "cur",
            "accounts": {
                "cur": {"current_5h_pct": 95.0, "current_7d_pct": 40.0, "next_swap_at_pct": 50},
                "spare": spare_acct,
            },
            "swap_history": [],
        }

    stale_spare = {"current_5h_pct": 5.0, "current_7d_pct": 5.0, "next_swap_at_pct": 90,
                   "token_stale": True, "per_model_weekly_pct": {"Fable": 100.0}}
    tgt = cus.pick_swap_target(_state_with_spare(stale_spare), cfg)
    assert tgt is not None and tgt.name == "spare", tgt
    assert "DEGRADED" not in tgt.reason, tgt.reason  # not refused on the stale %

    fresh_spare = {"current_5h_pct": 5.0, "current_7d_pct": 5.0, "next_swap_at_pct": 90,
                   "per_model_weekly_pct": {"Fable": 100.0}}
    tgt2 = cus.pick_swap_target(_state_with_spare(fresh_spare), cfg)
    assert tgt2 is not None and tgt2.name == "spare", tgt2
    assert "DEGRADED" in tgt2.reason, tgt2.reason  # fresh over-cap IS flagged


# ==========================================================================
# (d) Auto-refresh clears token_stale during a NORMAL (not force) poll:
#     the account is poll-due on the fast cadence, and polling it refreshes
#     the token and re-fetches real usage, clearing token_stale.
# ==========================================================================

_POLL_CFG = ("polling:\n"
             "  active_interval_seconds: 300\n"
             "  inactive_interval_seconds: 600\n"
             "poll_interval_seconds: 300\n")


def test_token_stale_account_is_poll_due_on_fast_cadence():
    """A token_stale INACTIVE account whose last_poll_ts is 400s old would be
    NOT due on the slow 600s inactive cadence, but the token_stale fast-track
    puts it on the 300s active cadence → due. A non-stale twin stays not-due."""
    config = {"polling": {"active_interval_seconds": 300, "inactive_interval_seconds": 600},
              "poll_interval_seconds": 300, "mode": "global"}
    last = _iso(datetime.now(timezone.utc) - timedelta(seconds=400))
    state = {"active": "other", "accounts": {
        "other": {"last_poll_ts": last},
        "spare": {"last_poll_ts": last, "token_stale": True},
    }}
    due, why = cus._account_poll_due(state, config, "spare")
    assert due is True, why
    assert "token_stale-fast" in why, why

    # Control: same age, NOT stale → slow inactive cadence → not due.
    state["accounts"]["spare"].pop("token_stale")
    due2, why2 = cus._account_poll_due(state, config, "spare")
    assert due2 is False, why2


def test_normal_poll_of_stale_account_refreshes_and_clears():
    """End-to-end for Bug 3: a token_stale account with a VALID refresh token,
    when polled the way the daemon's regular cycle polls it, mints a fresh
    token and completes a real usage poll — clearing token_stale in one cycle."""
    stale_oauth = {
        "accessToken": "old-access-token",
        "refreshToken": "valid-refresh-token",
        "expiresAt": int(time.time() * 1000) - 40 * 60 * 1000,  # expired 40 min ago
        "scopes": ["user:inference", "user:profile"],
    }
    last = _iso(datetime.now(timezone.utc) - timedelta(seconds=400))
    state = {"active": "other", "accounts": {
        "spare": {"last_poll_ts": last, "token_stale": True},
    }, "slots": {}, "swap_history": []}
    env = _PollEnv(_POLL_CFG, state)
    try:
        env.write_creds("spare", stale_oauth)
        config = cus.load_config()

        # 1. The daemon's poll gate WOULD poll this stale account this cycle.
        due, why = cus._account_poll_due(state, config, "spare")
        assert due is True, why

        # 2. Polling it refreshes the token and observes real usage → not stale.
        def fake_urlopen(req, timeout=None):
            if req.full_url == cus.OAUTH_TOKEN_URL:
                return _FakeResponse(json.dumps({
                    "access_token": "fresh-access-token",
                    "refresh_token": "fresh-refresh-token",
                    "expires_in": 3600,
                }).encode())
            assert req.full_url == cus.USAGE_API_URL
            assert req.headers.get("Authorization") == "Bearer fresh-access-token"
            return _FakeResponse(json.dumps({
                "five_hour": {"utilization": 8.0, "resets_at": None},
                "seven_day": {"utilization": 20.0, "resets_at": None},
            }).encode())

        unstub = _stub_urlopen(fake_urlopen)
        try:
            usage = cus.poll_account_usage("spare")
        finally:
            unstub()

        assert usage.token_stale is False
        assert usage.five_hour.utilization == 8.0

        # And update_state_with_usage clears the persisted flag (Branch 4).
        cus.update_state_with_usage(state, {"spare": usage})
        assert state["accounts"]["spare"].get("token_stale") in (None, False)
        assert state["accounts"]["spare"]["current_5h_pct"] == 8.0
    finally:
        env.restore()


def test_dead_refresh_token_still_degrades_gracefully():
    """Graceful-degrade guard: a token_stale account whose refresh token is
    dead stays token_stale (no crash), and the usage endpoint is never hit."""
    dead_oauth = {
        "accessToken": "old-access-token",
        "refreshToken": "dead-refresh-token",
        "expiresAt": int(time.time() * 1000) - 40 * 60 * 1000,
        "scopes": ["user:inference"],
    }
    state = {"active": "other", "accounts": {"spare": {}}, "slots": {}, "swap_history": []}
    env = _PollEnv(_POLL_CFG, state)
    calls = []
    try:
        env.write_creds("spare", dead_oauth)

        def raise_http_error(req, timeout=None):
            calls.append(req.full_url)
            raise urllib.error.HTTPError(req.full_url, 400, "Bad Request", {}, None)

        unstub = _stub_urlopen(raise_http_error)
        try:
            usage = cus.poll_account_usage("spare")
        finally:
            unstub()

        assert usage.token_stale is True
        assert calls == [cus.OAUTH_TOKEN_URL]  # usage endpoint never reached
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
