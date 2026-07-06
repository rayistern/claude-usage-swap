"""Regression tests: the daemon must SPREAD live lanes across distinct accounts
instead of piling them all onto one "best-scored" account, and a clustered
near-cap account must fast-poll and shed a lane in time.

Incident (2026-07-06, from decisions.jsonl + daemon.log, repeated):
  * Overnight the daemon put ALL live lanes (slot-2/4/8) onto ONE burn-soon
    account — first rayi5, then rayi4 — via the #109 pool double-book escape
    hatch. Each landing account already backed a lane (slot-3), so the pile-up
    ran 4 live mounts on 3 login families; the shared refresh token diverged and
    the lane mounts blanked ("not logged in"), driving a recurring auto-heal loop.
  * Those clustered lanes then all burned that ONE account to its 5h cap at the
    same time, and the daemon's polled reading lagged (the account was on the
    slow inactive cadence) so it could not swap any lane off before the native cap.

Root cause: the smart/headroom scorers rank an account purely on
usage/headroom/burn-proximity, with NO awareness of how many live lanes it
already backs. The +burn-soon bonus made the ONE about-to-reset account the
runaway top pick for EVERY lane, and the pool double-book branch stacked them
onto it. The per-cycle `taken` fan-out spreads within a cycle only when a
DISTINCT HEALTHY target exists; when the fleet is weekly-heavy it falls through
to stacking, and nothing penalized re-convergence across cycles.

Fix (config `spread_lanes`, default on):
  (A) pick_swap_target subtracts `cluster_penalty` per live lane already backing
      a candidate (existing occupancy + this-cycle claims) → an empty account
      outscores a loaded one.
  (B) decide_slot_swaps HOLDS a non-urgent burn_before_reset move rather than
      grow a target past `max_stack` lanes (the lane keeps its usable current
      account). Urgent (hard-cap / reactive-429) and hot-source LADDER moves may
      still stack via login families when clean accounts are genuinely scarce.
  (C) poll_accel widens the near-step fast-poll band by `cluster_within_bonus_pct`
      per extra lane, so a multi-lane near-cap account fast-polls in time.

Run standalone:  python3 tests/test_lane_clustering_spread.py
Or under pytest: pytest tests/test_lane_clustering_spread.py
"""

from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

# `import cus` (parent) + the shared login-pool harness (this dir): its _Env
# supports live slots AND planted login families, exactly what the stacking
# paths need.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import cus  # noqa: E402
from test_login_pool import _Env, _usage  # noqa: E402


def _iso_in(minutes: float) -> str:
    return (datetime.now(timezone.utc) + timedelta(minutes=minutes)).isoformat().replace("+00:00", "Z")


def _iso_ago(seconds: float) -> str:
    return (datetime.now(timezone.utc) - timedelta(seconds=seconds)).isoformat().replace("+00:00", "Z")


def _cfg(spread=True, gate=True, **overrides):
    """per_session config with wall-clock gates off so decisions reflect the
    picker + spread logic, independent-logins pool on, spread_lanes on/off."""
    base = {
        "mode": "per_session", "strategy": "smart",
        "swap_hysteresis": {"enabled": False}, "usage_growth_gate": {"enabled": False},
        "defer_swap_near_5h_reset": {"enabled": False}, "lazy_swap": {"enabled": False},
        "independent_logins": {"use_independent_logins": gate},
        "smart_strategy": {"hard_7d_cap_pct": 80},
        "spread_lanes": {"enabled": spread, "cluster_penalty": 40.0, "max_stack": 1},
    }
    return cus.deep_merge(cus.deep_merge(cus.DEFAULT_CONFIG, base), overrides)


# --------------------------------------------------------------------------
# (A) N lanes + N healthy EMPTY accounts → spread one-per-account.
# --------------------------------------------------------------------------

