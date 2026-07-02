"""Tests for Phase 3: the per-slot decision loop.

Plan: docs/plans/2026-07-02-per-session-accounts.md (3.1–3.5).

Covers:
  - occupied_slot_accounts: /proc-ground-truth occupancy (via mount_pids stub)
  - decide_slot_swaps: per-account ladder shim; fan-out of multiple slots on
    one hot account to DISTINCT targets; cold accounts hold
  - check_rate_limit_reactive_per_session: 429 attribution via sessions.log →
    urgent moves for that account's live slots only
  - _account_on_fast_cadence: occupied accounts poll fast in per_session;
    global-mode behavior unchanged
  - _per_session_cycle: end-to-end in-place slot move with the global mount
    and state.active untouched; periodic save-back only when live is fresher

Run standalone:  python3 tests/test_per_session_cycle.py
Run under pytest: pytest tests/test_per_session_cycle.py
"""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import cus  # noqa: E402


def _creds(refresh: str, expires_at: int = 2_000_000_000_000) -> dict:
    return {"claudeAiOauth": {"accessToken": f"at-{refresh}", "refreshToken": refresh, "expiresAt": expires_at}}


def _identity(name: str) -> dict:
    return {"userID": f"uid-{name}", "oauthAccount": {"emailAddress": f"{name}@x", "accountUuid": f"uuid-{name}"}}


def _usage(five_h: float, seven_d: float) -> "cus.AccountUsage":
    return cus.AccountUsage(
        five_hour=cus.UsageWindow(utilization=five_h, resets_at=None),
        seven_day=cus.UsageWindow(utilization=seven_d, resets_at=None),
    )


def _config(**overrides) -> dict:
    """per_session config with the timing/growth gates disabled so decision
    tests exercise the ladder + picker, not wall-clock state."""
    cfg = cus.deep_merge(cus.DEFAULT_CONFIG, {
        "mode": "per_session",
        "swap_hysteresis": {"enabled": False},
        "usage_growth_gate": {"enabled": False},
        "defer_swap_near_5h_reset": {"enabled": False},
        "lazy_swap": {"enabled": False},
        "strategy": "lowest_usage",
    })
    return cus.deep_merge(cfg, overrides)


class _Env:
    def __init__(self, accounts=("alpha", "beta", "gamma", "delta")) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        root = Path(self._tmp.name)
        self.claude_dir = root / ".claude"
        self.claude_json = root / ".claude.json"
        self.accounts_dir = root / "claude-accounts"

        (self.claude_dir / "projects").mkdir(parents=True)
        (self.claude_dir / "settings.json").write_text("{}")
        (self.claude_dir / ".credentials.json").write_text(json.dumps(_creds("rt-bare")))
        self.claude_json.write_text(json.dumps(_identity("bare")))

        for name in accounts:
            d = self.accounts_dir / f"account-{name}"
            d.mkdir(parents=True)
            (d / ".credentials.json").write_text(json.dumps(_creds(f"rt-{name}")))
            (d / ".claude.json").write_text(json.dumps(_identity(name)))

        cus.write_json(self.accounts_dir / "state.json", {
            "active": accounts[0],
            "accounts": {n: {"next_swap_at_pct": 50, "current_5h_pct": 0.0, "current_7d_pct": 0.0} for n in accounts},
            "swap_history": [],
        })

        self._saved = {k: getattr(cus, k) for k in
                       ("HOME", "CLAUDE_DIR", "CLAUDE_JSON", "CREDS_JSON", "ACCOUNTS_DIR", "STATE_JSON",
                        "CONFIG_YAML", "SESSIONS_LOG", "RATE_LIMIT_LOG", "DECISIONS_LOG", "INBOX_MD")}
        cus.HOME = root
        cus.CLAUDE_DIR = self.claude_dir
        cus.CLAUDE_JSON = self.claude_json
        cus.CREDS_JSON = self.claude_dir / ".credentials.json"
        cus.ACCOUNTS_DIR = self.accounts_dir
        cus.STATE_JSON = self.accounts_dir / "state.json"
        cus.CONFIG_YAML = self.accounts_dir / "config.yaml"
        cus.SESSIONS_LOG = self.accounts_dir / "sessions.log"
        cus.RATE_LIMIT_LOG = self.accounts_dir / "429.log"
        cus.DECISIONS_LOG = self.accounts_dir / "decisions.jsonl"
        cus.INBOX_MD = self.accounts_dir / "inbox.md"

        # Occupancy is /proc ground truth in production; tests declare it.
        self._saved_mount_pids = cus.mount_pids
        self.live_slots: set[str] = set()
        cus.mount_pids = lambda mount: [1] if Path(mount).name in self.live_slots else []
        cus._OCCUPIED_SLOTS_CACHE.clear()

    def make_slot(self, account: str | None, live: bool = True) -> str:
        state = cus.load_state()
        name, d = cus.create_slot(state)
        if account:
            (d / ".credentials.json").write_text(json.dumps(_creds(f"rt-{account}")))
            cus.write_json(d / ".claude.json", _identity(account))
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


