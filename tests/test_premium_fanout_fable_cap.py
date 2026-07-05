"""Regression tests: a PREMIUM lane must never be placed on a Fable-capped
account via the ladder / fan-out DEGRADED fallback — it must HOLD instead.

Incident (live 2026-07-05, from decisions.jsonl): rayi4 hit its 5-hour LADDER
step with three premium Fable lanes stacked on it. The daemon fanned all three
off at once. The first two lanes took the clean Fable targets; the third
(slot-8, premium pool) had no clean target left and fell to pick_swap_target's
aggregate `cap_fallback` — which, because the per-model Fable gate was folded
INTO that same SOFT filter, re-admitted rayi2 (per-model Fable = 100%, capped)
and landed the premium lane on it. A premium lane's whole purpose is to stay on
a Fable-clean account, so this broke its Fable work.

Why PR #139's hard-cap anti-pingpong HOLD didn't catch it: that guard only fires
when the FORCING gate is a HARD CAP. Here the forcing gate was the 5h LADDER
(Trigger 2), so #139 never ran, and the degraded fallback picked a Fable-capped
account.

The invariant these pin: for a premium lane (a pool that honors the per-model
weekly gate), pick_swap_target NEVER selects a target whose per-model Fable is
at/over the cap — INCLUDING in the ladder/fan-out degraded fallback. If the only
remaining targets are Fable-capped, the lane HOLDS on its current account (it can
move next cycle when a clean target frees, e.g. after a 5h reset). This is the
per-model analog of #139's hard-cap HOLD, applied to the ladder/fan-out path.
Standard-pool behavior is unchanged (standard ignores the Fable gate by design),
and the stale-reading guard (GH #161) is preserved — a stale/unknown per-model
reads 0.0 and stays eligible; only a FRESH at/over-cap reading excludes.

Run standalone:  python3 tests/test_premium_fanout_fable_cap.py
Or under pytest: pytest tests/test_premium_fanout_fable_cap.py
"""

from __future__ import annotations

import sys
from pathlib import Path

# `import cus` (parent) and the shared _Env harness (this dir), so the file
# runs both under pytest and standalone.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import cus  # noqa: E402
from test_per_session_cycle import _Env, _pool_config, _usage_pm  # noqa: E402


# --------------------------------------------------------------------------
# Direct pick_swap_target invariant (the source of both the primary pick and
# the fan-out re-pick). Two-account shims: {active (leaver), candidate}, so the
# only account the picker can return is the candidate.
# --------------------------------------------------------------------------

# Gate-on (premium) config. Fable target-side bar 97: an arrival at/over 97 is
# excluded. Aggregate hard 7d cap 80. Hysteresis/growth off so we probe the
# picker's cap logic, not wall-clock state.
CFG_PREMIUM = {
    "strategy": "smart",
    "thresholds": {"five_hour": True, "seven_day": True, "steps": [90, 96]},
    "smart_strategy": {
        "five_hour_headroom_weight": 0.6, "seven_day_headroom_weight": 0.4,
        "hard_7d_cap_pct": 80, "allow_rate_limited_targets": True,
    },
    "swap_hysteresis": {"enabled": False},
    "usage_growth_gate": {"enabled": False},
    "per_model_weekly": {"gate_enabled": True, "models": ["fable"], "cap_pct": 97},
}
# Standard view of the same install: _config_for_pool forces the gate off.
CFG_STANDARD = cus._config_for_pool(CFG_PREMIUM, "standard")


def _a(h5, h7, nxt=90, model=None, **flags):
    acct = {"current_5h_pct": h5, "current_7d_pct": h7,
            "next_swap_at_pct": nxt, "last_swap_ts": None}
    if model is not None:
        acct["per_model_weekly_pct"] = model
    acct.update(flags)
    return acct


def _pick(active, accts, cfg):
    return cus.pick_swap_target({"active": active, "accounts": accts}, cfg)


def test_premium_holds_when_only_target_is_fable_capped():
    """Premium: leaver (rayi4) vs a single reachable target (rayi2) at Fable
    100% (>= 97 cap), aggregate low. The picker must HOLD (return None) rather
    than degrade onto the Fable-dead account. Pre-fix this returned rayi2 via
    cap_fallback — the exact live mis-placement."""
    accts = {"rayi4": _a(85, 20, model={"fable": 30.0}),
             "rayi2": _a(10, 20, model={"fable": 100.0})}
    tgt = _pick("rayi4", accts, CFG_PREMIUM)
    assert tgt is None, f"expected HOLD, got target->{tgt and tgt.name} ({tgt and tgt.reason})"


