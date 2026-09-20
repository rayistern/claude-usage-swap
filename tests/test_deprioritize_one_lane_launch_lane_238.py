"""GH #238 — the 2026-09-18 incidents (slot-3 walled four accounts in a row):

  A. `cus deprioritize` / `cus reprioritize`: a pane or lane that can WAIT for
     its window to reset. The daemon never moves it off a wall (ladder or
     reactive-429), never counts it as needing a target, and its SOS is
     downgraded to INFO the way `cus disable` does for an account. A
     deprioritized pane sharing a lane with normal panes is a MIXED-PRIORITY
     conflict: surfaced everywhere, resolved nowhere (neither behaviour).
  C. One lane per account per daemon cycle: slot-10/12/13 all landed on rayi6
     the moment its families appeared. Now the 2nd/3rd HOLD until the next
     cycle has re-polled what the first move cost.
  D. `cus launch --lane`: (1) recognise a lane that already holds its OWN
     claimed family for the account (`cus slot move` leases one) instead of
     refusing with #104; (2) `--dry-run` runs every refusal check and writes
     nothing, so the split can pre-flight BEFORE killing the pane's old claude
     (three panes stranded at a bare shell on 2026-09-18/19).

Run standalone:  python3 tests/test_deprioritize_one_lane_launch_lane_238.py
Run under pytest: pytest tests/test_deprioritize_one_lane_launch_lane_238.py
"""
from __future__ import annotations

import sys
from pathlib import Path

import click
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import cus  # noqa: E402
from click.testing import CliRunner  # noqa: E402
from test_login_pool import _Env, _usage, _reactive, _reactive_cfg  # noqa: E402
from test_lane_clustering_spread import _cfg  # noqa: E402


def _stub_panes(panes):
    """Replace the live tmux scan with a fixed [(name, %id, slot)] list."""
    orig = cus._live_pane_slots
    cus._live_pane_slots = lambda tmux_socket=None: list(panes)
    return orig


# --------------------------------------------------------------------------
# A. the view: effective slots and the mixed-priority conflict (pure)
# --------------------------------------------------------------------------

def test_view_is_empty_and_makes_no_tmux_call_when_nothing_is_configured():
    orig = _stub_panes([("boom", "%1", "slot-1")])
    try:
        cus._live_pane_slots = lambda tmux_socket=None: (_ for _ in ()).throw(AssertionError("tmux scanned"))
        v = cus.deprioritized_view({}, {})
        assert v["slots"] == set() and v["conflicts"] == {} and v["panes"] == {}
    finally:
        cus._live_pane_slots = orig


def test_view_pane_alone_on_its_lane_deprioritizes_the_lane_but_a_shared_lane_is_a_conflict():
    cfg = {"session_locks": {"deprioritized_panes": ["2good1a"]}}
    live = [("2good1a", "%20", "slot-3"), ("4mainsite1a", "%10", "slot-3"),
            ("8jira2a", "%6", "slot-3"), ("cus1a", "%2", "slot-9")]
    v = cus.deprioritized_view(cfg, {}, live_panes=live)
    assert "slot-3" not in v["slots"]                       # NOT silently deprioritized
    assert "slot-3" in v["conflicts"]
    msg = v["conflicts"]["slot-3"]
    assert "2good1a is deprioritized" in msg and "2 normal pane(s)" in msg and "split it out" in msg
    assert v["panes"]["%20"] == ("2good1a", True, "slot-3")
    # alone on its lane => the lane is effectively deprioritized, no conflict
    v2 = cus.deprioritized_view(cfg, {}, live_panes=[("2good1a", "%20", "slot-3"), ("cus1a", "%2", "slot-9")])
    assert v2["slots"] == {"slot-3"} and v2["conflicts"] == {}
    # a %pane id works as the name; an explicit slot is a whole-lane choice, never a conflict
    v3 = cus.deprioritized_view({"session_locks": {"deprioritized_panes": ["%20"]}}, {}, live_panes=live)
    assert "slot-3" in v3["conflicts"]
    v4 = cus.deprioritized_view({"session_locks": {"deprioritized_slots": ["slot-3"]}}, {}, live_panes=live)
    assert v4["slots"] == {"slot-3"} and v4["conflicts"] == {}