def test_occupied_slot_accounts_only_live():
    env = _Env()
    try:
        env.make_slot("alpha", live=True)
        env.make_slot("beta", live=False)  # idle slot: holds an account, no session
        state = cus.load_state()
        occ = cus.occupied_slot_accounts(state)
        assert occ == {"alpha": ["slot-1"]}
    finally:
        env.restore()


def test_decide_slot_swaps_fans_out_distinct_targets():
    env = _Env()
    try:
        s1 = env.make_slot("alpha", live=True)
        s2 = env.make_slot("alpha", live=True)  # two sessions on the same hot account
        state = cus.load_state()
        state["accounts"]["alpha"].update({"current_5h_pct": 85.0, "current_7d_pct": 20.0})
        state["accounts"]["beta"].update({"current_5h_pct": 10.0, "current_7d_pct": 10.0})
        state["accounts"]["gamma"].update({"current_5h_pct": 20.0, "current_7d_pct": 15.0})
        usage = {"alpha": _usage(85.0, 20.0), "beta": _usage(10.0, 10.0), "gamma": _usage(20.0, 15.0)}

        moves = cus.decide_slot_swaps(state, _config(), usage)
        assert {m["slot"] for m in moves} == {s1, s2}
        assert all(m["from"] == "alpha" for m in moves)
        targets = [m["to"] for m in moves]
        assert len(set(targets)) == 2, f"fan-out must pick distinct targets, got {targets}"
        assert set(targets) <= {"beta", "gamma", "delta"}
    finally:
        env.restore()


def test_decide_slot_swaps_holds_below_threshold():
    env = _Env()
    try:
        env.make_slot("alpha", live=True)
        state = cus.load_state()
        usage = {"alpha": _usage(30.0, 10.0)}
        assert cus.decide_slot_swaps(state, _config(), usage) == []
    finally:
        env.restore()


def test_reactive_429_attributes_to_slot_account():
    env = _Env()
    try:
        s1 = env.make_slot("alpha", live=True)
        env.make_slot("beta", live=True)
        state = cus.load_state()
        # Timestamps relative to the real clock (the watermark is set to
        # now_iso() inside the function under test).
        from datetime import datetime, timedelta, timezone
        def _iso(minutes_ago: float) -> str:
            return (datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)).isoformat().replace("+00:00", "Z")
        # sessions.log records the panes; the 429 hit sA. Attribution now
        # resolves session → CURRENT slot via /proc (session_current_slot),
        # not the stale launch-time account field — stub it since there are no
        # real processes on the panes in-test. sA is on slot-1 (alpha), sB on
        # slot-2 (beta).
        cus.SESSIONS_LOG.write_text(f"{_iso(10)},sA,alpha,%1,/tmp\n{_iso(10)},sB,beta,%2,/tmp\n")
        cus.RATE_LIMIT_LOG.write_text(f"{_iso(1)},sA,429 rate limit\n")
        state["last_429_check_ts"] = _iso(5)
        _orig_scs = cus.session_current_slot
        cus.session_current_slot = lambda sid: {"sA": s1}.get(sid)
        try:
            moves = cus.check_rate_limit_reactive_per_session(state, _config())
            assert len(moves) == 1
            assert moves[0]["slot"] == s1 and moves[0]["from"] == "alpha"
            assert moves[0]["deferrable"] is False and moves[0]["gate"] == "reactive_429"

            # Watermark advanced → same entries don't re-trigger.
            assert cus.check_rate_limit_reactive_per_session(state, _config()) == []
        finally:
            cus.session_current_slot = _orig_scs
    finally:
        env.restore()


def test_fast_cadence_tracks_occupancy_in_per_session():
    env = _Env()
    try:
        env.make_slot("beta", live=True)
        state = cus.load_state()
        cfg = _config()
        assert cus._account_on_fast_cadence(state, cfg, "beta") is True, "occupied → fast"
        assert cus._account_on_fast_cadence(state, cfg, "gamma") is False, "idle pool → slow"
        # Global active is fast only while a bare session holds ~/.claude/
        # (mount_pids stub returns [] for it here).
        assert cus._account_on_fast_cadence(state, cfg, "alpha") is False
        # Global mode unchanged: active fast, others slow.
        gcfg = cus.deep_merge(cus.DEFAULT_CONFIG, {"mode": "global"})
        assert cus._account_on_fast_cadence(state, gcfg, "alpha") is True
        assert cus._account_on_fast_cadence(state, gcfg, "beta") is False
    finally:
        env.restore()


