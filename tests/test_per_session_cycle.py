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
            # exclude_accounts = the accounts every OTHER live slot holds, exactly
            # as _per_session_cycle now supplies (fix 2026-07-05, GH #104): beta is
            # live on slot-2, so the 429 escape must avoid it (moving onto a live
            # account double-books its single-use refresh token). Pre-fix the cycle
            # passed nothing and the escape could clobber beta; now it lands on a
            # free account (gamma/delta).
            moves = cus.check_rate_limit_reactive_per_session(state, _config(), exclude_accounts={"beta"})
            assert len(moves) == 1
            assert moves[0]["slot"] == s1 and moves[0]["from"] == "alpha"
            assert moves[0]["to"] != "beta", f"must not escape onto live beta (GH #104): {moves}"
            assert moves[0]["deferrable"] is False and moves[0]["gate"] == "reactive_429"

            # Watermark advanced → same entries don't re-trigger.
            assert cus.check_rate_limit_reactive_per_session(state, _config(), exclude_accounts={"beta"}) == []
        finally:
            cus.session_current_slot = _orig_scs
    finally:
        env.restore()


def test_parse_sessions_log_preserves_tmux_socket_and_legacy_rows():
    """The 6-col line (…,<pane>,<tmux_socket>,<cwd>) surfaces the socket and
    keeps cwd greedy-last; a legacy 5-col line (no socket column) still parses
    with tmux_socket=None. Backward-compat for logs written before the migration.
    """
    env = _Env()
    try:
        cus.SESSIONS_LOG.write_text(
            "2026-07-06T00:00:00Z,new,alpha,%0,/tmp/tmux-1000/committee-loop,/work,tree\n"
            "2026-07-06T00:00:01Z,old,beta,%1,/legacy\n"
            "2026-07-06T00:00:02Z,notmux,gamma,no-tmux,no-tmux,/x\n"
        )
        entries = cus._parse_sessions_log()
        # 6-col: socket parsed, cwd is the greedy remainder (comma preserved).
        assert entries[0]["tmux_socket"] == "/tmp/tmux-1000/committee-loop"
        assert entries[0]["cwd"] == "/work,tree"
        # legacy 5-col: no socket column → None, cwd = last field.
        assert entries[1]["tmux_socket"] is None
        assert entries[1]["cwd"] == "/legacy"
        # explicit "no-tmux" socket normalizes to None.
        assert entries[2]["tmux_socket"] is None
        assert entries[2]["cwd"] == "/x"
    finally:
        env.restore()


def test_session_current_slot_uses_tmux_socket_to_disambiguate_duplicate_panes():
    """session_current_slot must pass the recorded socket to pane_mount_name so
    a %0 on one tmux server resolves to that server's mount, not a same-named
    pane on the default socket."""
    orig_parse = cus._parse_sessions_log
    orig_pane_mount_name = cus.pane_mount_name
    seen = []
    try:
        cus._parse_sessions_log = lambda: [{
            "ts": "2026-07-06T00:00:00Z",
            "session_id": "sess",
            "account": "alpha",
            "pane": "%0",
            "tmux_socket": "/tmp/tmux-1000/committee-loop",
            "cwd": "/worktree",
        }]

        def fake_pane_mount_name(pane, tmux_socket=None):
            seen.append((pane, tmux_socket))
            return "slot-2" if tmux_socket == "/tmp/tmux-1000/committee-loop" else "slot-1"

        cus.pane_mount_name = fake_pane_mount_name
        assert cus.session_current_slot("sess") == "slot-2"
        assert seen == [("%0", "/tmp/tmux-1000/committee-loop")]
    finally:
        cus._parse_sessions_log = orig_parse
        cus.pane_mount_name = orig_pane_mount_name