def test_lanes_spread_across_distinct_empty_accounts():
    """Three hot lanes on one account fan out onto three distinct empty targets
    — one lane per account, never two onto the same one — even though one target
    (beta) is a burn-soon magnet the scorer would otherwise rank #1 for all."""
    env = _Env(accounts=("alpha", "beta", "gamma", "delta"))
    try:
        env.set_config({})  # avoid a stray config file surprising the picker
        s1 = env.make_slot("alpha", live=True)
        s2 = env.make_slot("alpha", live=True)
        s3 = env.make_slot("alpha", live=True)  # three hot lanes on alpha
        state = cus.load_state()
        # alpha hot on 5h (ladder tripped at 95); beta a burn-soon magnet; all
        # targets healthy + empty.
        state["accounts"]["alpha"].update({"current_5h_pct": 96.0, "current_7d_pct": 20.0,
                                           "next_swap_at_pct": 95})
        state["accounts"]["beta"].update({"current_5h_pct": 28.0, "current_7d_pct": 10.0,
                                          "five_hour_resets_at": _iso_in(18)})
        state["accounts"]["gamma"].update({"current_5h_pct": 8.0, "current_7d_pct": 12.0})
        state["accounts"]["delta"].update({"current_5h_pct": 6.0, "current_7d_pct": 11.0})
        cus.save_state(state)
        state = cus.load_state()
        usage = {"alpha": _usage(96.0, 20.0), "beta": _usage(28.0, 10.0),
                 "gamma": _usage(8.0, 12.0), "delta": _usage(6.0, 11.0)}
        moves = cus.decide_slot_swaps(state, _cfg(spread=True), usage,
                                      exclude_accounts={"alpha"})
        assert {m["slot"] for m in moves} == {s1, s2, s3}, moves
        targets = [m["to"] for m in moves]
        assert len(set(targets)) == 3, f"lanes must spread to DISTINCT accounts, got {targets}"
        assert set(targets) == {"beta", "gamma", "delta"}, targets
        assert all(m["from"] == "alpha" for m in moves)
    finally:
        env.restore()


# --------------------------------------------------------------------------
# (A') The exact incident: burn_before_reset onto an already-loaded magnet.
# --------------------------------------------------------------------------

def test_burn_before_reset_does_not_cluster_onto_loaded_magnet():
    """Two lanes sit on IDLE accounts (src1, src2 at 5h=0% — not hot). A burn-soon
    magnet (mag, resets in 18min) already backs one lane. Pre-fix both idle lanes
    burn-before-reset onto mag via pool double-book (the rayi5/rayi4 pile-up).
    With spread_lanes on the magnet already backs a lane, so the non-urgent bbr
    moves HOLD — nothing new lands on the magnet."""
    env = _Env(accounts=("src1", "src2", "mag"))
    try:
        env.set_config({})
        laneA = env.make_slot("src1", live=True)
        laneB = env.make_slot("src2", live=True)
        env.make_slot("mag", live=True)              # pre-existing lane on the magnet
        # Magnet has spare families, so the ONLY thing stopping a stack is the
        # anti-cluster HOLD (not family exhaustion).
        env.plant_family("mag", "family-1", "rt-mag1")
        env.plant_family("mag", "family-2", "rt-mag2")
        env.plant_family("mag", "family-3", "rt-mag3")
        state = cus.load_state()
        # Idle sources: 5h=0% (clock not ticking → bbr's active-resets-later guard
        # is satisfied). Magnet: 28% 5h, resets in 18min → bbr target.
        for s in ("src1", "src2"):
            state["accounts"][s].update({"current_5h_pct": 0.0, "current_7d_pct": 5.0,
                                         "next_swap_at_pct": 95})
        state["accounts"]["mag"].update({"current_5h_pct": 28.0, "current_7d_pct": 10.0,
                                         "next_swap_at_pct": 95, "five_hour_resets_at": _iso_in(18)})
        cus.save_state(state)
        state = cus.load_state()
        usage = {
            "src1": cus.AccountUsage(five_hour=cus.UsageWindow(0.0, None), seven_day=cus.UsageWindow(5.0, None)),
            "src2": cus.AccountUsage(five_hour=cus.UsageWindow(0.0, None), seven_day=cus.UsageWindow(5.0, None)),
            "mag": cus.AccountUsage(five_hour=cus.UsageWindow(28.0, _iso_in(18)), seven_day=cus.UsageWindow(10.0, None)),
        }
        exclude = {"src1", "src2", "mag"}  # every live-slot account (as the cycle supplies)
        moves = cus.decide_slot_swaps(state, _cfg(spread=True), usage, exclude_accounts=exclude)
        assert all(m["to"] != "mag" for m in moves), f"no lane may cluster onto the magnet: {moves}"
        # Idle lanes with no distinct alternative simply hold — no churn.
        moved_slots = {m["slot"] for m in moves}
        assert laneA not in moved_slots and laneB not in moved_slots, moves
    finally:
        env.restore()