def test_premium_moves_onto_clean_fable_target():
    """Premium: a Fable-clean target (merkos, Fable 30% < 97) is available →
    the picker returns it normally (proves the HOLD blocks only the no-clean
    case, not a genuine escape)."""
    accts = {"rayi4": _a(85, 20, model={"fable": 30.0}),
             "merkos": _a(10, 20, model={"fable": 30.0})}
    tgt = _pick("rayi4", accts, CFG_PREMIUM)
    assert tgt is not None and tgt.name == "merkos", f"expected merkos, got {tgt and tgt.name}"
    assert "[DEGRADED:" not in tgt.reason, f"clean target must not be degraded: {tgt.reason}"


def test_standard_still_lands_on_fable_capped_target():
    """Standard: the SAME Fable-capped-only situation. The gate is off for the
    standard pool (GH #99), so rayi2's Fable=100% is ignored and it remains a
    valid target. Unchanged behavior — standard serves aggregate headroom."""
    accts = {"rayi4": _a(85, 20, model={"fable": 30.0}),
             "rayi2": _a(10, 20, model={"fable": 100.0})}
    tgt = _pick("rayi4", accts, CFG_STANDARD)
    assert tgt is not None and tgt.name == "rayi2", f"standard must still pick rayi2, got {tgt and tgt.name}"


def test_premium_stale_per_model_target_still_eligible():
    """Stale-guard preserved (GH #161): rayi2 shows a CACHED Fable 100% but is
    token_stale, so _max_model_weekly_from_acct reads it as 0.0 (unknown, not a
    hard cap). The picker must still pick it — we never refuse a target on a
    number we couldn't reconfirm. Only a FRESH at/over-cap reading excludes."""
    accts = {"rayi4": _a(85, 20, model={"fable": 30.0}),
             "rayi2": _a(10, 20, model={"fable": 100.0}, token_stale=True)}
    tgt = _pick("rayi4", accts, CFG_PREMIUM)
    assert tgt is not None and tgt.name == "rayi2", (
        f"stale per-model must stay eligible, got {tgt and tgt.name}")


# --------------------------------------------------------------------------
# End-to-end fan-out (decide_slot_swaps): the exact rayi4 incident shape.
# Uses the shared _Env harness (/proc-occupancy stub + temp trees).
# --------------------------------------------------------------------------

def _fanout_config():
    """Gate-on per_session config: Fable capped at 97, aggregate hard cap 80,
    smart strategy. Timing/growth/defer/lazy gates already off in _pool_config,
    so the decision reflects the ladder + picker, not wall-clock state."""
    return _pool_config()


def test_fanout_premium_third_lane_holds_off_fable_capped():
    """(a) THE INCIDENT: three premium lanes on a hot account (alpha, 5h=85% →
    ladder tripped). Two clean Fable targets (beta, gamma) + one Fable-capped
    (delta, Fable=100%). Fan-out places the first two on the clean targets; the
    third has only delta left and must HOLD — NOT land on the Fable-capped
    delta. Pre-fix the third lane fanned onto delta via the degraded fallback."""
    env = _Env()
    try:
        s1 = env.make_slot("alpha", live=True)
        s2 = env.make_slot("alpha", live=True)
        s3 = env.make_slot("alpha", live=True)  # three premium lanes on one hot account
        state = cus.load_state()
        # alpha hot on 5h (ladder), Fable clean; beta/gamma clean; delta Fable-capped.
        state["accounts"]["alpha"].update({"current_5h_pct": 85.0, "current_7d_pct": 20.0,
                                           "per_model_weekly_pct": {"Fable": 30.0}})
        state["accounts"]["beta"].update({"current_5h_pct": 5.0, "current_7d_pct": 10.0,
                                          "per_model_weekly_pct": {"Fable": 20.0}})
        state["accounts"]["gamma"].update({"current_5h_pct": 8.0, "current_7d_pct": 12.0,
                                           "per_model_weekly_pct": {"Fable": 25.0}})
        state["accounts"]["delta"].update({"current_5h_pct": 6.0, "current_7d_pct": 11.0,
                                           "per_model_weekly_pct": {"Fable": 100.0}})
        cus._OCCUPIED_SLOTS_CACHE.clear()
        usage = {
            "alpha": _usage_pm(85.0, 20.0, fable=30.0),
            "beta": _usage_pm(5.0, 10.0, fable=20.0),
            "gamma": _usage_pm(8.0, 12.0, fable=25.0),
            "delta": _usage_pm(6.0, 11.0, fable=100.0),
        }
        moves = cus.decide_slot_swaps(state, _fanout_config(), usage)
        targets = {m["to"] for m in moves}
        moved_slots = {m["slot"] for m in moves}
        assert len(moves) == 2, f"exactly two lanes move (two clean targets), got {moves}"
        assert targets == {"beta", "gamma"}, f"must take the clean Fable targets, got {targets}"
        assert "delta" not in targets, "premium lane must NOT land on the Fable-capped delta"
        # The third lane holds (whichever slot did not get a clean target).
        held = {s1, s2, s3} - moved_slots
        assert len(held) == 1, f"exactly one premium lane HOLDS, held={held}"
        assert all(m["pool"] == "premium" for m in moves)
    finally:
        env.restore()


