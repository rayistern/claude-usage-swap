"""Tests for the 2026-07-03 token-stale blindness incident (GH #130).

Background: when an account's stored ACCESS token goes stale (expired access
token, still-valid refresh token), `poll_account_usage()` short-circuits with
`token_stale` and never observes real usage. The daemon's Branch 0 in
`update_state_with_usage()` still stamped `last_poll_ts = polled_at` on every
such cycle even though NOTHING was observed. `_five_hour_rolled_since_poll()`
read `last_poll_ts` as if it meant "last real observation", so the
continuously-restamped value made it return False forever — the GH #59 countdown
reset inference never fired and the account sat frozen at a stale-high % (rayi2
at ~99% for 3.7h) while its 5h window had actually reset.

Fix Part A: a new state field `last_observed_ts` = "last time we OBSERVED real
usage", written only on the success branch. `_five_hour_rolled_since_poll()` and
the burn-rate estimator read `last_observed_ts or last_poll_ts` (legacy
fallback). `last_poll_ts` keeps its attempt-time semantics for throttling.

Fix Part B: `_read_access_token_with_expiry()` prefers a live LANE's own fresh
`.credentials.json` over the (likely-stale) primary account store when polling.

Run standalone:  python3 tests/test_token_stale_observability.py
Or under pytest: pytest tests/test_token_stale_observability.py
"""

import json
import sys
import tempfile
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import cus  # noqa: E402


def _iso(dt: datetime) -> str:
    return dt.isoformat()


def _now() -> datetime:
    return datetime.now(timezone.utc)


# --------------------------------------------------------------------------
# Fix Part A — Branch-0 stamping no longer defeats GH #59 reset inference
# --------------------------------------------------------------------------

def test_success_poll_writes_last_observed_ts():
    """A successful poll (Branch 4) stamps BOTH last_poll_ts and the new
    last_observed_ts — they coincide the moment we actually observe usage."""
    saved = cus.load_config
    cus.load_config = lambda: cus.DEFAULT_CONFIG
    try:
        state = {"accounts": {"a": {}}}
        u = cus.AccountUsage.empty()
        u.five_hour = cus.UsageWindow(utilization=40.0, resets_at=None)
        u.seven_day = cus.UsageWindow(utilization=10.0, resets_at=None)
        cus.update_state_with_usage(state, {"a": u})
        acct = state["accounts"]["a"]
        assert acct["last_observed_ts"] == u.polled_at
        assert acct["last_poll_ts"] == u.polled_at
    finally:
        cus.load_config = saved


def test_token_stale_advances_poll_ts_but_not_observed_ts():
    """Branch 0 (token_stale) MUST restamp last_poll_ts (it settled a poll
    attempt, drives throttling) but MUST NOT advance last_observed_ts — nothing
    real was observed. This is the exact write that used to blind the account."""
    saved = cus.load_config
    cus.load_config = lambda: cus.DEFAULT_CONFIG
    try:
        old_observed = _iso(_now() - timedelta(minutes=30))
        old_poll = _iso(_now() - timedelta(minutes=30))
        state = {"accounts": {"a": {
            "current_5h_pct": 99.0,
            "last_observed_ts": old_observed,
            "last_poll_ts": old_poll,
        }}}
        u = cus.AccountUsage.empty()
        u.token_stale = True
        u.raw = {"error": "stored access token expired (refresh token still valid)"}
        cus.update_state_with_usage(state, {"a": u})
        acct = state["accounts"]["a"]
        assert acct["last_poll_ts"] == u.polled_at        # attempt time advanced
        assert acct["last_observed_ts"] == old_observed    # observation time frozen
        assert acct["current_5h_pct"] == 99.0              # % preserved, not observed
    finally:
        cus.load_config = saved