# --------------------------------------------------------------------------
# A. commands persist the flag; unknown-init refuses
# --------------------------------------------------------------------------

def test_deprioritize_and_reprioritize_persist_slot_and_pane_flags():
    env = _Env(accounts=("alpha", "beta"))
    env.set_config({})
    orig = _stub_panes([("2good1a", "%20", "slot-3"), ("other", "%21", "slot-3")])
    try:
        r = CliRunner().invoke(cus.cli, ["deprioritize", "3"])
        assert r.exit_code == 0, r.output
        assert cus.read_yaml(cus.CONFIG_YAML)["session_locks"]["deprioritized_slots"] == ["slot-3"]
        r = CliRunner().invoke(cus.cli, ["deprioritize", "slot-3"])
        assert "already deprioritized" in r.output
        r = CliRunner().invoke(cus.cli, ["reprioritize", "slot-3"])
        assert r.exit_code == 0 and "Reprioritized slot-3" in r.output, r.output
        assert cus.read_yaml(cus.CONFIG_YAML)["session_locks"]["deprioritized_slots"] == []
        # a pane: persisted by name, its live lane named, and the conflict said NOW
        r = CliRunner().invoke(cus.cli, ["deprioritize", "2good1a"])
        assert r.exit_code == 0, r.output
        assert cus.read_yaml(cus.CONFIG_YAML)["session_locks"]["deprioritized_panes"] == ["2good1a"]
        assert "currently runs in slot-3" in r.output and "MIXED PRIORITY" in r.output
        r = CliRunner().invoke(cus.cli, ["reprioritize", "2good1a"])
        assert cus.read_yaml(cus.CONFIG_YAML)["session_locks"]["deprioritized_panes"] == []
        r = CliRunner().invoke(cus.cli, ["reprioritize", "2good1a"])
        assert "was not deprioritized" in r.output
    finally:
        cus._live_pane_slots = orig
        env.restore()


# --------------------------------------------------------------------------
# A. the daemon never moves a deprioritized lane, and picks NEITHER behaviour
#    for a mixed lane
# --------------------------------------------------------------------------

def _hot_alpha_clean_beta(env):
    state = cus.load_state()
    state["accounts"]["alpha"].update({"current_5h_pct": 96.0, "current_7d_pct": 20.0, "next_swap_at_pct": 95})
    state["accounts"]["beta"].update({"current_5h_pct": 5.0, "current_7d_pct": 10.0})
    cus.save_state(state)
    return cus.load_state(), {"alpha": _usage(96.0, 20.0), "beta": _usage(5.0, 10.0)}


def test_ladder_never_moves_a_deprioritized_lane_off_its_wall():
    """2good1a's lane at 96% with a clean beta available: a normal lane moves;
    the deprioritized one stays and the log says why."""
    env = _Env(accounts=("alpha", "beta"))
    try:
        env.set_config({})
        s1 = env.make_slot("alpha", live=True)
        state, usage = _hot_alpha_clean_beta(env)
        control = cus.decide_slot_swaps(state, _cfg(), usage, exclude_accounts={"alpha"})
        assert [m["slot"] for m in control] == [s1], control
        cfg = cus.deep_merge(_cfg(), {"session_locks": {"deprioritized_slots": [s1]}})
        assert cus.decide_slot_swaps(state, cfg, usage, exclude_accounts={"alpha"}) == []
        # by PANE name, alone on its lane => same thing
        orig = _stub_panes([("2good1a", "%20", s1)])
        try:
            cfg = cus.deep_merge(_cfg(), {"session_locks": {"deprioritized_panes": ["2good1a"]}})
            assert cus.decide_slot_swaps(state, cfg, usage, exclude_accounts={"alpha"}) == []
            # MIXED: 2good1a shares s1 with a normal pane => NEITHER: no move
            cus._live_pane_slots = lambda tmux_socket=None: [("2good1a", "%20", s1), ("4mainsite1a", "%10", s1)]
            assert cus.decide_slot_swaps(state, cfg, usage, exclude_accounts={"alpha"}) == []
        finally:
            cus._live_pane_slots = orig
    finally:
        env.restore()