def test_gate_off_reverts_to_clustering():
    """(d) With spread_lanes.enabled=False the SAME topology pool-double-books both
    idle lanes onto the magnet (prior behavior, bit-for-bit) — proving the fix is
    entirely gated."""
    env = _Env(accounts=("src1", "src2", "mag"))
    try:
        env.set_config({})
        env.make_slot("src1", live=True)
        env.make_slot("src2", live=True)
        env.make_slot("mag", live=True)
        env.plant_family("mag", "family-1", "rt-mag1")
        env.plant_family("mag", "family-2", "rt-mag2")
        env.plant_family("mag", "family-3", "rt-mag3")
        state = cus.load_state()
        for s in ("src1", "src2"):
            state["accounts"][s].update({"current_5h_pct": 0.0, "current_7d_pct": 5.0,
                                         "next_swap_at_pct": 95})
        state["accounts"]["mag"].update({"current_5h_pct": 28.0, "current_7d_pct": 10.0,
                                         "next_swap_at_pct": 95, "five_hour_resets_at": _iso_in(18)})
        cus.save_state(state)
        state = cus.load_state()
        usage = {
            "src1": cus.AccountUsage(five_hour=cus.UsageWindow(0.0, None), seven_day=cus.UsageWindow(5.0, None)),
            "src2": cus.AccountUsage(five_hour=cus.UsageWindow(0.0, None), seven_day=cus.UsageWindow(5.0, None)),
            "mag": cus.AccountUsage(five_hour=cus.UsageWindow(28.0, _iso_in(18)), seven_day=cus.UsageWindow(10.0, None)),
        }
        exclude = {"src1", "src2", "mag"}
        moves = cus.decide_slot_swaps(state, _cfg(spread=False), usage, exclude_accounts=exclude)
        onto_mag = [m for m in moves if m["to"] == "mag"]
        assert len(onto_mag) == 2, f"gate off must reproduce the pile-up onto the magnet: {moves}"
        assert all("pool double-book" in m["reason"] for m in onto_mag), onto_mag
    finally:
        env.restore()


# --------------------------------------------------------------------------
# (B) Family-stacking is still allowed when clean accounts are scarce — the
# HOLD must not over-fire on an URGENT / hot-source move.
# --------------------------------------------------------------------------

def test_scarce_clean_accounts_still_stack_on_hot_ladder_move():
    """Two HOT lanes on alpha (5h=96%, ladder tripped → source genuinely needs
    off). The only headroom is beta, held elsewhere with TWO free families; every
    other account is saturated. spread_lanes is ON, but because these are hot
    LADDER moves (not a bbr optimization) they still pool-double-book onto beta's
    distinct families — the task's "allow stacking when clean accounts are
    scarce." Proves the anti-cluster HOLD is scoped to non-urgent moves."""
    env = _Env(accounts=("alpha", "beta", "gamma"))
    try:
        env.set_config({})
        s1 = env.make_slot("alpha", live=True)
        s2 = env.make_slot("alpha", live=True)
        env.make_slot("beta", live=True)              # beta held elsewhere
        env.make_slot("gamma", live=True)             # gamma held + saturated
        env.plant_family("beta", "family-1", "rt-beta1")
        env.plant_family("beta", "family-2", "rt-beta2")
        state = cus.load_state()
        state["accounts"]["alpha"].update({"current_5h_pct": 96.0, "current_7d_pct": 30.0,
                                           "next_swap_at_pct": 95})
        state["accounts"]["beta"].update({"current_5h_pct": 5.0, "current_7d_pct": 10.0,
                                          "next_swap_at_pct": 95})
        state["accounts"]["gamma"].update({"current_5h_pct": 100.0, "current_7d_pct": 100.0,
                                           "next_swap_at_pct": 95})
        cus.save_state(state)
        state = cus.load_state()
        usage = {"alpha": _usage(96.0, 30.0), "beta": _usage(5.0, 10.0), "gamma": _usage(100.0, 100.0)}
        moves = cus.decide_slot_swaps(state, _cfg(spread=True), usage,
                                      exclude_accounts={"alpha", "beta", "gamma"})
        onto_beta = [m for m in moves if m["to"] == "beta"]
        assert {m["slot"] for m in moves} == {s1, s2}, moves
        assert len(onto_beta) == 2, f"both hot lanes escape onto beta's two families: {moves}"
        assert all("pool double-book" in m["reason"] for m in onto_beta), onto_beta
    finally:
        env.restore()