def test_rolled_uses_observation_time_not_poll_attempt():
    """`_five_hour_rolled_since_poll` keys off last_observed_ts: a 5h window that
    reset AFTER our last real observation reads as rolled EVEN WHEN last_poll_ts
    was just restamped by a token_stale cycle (the incident condition)."""
    reset_10m_ago = _iso(_now() - timedelta(minutes=10))
    acct_blind = {
        "five_hour_resets_at": reset_10m_ago,
        "last_observed_ts": _iso(_now() - timedelta(minutes=30)),  # before reset
        "last_poll_ts": _iso(_now()),                              # restamped just now
    }
    # Rolled: our last real observation predates the reset, despite fresh poll_ts.
    assert cus._five_hour_rolled_since_poll(acct_blind) is True

    acct_observed = {
        "five_hour_resets_at": reset_10m_ago,
        "last_observed_ts": _iso(_now() - timedelta(minutes=2)),   # after reset
        "last_poll_ts": _iso(_now()),
    }
    # Not rolled: we already observed the post-reset state.
    assert cus._five_hour_rolled_since_poll(acct_observed) is False


def test_rolled_falls_back_to_last_poll_ts_for_legacy_state():
    """Legacy state files written before last_observed_ts existed must keep
    today's behavior: fall back to last_poll_ts."""
    reset_10m_ago = _iso(_now() - timedelta(minutes=10))
    legacy_rolled = {
        "five_hour_resets_at": reset_10m_ago,
        "last_poll_ts": _iso(_now() - timedelta(minutes=30)),  # no last_observed_ts key
    }
    assert cus._five_hour_rolled_since_poll(legacy_rolled) is True


def test_token_stale_cycle_then_inference_fires():
    """End-to-end: a token_stale poll cycle followed by the countdown inference
    now correctly zeroes a rolled-over window. Before the fix, Branch 0's
    last_poll_ts restamp made the inference a permanent no-op."""
    saved = cus.load_config
    cus.load_config = lambda: cus.DEFAULT_CONFIG
    try:
        reset_10m_ago = _iso(_now() - timedelta(minutes=10))
        state = {"accounts": {"a": {
            "current_5h_pct": 99.0,
            "five_hour_resets_at": reset_10m_ago,
            "last_observed_ts": _iso(_now() - timedelta(minutes=30)),  # pre-reset
            "last_poll_ts": _iso(_now() - timedelta(minutes=30)),
        }}}
        # Simulate the poll cycle: token_stale → Branch 0 restamps last_poll_ts.
        u = cus.AccountUsage.empty()
        u.token_stale = True
        u.raw = {"error": "stored access token expired (refresh token still valid)"}
        cus.update_state_with_usage(state, {"a": u})
        # Now the countdown inference runs — and must fire.
        inferred = cus._apply_countdown_reset_inference(state, cus.DEFAULT_CONFIG)
        acct = state["accounts"]["a"]
        assert "a" in inferred
        assert acct["current_5h_pct"] == 0.0
        assert acct["five_hour_reset_inferred"] is True
        assert acct["five_hour_pct_pre_reset"] == 99.0
    finally:
        cus.load_config = saved


# --------------------------------------------------------------------------
# Fix Part B — poll via the freshest live-lane credential
# --------------------------------------------------------------------------

class _CredEnv:
    """Redirect cus's path constants at a throwaway account tree and stub
    mount_in_use, so `_read_access_token_with_expiry` can be exercised against
    controlled slot/primary creds files without touching the real system."""

    def __init__(self):
        self._saved = {
            "STATE_JSON": cus.STATE_JSON,
            "ACCOUNTS_DIR": cus.ACCOUNTS_DIR,
            "CREDS_JSON": cus.CREDS_JSON,
            "mount_in_use": cus.mount_in_use,
        }
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        cus.ACCOUNTS_DIR = self.root
        cus.STATE_JSON = self.root / "state.json"
        cus.CREDS_JSON = self.root / "live-shared" / ".credentials.json"
        self._live_mounts: set[str] = set()
        # mount_in_use(path) → True iff its dir name is registered live.
        cus.mount_in_use = lambda d: Path(d).name in self._live_mounts

    def write_creds(self, path: Path, token: str, expires_at_ms: int):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"claudeAiOauth": {
            "accessToken": token, "expiresAt": expires_at_ms}}))

    def slot_creds(self, slot_name: str) -> Path:
        return self.root / slot_name / ".credentials.json"

    def primary_creds(self, account: str) -> Path:
        return self.root / f"account-{account}" / ".credentials.json"

    def mark_live(self, slot_name: str):
        self._live_mounts.add(slot_name)

    def write_state(self, state: dict):
        cus.STATE_JSON.write_text(json.dumps(state))

    def restore(self):
        for k, v in self._saved.items():
            setattr(cus, k, v)
        self._tmp.cleanup()


