"""Tests for the per-account `disabled: true` out-of-rotation flag (2026-07-03).

Origin: user request — `default` depleted its Fable weekly allotment while the
polled per-model number still read 87%, under the per_model_weekly 97% cap, so
the automatic gate couldn't see it. The operator needs a hard switch that takes
an account out of the target rotation regardless of what polling reports.

Semantics pinned here:
  - NEVER picked by pick_swap_target, for any strategy, even as the ONLY
    candidate (operator mandate outranks every degraded-pool fallback).
  - NEVER picked by pick_launch_account, including its two raw fallbacks that
    bypass pick_swap_target's universal filters.
  - (2026-07-21) A live lane whose CURRENT account is disabled is EVICTED off it
    by decide_swap's Trigger 0 — to a clean target when one exists, else HOLD
    (never evict onto a degraded/over-cap target). Reverses the prior pick-side-
    only behavior after the slot-6/03 incident (a premium lane stranded+walled on
    a disabled Fable-maxed account).

Run standalone:  python3 tests/test_disabled_accounts.py
Or under pytest: pytest tests/test_disabled_accounts.py
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import cus  # noqa: E402


def _a(h5, h7, nxt=80):
    return {"current_5h_pct": h5, "current_7d_pct": h7,
            "next_swap_at_pct": nxt, "last_swap_ts": None}


def _cfg(disabled=(), strategy="smart"):
    return {
        "strategy": strategy,
        "accounts": [{"name": "alpha", "priority": 1},
                     {"name": "beta", "priority": 1, "disabled": "beta" in disabled},
                     {"name": "gamma", "priority": 1, "disabled": "gamma" in disabled}],
        "thresholds": {"five_hour": True, "seven_day": True, "steps": [80, 90]},
        "swap_hysteresis": {"enabled": False},
        "usage_growth_gate": {"enabled": False},
    }


def test_disabled_account_never_picked_even_when_best():
    """beta is the obvious winner (0% everywhere) but is disabled — the pick
    must land on gamma, for every strategy."""
    accts = {"alpha": _a(95, 40), "beta": _a(0, 0), "gamma": _a(30, 20)}
    for strategy in ("smart", "headroom", "lowest_usage", "drain", "round_robin", "strict_priority"):
        t = cus.pick_swap_target({"active": "alpha", "accounts": accts},
                                 _cfg(disabled=("beta",), strategy=strategy))
        assert t is not None and t.name == "gamma", f"{strategy}: {t and t.name}"


def test_disabled_only_candidate_means_no_target():
    """When the only other account is disabled, hold (None) — no fallback tier
    may re-admit an operator-disabled account."""
    accts = {"alpha": _a(95, 40), "beta": _a(0, 0)}
    t = cus.pick_swap_target({"active": "alpha", "accounts": accts}, _cfg(disabled=("beta",)))
    assert t is None, t


def test_model_weekly_target_cap_excludes_warm_model_account():
    """Asymmetric per-model bar (2026-07-03, user): an account at Fable=87%
    must not be PICKED as a target when target_cap_pct=80, even though 87 is
    under the swap-away cap_pct=97 (a lane already on it may keep draining).
    Unset target_cap_pct falls back to cap_pct — old symmetric behavior."""
    accts = {"alpha": _a(95, 40),
             "beta": dict(_a(0, 10), per_model_weekly_pct={"Fable": 87.0}),
             "gamma": _a(30, 20)}
    cfg = _cfg()
    cfg["per_model_weekly"] = {"gate_enabled": True, "models": ["Fable"],
                               "cap_pct": 97, "target_cap_pct": 80}
    t = cus.pick_swap_target({"active": "alpha", "accounts": accts}, cfg)
    assert t is not None and t.name == "gamma", t and t.name
    # Fallback: without target_cap_pct, 87 < 97 keeps beta eligible (and it
    # wins on headroom) — pins that the new knob is opt-in.
    del cfg["per_model_weekly"]["target_cap_pct"]
    t2 = cus.pick_swap_target({"active": "alpha", "accounts": accts}, cfg)
    assert t2 is not None and t2.name == "beta", t2 and t2.name


def test_launch_raw_fallbacks_skip_disabled(monkeypatch):
    """pick_launch_account's raw fallbacks bypass pick_swap_target — they must
    apply the exclusion themselves. Force the fallback path by making every
    account fail the picker (all saturated), leaving only the raw
    lowest-estimated-usage scan."""
    monkeypatch.setattr(cus, "mount_in_use", lambda d: False)
    monkeypatch.setattr(cus, "_live_slot_accounts", lambda state: set())
    accts = {"beta": _a(99, 95), "gamma": _a(99, 96)}  # both full → _try passes fail
    cfg = _cfg(disabled=("beta",))
    t = cus.pick_launch_account({"active": None, "accounts": accts, "slots": {}}, cfg)
    # beta has the lower estimated usage but is disabled → gamma.
    assert t is not None and t.name == "gamma", t and t.name


# --------------------------------------------------------------------------
# Eviction: decide_swap Trigger 0 moves a live lane OFF a disabled account
# (2026-07-21, the slot-6/03 incident).
# --------------------------------------------------------------------------

def test_disabled_active_evicted_to_clean_target():
    """The ACTIVE account is disabled — decide_swap must force it off (Trigger 0)
    onto the clean, non-disabled target, non-deferrably. usage_by_account can be
    empty: the disabled-evict trigger runs before any per-account usage lookup."""
    accts = {"alpha": _a(95, 40), "beta": _a(10, 5), "gamma": _a(30, 20)}
    state = {"active": "beta", "accounts": accts}
    d = cus.decide_swap(state, _cfg(disabled=("beta",)), {})
    assert d is not None, "disabled active must trigger an eviction swap"
    assert d.target == "gamma", d.target        # beta excluded (active+disabled)
    assert d.gate == "disabled_evict", d.gate
    assert d.deferrable is False, d.deferrable   # vacate promptly, ignore cache warmth


def test_disabled_active_holds_when_no_target():
    """Disabled active with NO non-disabled candidate → HOLD (None). Never evict
    onto nothing, and never onto another disabled account. (_cfg only exposes a
    `disabled` flag on beta/gamma, so use those two as the whole fleet.)"""
    accts = {"beta": _a(10, 5), "gamma": _a(30, 20)}
    state = {"active": "beta", "accounts": accts}
    # both accounts disabled → the only other candidate (gamma) is excluded.
    d = cus.decide_swap(state, _cfg(disabled=("beta", "gamma")), {})
    assert d is None, d


def test_disabled_active_holds_when_only_target_over_cap():
    """Disabled active, but every reachable target is itself over the hard 7d cap
    (default 80%) → HOLD rather than wall the lane on a fresh over-cap account.
    Guards against 'evict a disabled-but-clean lane straight into a Fable wall'."""
    accts = {"alpha": _a(10, 92), "beta": _a(10, 5), "gamma": _a(20, 95)}
    state = {"active": "beta", "accounts": accts}
    d = cus.decide_swap(state, _cfg(disabled=("beta",)), {})
    assert d is None, d


if __name__ == "__main__":
    test_disabled_account_never_picked_even_when_best()
    test_disabled_only_candidate_means_no_target()
    test_disabled_active_evicted_to_clean_target()
    test_disabled_active_holds_when_no_target()
    test_disabled_active_holds_when_only_target_over_cap()
    # monkeypatch-dependent test runs under pytest only
    print("ok")