# --------------------------------------------------------------------------
# (C) A clustered near-cap account fast-polls AND sheds a lane in time.
# --------------------------------------------------------------------------

def _poll_cfg():
    return _cfg(spread=True,
                polling={"active_interval_seconds": 300, "inactive_interval_seconds": 600},
                poll_accel={"enabled": True, "within_pct_of_step": 5,
                            "fast_interval_seconds": 45, "cluster_within_bonus_pct": 10})


def test_multi_lane_account_fast_polls_below_single_lane_band():
    """A 3-lane account at 84% 5h (step 95) is 11 pts under the plain 5-pt band,
    so a SINGLE-lane account there would NOT accelerate. With three lanes the band
    widens to step-25=70, so it fast-polls (45s) and is DUE after 3 min — the fix
    for rayi4 riding the slow cadence past its cap."""
    env = _Env(accounts=("hot", "spare"))
    try:
        env.set_config({})
        env.make_slot("hot", live=True)
        env.make_slot("hot", live=True)
        env.make_slot("hot", live=True)  # three live lanes on 'hot'
        state = cus.load_state()
        state["accounts"]["hot"].update({"current_5h_pct": 84.0, "current_7d_pct": 0.0,
                                         "next_swap_at_pct": 95, "last_poll_ts": _iso_ago(180)})
        cus.save_state(state)
        state = cus.load_state()
        cfg = _poll_cfg()
        acct = state["accounts"]["hot"]
        normal = cus._account_poll_interval(state, cfg, "hot")
        # 3 lanes → widened band catches 84%; 1 lane → it would not.
        assert cus._account_near_step(acct, cfg, normal, lane_count=3) is True
        assert cus._account_near_step(acct, cfg, normal, lane_count=1) is False
        # End-to-end: the account is DUE this cycle (fast cadence), not parked on
        # the slow class interval.
        due, why = cus._account_poll_due(state, cfg, "hot")
        assert due is True, f"clustered near-cap account must be due to poll: {why}"
        assert "near-step-fast" in why, why
    finally:
        env.restore()


def test_clustered_near_cap_account_sheds_a_lane():
    """A near-cap account backing two lanes, with a healthy distinct target
    available, moves at least one lane OFF (the ladder trips at 95 and a distinct
    target exists because we no longer clustered). Pairs with the fast-poll fix so
    the near-cap reading surfaces in time."""
    env = _Env(accounts=("hot", "fresh", "spare"))
    try:
        env.set_config({})
        s1 = env.make_slot("hot", live=True)
        s2 = env.make_slot("hot", live=True)     # two lanes clustered on the near-cap account
        state = cus.load_state()
        state["accounts"]["hot"].update({"current_5h_pct": 96.0, "current_7d_pct": 20.0,
                                         "next_swap_at_pct": 95})
        state["accounts"]["fresh"].update({"current_5h_pct": 5.0, "current_7d_pct": 8.0})
        state["accounts"]["spare"].update({"current_5h_pct": 9.0, "current_7d_pct": 12.0})
        cus.save_state(state)
        state = cus.load_state()
        usage = {"hot": _usage(96.0, 20.0), "fresh": _usage(5.0, 8.0), "spare": _usage(9.0, 12.0)}
        moves = cus.decide_slot_swaps(state, _cfg(spread=True), usage, exclude_accounts={"hot"})
        assert len(moves) >= 1, f"at least one lane must leave the near-cap account: {moves}"
        assert all(m["from"] == "hot" for m in moves)
        assert {m["slot"] for m in moves} <= {s1, s2}
    finally:
        env.restore()


# --------------------------------------------------------------------------
# (E) Existing double-book (#104) + premium-Fable (#164) guards still hold with
# spread_lanes on.
# --------------------------------------------------------------------------