def test_pane_mount_name_disambiguates_same_pane_across_tmux_sockets():
    """Core cross-server fix: the SAME pane id (%3) on two tmux servers must
    resolve to DIFFERENT mounts. The socket selects which server's pane PID is
    read; without it both %3s hit the default socket and collapse to one mount
    → wrong account attribution."""
    env = _Env()
    try:
        sock_a = "/tmp/tmux-1000/default"
        sock_b = "/tmp/tmux-1000/committee-loop"
        pid_by_socket = {sock_a: 101, sock_b: 202}
        mount_by_pid = {101: cus.ACCOUNTS_DIR / "slot-1", 202: cus.ACCOUNTS_DIR / "slot-2"}
        orig = (cus._pane_pid, cus._find_descendant_claude_pid, cus._pid_config_dir)
        # A None socket means "the default tmux server" — pane_mount_name runs
        # bare `tmux` (no -S), which connects to the default socket, modeled here
        # as sock_a. So resolve None the same way _pane_pid would on the default
        # server, rather than leaving a stub gap that masks the real behavior.
        cus._pane_pid = lambda pane, tmux_socket=None: pid_by_socket.get(tmux_socket if tmux_socket is not None else sock_a)
        cus._find_descendant_claude_pid = lambda pid: pid
        cus._pid_config_dir = lambda pid: str(mount_by_pid[pid])
        try:
            assert cus.pane_mount_name("%3", sock_a) == "slot-1"
            assert cus.pane_mount_name("%3", sock_b) == "slot-2"
            # No socket → the DEFAULT tmux server (bare `tmux`), modeled as sock_a
            # → slot-1; an EXPLICIT sock_b still resolves independently (%3 does
            # not collapse across servers).
            assert cus.pane_mount_name("%3", None) == "slot-1"
        finally:
            cus._pane_pid, cus._find_descendant_claude_pid, cus._pid_config_dir = orig
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




def _usage_pm(five_h, seven_d, fable=None):
    """AccountUsage with an optional Fable per-model weekly window."""
    pm = {}
    if fable is not None:
        pm["Fable"] = cus.UsageWindow(utilization=fable, resets_at=None)
    return cus.AccountUsage(
        five_hour=cus.UsageWindow(utilization=five_h, resets_at=None),
        seven_day=cus.UsageWindow(utilization=seven_d, resets_at=None),
        per_model_weekly=pm,
    )


def _pool_config(**overrides):
    """Gate-on config: Fable capped at 97, aggregate hard cap 80."""
    return _config(
        strategy="smart",
        smart_strategy={"hard_7d_cap_pct": 80},
        per_model_weekly={"gate_enabled": True, "models": ["Fable"], "cap_pct": 97},
        **overrides,
    )


def test_slot_pool_defaults_and_explicit():
    env = _Env()
    try:
        s1 = env.make_slot("alpha", live=True)
        state = cus.load_state()
        cfg = _config()  # default_pool defaults to "premium"
        assert cus._slot_pool(state, s1, cfg) == "premium"
        state["slots"][s1]["pool"] = "standard"
        assert cus._slot_pool(state, s1, cfg) == "standard"
        # Unknown value falls back to the configured default.
        state["slots"][s1]["pool"] = "bogus"
        assert cus._slot_pool(state, s1, cfg) == "premium"
    finally:
        env.restore()


def test_config_for_pool_disables_model_gate_for_standard():
    cfg = _pool_config()
    assert cfg["per_model_weekly"]["gate_enabled"] is True
    prem = cus._config_for_pool(cfg, "premium")
    std = cus._config_for_pool(cfg, "standard")
    assert prem is cfg  # premium: unchanged object
    assert std["per_model_weekly"]["gate_enabled"] is False
    assert std["per_model_weekly"]["reserve_safe_for_premium"] is True
    assert cfg["per_model_weekly"]["gate_enabled"] is True  # original untouched


