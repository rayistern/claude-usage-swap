"""Tests for 72-hour seven_day reset detection (2026-07-05).

Field finding (gist: monperrus/3ac4b303a84946bbeaf2b1123ee99491): the Claude
`seven_day` "weekly" cap actually REFRESHES every ~72h at a fixed UTC anchor
(~04:50-05:00 UTC), NOT every 7 days. The API's seven_day.resets_at reports
~7 days out (when the oldest tokens age off), which is misleading for budget
purposes. cus infers the real reset from its observable signature — a sharp
DROP in current_7d_pct between two consecutive OBSERVATIONS — and projects the
next one 72h forward.

Pins:
  - _is_seven_day_reset_drop: pure detector; sharp drop = reset, jitter != reset.
  - update_state_with_usage: a poll cycle whose 7d drops sharply stamps
    seven_day_last_reset_ts; a small fluctuation does not.
  - projected_seven_day_reset: projects last_reset + 72h; falls back to the API
    resets_at on cold start; returns the EARLIER of projected-vs-API when both
    exist (the real refresh precedes the oldest-tokens boundary); rolls a stale
    last_reset forward by whole 72h periods.
"""

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import cus  # noqa: E402


def _iso(dt):
    return dt.isoformat().replace("+00:00", "Z")


def _now():
    return datetime.now(timezone.utc)


def _parse(iso_str):
    return datetime.fromisoformat(iso_str.replace("Z", "+00:00"))


# --------------------------------------------------------------------------
# _is_seven_day_reset_drop — pure detector
# --------------------------------------------------------------------------

def test_sharp_drop_is_a_reset():
    # 60% -> 10% = 50-point drop, well past the 15-point default threshold.
    assert cus._is_seven_day_reset_drop(60.0, 10.0, cus.DEFAULT_CONFIG) is True


def test_drop_exactly_at_threshold_is_a_reset():
    # Boundary: default seven_day_reset_drop_pct is 15; a drop of exactly 15
    # counts (>= comparison).
    assert cus._is_seven_day_reset_drop(30.0, 15.0, cus.DEFAULT_CONFIG) is True


def test_small_fluctuation_is_not_a_reset():
    # A 5-point dip (jitter / oldest-tokens-age-off trickle) is NOT a reset.
    assert cus._is_seven_day_reset_drop(60.0, 55.0, cus.DEFAULT_CONFIG) is False


def test_increase_is_not_a_reset():
    # Usage climbing (normal within-window accrual) is never a reset.
    assert cus._is_seven_day_reset_drop(40.0, 55.0, cus.DEFAULT_CONFIG) is False


def test_missing_value_is_not_a_reset():
    # Cold start / first observation: no prior (or no new) value → not a reset.
    assert cus._is_seven_day_reset_drop(None, 10.0, cus.DEFAULT_CONFIG) is False
    assert cus._is_seven_day_reset_drop(60.0, None, cus.DEFAULT_CONFIG) is False


def test_drop_threshold_is_config_driven():
    # Tighten the knob so a 20-point drop no longer qualifies.
    cfg = dict(cus.DEFAULT_CONFIG)
    cfg["seven_day_reset_drop_pct"] = 40
    assert cus._is_seven_day_reset_drop(60.0, 40.0, cfg) is False   # 20 < 40
    assert cus._is_seven_day_reset_drop(60.0, 10.0, cfg) is True    # 50 >= 40


# --------------------------------------------------------------------------
# (a) simulated poll sequence where 7d drops sharply
# --------------------------------------------------------------------------

def test_poll_with_sharp_7d_drop_stamps_last_reset_and_projects_72h():
    saved = cus.load_config
    cus.load_config = lambda: cus.DEFAULT_CONFIG
    try:
        api_7d_out = _iso(_now() + timedelta(days=7))  # misleading API boundary
        state = {"accounts": {"a": {
            "current_5h_pct": 20.0,
            "current_7d_pct": 60.0,   # prior observation (before reset)
            "last_observed_ts": _iso(_now() - timedelta(minutes=30)),
        }}}
        u = cus.AccountUsage.empty()
        u.five_hour = cus.UsageWindow(utilization=5.0, resets_at=None)
        u.seven_day = cus.UsageWindow(utilization=10.0, resets_at=api_7d_out)  # 60 -> 10 drop
        cus.update_state_with_usage(state, {"a": u})
        acct = state["accounts"]["a"]

        # Reset event recorded at the poll time.
        assert acct["seven_day_last_reset_ts"] == u.polled_at

        # Projection = drop time + 72h (and earlier than the ~7-day API value).
        projected = cus.projected_seven_day_reset(acct, cus.DEFAULT_CONFIG)
        expected = _parse(u.polled_at) + timedelta(hours=72)
        assert abs((_parse(projected) - expected).total_seconds()) < 1
        assert _parse(projected) < _parse(api_7d_out)
    finally:
        cus.load_config = saved


