"""Regression tests for the "swap fires too late" fix (2026-07-06 rayi3 incident).

Incident: an account burned from UNDER its 95% ladder step to 100% 5h BETWEEN
two ~180s daemon polls, so a live session hit Claude's native "usage limit" menu
before the daemon swapped its lane off. The whole point of the 95% step is to
swap BEFORE the cap; the swap fired late because the daemon's ladder TRIGGER read
the stale last-polled value (91%) instead of the burn-extrapolated current usage.

Two complementary, config-gated (`poll_accel`) fixes, both pinned here:
  Fix A — the ladder/cap swap TRIGGER decides on burn-EXTRAPOLATED usage
    (last poll + burn_rate x time-to-next-poll), so an account at 91%-last-poll
    but climbing fast trips the step NOW instead of next cycle.
  Fix B — accounts whose extrapolated usage is within `within_pct_of_step` of
    their next step (or projected to cross it before the next normal poll) are
    polled on the fast `fast_interval_seconds` cadence, so the fast burn is SEEN
    in time. Only near-step accounts accelerate; a 429-backed-off account never.

Run standalone:  python3 tests/test_swap_too_late_extrapolate.py
Or under pytest: pytest tests/test_swap_too_late_extrapolate.py
"""

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import cus  # noqa: E402


def _iso(dt):
    return dt.isoformat().replace("+00:00", "Z")


# Self-contained config so the tests don't depend on the operator's live
# config.yaml. hard_7d_cap_pct is set HIGH (99) so the 7d LADDER step (95) is the
# thing under test, not the hard cap. defer/BBR left off so we probe the ladder
# trigger's input value cleanly.
def _cfg(poll_accel_enabled=True, within=5, fast=45,
         active_iv=180, inactive_iv=600):
    return {
        "strategy": "smart",
        "poll_interval_seconds": 300,
        "polling": {"active_interval_seconds": active_iv,
                    "inactive_interval_seconds": inactive_iv},
        "thresholds": {"five_hour": True, "seven_day": True, "steps": [50, 75, 90]},
        "smart_strategy": {"hard_7d_cap_pct": 99, "allow_rate_limited_targets": True,
                           "five_hour_headroom_weight": 0.6,
                           "seven_day_headroom_weight": 0.4},
        "swap_hysteresis": {"enabled": True, "min_improvement_pct": 5,
                            "min_seconds_between_swaps": 3000},
        "usage_growth_gate": {"enabled": True, "min_delta_pct": 0.5, "saturation_pct": 95},
        "defer_swap_near_5h_reset": {"enabled": False},
        "estimator": {"enabled": True, "max_extrapolation_minutes": 10},
        "poll_accel": {"enabled": poll_accel_enabled,
                       "within_pct_of_step": within, "fast_interval_seconds": fast},
    }


def _u(h5, h7):
    return cus.AccountUsage(five_hour=cus.UsageWindow(h5, None),
                            seven_day=cus.UsageWindow(h7, None))


# ==========================================================================
# Pure helpers (deterministic — `now` injected)
# ==========================================================================

def test_extrapolated_effective_pct_5h_look_ahead():
    """91% last-poll + 1.0%/min 5h burn, projected 5 min forward → ~96%."""
    now = datetime(2026, 7, 6, 12, 0, tzinfo=timezone.utc)
    acct = {"current_5h_pct": 91.0, "current_7d_pct": 10.0,
            "burn_rate_5h_pct_per_min": 1.0,
            "last_observed_ts": _iso(now - timedelta(minutes=2))}
    cfg = _cfg()
    # look_ahead 180s + 120s elapsed = 5 min at 1.0%/min → 91 + 5 = 96.
    est = cus._account_estimated_effective_pct(acct, cfg, now, look_ahead_seconds=180)
    assert 95.9 <= est <= 96.1, est
    # to-now only (no look-ahead) = 91 + 2 = 93 (would NOT trip the 95 step).
    assert abs(cus._account_estimated_effective_pct(acct, cfg, now) - 93.0) < 0.1


def test_extrapolated_effective_pct_7d_look_ahead():
    """Same, but the pressure is the 7d window."""
    now = datetime(2026, 7, 6, 12, 0, tzinfo=timezone.utc)
    acct = {"current_5h_pct": 10.0, "current_7d_pct": 91.0,
            "burn_rate_7d_pct_per_min": 1.0,
            "last_observed_ts": _iso(now - timedelta(minutes=2))}
    est = cus._account_estimated_effective_pct(acct, _cfg(), now, look_ahead_seconds=180)
    assert 95.9 <= est <= 96.1, est