def test_standard_pool_prefers_model_exhausted_target_to_preserve_safe_accounts():
    env = _Env(accounts=("alpha", "beta", "gamma"))
    try:
        state = cus.load_state()
        state["active"] = "alpha"
        state["accounts"]["alpha"].update({"current_5h_pct": 100.0, "current_7d_pct": 20.0,
                                           "per_model_weekly_pct": {"Fable": 20.0}})
        state["accounts"]["beta"].update({"current_5h_pct": 0.0, "current_7d_pct": 10.0,
                                          "per_model_weekly_pct": {"Fable": 20.0}})
        # gamma: Fable-exhausted (model-spent) but HEALTHY — aggregate headroom
        # AND below the first ladder step (won't immediately re-trip), so the
        # reserve narrows the standard lane onto it. (7d=40, adapted from the
        # pre-fix 60: the required not-re-trip guard now excludes a model-spent
        # target that sits at/over steps[0]=50, so it must be genuinely healthy.)
        state["accounts"]["gamma"].update({"current_5h_pct": 9.0, "current_7d_pct": 40.0,
                                           "per_model_weekly_pct": {"Fable": 100.0}})

        target = cus.pick_swap_target(state, cus._config_for_pool(_pool_config(), "standard"))

        assert target is not None
        assert target.name == "gamma"
    finally:
        env.restore()


def test_standard_pool_falls_back_to_safe_account_when_model_exhausted_target_degraded():
    # reserve_safe_for_premium is a PREFERENCE, not a wall. When every
    # model-exhausted (Fable-spent) account is itself over the 7d cap (or would
    # immediately re-trip), a standard lane must fall back to a Fable-fresh
    # account that still has aggregate headroom rather than starve on the
    # degraded model-spent one. Regression for the 2026-07-06 slot-5 SOS: all
    # headroom accounts were Fable-fresh, so reserve narrowed the lane to a lone
    # 81%-7d account and pick_swap_target returned DEGRADED → a false "no swap
    # target" alarm while idle capacity sat reserved.
    env = _Env(accounts=("alpha", "beta", "gamma"))
    try:
        state = cus.load_state()
        state["active"] = "alpha"
        # alpha: the standard lane's own account, leaving on 5h.
        state["accounts"]["alpha"].update({"current_5h_pct": 100.0, "current_7d_pct": 20.0,
                                           "per_model_weekly_pct": {"Fable": 20.0}})
        # beta: Fable-fresh AND aggregate headroom — the correct fallback target.
        state["accounts"]["beta"].update({"current_5h_pct": 0.0, "current_7d_pct": 10.0,
                                          "per_model_weekly_pct": {"Fable": 20.0}})
        # gamma: Fable-exhausted (model-spent) but over the 80% 7d cap → degraded.
        state["accounts"]["gamma"].update({"current_5h_pct": 0.0, "current_7d_pct": 85.0,
                                           "per_model_weekly_pct": {"Fable": 100.0}})

        target = cus.pick_swap_target(state, cus._config_for_pool(_pool_config(), "standard"))

        assert target is not None
        assert target.name == "beta", f"expected fallback to Fable-fresh beta, got {target.name}"
        assert "DEGRADED" not in target.reason, target.reason
    finally:
        env.restore()


def test_non_gated_install_does_not_reserve_model_exhausted_targets():
    # REQUIRED-FIX regression (reviewer 2026-07-06): reserve_safe_for_premium
    # must be INACTIVE when the per-model gate is off (a non-gated install, the
    # default). The key lives only in the standard-pool VIEW that _config_for_pool
    # builds for a GATED install — it is not in DEFAULT_CONFIG — so a non-gated
    # install never steers toward model-exhausted accounts. pick_swap_target here
    # must pick by usage (gamma, lowest), NOT the model-exhausted beta.
    env = _Env(accounts=("alpha", "beta", "gamma"))
    try:
        state = cus.load_state()
        state["active"] = "alpha"
        state["accounts"]["alpha"].update({"current_5h_pct": 100.0, "current_7d_pct": 20.0,
                                           "per_model_weekly_pct": {"Fable": 20.0}})
        # beta: model-exhausted (Fable=100) but aggregate-healthy — a non-gated
        # install must NOT prefer it just because its per-model week is spent.
        state["accounts"]["beta"].update({"current_5h_pct": 5.0, "current_7d_pct": 12.0,
                                          "per_model_weekly_pct": {"Fable": 100.0}})
        # gamma: Fable-fresh and lowest usage — the correct lowest_usage pick.
        state["accounts"]["gamma"].update({"current_5h_pct": 0.0, "current_7d_pct": 8.0,
                                           "per_model_weekly_pct": {"Fable": 10.0}})
        # _config() is a non-gated install (per_model_weekly.gate_enabled False).
        target = cus.pick_swap_target(state, _config())
        assert target is not None
        assert target.name == "gamma", f"non-gated install must not reserve; got {target.name}"
    finally:
        env.restore()


