"""Regression tests for the hard-cap swap PING-PONG (2026-07-05, PR #139).

Incident (confirmed from a live `cus decisions` trace): a lane/shared-mount
forced off an account by a HARD-CAP gate (aggregate 7d `hard_7d_cap` OR the
per-model weekly Fable cap) did a LATERAL swap onto another account that was
ITSELF at/over that same cap — relieving nothing — then swapped back next cycle,
oscillating forever. `min_seconds_between_*` only throttled the rate.

Exact live shape:
  - gate = per-model weekly Fable cap, cap_pct = 97.
  - `default` at Fable 100% (>= 97 → capped) swaps toward its best target… which
    would be `rayi1` (real Fable headroom), but that move FAILS every cycle
    (pool exhausted — no free login family; see the over-subscription fix in the
    same PR). With `rayi1` unreachable, pick_swap_target's headroom fallback
    returned `rayi2` at Fable 97% (== cap → also capped), so default→rayi2→
    default bounced forever.

Root cause of why the pre-existing "[DEGRADED: no targets below 7d cap]" guard
missed it: pick_swap_target filters ARRIVALS with the target-side model cap
(`per_model_weekly.target_cap_pct`), which can sit ABOVE the swap-away `cap_pct`
that tripped — so rayi2 at 97 passed the picker un-degraded even though it
clears nothing.

The invariant these pin: a hard-cap FORCED swap must land on a target STRICTLY
BELOW the forcing cap on the same dimension (genuine relief). A target at/over
the cap → HOLD (holding on the current capped account is strictly no worse than
a churning lateral move; SOS surfaces the all-capped condition). Covers both the
per-model weekly cap and the aggregate hard_7d_cap.

Run standalone:  python3 tests/test_hard_cap_pingpong.py
Or under pytest: pytest tests/test_hard_cap_pingpong.py
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import cus  # noqa: E402


# --------------------------------------------------------------------------
# Aggregate hard_7d_cap gate
# --------------------------------------------------------------------------

CFG_AGG = {
    "strategy": "smart",
    "thresholds": {"five_hour": True, "seven_day": True, "steps": [90, 96]},
    "smart_strategy": {
        "five_hour_headroom_weight": 0.6, "seven_day_headroom_weight": 0.4,
        "hard_7d_cap_pct": 95, "allow_rate_limited_targets": True,
    },
    "swap_hysteresis": {"enabled": False},   # probe the cap logic, not wall-clock
    "usage_growth_gate": {"enabled": False},
}


def _u(h5, h7, model=None):
    """AccountUsage; `model` (dict display_name->pct) seeds per-model weekly."""
    pmw = {m: cus.UsageWindow(p, None) for m, p in (model or {}).items()}
    return cus.AccountUsage(five_hour=cus.UsageWindow(h5, None),
                            seven_day=cus.UsageWindow(h7, None),
                            per_model_weekly=pmw)


def _a(h5, h7, nxt=90, model=None):
    # last_swap_ts=None so hysteresis never blocks — probe the cap logic.
    acct = {"current_5h_pct": h5, "current_7d_pct": h7,
            "next_swap_at_pct": nxt, "last_swap_ts": None}
    if model is not None:
        acct["per_model_weekly_pct"] = model
    return acct


def _decide(active, accts, usage, cfg):
    return cus.decide_swap({"active": active, "accounts": accts}, cfg, usage)


def test_aggregate_cap_holds_when_all_targets_at_or_over_cap():
    """Active over the 95% 7d cap, and every reachable target is ALSO at/over it
    → HOLD (a lateral move to an equally-capped account relieves nothing)."""
    accts = {"default": _a(0, 96), "merkos": _a(10, 97), "rayi": _a(5, 98)}
    usage = {"default": _u(0, 96), "merkos": _u(10, 97), "rayi": _u(5, 98)}
    d = _decide("default", accts, usage, CFG_AGG)
    assert d is None, f"expected HOLD (anti-pingpong), got swap->{d and d.target}"


def test_aggregate_cap_boundary_target_at_exactly_cap_is_rejected():
    """Boundary: a target at EXACTLY the cap (95 == 95) is not relief → HOLD.
    Only strictly-below-cap is a genuine escape."""
    accts = {"default": _a(0, 96), "merkos": _a(10, 95)}
    usage = {"default": _u(0, 96), "merkos": _u(10, 95)}
    d = _decide("default", accts, usage, CFG_AGG)
    assert d is None, f"target at exactly the cap must be rejected, got swap->{d and d.target}"


def test_aggregate_cap_swaps_when_a_below_cap_target_exists():
    """Normal case unchanged: a target strictly below the cap (merkos 7d=40 < 95)
    is genuine relief → the forced swap proceeds exactly as before."""
    accts = {"default": _a(0, 96), "merkos": _a(10, 40)}
    usage = {"default": _u(0, 96), "merkos": _u(10, 40)}
    d = _decide("default", accts, usage, CFG_AGG)
    assert d is not None and d.target == "merkos" and d.gate == "hard_7d_cap", d


# --------------------------------------------------------------------------
# Per-model weekly cap gate — the exact live incident
# --------------------------------------------------------------------------

CFG_MODEL = {
    "strategy": "smart",
    "thresholds": {"five_hour": True, "seven_day": True, "steps": [90, 96]},
    "smart_strategy": {
        "five_hour_headroom_weight": 0.6, "seven_day_headroom_weight": 0.4,
        "hard_7d_cap_pct": 95, "allow_rate_limited_targets": True,
    },
    "swap_hysteresis": {"enabled": False},
    "usage_growth_gate": {"enabled": False},
    # cap_pct 97 = swap-AWAY cap (what trips `default`); target_cap_pct 100 =
    # target-side bar the picker filters arrivals with. The gap is precisely what
    # let a 97%-Fable target slip through the picker un-degraded pre-fix.
    "per_model_weekly": {"gate_enabled": True, "models": ["fable"],
                         "cap_pct": 97, "target_cap_pct": 100},
}


def test_per_model_cap_holds_on_equally_capped_target():
    """The live ping-pong in miniature: `default` at Fable 100% (>= 97 cap) is
    forced to swap, but the only reachable target (rayi2) is at Fable 97% (== cap,
    below the target-side 100 bar so the picker offers it un-degraded). Must HOLD,
    not bounce onto it. Pre-fix this returned SwapDecision(target=rayi2) → the
    oscillation."""
    accts = {"default": _a(0, 20, model={"fable": 100.0}),
             "rayi2": _a(20, 20, model={"fable": 97.0})}
    usage = {"default": _u(0, 20, model={"fable": 100.0}),
             "rayi2": _u(20, 20, model={"fable": 97.0})}
    d = _decide("default", accts, usage, CFG_MODEL)
    assert d is None, f"expected HOLD (anti-pingpong), got swap->{d and d.target}"


def test_per_model_cap_swaps_onto_below_cap_target():
    """Genuine relief: rayi2 at Fable 50% (< 97 cap) → the forced swap proceeds.
    Proves the guard blocks only the no-relief case, not a real escape."""
    accts = {"default": _a(0, 20, model={"fable": 100.0}),
             "rayi2": _a(20, 20, model={"fable": 50.0})}
    usage = {"default": _u(0, 20, model={"fable": 100.0}),
             "rayi2": _u(20, 20, model={"fable": 50.0})}
    d = _decide("default", accts, usage, CFG_MODEL)
    assert d is not None and d.target == "rayi2" and d.gate == "hard_7d_cap", d


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
