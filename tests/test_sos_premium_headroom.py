"""Tests for the PRE-EMPTIVE premium-headroom SOS (GH #150, 2026-07-05).

Incident: live premium sessions 429'd because when their accounts hit their 5h
caps, the daemon had NO valid swap target — the one spare account had gone
token_stale/offline. The existing per-lane "no swap target" SOS (Condition 2b)
only fires once a lane is ALREADY at 100%, i.e. after a session is wedged. This
new check (Condition 2c, `_diagnose_premium_headroom`) fires EARLY: while lanes
are still climbing, the instant the count of VALID swap targets for the premium
pool collapses to zero (URGENT) or a single target while a lane is already past
the high-water step (WARNING).

Covers:
  - the pure predicate `_diagnose_premium_headroom` (state+config+occupied+free
    pooled families → condition|None) with plain dicts — no I/O
  - the loss-reason labeller `_premium_target_loss_reason`
  - end-to-end through `diagnose()` on a real (temp) tree with a live premium
    lane and a token_stale spare (reuses test_login_pool's on-disk _Env)

Run standalone:  python3 tests/test_sos_premium_headroom.py
Run under pytest: pytest tests/test_sos_premium_headroom.py
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import cus  # noqa: E402
from test_login_pool import _Env  # noqa: E402  (reuse the on-disk fixture)


def _cfg(**over) -> dict:
    """DEFAULT_CONFIG in a lane-serving mode, plus any overrides."""
    base = cus.deep_merge(cus.DEFAULT_CONFIG, {"mode": "per_session"})
    return cus.deep_merge(base, over) if over else base


def _acct(five_h: float = 0.0, seven_d: float = 0.0, next_at: int = 50, **flags) -> dict:
    a = {"current_5h_pct": five_h, "current_7d_pct": seven_d, "next_swap_at_pct": next_at}
    a.update(flags)
    return a


def _state(accounts: dict, premium_lane_accounts: tuple[str, ...], active: str | None = None) -> dict:
    """Build a state.json-shaped dict with one premium slot per named account."""
    slots = {}
    for i, acct_name in enumerate(premium_lane_accounts, start=1):
        slots[f"slot-{i}"] = {"account": acct_name, "pool": "premium"}
    return {
        "active": active or (premium_lane_accounts[0] if premium_lane_accounts else None),
        "accounts": accounts,
        "slots": slots,
        "swap_history": [],
    }


def _occupied(premium_lane_accounts: tuple[str, ...]) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    for i, acct_name in enumerate(premium_lane_accounts, start=1):
        out.setdefault(acct_name, []).append(f"slot-{i}")
    return out


# --------------------------------------------------------------------------
# (a) premium lane + 0 valid targets → URGENT
# --------------------------------------------------------------------------

def test_zero_valid_targets_fires_urgent():
    accounts = {
        "alpha": _acct(five_h=60.0, seven_d=20.0),          # the live premium lane
        "beta": _acct(five_h=0.0, seven_d=0.0, token_stale=True),  # the only spare, offline
    }
    state = _state(accounts, ("alpha",))
    cond = cus._diagnose_premium_headroom(state, _cfg(), _occupied(("alpha",)), set())
    assert cond is not None
    assert cond.severity == "urgent"
    assert "1 premium lane(s) live, 0 valid swap targets" in cond.summary
    assert "429" in cond.summary
    # The offline spare is named as lost capacity with its concrete lever.
    assert "beta TOKEN_STALE" in cond.action
    assert "cus relogin" in cond.action
    assert "cus add" in cond.action


# --------------------------------------------------------------------------
# (b) premium lanes + healthy targets → nothing
# --------------------------------------------------------------------------

def test_healthy_target_no_condition():
    accounts = {
        "alpha": _acct(five_h=60.0, seven_d=20.0),  # lane, still climbing (< 90)
        "beta": _acct(five_h=5.0, seven_d=5.0),     # healthy spare = real headroom
    }
    state = _state(accounts, ("alpha",))
    assert cus._diagnose_premium_headroom(state, _cfg(), _occupied(("alpha",)), set()) is None


def test_two_valid_targets_even_with_hot_lane_no_condition():
    # >1 valid target = healthy pool; a hot lane alone must NOT trip the warning.
    accounts = {
        "alpha": _acct(five_h=95.0, seven_d=20.0),  # hot lane, past 90
        "beta": _acct(five_h=5.0, seven_d=5.0),
        "gamma": _acct(five_h=10.0, seven_d=8.0),
    }
    state = _state(accounts, ("alpha",))
    assert cus._diagnose_premium_headroom(state, _cfg(), _occupied(("alpha",)), set()) is None


def test_global_mode_never_fires():
    accounts = {"alpha": _acct(five_h=99.0), "beta": _acct(token_stale=True)}
    state = _state(accounts, ("alpha",))
    # global mode has no slot lanes → None regardless of headroom.
    assert cus._diagnose_premium_headroom(state, _cfg(mode="global"), _occupied(("alpha",)), set()) is None


def test_no_premium_lanes_never_fires():
    accounts = {"alpha": _acct(), "beta": _acct(token_stale=True)}
    # A standard-pool lane must not trigger the premium alarm.
    state = {"active": "alpha", "accounts": accounts,
             "slots": {"slot-1": {"account": "alpha", "pool": "standard"}}, "swap_history": []}
    assert cus._diagnose_premium_headroom(state, _cfg(), {"alpha": ["slot-1"]}, set()) is None


def test_no_lanes_at_all_never_fires():
    accounts = {"alpha": _acct(), "beta": _acct(token_stale=True)}
    state = {"active": "alpha", "accounts": accounts, "slots": {}, "swap_history": []}
    assert cus._diagnose_premium_headroom(state, _cfg(), {}, set()) is None


# --------------------------------------------------------------------------
# (c) a stale account that would otherwise be the only target → named lost
# --------------------------------------------------------------------------

def test_stale_only_target_named_and_is_the_pivot():
    # beta is healthy on every axis EXCEPT token_stale — so it is the pivotal
    # capacity: with the stale flag it's lost (URGENT, named); without it, the
    # pool has a valid target and nothing fires.
    stale = {
        "alpha": _acct(five_h=70.0, seven_d=20.0),
        "beta": _acct(five_h=3.0, seven_d=4.0, token_stale=True),
    }
    state = _state(stale, ("alpha",))
    cond = cus._diagnose_premium_headroom(state, _cfg(), _occupied(("alpha",)), set())
    assert cond is not None and cond.severity == "urgent"
    assert "beta TOKEN_STALE" in cond.action

    # Same fleet, beta revived → valid target appears → all clear.
    healthy = {
        "alpha": _acct(five_h=70.0, seven_d=20.0),
        "beta": _acct(five_h=3.0, seven_d=4.0),
    }
    assert cus._diagnose_premium_headroom(_state(healthy, ("alpha",)), _cfg(),
                                          _occupied(("alpha",)), set()) is None


def test_lost_capacity_names_capped_and_rate_limited():
    accounts = {
        "alpha": _acct(five_h=70.0, seven_d=20.0),                  # lane
        "beta": _acct(five_h=10.0, seven_d=90.0),                   # over 7d cap (80)
        "gamma": _acct(five_h=10.0, seven_d=10.0, rate_limited=True),  # rate limited
    }
    state = _state(accounts, ("alpha",))
    cond = cus._diagnose_premium_headroom(state, _cfg(), _occupied(("alpha",)), set())
    assert cond is not None and cond.severity == "urgent"
    assert "over 7d cap" in cond.action        # beta named with its reason
    assert "gamma RATE_LIMITED" in cond.action  # gamma named with its reason


# --------------------------------------------------------------------------
# (d) high-water WARNING tier: exactly one valid target + a hot lane
# --------------------------------------------------------------------------

def test_high_water_warning_tier():
    accounts = {
        "alpha": _acct(five_h=92.0, seven_d=20.0),  # lane past the 90 top step
        "beta": _acct(five_h=5.0, seven_d=5.0),     # the single remaining valid target
    }
    state = _state(accounts, ("alpha",))
    cond = cus._diagnose_premium_headroom(state, _cfg(), _occupied(("alpha",)), set())
    assert cond is not None
    assert cond.severity == "warning"
    assert "only 1 valid swap target" in cond.summary
    assert "alpha @ 92%" in cond.summary


def test_one_target_but_no_hot_lane_stays_quiet():
    # A single valid target while every lane is still cool = not yet a warning.
    accounts = {
        "alpha": _acct(five_h=40.0, seven_d=20.0),  # cool lane (< 90)
        "beta": _acct(five_h=5.0, seven_d=5.0),
    }
    state = _state(accounts, ("alpha",))
    assert cus._diagnose_premium_headroom(state, _cfg(), _occupied(("alpha",)), set()) is None


def test_high_water_threshold_is_config_driven():
    # Lower the top step to 80 → an 85% lane now counts as hot.
    accounts = {
        "alpha": _acct(five_h=85.0, seven_d=20.0),
        "beta": _acct(five_h=5.0, seven_d=5.0),
    }
    state = _state(accounts, ("alpha",))
    cfg = _cfg(thresholds={"steps": [50, 80]})
    cond = cus._diagnose_premium_headroom(state, cfg, _occupied(("alpha",)), set())
    assert cond is not None and cond.severity == "warning"
    assert "past 80%" in cond.summary


# --------------------------------------------------------------------------
# Pool reachability: a held account with a FREE pooled family is a valid target
# --------------------------------------------------------------------------

def test_held_account_with_free_family_counts_as_target():
    # Two live premium lanes, both cool; beta is live-held but carries a free
    # pooled family, so a lane can borrow a distinct family → beta is a valid
    # target and the pool is NOT starved. (Contrast the no-free-family sibling
    # below, which is URGENT.) Alpha kept below the high-water step so only the
    # target-count logic — not the hot-lane warning — is under test.
    accounts = {
        "alpha": _acct(five_h=60.0, seven_d=20.0),  # cool lane
        "beta": _acct(five_h=5.0, seven_d=5.0),     # live-held but has a free family
    }
    state = _state(accounts, ("alpha", "beta"))
    cfg = _cfg(independent_logins={"use_independent_logins": True})
    # free_family_accounts marks beta as borrowable.
    cond = cus._diagnose_premium_headroom(state, cfg, _occupied(("alpha", "beta")), {"beta"})
    assert cond is None


def test_held_account_without_free_family_is_starvation():
    # Same two live premium lanes, but NO free families → each blocks the other,
    # no idle spare → 0 valid targets → URGENT.
    accounts = {
        "alpha": _acct(five_h=95.0, seven_d=20.0),
        "beta": _acct(five_h=5.0, seven_d=5.0),
    }
    state = _state(accounts, ("alpha", "beta"))
    cfg = _cfg(independent_logins={"use_independent_logins": True})
    cond = cus._diagnose_premium_headroom(state, cfg, _occupied(("alpha", "beta")), set())
    assert cond is not None and cond.severity == "urgent"
    assert "2 premium lane(s) live, 0 valid swap targets" in cond.summary


# --------------------------------------------------------------------------
# Loss-reason labeller
# --------------------------------------------------------------------------

def test_loss_reason_labels():
    cfg = _cfg()
    assert cus._premium_target_loss_reason("x", _acct(token_stale=True), cfg) == "TOKEN_STALE"
    assert cus._premium_target_loss_reason("x", _acct(token_expired=True), cfg) == "TOKEN_EXPIRED"
    assert cus._premium_target_loss_reason("x", _acct(rate_limited=True), cfg) == "RATE_LIMITED"
    assert "SATURATED" in cus._premium_target_loss_reason("x", _acct(five_h=100.0), cfg)
    assert "over 7d cap" in cus._premium_target_loss_reason("x", _acct(seven_d=90.0), cfg)
    # At its own ladder step but below caps → would re-trip on arrival.
    assert "ladder step" in cus._premium_target_loss_reason(
        "x", _acct(five_h=55.0, next_at=50), cfg)


# --------------------------------------------------------------------------
# End-to-end through diagnose() with a real (temp) tree
# --------------------------------------------------------------------------

def _headroom_conditions(conditions):
    return [c for c in conditions if "valid swap target" in c.summary]


def test_diagnose_e2e_stale_spare_fires_urgent():
    env = _Env(accounts=("alpha", "beta"))
    try:
        env.make_slot("alpha", live=True)  # a live premium lane on alpha
        state = cus.load_state()
        state["accounts"]["alpha"].update({"current_5h_pct": 60.0, "current_7d_pct": 20.0})
        state["accounts"]["beta"]["token_stale"] = True  # the only spare is offline
        cus.save_state(state)
        env.set_config({"mode": "per_session"})
        conds = _headroom_conditions(cus.diagnose())
        assert len(conds) == 1
        assert conds[0].severity == "urgent"
        assert "beta TOKEN_STALE" in conds[0].action
    finally:
        env.restore()


def test_diagnose_e2e_healthy_spare_no_condition():
    env = _Env(accounts=("alpha", "beta"))
    try:
        env.make_slot("alpha", live=True)
        state = cus.load_state()
        state["accounts"]["alpha"].update({"current_5h_pct": 60.0, "current_7d_pct": 20.0})
        state["accounts"]["beta"].update({"current_5h_pct": 5.0, "current_7d_pct": 5.0})
        cus.save_state(state)
        env.set_config({"mode": "per_session"})
        assert _headroom_conditions(cus.diagnose()) == []
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