def test_reserve_skips_would_re_trip_model_spent_target():
    # REQUIRED-FIX regression (reviewer 2026-07-06): the reserve guard is
    # `7d-under-cap AND not-would-immediately-re-trip`, not 7d-only. A standard
    # lane must NOT be narrowed onto a model-spent account that would re-trip its
    # own ladder on arrival when a HEALTHY Fable-fresh target exists in the full
    # pool. Here beta is model-spent (Fable=100) AND would re-trip (5h=60 ≥
    # steps[0]=50) though its 7d (20) is under the 80 cap; gamma is Fable-fresh
    # and healthy. A 7d-only guard would narrow onto beta and strand the lane on
    # a re-tripping (DEGRADED) target; the fix picks gamma.
    env = _Env(accounts=("alpha", "beta", "gamma"))
    try:
        state = cus.load_state()
        state["active"] = "alpha"
        state["accounts"]["alpha"].update({"current_5h_pct": 100.0, "current_7d_pct": 20.0,
                                           "per_model_weekly_pct": {"Fable": 20.0}})
        state["accounts"]["beta"].update({"current_5h_pct": 60.0, "current_7d_pct": 20.0,
                                          "per_model_weekly_pct": {"Fable": 100.0}})
        state["accounts"]["gamma"].update({"current_5h_pct": 0.0, "current_7d_pct": 10.0,
                                           "per_model_weekly_pct": {"Fable": 20.0}})

        target = cus.pick_swap_target(state, cus._config_for_pool(_pool_config(), "standard"))

        assert target is not None
        assert target.name == "gamma", f"expected healthy Fable-fresh gamma, got {target.name}"
        assert "DEGRADED" not in target.reason, target.reason
    finally:
        env.restore()


def test_premium_slot_degrades_temporarily_when_no_model_safe_target():
    env = _Env(accounts=("alpha", "beta"))
    try:
        slot = env.make_slot("alpha", live=True)
        state = cus.load_state()
        state["accounts"]["alpha"].update({"current_5h_pct": 100.0, "current_7d_pct": 25.0,
                                           "per_model_weekly_pct": {"Fable": 30.0}})
        # beta: Fable-dead (premium gate rejects it) but aggregate-healthy — a
        # CLEAN standard target (7d=30 < steps[0]=50, adapted from the pre-fix
        # 63 so the standard retry yields a non-DEGRADED pick, per PIECE 4).
        state["accounts"]["beta"].update({"current_5h_pct": 0.0, "current_7d_pct": 30.0,
                                          "per_model_weekly_pct": {"Fable": 100.0}})
        cus.save_state(state)
        state = cus.load_state()
        usage = {"alpha": _usage_pm(100.0, 25.0, fable=30.0),
                 "beta": _usage_pm(0.0, 30.0, fable=100.0)}

        moves = cus.decide_slot_swaps(state, _pool_config(), usage)
        assert len(moves) == 1, moves
        assert moves[0]["slot"] == slot
        assert moves[0]["to"] == "beta"
        assert moves[0]["pool"] == "standard"
        assert "premium degraded to standard" in moves[0]["reason"]

        cus._execute_slot_moves(moves, state, _pool_config(), no_execute=False)
        # The move is for THIS cycle only — the slot's pool tag stays premium.
        assert cus._slot_pool(cus.load_state(), slot, _pool_config()) == "premium"
    finally:
        env.restore()