def test_reactive_429_never_moves_a_deprioritized_lane():
    env = _Env(accounts=("alpha", "beta"))
    try:
        env.set_config({})
        s1 = env.make_slot("alpha", live=True)
        state = cus.load_state()
        state["accounts"]["alpha"].update({"current_5h_pct": 95.0, "current_7d_pct": 30.0})
        state["accounts"]["beta"].update({"current_5h_pct": 5.0, "current_7d_pct": 10.0})
        cus.save_state(state)
        state = cus.load_state()
        assert len(_reactive(env, state, _reactive_cfg(False), {"sA": s1}, set())) == 1   # control
        cfg = cus.deep_merge(_reactive_cfg(False), {"session_locks": {"deprioritized_slots": [s1]}})
        assert _reactive(env, state, cfg, {"sA": s1}, set()) == []
    finally:
        env.restore()


# --------------------------------------------------------------------------
# A. SOS: URGENT for a deprioritized lane becomes INFO; a mixed lane WARNs
# --------------------------------------------------------------------------

def test_sos_downgrades_a_deprioritized_lane_to_info_and_warns_on_a_mixed_lane():
    env = _Env(accounts=("alpha", "beta"))
    try:
        env.set_config({})
        s1 = env.make_slot("alpha", live=True)
        state = cus.load_state()
        state["accounts"]["alpha"].update({"current_5h_pct": 100.0, "current_7d_pct": 30.0, "next_swap_at_pct": 95})
        state["accounts"]["beta"].update({"current_5h_pct": 100.0, "current_7d_pct": 100.0, "next_swap_at_pct": 95})
        cus.save_state(state)
        state = cus.load_state()
        base = cus.deep_merge(cus.DEFAULT_CONFIG, {"mode": "per_session"})
        loud = [c for c in cus.diagnose(state, base)
                if s1 in c.summary and c.severity in ("urgent", "warning")]
        assert loud, "control: the walled lane with no target must be loud before deprioritizing"
        cfg = cus.deep_merge(base, {"session_locks": {"deprioritized_slots": [s1]}})
        after = cus.diagnose(state, cfg)
        assert not [c for c in after if s1 in c.summary and c.severity in ("urgent", "warning")], after
        info = [c for c in after if s1 in c.summary and c.severity == "info"]
        assert info and "deprioritized lane" in info[0].summary and "reprioritize" in info[0].action
        # mixed lane: a WARNING of its own that names the split
        orig = _stub_panes([("2good1a", "%20", s1), ("4mainsite1a", "%10", s1)])
        try:
            cfg = cus.deep_merge(base, {"session_locks": {"deprioritized_panes": ["2good1a"]}})
            mixed = [c for c in cus.diagnose(state, cfg) if "MIXED PRIORITY" in c.summary]
            assert mixed and mixed[0].severity == "warning" and "split it out" in mixed[0].action
            assert "NEITHER" in mixed[0].action
        finally:
            cus._live_pane_slots = orig
    finally:
        env.restore()


# --------------------------------------------------------------------------
# A. the flag shows in `cus panes` (rows, table, --me) and `cus sessions` rows
# --------------------------------------------------------------------------

def test_panes_rows_carry_the_flag_and_the_conflict():
    rows = [{"pane": "%20", "tmux_session": "2good1a", "slot": "slot-3"},
            {"pane": "%10", "tmux_session": "4mainsite1a", "slot": "slot-3"},
            {"pane": "%2", "tmux_session": "cus1a", "slot": "slot-9"}]
    cus.apply_deprioritization_to_pane_rows(rows, {})
    assert all(r["deprioritized"] is False and r["deprioritized_conflict"] is None for r in rows)
    cus.apply_deprioritization_to_pane_rows(rows, {"session_locks": {"deprioritized_panes": ["2good1a"]}})
    assert rows[0]["deprioritized"] is True and rows[1]["deprioritized"] is False
    assert "split it out" in rows[0]["deprioritized_conflict"] and rows[1]["deprioritized_conflict"] == rows[0]["deprioritized_conflict"]
    assert rows[2]["deprioritized_conflict"] is None
    cus.apply_deprioritization_to_pane_rows(rows, {"session_locks": {"deprioritized_slots": ["slot-9"]}})
    assert rows[2]["deprioritized"] is True and rows[2]["deprioritized_conflict"] is None