def test_extrapolation_never_below_raw_and_clamped():
    """Upward-only + clamped to 100 (never masks a real over-cap poll)."""
    now = datetime(2026, 7, 6, 12, 0, tzinfo=timezone.utc)
    acct = {"current_5h_pct": 98.0, "current_7d_pct": 0.0,
            "burn_rate_5h_pct_per_min": 5.0,
            "last_observed_ts": _iso(now - timedelta(minutes=5))}
    est = cus._account_estimated_effective_pct(acct, _cfg(), now, look_ahead_seconds=600)
    assert est == 100.0
    # zero burn → equals the raw polled value exactly.
    acct2 = {"current_5h_pct": 60.0, "current_7d_pct": 0.0,
             "burn_rate_5h_pct_per_min": 0.0,
             "last_observed_ts": _iso(now - timedelta(minutes=5))}
    assert cus._account_estimated_effective_pct(acct2, _cfg(), now, look_ahead_seconds=600) == 60.0


def test_account_near_step_predicate():
    now = datetime(2026, 7, 6, 12, 0, tzinfo=timezone.utc)
    cfg = _cfg(within=5)
    # within 5 pts of the 95 step (92 >= 90), zero burn → near via condition (1).
    near = {"current_5h_pct": 92.0, "current_7d_pct": 0.0, "next_swap_at_pct": 95,
            "burn_rate_5h_pct_per_min": 0.0, "last_observed_ts": _iso(now)}
    assert cus._account_near_step(near, cfg, 180, now) is True
    # far (60%) + no burn → not near.
    far = {"current_5h_pct": 60.0, "current_7d_pct": 0.0, "next_swap_at_pct": 95,
           "burn_rate_5h_pct_per_min": 0.0, "last_observed_ts": _iso(now)}
    assert cus._account_near_step(far, cfg, 180, now) is False
    # far NOW (80%) but fast burn projects crossing 95 within 180s → near via (2).
    climbing = {"current_5h_pct": 80.0, "current_7d_pct": 0.0, "next_swap_at_pct": 95,
                "burn_rate_5h_pct_per_min": 6.0, "last_observed_ts": _iso(now)}
    assert cus._account_near_step(climbing, cfg, 180, now) is True
    # gate off → never near.
    assert cus._account_near_step(near, _cfg(poll_accel_enabled=False), 180, now) is False


# ==========================================================================
# Fix A — decide_swap trips EARLY on the extrapolated value
# (test a: 91% last-poll + high burn swaps; gate off would wait)
# ==========================================================================

def _active_burning(window):
    """Active account at 91% on `window`, burning ~1.2%/min, observed 30s ago."""
    now = datetime.now(timezone.utc)
    acct = {"next_swap_at_pct": 95, "last_observed_ts": _iso(now - timedelta(seconds=30))}
    if window == "5h":
        acct.update({"current_5h_pct": 91.0, "current_7d_pct": 10.0,
                     "prev_current_5h_pct": 89.0, "burn_rate_5h_pct_per_min": 1.2})
    else:
        acct.update({"current_5h_pct": 10.0, "current_7d_pct": 91.0,
                     "prev_current_7d_pct": 89.0, "burn_rate_7d_pct_per_min": 1.2})
    return acct


def _state(active_acct, window):
    # A genuinely-fresh target so pick_swap_target has somewhere to go.
    target = {"current_5h_pct": 8.0, "current_7d_pct": 8.0, "next_swap_at_pct": 50,
              "last_observed_ts": _iso(datetime.now(timezone.utc))}
    return {"active": "hot", "accounts": {"hot": active_acct, "spare": target}}


def _usage(window):
    return ({"hot": _u(91, 10), "spare": _u(8, 8)} if window == "5h"
            else {"hot": _u(10, 91), "spare": _u(8, 8)})


def test_ladder_trips_on_extrapolated_5h_when_enabled():
    """91% 5h last-poll + fast burn → extrapolated past the 95 step → SWAP."""
    st = _state(_active_burning("5h"), "5h")
    d = cus.decide_swap(st, _cfg(), _usage("5h"))
    assert d is not None and d.target == "spare", f"expected early swap, got {d}"