def test_premium_slot_swaps_off_model_exhausted_standard_holds():
    # alpha's Fable week is at 98% (over the 97 cap) but aggregate is low.
    # A PREMIUM slot on alpha must swap off it (hard-cap force-swap); a
    # STANDARD slot on alpha must HOLD (model gate ignored, aggregate under cap).
    env = _Env()
    try:
        s_prem = env.make_slot("alpha", live=True)
        state = cus.load_state()
        state["accounts"]["alpha"].update({"current_5h_pct": 10.0, "current_7d_pct": 15.0,
                                           "per_model_weekly_pct": {"Fable": 98.0}})
        state["accounts"]["beta"].update({"current_5h_pct": 5.0, "current_7d_pct": 10.0})
        usage = {"alpha": _usage_pm(10.0, 15.0, fable=98.0), "beta": _usage_pm(5.0, 10.0)}
        cfg = _pool_config()

        # Premium (default pool): swaps off alpha.
        moves = cus.decide_slot_swaps(state, cfg, usage)
        assert len(moves) == 1 and moves[0]["from"] == "alpha" and moves[0]["pool"] == "premium"
        assert moves[0]["gate"] == "hard_7d_cap"

        # Retag the slot standard: same numbers → HOLD.
        state["slots"][s_prem]["pool"] = "standard"
        cus._OCCUPIED_SLOTS_CACHE.clear()
        assert cus.decide_slot_swaps(state, cfg, usage) == []
    finally:
        env.restore()


def test_two_pools_same_account_decide_independently():
    # Two live slots on alpha (Fable exhausted): one premium, one standard.
    # The premium one moves, the standard one stays — proving (account, pool)
    # grouping produces independent decisions in a single cycle.
    env = _Env()
    try:
        s_prem = env.make_slot("alpha", live=True)
        s_std = env.make_slot("alpha", live=True)
        state = cus.load_state()
        state["slots"][s_std]["pool"] = "standard"
        state["accounts"]["alpha"].update({"current_5h_pct": 10.0, "current_7d_pct": 15.0,
                                           "per_model_weekly_pct": {"Fable": 98.0}})
        state["accounts"]["beta"].update({"current_5h_pct": 5.0, "current_7d_pct": 10.0})
        cus._OCCUPIED_SLOTS_CACHE.clear()
        usage = {"alpha": _usage_pm(10.0, 15.0, fable=98.0), "beta": _usage_pm(5.0, 10.0)}
        moves = cus.decide_slot_swaps(state, _pool_config(), usage)
        by_slot = {m["slot"]: m for m in moves}
        assert set(by_slot) == {s_prem}, f"only the premium slot moves, got {list(by_slot)}"
        assert by_slot[s_prem]["pool"] == "premium"
    finally:
        env.restore()


def test_launch_prepare_records_pool():
    env = _Env()
    try:
        state = cus.load_state()
        cfg = _config()
        slot_name, _, acct = cus._launch_prepare("alpha", state, cfg, pool="standard")
        fresh = cus.load_state()
        assert fresh["slots"][slot_name]["pool"] == "standard"
        # Default when unspecified = premium.
        state2 = cus.load_state()
        slot2, _, _ = cus._launch_prepare("beta", state2, cfg)
        assert cus.load_state()["slots"][slot2]["pool"] == "premium"
    finally:
        env.restore()


def test_reactive_entries_param_does_not_advance_watermark():
    # Hybrid reads the 429 log once and owns the watermark; when it passes
    # `entries` to the reactive fns, they must NOT re-advance last_429_check_ts.
    env = _Env()
    try:
        state = cus.load_state()
        state["active"] = "alpha"
        wm = "2020-01-01T00:00:00Z"
        state["last_429_check_ts"] = wm
        assert cus.check_rate_limit_reactive_per_session(state, _config(), entries=[]) == []
        assert state["last_429_check_ts"] == wm
        assert cus.check_rate_limit_reactive(state, _config(), entries=[]) is None
        assert state["last_429_check_ts"] == wm
        # With entries=None (default) it DOES own+advance the watermark.
        cus.RATE_LIMIT_LOG.write_text("")  # empty log
        cus.check_rate_limit_reactive(state, _config(), entries=None)
        assert state["last_429_check_ts"] != wm
    finally:
        env.restore()