def test_panes_table_and_me_print_the_flag(monkeypatch, tmp_path):
    from test_panes_view import _pane_on, _pm_state, FABLE_HEAVY, _payload  # noqa: E402
    row, head = _pane_on(monkeypatch, tmp_path, "s-dep", FABLE_HEAVY, _pm_state(10.0))
    row["deprioritized"], row["deprioritized_conflict"] = True, None
    table = cus.render_panes_table(_payload([row], {"rayi5": head}))
    assert "└─ deprioritized: wall-and-wait" in table
    me = cus.render_me(_payload([row], {"rayi5": head}), row)
    assert "DEPRIORITIZED (wall-and-wait" in me
    row["deprioritized_conflict"] = "slot-7: x is deprioritized but shares the lane with 1 normal pane(s) (y); split it out"
    assert "MIXED PRIORITY: slot-7" in cus.render_panes_table(_payload([row], {"rayi5": head}))
    assert "MIXED PRIORITY: slot-7" in cus.render_me(_payload([row], {"rayi5": head}), row)


# --------------------------------------------------------------------------
# C. one lane per account per cycle (ladder and reactive)
# --------------------------------------------------------------------------

def test_two_walled_lanes_one_target_only_one_lands_per_cycle():
    """The rayi6 pile-up: two hot lanes, one clean account with TWO free
    families. Pre-fix both landed in one cycle; now one lands, one holds."""
    env = _Env(accounts=("alpha", "beta", "gamma"))
    try:
        env.set_config({})
        s1 = env.make_slot("alpha", live=True)
        s2 = env.make_slot("alpha", live=True)
        env.make_slot("beta", live=True)
        env.make_slot("gamma", live=True)
        env.plant_family("beta", "family-1", "rt-b1")
        env.plant_family("beta", "family-2", "rt-b2")
        state = cus.load_state()
        state["accounts"]["alpha"].update({"current_5h_pct": 96.0, "current_7d_pct": 30.0, "next_swap_at_pct": 95})
        state["accounts"]["beta"].update({"current_5h_pct": 5.0, "current_7d_pct": 10.0, "next_swap_at_pct": 95})
        state["accounts"]["gamma"].update({"current_5h_pct": 100.0, "current_7d_pct": 100.0, "next_swap_at_pct": 95})
        cus.save_state(state)
        state = cus.load_state()
        usage = {"alpha": _usage(96.0, 30.0), "beta": _usage(5.0, 10.0), "gamma": _usage(100.0, 100.0)}
        moves = cus.decide_slot_swaps(state, _cfg(spread=True), usage, exclude_accounts={"alpha", "beta", "gamma"})
        assert len(moves) == 1 and moves[0]["to"] == "beta" and moves[0]["slot"] in {s1, s2}, moves
        # the lever off => the old stacking (pinned by test_lane_clustering_spread)
        off = _cfg(spread=True, **{"spread_lanes": {"one_lane_per_account_per_cycle": False}})
        assert len(cus.decide_slot_swaps(state, off, usage, exclude_accounts={"alpha", "beta", "gamma"})) == 2
    finally:
        env.restore()


