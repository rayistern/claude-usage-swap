"""Tests for the PRE-EMPTIVE creds-health early-warning layer (2026-07-06).

Motivation (incident 2026-07-05): a live LOCKED lane (slot-6, rayi2) had its mount
creds blank out. The REACTIVE blank-mount auto-heal (GH #141 + lane follow-up)
caught and restored it — good — but the operator only learned via an after-the-fact
SOS. This layer ADDS a PROACTIVE scan each cycle over every LIVE mount that emits a
soft WARNING (distinct from the reactive blank-mount URGENT) BEFORE a mount fails:

  (1) access token near expiry AND the mount isn't refreshing → heads-up WARNING;
  (2) refresh token within warn_expiry_within_days of its assumed TTL → "plan a
      browser re-login before <date>" WARNING;
  (3) access token already stale (present but past expiresAt) → degrading WARNING.

The reactive detection/heal path is untouched — a genuinely BLANK mount still fires
the reactive URGENT and the new predicate returns None for it, so the two layers do
not collide.

Coverage (matches the task's a–e):
  (a) live mount, access token expiring within the window + stale/idle → WARNING
  (b) healthy freshly-refreshed mount → no warning
  (c) refresh token within TTL-warn window → WARNING naming the re-login date
  (d) already-blank mount → still the reactive URGENT, NOT the new WARNING
  (e) de-dup: the same aging condition yields a STABLE (severity, summary) across
      two cycles, so the existing SOS notify de-dup fires it once.

Run standalone:  python3 tests/test_preemptive_creds_health.py
Run under pytest: pytest tests/test_preemptive_creds_health.py
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import cus  # noqa: E402


# --------------------------------------------------------------------------
# Fixtures
# --------------------------------------------------------------------------

def _valid(access: str = "at-live", refresh: str = "rt-live",
           expires_at: int = 2_000_000_000_000) -> dict:
    return {"claudeAiOauth": {"accessToken": access, "refreshToken": refresh,
                              "expiresAt": expires_at}}


def _blank() -> dict:
    """The 2026-07-05 blank signature: epoch expiresAt + empty token."""
    return {"claudeAiOauth": {"accessToken": "", "refreshToken": "", "expiresAt": 0}}


NOW_MS = 1_700_000_000_000  # fixed reference "now" for deterministic pure tests


def _cfg(**creds_warn) -> dict:
    """A minimal config with creds_warn defaults, overridable per test."""
    cw = {"enabled": True, "access_expiry_minutes": 45, "recent_refresh_minutes": 10}
    cw.update(creds_warn)
    return {
        "creds_warn": cw,
        "independent_logins": {"refresh_token_ttl_days": 30, "warn_expiry_within_days": 5,
                               "use_independent_logins": False},
    }


# --------------------------------------------------------------------------
# (a) access token expiring within the window + not recently refreshed → WARNING
# --------------------------------------------------------------------------

def test_a_access_near_expiry_idle_warns():
    creds = _valid(expires_at=NOW_MS + 20 * 60_000)  # 20 min out, inside 45-min window
    cond = cus._diagnose_mount_creds_health(
        "slot-6", "rayi2", creds, NOW_MS, _cfg(), recently_refreshed=False)
    assert cond is not None
    assert cond.severity == "warning"
    assert "expires soon" in cond.summary
    assert "slot-6" in cond.summary and "rayi2" in cond.summary
    assert "~20 min" in cond.action


def test_a_access_already_stale_warns_condition3():
    creds = _valid(expires_at=NOW_MS - 5 * 60_000)  # already 5 min past → token_stale
    cond = cus._diagnose_mount_creds_health(
        "shared", "merkos", creds, NOW_MS, _cfg())
    assert cond is not None
    assert cond.severity == "warning"
    assert "already stale" in cond.summary
    assert "cus relogin merkos" in cond.action


# --------------------------------------------------------------------------
# (b) healthy / freshly-refreshed mount → no warning
# --------------------------------------------------------------------------

def test_b_healthy_far_future_token_no_warning():
    creds = _valid(expires_at=NOW_MS + 6 * 3600 * 1000)  # 6h out — normal fresh token
    assert cus._diagnose_mount_creds_health(
        "slot-1", "rayi", creds, NOW_MS, _cfg()) is None


def test_b_recently_refreshed_suppresses_near_expiry():
    # Near expiry but the creds file was just rewritten → actively self-maintaining,
    # not at risk. recently_refreshed=True must suppress the access WARNING.
    creds = _valid(expires_at=NOW_MS + 10 * 60_000)  # 10 min out
    assert cus._diagnose_mount_creds_health(
        "slot-1", "rayi", creds, NOW_MS, _cfg(), recently_refreshed=True) is None


def test_b_disabled_gate_no_warning():
    creds = _valid(expires_at=NOW_MS + 5 * 60_000)  # would otherwise warn
    assert cus._diagnose_mount_creds_health(
        "slot-1", "rayi", creds, NOW_MS, _cfg(enabled=False)) is None


# --------------------------------------------------------------------------
# (c) refresh token within TTL-warn window → WARNING naming the re-login date
# --------------------------------------------------------------------------

def test_c_refresh_near_ttl_warns_with_date():
    # age 27d, ttl 30d, warn_within 5d → days_left 3 → "near".
    # Access token itself healthy (far future), so ONLY the refresh-TTL branch fires.
    creds = _valid(expires_at=NOW_MS + 6 * 3600 * 1000)
    cond = cus._diagnose_mount_creds_health(
        "slot-6", "rayi2", creds, NOW_MS, _cfg(), refresh_age_days=27.0)
    assert cond is not None
    assert cond.severity == "warning"
    assert "refresh token" in cond.summary
    assert "cus relogin rayi2" in cond.action
    # Relogin-by date = now + 3 days, derived from NOW_MS deterministically.
    from datetime import datetime, timedelta, timezone
    want = (datetime.fromtimestamp(NOW_MS / 1000, timezone.utc) + timedelta(days=3)).date().isoformat()
    assert want in cond.action


def test_c_refresh_past_ttl_warns():
    creds = _valid(expires_at=NOW_MS + 6 * 3600 * 1000)
    cond = cus._diagnose_mount_creds_health(
        "shared", "rayi", creds, NOW_MS, _cfg(), refresh_age_days=33.0)  # past 30d
    assert cond is not None and cond.severity == "warning"
    assert "past its assumed" in cond.summary


def test_c_refresh_ok_no_warning():
    creds = _valid(expires_at=NOW_MS + 6 * 3600 * 1000)
    assert cus._diagnose_mount_creds_health(
        "slot-1", "rayi", creds, NOW_MS, _cfg(), refresh_age_days=2.0) is None


def test_c_refresh_ttl_beats_access_near_expiry():
    # Both aging: refresh near TTL AND access near expiry. Refresh-TTL (human action,
    # a browser re-login) must win — one warning per mount, the more actionable one.
    creds = _valid(expires_at=NOW_MS + 15 * 60_000)  # access near expiry too
    cond = cus._diagnose_mount_creds_health(
        "slot-6", "rayi2", creds, NOW_MS, _cfg(), refresh_age_days=27.0, recently_refreshed=False)
    assert cond is not None
    assert "refresh token" in cond.summary  # not the access-expiry summary


# --------------------------------------------------------------------------
# (d) already-blank mount → predicate returns None (reactive URGENT owns it)
# --------------------------------------------------------------------------

def test_d_blank_mount_no_warning_from_this_layer():
    # A blank mount is the reactive layer's URGENT; the pre-emptive predicate must
    # NOT also emit a WARNING for it (the two layers must not collide).
    assert cus._diagnose_mount_creds_health(
        "slot-6", "rayi2", _blank(), NOW_MS, _cfg(), refresh_age_days=27.0) is None
    assert cus._diagnose_mount_creds_health(
        "shared", "merkos", None, NOW_MS, _cfg()) is None


# --------------------------------------------------------------------------
# End-to-end scan through _diagnose_live_mounts_creds_health (on-disk temp tree)
# --------------------------------------------------------------------------

class _Env:
    """Throwaway on-disk tree with cus path constants repointed at it, so the scan
    runs for real against temp files (no live-machine mutation). Mirrors
    test_lane_mount_blank_heal's live-lane /proc mock."""

    def __init__(self, active: str, mode: str = "per_session") -> None:
        self._tmp = tempfile.TemporaryDirectory()
        root = Path(self._tmp.name)
        self.root = root
        self.claude_dir = root / ".claude"
        self.accounts_dir = root / "claude-accounts"
        (self.claude_dir / "projects").mkdir(parents=True)
        self.accounts_dir.mkdir(parents=True)

        self.creds_json = self.claude_dir / ".credentials.json"
        self.creds_json.write_text(json.dumps(_valid("at-shared", "rt-shared")))
        self.claude_json = root / ".claude.json"
        self.claude_json.write_text(json.dumps(
            {"oauthAccount": {"accountUuid": f"uuid-{active}", "emailAddress": f"{active}@x"}}))

        self.state_json = self.accounts_dir / "state.json"
        self.state_json.write_text(json.dumps({
            "active": active,
            "accounts": {active: {"next_swap_at_pct": 50,
                                  "current_5h_pct": 0.0, "current_7d_pct": 0.0}},
            "slots": {},
            "swap_history": [],
        }))
        self.config_yaml = self.accounts_dir / "config.yaml"
        cus.write_yaml(self.config_yaml, {"mode": mode})
        self.inbox_md = self.accounts_dir / "inbox.md"

        self._saved = {k: getattr(cus, k) for k in (
            "HOME", "CLAUDE_DIR", "CREDS_JSON", "CLAUDE_JSON", "ACCOUNTS_DIR",
            "STATE_JSON", "CONFIG_YAML", "INBOX_MD")}
        cus.HOME = root
        cus.CLAUDE_DIR = self.claude_dir
        cus.CREDS_JSON = self.creds_json
        cus.CLAUDE_JSON = self.claude_json
        cus.ACCOUNTS_DIR = self.accounts_dir
        cus.STATE_JSON = self.state_json
        cus.CONFIG_YAML = self.config_yaml
        cus.INBOX_MD = self.inbox_md

        self._saved_mount_pids = cus.mount_pids
        self.live_slots: set[str] = set()
        cus.mount_pids = lambda mount: [1] if Path(mount).name in self.live_slots else []
        cus._OCCUPIED_SLOTS_CACHE.clear()

    def make_account(self, name: str, creds: dict, refreshed_days_ago: float | None = None) -> None:
        d = self.accounts_dir / f"account-{name}"
        d.mkdir(exist_ok=True)
        (d / ".credentials.json").write_text(json.dumps(creds))
        (d / ".claude.json").write_text(json.dumps(
            {"oauthAccount": {"accountUuid": f"uuid-{name}", "emailAddress": f"{name}@x"}}))
        meta = {"name": name}
        if refreshed_days_ago is not None:
            from datetime import datetime, timedelta, timezone
            ts = (datetime.now(timezone.utc) - timedelta(days=refreshed_days_ago)).isoformat()
            meta["refreshed_ts"] = ts
        cus.write_yaml(d / "meta.yaml", meta)
        # Register the account in state so diagnose()'s account-keyed checks
        # (e.g. _diagnose_premium_headroom) don't KeyError on a slot's account
        # that isn't in state["accounts"].
        state = cus.load_state()
        state.setdefault("accounts", {}).setdefault(
            name, {"next_swap_at_pct": 50, "current_5h_pct": 0.0, "current_7d_pct": 0.0})
        cus.save_state(state)

    def make_slot(self, account: str, live: bool, mount_creds: dict,
                  mtime_minutes_ago: float = 60.0) -> str:
        state = cus.load_state()
        name, d = cus.create_slot(state)
        cred = d / ".credentials.json"
        cred.write_text(json.dumps(mount_creds))
        # Backdate the creds file mtime so recently_refreshed reads False (a
        # just-written file would otherwise be treated as actively refreshing).
        past = time.time() - mtime_minutes_ago * 60
        os.utime(cred, (past, past))
        (d / ".claude.json").write_text(json.dumps({"oauthAccount": {"emailAddress": f"{account}@x"}}))
        state["slots"][name]["account"] = account
        cus.save_state(state)
        if live:
            self.live_slots.add(name)
        cus._OCCUPIED_SLOTS_CACHE.clear()
        return name

    def restore(self) -> None:
        for k, v in self._saved.items():
            setattr(cus, k, v)
        cus.mount_pids = self._saved_mount_pids
        cus._OCCUPIED_SLOTS_CACHE.clear()
        self._tmp.cleanup()