def test_hybrid_cycle_moves_slot_and_shared_mount_together():
    # The core hybrid promise: in ONE cycle, a hot slot moves AND the hot
    # shared mount (bare sessions) swaps. Slot moves carry slot=slot-N;
    # the shared-mount swap carries slot=None — how we tell them apart.
    env = _Env()
    try:
        s1 = env.make_slot("alpha", live=True)      # slotted session on alpha
        state = cus.load_state()
        state["active"] = "beta"                     # bare sessions on the shared mount = beta
        state["accounts"]["alpha"].update({"current_5h_pct": 95.0, "current_7d_pct": 20.0})
        state["accounts"]["beta"].update({"current_5h_pct": 96.0, "current_7d_pct": 20.0})
        state["accounts"]["gamma"].update({"current_5h_pct": 5.0, "current_7d_pct": 5.0})
        state["accounts"]["delta"].update({"current_5h_pct": 6.0, "current_7d_pct": 6.0})
        cus.save_state(state)
        cus._OCCUPIED_SLOTS_CACHE.clear()
        usage = {n: _usage(state["accounts"][n]["current_5h_pct"], state["accounts"][n]["current_7d_pct"])
                 for n in ("alpha", "beta", "gamma", "delta")}
        calls = []
        orig = cus.execute_swap
        cus.execute_swap = lambda target, trigger="manual", slot=None, bump_ladder=True: calls.append((target, slot))
        try:
            cus._hybrid_cycle(state, _config(mode="hybrid"), usage, no_execute=False)
        finally:
            cus.execute_swap = orig
        slot_moves = [c for c in calls if c[1] == s1]
        shared_moves = [c for c in calls if c[1] is None]
        assert slot_moves, f"expected a slot move for {s1}; calls={calls}"
        assert shared_moves, f"expected a shared-mount swap (slot=None); calls={calls}"
    finally:
        env.restore()


def test_decide_slot_swaps_excludes_cross_mount_accounts():
    # GH #104: a slot must never move onto an account another live mount holds.
    # alpha (slot) is hot; beta is the shared-mount account (excluded); gamma is
    # the only other fresh account → the slot must pick gamma, never beta.
    env = _Env()
    try:
        s1 = env.make_slot("alpha", live=True)
        state = cus.load_state()
        state["accounts"]["alpha"].update({"current_5h_pct": 95.0, "current_7d_pct": 20.0})
        state["accounts"]["beta"].update({"current_5h_pct": 2.0, "current_7d_pct": 2.0})   # lowest — would win if not excluded
        state["accounts"]["gamma"].update({"current_5h_pct": 8.0, "current_7d_pct": 8.0})
        cus.save_state(state)
        cus._OCCUPIED_SLOTS_CACHE.clear()
        usage = {n: _usage(state["accounts"][n]["current_5h_pct"], state["accounts"][n]["current_7d_pct"])
                 for n in ("alpha", "beta", "gamma", "delta")}
        moves = cus.decide_slot_swaps(state, _config(), usage, exclude_accounts={"beta"})
        assert moves and moves[0]["slot"] == s1
        assert moves[0]["to"] != "beta", f"slot must not move onto excluded 'beta'; got {moves[0]['to']}"
    finally:
        env.restore()