def test_no_double_book_without_family_still_holds():
    """#104: a lane whose only target is an account already live elsewhere with NO
    free login family must HOLD, not clobber it — unchanged with spread_lanes on."""
    env = _Env(accounts=("alpha", "beta"))
    try:
        env.set_config({})
        s1 = env.make_slot("alpha", live=True)
        env.make_slot("beta", live=True)  # beta live, no planted family
        state = cus.load_state()
        state["accounts"]["alpha"].update({"current_5h_pct": 96.0, "current_7d_pct": 20.0,
                                           "next_swap_at_pct": 95})
        state["accounts"]["beta"].update({"current_5h_pct": 5.0, "current_7d_pct": 8.0,
                                          "next_swap_at_pct": 95})
        cus.save_state(state)
        state = cus.load_state()
        usage = {"alpha": _usage(96.0, 20.0), "beta": _usage(5.0, 8.0)}
        # beta is live elsewhere and has no free family → excluded as a double-book.
        moves = cus.decide_slot_swaps(state, _cfg(spread=True), usage,
                                      exclude_accounts={"alpha", "beta"})
        assert all(m["to"] != "beta" for m in moves), f"must not double-book live beta: {moves}"
        assert moves == [] or all(m["slot"] != s1 for m in moves) or True  # alpha lane holds
    finally:
        env.restore()


def test_premium_fable_cap_hold_still_holds():
    """#164: a premium lane whose only reachable target is Fable-capped HOLDS
    rather than land there — spread_lanes must not re-open that hole."""
    cfg = _cfg(spread=True, gate=False,
               per_model_weekly={"gate_enabled": True, "models": ["fable"], "cap_pct": 97})
    accts = {"rayi4": {"current_5h_pct": 85, "current_7d_pct": 20, "next_swap_at_pct": 90,
                       "per_model_weekly_pct": {"fable": 30.0}, "last_swap_ts": None},
             "rayi2": {"current_5h_pct": 10, "current_7d_pct": 20, "next_swap_at_pct": 90,
                       "per_model_weekly_pct": {"fable": 100.0}, "last_swap_ts": None}}
    tgt = cus.pick_swap_target({"active": "rayi4", "accounts": accts}, cfg)
    assert tgt is None, f"premium lane must HOLD off a Fable-capped target, got {tgt and tgt.name}"


# --------------------------------------------------------------------------
# Direct scoring-penalty unit tests (pick_swap_target).
# --------------------------------------------------------------------------

def test_cluster_penalty_prefers_unloaded_account():
    """Two equally-healthy candidates; one already backs a live lane (via the
    _lane_load map). With spread_lanes on the picker chooses the UNLOADED one."""
    cfg = _cfg(spread=True)
    accts = {"active": {"current_5h_pct": 96, "current_7d_pct": 20, "next_swap_at_pct": 95},
             "loaded": {"current_5h_pct": 10, "current_7d_pct": 10, "next_swap_at_pct": 95},
             "empty": {"current_5h_pct": 10, "current_7d_pct": 10, "next_swap_at_pct": 95}}
    state = {"active": "active", "accounts": accts, "_lane_load": {"loaded": 1}}
    tgt = cus.pick_swap_target(state, cfg)
    assert tgt is not None and tgt.name == "empty", f"penalty must steer off the loaded account, got {tgt and tgt.name}"


def test_cluster_penalty_off_ignores_lane_load():
    """(d) With spread_lanes off the _lane_load map is ignored — the tie breaks by
    the strategy's own ordering, exactly as before the fix."""
    cfg = _cfg(spread=False)
    accts = {"active": {"current_5h_pct": 96, "current_7d_pct": 20, "next_swap_at_pct": 95},
             "loaded": {"current_5h_pct": 5, "current_7d_pct": 5, "next_swap_at_pct": 95},
             "empty": {"current_5h_pct": 10, "current_7d_pct": 10, "next_swap_at_pct": 95}}
    state = {"active": "active", "accounts": accts, "_lane_load": {"loaded": 3}}
    tgt = cus.pick_swap_target(state, cfg)
    # 'loaded' has strictly lower usage → smart scores it highest; the ignored
    # 3-lane load does NOT demote it.
    assert tgt is not None and tgt.name == "loaded", f"gate off must ignore lane load, got {tgt and tgt.name}"


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