def _now_plus_min(minutes: float) -> int:
    return int(time.time() * 1000) + int(minutes * 60_000)


def test_e2e_live_lane_access_near_expiry_warns():
    env = _Env(active="rayi", mode="per_session")
    try:
        env.make_account("rayi2", _valid())
        # Access token 20 min out; file mtime backdated 60 min → not recently refreshed.
        slot = env.make_slot("rayi2", live=True,
                             mount_creds=_valid("at-lane", "rt-lane", _now_plus_min(20)))
        conds = cus._diagnose_live_mounts_creds_health(cus.load_state(), cus.load_config())
        warns = [c for c in conds if "expires soon" in c.summary]
        assert len(warns) == 1
        assert warns[0].severity == "warning"
        assert slot in warns[0].summary and "rayi2" in warns[0].summary
    finally:
        env.restore()


def test_e2e_live_lane_refresh_ttl_warns_from_snapshot_age():
    env = _Env(active="rayi", mode="per_session")
    try:
        # Base-snapshot age proxy: meta.yaml refreshed_ts 27 days ago → within warn.
        env.make_account("rayi2", _valid(), refreshed_days_ago=27.0)
        # Access token healthy (far future) so ONLY the refresh-TTL branch can fire.
        env.make_slot("rayi2", live=True,
                      mount_creds=_valid("at-lane", "rt-lane", _now_plus_min(600)))
        conds = cus._diagnose_live_mounts_creds_health(cus.load_state(), cus.load_config())
        warns = [c for c in conds if "refresh token" in c.summary]
        assert len(warns) == 1
        assert "cus relogin rayi2" in warns[0].action
    finally:
        env.restore()