def test_hybrid_never_double_books_an_account():
    # GH #104: in one hybrid cycle the slot move and the shared-mount swap must
    # land on DISTINCT accounts, and neither on the other mount's account.
    env = _Env()
    try:
        s1 = env.make_slot("alpha", live=True)     # slot on alpha (hot)
        state = cus.load_state()
        state["active"] = "beta"                    # shared mount on beta (hot)
        state["accounts"]["alpha"].update({"current_5h_pct": 95.0, "current_7d_pct": 20.0})
        state["accounts"]["beta"].update({"current_5h_pct": 96.0, "current_7d_pct": 20.0})
        state["accounts"]["gamma"].update({"current_5h_pct": 5.0, "current_7d_pct": 5.0})
        state["accounts"]["delta"].update({"current_5h_pct": 6.0, "current_7d_pct": 6.0})
        cus.save_state(state)
        cus._OCCUPIED_SLOTS_CACHE.clear()
        usage = {n: _usage(state["accounts"][n]["current_5h_pct"], state["accounts"][n]["current_7d_pct"])
                 for n in ("alpha", "beta", "gamma", "delta")}
        calls = []
        orig = cus.execute_swap
        cus.execute_swap = lambda target, trigger="manual", slot=None, bump_ladder=True: calls.append((target, slot))
        try:
            cus._hybrid_cycle(state, _config(mode="hybrid"), usage, no_execute=False)
        finally:
            cus.execute_swap = orig
        slot_target = next((t for t, sl in calls if sl == s1), None)
        shared_target = next((t for t, sl in calls if sl is None), None)
        assert slot_target and shared_target, f"expected both a slot and a shared move; calls={calls}"
        assert slot_target != shared_target, f"double-booked: both mounts took {slot_target}"
        assert slot_target != "beta", "slot must not take the shared mount's account"
        assert shared_target != "alpha", "shared mount must not take the slot's account"
    finally:
        env.restore()


def test_pick_launch_account_never_picks_a_live_account():
    # GH #104: even when a live-slot account is the lowest-usage (would-win)
    # candidate, launch must not pick it — a second live mount on it signs one
    # session out. alpha is on a live slot at 0%; beta is the lowest FREE one.
    env = _Env()
    try:
        env.make_slot("alpha", live=True)
        state = cus.load_state()
        state["active"] = None  # no bare shared session in this test
        state["accounts"]["alpha"].update({"current_5h_pct": 0.0, "current_7d_pct": 0.0})
        state["accounts"]["beta"].update({"current_5h_pct": 10.0, "current_7d_pct": 10.0})
        state["accounts"]["gamma"].update({"current_5h_pct": 20.0, "current_7d_pct": 20.0})
        state["accounts"]["delta"].update({"current_5h_pct": 30.0, "current_7d_pct": 30.0})
        cus.save_state(state)
        cus._OCCUPIED_SLOTS_CACHE.clear()
        picked = cus.pick_launch_account(state, _config())
        assert picked is not None and picked.name != "alpha", f"must not pick the live account; got {picked and picked.name}"
        assert picked.name == "beta", f"expected lowest FREE account 'beta'; got {picked.name}"
    finally:
        env.restore()


def test_launch_prepare_refuses_explicit_live_account():
    # GH #104: `cus launch alpha` when alpha is already on a live slot is
    # refused (unless --force).
    env = _Env()
    try:
        env.make_slot("alpha", live=True)
        state = cus.load_state()
        state["active"] = None
        cus.save_state(state)
        cus._OCCUPIED_SLOTS_CACHE.clear()
        refused = False
        try:
            cus._launch_prepare("alpha", cus.load_state(), _config())
        except Exception as e:  # click.ClickException
            refused = "already running on a live mount" in str(e)
        assert refused, "launching an explicit already-live account must be refused"
    finally:
        env.restore()


def test_decide_slot_swaps_holds_when_pool_exhausted_no_double_book():
    """Two hot slots, ONE free target: first slot takes it, second HOLDS on its
    account instead of doubling up (2026-07-03 — the double-up fallback put one
    token family on two live mounts, the #104 clobber; slot-3/slot-4 incident)."""
    env = _Env(accounts=("alpha", "beta"))
    try:
        s1 = env.make_slot("alpha", live=True)
        s2 = env.make_slot("alpha", live=True)
        state = cus.load_state()
        state["accounts"]["alpha"].update({"current_5h_pct": 85.0, "current_7d_pct": 20.0})
        state["accounts"]["beta"].update({"current_5h_pct": 10.0, "current_7d_pct": 10.0})
        usage = {"alpha": _usage(85.0, 20.0), "beta": _usage(10.0, 10.0)}

        moves = cus.decide_slot_swaps(state, _config(), usage)
        assert len(moves) == 1, f"exactly one slot moves; the other holds — got {moves}"
        assert moves[0]["to"] == "beta"
        assert moves[0]["slot"] in {s1, s2}
    finally:
        env.restore()