def test_ladder_holds_on_stale_5h_when_disabled():
    """Same account, poll_accel OFF → trigger reads raw 91% < 95 step → HOLD.
    This is the exact too-late behavior the fix corrects."""
    st = _state(_active_burning("5h"), "5h")
    d = cus.decide_swap(st, _cfg(poll_accel_enabled=False), _usage("5h"))
    assert d is None, f"expected HOLD on raw 91%, got swap->{d and d.target}"


def test_ladder_trips_on_extrapolated_7d_when_enabled():
    st = _state(_active_burning("7d"), "7d")
    d = cus.decide_swap(st, _cfg(), _usage("7d"))
    assert d is not None and d.target == "spare", f"expected early swap, got {d}"


def test_ladder_holds_on_stale_7d_when_disabled():
    st = _state(_active_burning("7d"), "7d")
    d = cus.decide_swap(st, _cfg(poll_accel_enabled=False), _usage("7d"))
    assert d is None, f"expected HOLD on raw 91%, got swap->{d and d.target}"


# ==========================================================================
# Fix B — _account_poll_due fast cadence near the threshold
# (test b/c/d/e)
# ==========================================================================

def _poll_state(window, poll_age_s, backoff=False):
    """State with `hot` as the ACTIVE account (normal active cadence 180s),
    last polled `poll_age_s` ago. 92% on `window` (within 5 of the 95 step)."""
    now = datetime.now(timezone.utc)
    hot = {"next_swap_at_pct": 95,
           "last_poll_ts": _iso(now - timedelta(seconds=poll_age_s)),
           "last_observed_ts": _iso(now - timedelta(seconds=poll_age_s)),
           "burn_rate_5h_pct_per_min": 0.0, "burn_rate_7d_pct_per_min": 0.0}
    hot["current_5h_pct"] = 92.0 if window == "5h" else 10.0
    hot["current_7d_pct"] = 92.0 if window == "7d" else 10.0
    if backoff:
        hot["poll_backoff_until_ts"] = _iso(now + timedelta(seconds=300))
    return {"active": "hot", "accounts": {"hot": hot}}


def test_near_step_account_due_on_fast_cadence():
    """(b) 60s since last poll < normal 180s active interval, so NOT normally
    due — but within 5pts of the 95 step → fast 45s cadence → DUE."""
    for window in ("5h", "7d"):
        due, why = cus._account_poll_due(_poll_state(window, 60), _cfg(fast=45), "hot")
        assert due is True and "near-step-fast" in why, f"{window}: {due} {why}"


def test_far_account_not_accelerated():
    """(c) An account far from any step keeps the normal cadence (not due at 60s
    into a 180s interval), and the reason shows no acceleration."""
    now = datetime.now(timezone.utc)
    hot = {"current_5h_pct": 50.0, "current_7d_pct": 10.0, "next_swap_at_pct": 95,
           "last_poll_ts": _iso(now - timedelta(seconds=60)),
           "last_observed_ts": _iso(now - timedelta(seconds=60)),
           "burn_rate_5h_pct_per_min": 0.0, "burn_rate_7d_pct_per_min": 0.0}
    st = {"active": "hot", "accounts": {"hot": hot}}
    due, why = cus._account_poll_due(st, _cfg(), "hot")
    assert due is False and "near-step-fast" not in why, (due, why)


def test_backoff_account_not_fast_polled():
    """(d) An account in 429 poll-backoff is NEVER fast-polled even when near a
    step — the backoff gate wins."""
    due, why = cus._account_poll_due(_poll_state("5h", 60, backoff=True), _cfg(), "hot")
    assert "near-step-fast" not in why, why
    assert due is False, (due, why)  # 60s < normal 180s and no acceleration


def test_gate_off_reverts_to_normal_cadence():
    """(e) poll_accel disabled → the SAME near-step account is NOT accelerated;
    behavior is identical to prior (not due at 60s into the 180s interval)."""
    st = _poll_state("5h", 60)
    due, why = cus._account_poll_due(st, _cfg(poll_accel_enabled=False), "hot")
    assert due is False and "near-step-fast" not in why, (due, why)
    # sanity: the ONLY difference vs test_near_step_account_due... is the gate.
    due_on, _ = cus._account_poll_due(st, _cfg(), "hot")
    assert due_on is True


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