def test_per_session_cycle_moves_hot_slot_in_place():
    env = _Env()
    try:
        s1 = env.make_slot("alpha", live=True)
        state = cus.load_state()
        state["accounts"]["alpha"].update({"current_5h_pct": 85.0, "current_7d_pct": 20.0})
        state["accounts"]["beta"].update({"current_5h_pct": 10.0, "current_7d_pct": 10.0})
        usage = {"alpha": _usage(85.0, 20.0), "beta": _usage(10.0, 10.0)}
        global_before = (cus.CREDS_JSON.read_text(), cus.CLAUDE_JSON.read_text())

        cus._per_session_cycle(state, _config(), usage, no_execute=False)

        st = cus.load_state()
        assert st["slots"][s1]["account"] != "alpha", "hot slot moved"
        d = env.accounts_dir / s1
        installed = json.loads((d / ".credentials.json").read_text())["claudeAiOauth"]["refreshToken"]
        assert installed == f"rt-{st['slots'][s1]['account']}"
        assert st["active"] == "alpha", "state.active untouched"
        assert (cus.CREDS_JSON.read_text(), cus.CLAUDE_JSON.read_text()) == global_before, \
            "global mount NEVER written in per_session mode"
        assert st["accounts"]["alpha"]["next_swap_at_pct"] == 75, "outgoing ladder bumped"
    finally:
        env.restore()


def test_per_session_cycle_no_execute_holds():
    env = _Env()
    try:
        s1 = env.make_slot("alpha", live=True)
        state = cus.load_state()
        usage = {"alpha": _usage(85.0, 20.0), "beta": _usage(10.0, 10.0)}
        cus._per_session_cycle(state, _config(), usage, no_execute=True)
        st = cus.load_state()
        assert st["slots"][s1]["account"] == "alpha", "--no-execute: nothing moved"
    finally:
        env.restore()


def test_periodic_saveback_only_when_fresher():
    env = _Env()
    try:
        s1 = env.make_slot("alpha", live=True)
        d = env.accounts_dir / s1
        snap = env.accounts_dir / "account-alpha" / ".credentials.json"
        snap_before = snap.read_text()
        state = cus.load_state()

        # Same expiresAt → periodic pass is a no-op (no backup churn).
        cus._per_session_cycle(state, _config(), {}, no_execute=True)
        assert snap.read_text() == snap_before

        # Session refreshed its tokens → periodic pass saves them back.
        (d / ".credentials.json").write_text(json.dumps(_creds("rt-alpha", expires_at=3_000_000_000_000)))
        state = cus.load_state()
        cus._per_session_cycle(state, _config(), {}, no_execute=True)
        assert json.loads(snap.read_text())["claudeAiOauth"]["expiresAt"] == 3_000_000_000_000
    finally:
        env.restore()


def test_decide_slot_swaps_skips_locked_slot():
    env = _Env()
    try:
        s1 = env.make_slot("alpha", live=True)
        s2 = env.make_slot("alpha", live=True)
        state = cus.load_state()
        state["accounts"]["alpha"].update({"current_5h_pct": 85.0, "current_7d_pct": 20.0})
        usage = {"alpha": _usage(85.0, 20.0), "beta": _usage(10.0, 10.0), "gamma": _usage(20.0, 15.0)}
        cfg = _config(session_locks={"locked_slots": [s1]})
        moves = cus.decide_slot_swaps(state, cfg, usage)
        # Only the unlocked slot moves; the locked one is frozen in place.
        assert {m["slot"] for m in moves} == {s2}

        # Locking BOTH slots: the hot account produces no moves at all.
        cfg_all = _config(session_locks={"locked_slots": [s1, s2]})
        assert cus.decide_slot_swaps(state, cfg_all, usage) == []
    finally:
        env.restore()


def test_reactive_429_skips_locked_slot():
    env = _Env()
    try:
        s1 = env.make_slot("alpha", live=True)
        state = cus.load_state()
        from datetime import datetime, timedelta, timezone
        def _iso(minutes_ago: float) -> str:
            return (datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)).isoformat().replace("+00:00", "Z")
        cus.SESSIONS_LOG.write_text(f"{_iso(10)},sA,alpha,%1,/tmp\n")
        cus.RATE_LIMIT_LOG.write_text(f"{_iso(1)},sA,429 rate limit\n")
        state["last_429_check_ts"] = _iso(5)
        _orig_scs = cus.session_current_slot
        cus.session_current_slot = lambda sid: {"sA": s1}.get(sid)
        try:
            # A real 429 on a locked slot must NOT move it (the lock is the
            # user's explicit override; SOS surfaces the exhaustion instead).
            cfg = _config(session_locks={"locked_slots": [s1]})
            assert cus.check_rate_limit_reactive_per_session(state, cfg) == []
        finally:
            cus.session_current_slot = _orig_scs
    finally:
        env.restore()



if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn()
        print(f"  ✓ {fn.__name__}")
    print(f"{len(fns)} passed")