def test_two_429s_in_one_cycle_land_one_lane_on_the_target():
    env = _Env(accounts=("alpha", "beta"))
    try:
        env.set_config({"independent_logins": {"use_independent_logins": True}})
        s1 = env.make_slot("alpha", live=True)
        s2 = env.make_slot("alpha", live=True)
        env.make_slot("beta", live=True)
        env.plant_family("beta", "family-1", "rt-b1")
        env.plant_family("beta", "family-2", "rt-b2")
        state = cus.load_state()
        state["accounts"]["alpha"].update({"current_5h_pct": 95.0, "current_7d_pct": 30.0})
        state["accounts"]["beta"].update({"current_5h_pct": 5.0, "current_7d_pct": 10.0})
        cus.save_state(state)
        state = cus.load_state()
        moves = _reactive(env, state, _reactive_cfg(True), {"sA": s1, "sB": s2}, {"beta"})
        assert len(moves) == 1 and moves[0]["to"] == "beta", moves
        off = cus.deep_merge(_reactive_cfg(True), {"spread_lanes": {"one_lane_per_account_per_cycle": False}})
        assert len(_reactive(env, state, off, {"sA": s1, "sB": s2}, {"beta"})) == 2
    finally:
        env.restore()


# --------------------------------------------------------------------------
# D. launch --lane: a lane's own claimed family is accepted; --dry-run
#    refuses before any destructive step
# --------------------------------------------------------------------------

def test_launch_lane_accepts_a_lane_that_already_holds_its_own_claimed_family():
    """2026-09-18 z-email-opus split: `cus slot move slot-7 rayi6` leased
    rayi6/family-N to slot-7; `cus launch rayi6 --lane slot-7` then refused
    with #104 because rayi6 was live on another lane. It must accept."""
    env = _Env(accounts=("alpha", "beta"))
    try:
        env.set_config({"independent_logins": {"use_independent_logins": True}})
        env.make_slot("alpha", live=True)                                   # alpha live elsewhere
        lane = env.make_slot("alpha", live=False, family_id="family-1")      # the moved-to lane, idle
        env.plant_family("alpha", "family-1", "rt-a1")
        state, config = cus.load_state(), cus.load_config()
        got = cus._launch_prepare("alpha", state, config, lane=lane, dry_run=True)
        assert got == (lane, cus.slot_path(lane), "alpha"), got
        # controls: gate off => still #104; leased for ANOTHER account => still #104
        with pytest.raises(click.ClickException) as ei:
            cus._launch_prepare("alpha", state, cus.deep_merge(config, {"independent_logins": {"use_independent_logins": False}}),
                                lane=lane, dry_run=True)
        assert "GH #104" in str(ei.value)
        st2 = cus.load_state(); st2["slots"][lane]["login_family"] = "beta/family-1"; cus.save_state(st2)
        with pytest.raises(click.ClickException):
            cus._launch_prepare("alpha", cus.load_state(), config, lane=lane, dry_run=True)
    finally:
        env.restore()


def test_launch_dry_run_refuses_before_any_write_and_succeeds_without_writing():
    env = _Env(accounts=("alpha", "beta"))
    try:
        env.set_config({})
        env.make_slot("alpha", live=True)
        state, config = cus.load_state(), cus.load_config()
        before = cus.load_state()
        with pytest.raises(click.ClickException) as ei:
            cus._launch_prepare("alpha", state, config, lane="slot-9", dry_run=True)
        assert "GH #104" in str(ei.value)
        assert not cus.slot_path("slot-9").exists() and "slot-9" not in cus.load_state()["slots"]
        assert cus.load_state() == before
        # a lane that would be accepted: still nothing written in dry-run
        got = cus._launch_prepare("beta", state, config, lane="slot-9", dry_run=True)
        assert got == ("slot-9", cus.slot_path("slot-9"), "beta")
        assert not cus.slot_path("slot-9").exists() and "slot-9" not in cus.load_state()["slots"]
        # auto pick: the account is decided, no slot reserved
        slot_name, _d, acct = cus._launch_prepare(None, cus.load_state(), config, dry_run=True)
        assert slot_name == "(auto)" and acct == "beta"
        # (the auto pick's fresh-reading verification records a poll timestamp —
        # a reading, not a lane write; what must not happen is a reservation)
        assert cus.load_state()["slots"] == before["slots"]
    finally:
        env.restore()


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