# --------------------------------------------------------------------------
# (b) small fluctuation is NOT treated as a reset
# --------------------------------------------------------------------------

def test_poll_with_small_7d_fluctuation_not_a_reset():
    saved = cus.load_config
    cus.load_config = lambda: cus.DEFAULT_CONFIG
    try:
        api_7d_out = _iso(_now() + timedelta(days=7))
        state = {"accounts": {"a": {
            "current_5h_pct": 20.0,
            "current_7d_pct": 60.0,
        }}}
        u = cus.AccountUsage.empty()
        u.five_hour = cus.UsageWindow(utilization=22.0, resets_at=None)
        u.seven_day = cus.UsageWindow(utilization=57.0, resets_at=api_7d_out)  # 3-point dip
        cus.update_state_with_usage(state, {"a": u})
        acct = state["accounts"]["a"]

        # No reset stamped → projection falls back to the API boundary.
        assert "seven_day_last_reset_ts" not in acct
        projected = cus.projected_seven_day_reset(acct, cus.DEFAULT_CONFIG)
        assert projected == api_7d_out
    finally:
        cus.load_config = saved


# --------------------------------------------------------------------------
# (c) cold start (no drop observed yet) → API fallback
# --------------------------------------------------------------------------

def test_cold_start_falls_back_to_api_resets_at():
    api_ts = _iso(_now() + timedelta(days=7))
    acct = {"current_7d_pct": 40.0, "seven_day_resets_at": api_ts}
    # No seven_day_last_reset_ts yet → use the raw API timestamp unchanged.
    assert cus.projected_seven_day_reset(acct, cus.DEFAULT_CONFIG) == api_ts


def test_no_data_returns_none():
    acct = {"current_7d_pct": 40.0}
    assert cus.projected_seven_day_reset(acct, cus.DEFAULT_CONFIG) is None


# --------------------------------------------------------------------------
# (d) the projection feeds the reset-timing path (earlier-of logic + roll-fwd)
# --------------------------------------------------------------------------

def test_projection_returns_earlier_of_projected_and_api():
    # last_reset was 1h ago → projected refresh is ~71h out; API says ~7 days.
    # The real (earlier) refresh must win — this is the value the statusline
    # 7d countdown now renders instead of the raw API boundary.
    last_reset = _iso(_now() - timedelta(hours=1))
    api_ts = _iso(_now() + timedelta(days=7))
    acct = {"seven_day_last_reset_ts": last_reset, "seven_day_resets_at": api_ts}
    projected = cus.projected_seven_day_reset(acct, cus.DEFAULT_CONFIG)
    expected = _parse(last_reset) + timedelta(hours=72)
    assert abs((_parse(projected) - expected).total_seconds()) < 1
    assert _parse(projected) < _parse(api_ts)


def test_api_wins_when_it_is_earlier_than_projection():
    # Degenerate case: API boundary is sooner than the 72h projection. The
    # helper still returns the earlier of the two.
    last_reset = _iso(_now() - timedelta(hours=1))          # projection ~71h out
    api_ts = _iso(_now() + timedelta(hours=2))               # API only 2h out
    acct = {"seven_day_last_reset_ts": last_reset, "seven_day_resets_at": api_ts}
    assert cus.projected_seven_day_reset(acct, cus.DEFAULT_CONFIG) == api_ts


def test_stale_last_reset_rolls_forward_to_future():
    # A last_reset left behind ~200h ago (several missed 72h cycles) must roll
    # forward by whole periods to the NEXT upcoming refresh, never a past stamp.
    last_reset = _iso(_now() - timedelta(hours=200))
    acct = {"seven_day_last_reset_ts": last_reset}  # no API value
    projected = cus.projected_seven_day_reset(acct, cus.DEFAULT_CONFIG)
    dt = _parse(projected)
    assert dt > _now()
    # It lands on the fixed 72h anchor grid: (projected - last_reset) is a whole
    # multiple of 72h.
    delta_h = (dt - _parse(last_reset)).total_seconds() / 3600.0
    assert abs(delta_h - round(delta_h / 72.0) * 72.0) < 0.01


def test_reset_hours_is_config_driven():
    # Change the cadence knob and the projection tracks it.
    cfg = dict(cus.DEFAULT_CONFIG)
    cfg["seven_day_reset_hours"] = 48
    last_reset = _iso(_now() - timedelta(hours=1))
    acct = {"seven_day_last_reset_ts": last_reset}
    projected = cus.projected_seven_day_reset(acct, cfg)
    expected = _parse(last_reset) + timedelta(hours=48)
    assert abs((_parse(projected) - expected).total_seconds()) < 1