def _fresh_ms() -> int:
    return int(time.time() * 1000) + 3_600_000    # +1h


def _stale_ms() -> int:
    return int(time.time() * 1000) - 3_600_000    # -1h


def test_prefers_live_slot_creds_over_stale_primary():
    """The core Part B fix: an account backing a LIVE slot polls with the
    slot's own fresh token, not the aged-out primary store."""
    env = _CredEnv()
    try:
        env.write_creds(env.primary_creds("x"), "STALE_PRIMARY", _stale_ms())
        env.write_creds(env.slot_creds("slot-1"), "FRESH_SLOT", _fresh_ms())
        env.mark_live("slot-1")
        env.write_state({"active": "someone_else",
                         "slots": {"slot-1": {"account": "x"}}})
        token, exp = cus._read_access_token_with_expiry("x")
        assert token == "FRESH_SLOT"
    finally:
        env.restore()


def test_skips_missing_slot_creds_falls_back_to_primary():
    """A slot dir that exists but has no `.credentials.json` is skipped
    silently; polling falls back to the primary store."""
    env = _CredEnv()
    try:
        env.write_creds(env.primary_creds("x"), "PRIMARY_TOKEN", _fresh_ms())
        (env.root / "slot-1").mkdir(parents=True)   # dir exists, no creds file
        env.mark_live("slot-1")
        env.write_state({"active": None,
                         "slots": {"slot-1": {"account": "x"}}})
        token, exp = cus._read_access_token_with_expiry("x")
        assert token == "PRIMARY_TOKEN"
    finally:
        env.restore()


def test_all_stale_returns_primary_pair_for_token_stale_detection():
    """When every candidate is stale, return the primary store's pair exactly
    as before so downstream token_stale detection still fires."""
    env = _CredEnv()
    try:
        env.write_creds(env.primary_creds("x"), "STALE_PRIMARY", _stale_ms())
        env.write_creds(env.slot_creds("slot-1"), "STALE_SLOT", _stale_ms())
        env.mark_live("slot-1")
        env.write_state({"active": None,
                         "slots": {"slot-1": {"account": "x"}}})
        token, exp = cus._read_access_token_with_expiry("x")
        assert token == "STALE_PRIMARY"
        assert exp < int(time.time() * 1000)   # still flagged stale → token_stale
    finally:
        env.restore()


def test_live_slot_preferred_over_idle_slot():
    """Among multiple slots backing the account, a LIVE one wins over an idle
    one — the live session keeps its creds freshest."""
    env = _CredEnv()
    try:
        env.write_creds(env.primary_creds("x"), "PRIMARY", _stale_ms())
        env.write_creds(env.slot_creds("slot-1"), "IDLE_SLOT", _fresh_ms())
        env.write_creds(env.slot_creds("slot-2"), "LIVE_SLOT", _fresh_ms())
        env.mark_live("slot-2")   # only slot-2 is live
        env.write_state({"active": None,
                         "slots": {"slot-1": {"account": "x"},
                                   "slot-2": {"account": "x"}}})
        token, exp = cus._read_access_token_with_expiry("x")
        assert token == "LIVE_SLOT"
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