def test_e2e_healthy_lane_no_warning():
    env = _Env(active="rayi", mode="per_session")
    try:
        env.make_account("rayi2", _valid(), refreshed_days_ago=1.0)
        env.make_slot("rayi2", live=True,
                      mount_creds=_valid("at-lane", "rt-lane", _now_plus_min(600)))
        assert cus._diagnose_live_mounts_creds_health(cus.load_state(), cus.load_config()) == []
    finally:
        env.restore()


def test_e2e_blank_lane_no_preemptive_warning_reactive_urgent_only():
    # A blanked live lane must produce the REACTIVE URGENT (via diagnose) and NOT a
    # pre-emptive WARNING — the two layers do not collide (task (d)).
    env = _Env(active="rayi", mode="per_session")
    try:
        env.make_account("rayi2", _valid(), refreshed_days_ago=27.0)  # aging, would-warn
        slot = env.make_slot("rayi2", live=True, mount_creds=_blank())
        pre = cus._diagnose_live_mounts_creds_health(cus.load_state(), cus.load_config())
        assert pre == []  # no pre-emptive warning for a blank
        # The reactive layer still surfaces its URGENT for the blank lane.
        urgent = [c for c in cus.diagnose() if "mount creds are blanked" in c.summary]
        assert len(urgent) == 1 and urgent[0].severity == "urgent"
        assert slot in urgent[0].summary
    finally:
        env.restore()


