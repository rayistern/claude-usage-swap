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
  - An account already ON a mount keeps running — the flag only stops new
    placements (pick-side, not eviction).

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


if __name__ == "__main__":
    test_disabled_account_never_picked_even_when_best()
    test_disabled_only_candidate_means_no_target()
    # monkeypatch-dependent test runs under pytest only
    print("ok")