def test_fanout_premium_moves_when_clean_target_available():
    """(b) Control: two premium lanes, two clean Fable targets (beta, gamma) →
    BOTH move normally, neither touches the Fable-capped delta. No spurious
    HOLD when clean capacity exists."""
    env = _Env()
    try:
        env.make_slot("alpha", live=True)
        env.make_slot("alpha", live=True)
        state = cus.load_state()
        state["accounts"]["alpha"].update({"current_5h_pct": 85.0, "current_7d_pct": 20.0,
                                           "per_model_weekly_pct": {"Fable": 30.0}})
        state["accounts"]["beta"].update({"current_5h_pct": 5.0, "current_7d_pct": 10.0,
                                          "per_model_weekly_pct": {"Fable": 20.0}})
        state["accounts"]["gamma"].update({"current_5h_pct": 8.0, "current_7d_pct": 12.0,
                                           "per_model_weekly_pct": {"Fable": 25.0}})
        state["accounts"]["delta"].update({"current_5h_pct": 6.0, "current_7d_pct": 11.0,
                                           "per_model_weekly_pct": {"Fable": 100.0}})
        cus._OCCUPIED_SLOTS_CACHE.clear()
        usage = {
            "alpha": _usage_pm(85.0, 20.0, fable=30.0),
            "beta": _usage_pm(5.0, 10.0, fable=20.0),
            "gamma": _usage_pm(8.0, 12.0, fable=25.0),
            "delta": _usage_pm(6.0, 11.0, fable=100.0),
        }
        moves = cus.decide_slot_swaps(state, _fanout_config(), usage)
        targets = {m["to"] for m in moves}
        assert len(moves) == 2, f"both premium lanes move, got {moves}"
        assert targets == {"beta", "gamma"}, targets
        assert "delta" not in targets
    finally:
        env.restore()


def test_fanout_standard_third_lane_lands_on_fable_capped():
    """(c) Standard pool, same topology: the gate is off, so the third STANDARD
    lane MAY land on the Fable-capped delta. Proves the fix is scoped to premium
    and leaves standard fan-out unchanged."""
    env = _Env()
    try:
        s1 = env.make_slot("alpha", live=True)
        s2 = env.make_slot("alpha", live=True)
        s3 = env.make_slot("alpha", live=True)
        state = cus.load_state()
        for s in (s1, s2, s3):
            state["slots"][s]["pool"] = "standard"
        state["accounts"]["alpha"].update({"current_5h_pct": 85.0, "current_7d_pct": 20.0,
                                           "per_model_weekly_pct": {"Fable": 30.0}})
        state["accounts"]["beta"].update({"current_5h_pct": 5.0, "current_7d_pct": 10.0,
                                          "per_model_weekly_pct": {"Fable": 20.0}})
        state["accounts"]["gamma"].update({"current_5h_pct": 8.0, "current_7d_pct": 12.0,
                                           "per_model_weekly_pct": {"Fable": 25.0}})
        state["accounts"]["delta"].update({"current_5h_pct": 6.0, "current_7d_pct": 11.0,
                                           "per_model_weekly_pct": {"Fable": 100.0}})
        cus._OCCUPIED_SLOTS_CACHE.clear()
        usage = {
            "alpha": _usage_pm(85.0, 20.0, fable=30.0),
            "beta": _usage_pm(5.0, 10.0, fable=20.0),
            "gamma": _usage_pm(8.0, 12.0, fable=25.0),
            "delta": _usage_pm(6.0, 11.0, fable=100.0),
        }
        moves = cus.decide_slot_swaps(state, _fanout_config(), usage)
        targets = {m["to"] for m in moves}
        assert len(moves) == 3, f"all three standard lanes move (gate off), got {moves}"
        assert targets == {"beta", "gamma", "delta"}, targets
        assert all(m["pool"] == "standard" for m in moves)
    finally:
        env.restore()


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    failed = 0
    for t in tests:
        try:
            t()
            print(f"PASS  {t.__name__}")
        except AssertionError as e:
            failed += 1
            print(f"FAIL  {t.__name__}: {e}")
    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    sys.exit(1 if failed else 0)