def test_e2e_disabled_gate_scans_nothing():
    env = _Env(active="rayi", mode="per_session")
    try:
        env.make_account("rayi2", _valid(), refreshed_days_ago=27.0)
        env.make_slot("rayi2", live=True,
                      mount_creds=_valid("at-lane", "rt-lane", _now_plus_min(20)))
        # deepcopy before mutating: load_config()/deep_merge shares nested dicts
        # with the module-global DEFAULT_CONFIG by reference, so an in-place edit
        # of cfg["creds_warn"] would corrupt the gate for every later test.
        import copy
        cfg = copy.deepcopy(cus.load_config())
        cfg["creds_warn"]["enabled"] = False
        assert cus._diagnose_live_mounts_creds_health(cus.load_state(), cfg) == []
    finally:
        env.restore()


# --------------------------------------------------------------------------
# (e) de-dup: the same aging condition yields a STABLE (severity, summary) across
#     two cycles, so maybe_write_sos's notify hash won't re-fire every poll.
# --------------------------------------------------------------------------

def test_e_dedup_stable_signature_across_two_cycles():
    env = _Env(active="rayi", mode="per_session")
    try:
        env.make_account("rayi2", _valid(), refreshed_days_ago=27.0)
        env.make_slot("rayi2", live=True,
                      mount_creds=_valid("at-lane", "rt-lane", _now_plus_min(600)))
        c1 = cus._diagnose_live_mounts_creds_health(cus.load_state(), cus.load_config())
        c2 = cus._diagnose_live_mounts_creds_health(cus.load_state(), cus.load_config())
        assert c1 and c2
        sig1 = [(c.severity, c.summary) for c in c1]
        sig2 = [(c.severity, c.summary) for c in c2]
        assert sig1 == sig2  # identical → the existing notify de-dup fires once
    finally:
        env.restore()


if __name__ == "__main__":
    import traceback

    funcs = [v for k, v in sorted(globals().items())
             if k.startswith("test_") and callable(v)]
    failed = 0
    for fn in funcs:
        try:
            fn()
            print(f"  ok   {fn.__name__}")
        except Exception:  # noqa: BLE001
            failed += 1
            print(f"  FAIL {fn.__name__}")
            traceback.print_exc()
    print(f"\n{len(funcs) - failed}/{len(funcs)} passed")
    sys.exit(1 if failed else 0)