def test_ladder_hysteresis_reads_auto_swap_clock_only():
    """The ladder cooldown is armed by last_auto_swap_ts (daemon swaps), not
    last_swap_ts (which launches also bump) — 2026-07-03: launches kept
    re-arming a 50-min cooldown and parked hot slots while they climbed."""
    env = _Env(accounts=("alpha", "beta"))
    try:
        state = cus.load_state()
        state["accounts"]["alpha"].update({
            "current_5h_pct": 85.0, "current_7d_pct": 20.0,
            "last_swap_ts": cus.now_iso(),  # a launch just installed alpha somewhere
        })
        state["accounts"]["beta"].update({"current_5h_pct": 10.0, "current_7d_pct": 10.0})
        usage = {"alpha": _usage(85.0, 20.0), "beta": _usage(10.0, 10.0)}
        cfg = _config(swap_hysteresis={"enabled": True, "min_seconds_between_swaps": 3000})

        d = cus.decide_swap(state, cfg, usage, {})
        assert d is not None and d.target == "beta", \
            "a fresh LAUNCH must not defer the ladder"

        state["accounts"]["alpha"]["last_auto_swap_ts"] = cus.now_iso()
        d = cus.decide_swap(state, cfg, usage, {})
        assert d is None, "a fresh DAEMON swap arms the cooldown"
    finally:
        env.restore()


def test_duplicate_live_mount_families_detects_shared_family():
    """Two LIVE mounts holding one refresh-token family are the #104 clobber;
    idle duplicates aren't flagged (nothing refreshes an idle mount).
    2026-07-03: this condition existed on slot-3/slot-4 while `cus sos` said
    all-clear — the store-based check only sees provisioned logins."""
    env = _Env()
    try:
        s1 = env.make_slot("alpha", live=True)
        s2 = env.make_slot("alpha", live=True)  # same snapshot copy → same family
        state = cus.load_state()
        dups = cus.duplicate_live_mount_families(state)
        assert len(dups) == 1, dups
        assert set(dups[0]["mounts"]) == {f"{s1} (alpha)", f"{s2} (alpha)"}

        env.live_slots.discard(s2)  # session exits → duplicate is idle → no clobber risk
        cus._OCCUPIED_SLOTS_CACHE.clear()
        assert cus.duplicate_live_mount_families(state) == []
    finally:
        env.restore()


def test_double_booked_live_accounts_both_phases():
    """The by-account #104 check catches both clobber phases: identical
    families (will_clobber — refresh pending) AND diverged families
    (already_rotated — one mount holds a dead refresh token; the same-family
    check goes quiet the moment the first rotation lands, which is how
    slot-3/slot-4 dodged SOS between two scans on 2026-07-03)."""
    env = _Env()
    try:
        s1 = env.make_slot("alpha", live=True)
        s2 = env.make_slot("alpha", live=True)  # same snapshot copy → same family
        state = cus.load_state()
        dbs = cus.double_booked_live_accounts(state)
        assert len(dbs) == 1 and dbs[0]["account"] == "alpha"
        assert dbs[0]["phase"] == "will_clobber"
        assert dbs[0]["mounts"] == sorted([s1, s2])

        # One mount's token rotates → families diverge → still flagged.
        (cus.slot_path(s2) / ".credentials.json").write_text(json.dumps(_creds("rt-alpha-rotated")))
        dbs = cus.double_booked_live_accounts(state)
        assert len(dbs) == 1 and dbs[0]["phase"] == "already_rotated"

        # A single live mount per account is fine.
        env.live_slots.discard(s2)
        cus._OCCUPIED_SLOTS_CACHE.clear()
        assert cus.double_booked_live_accounts(state) == []
    finally:
        env.restore()


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn()
        print(f"  ✓ {fn.__name__}")
    print(f"{len(fns)} passed")
