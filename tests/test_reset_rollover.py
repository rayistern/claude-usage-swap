"""Tests for live 5h-window rollover detection (GH #59).

`_five_hour_rolled_since_poll` lets the statusline flag "this 5h window just
reset" the moment its live countdown elapses — without waiting for the next
poll to confirm — while NOT false-flagging an idle account that's been sitting
at 0% since a poll already observed the reset.

Run standalone:  python3 tests/test_reset_rollover.py
Or under pytest: pytest tests/test_reset_rollover.py
"""

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import cus  # noqa: E402


def _iso(dt: datetime) -> str:
    return dt.isoformat().replace("+00:00", "Z")


def _now() -> datetime:
    return datetime.now(timezone.utc)


def test_rolled_when_reset_passed_and_poll_is_stale():
    """Reset fired 5 min ago, last poll was 12 min ago (before the reset) →
    the stored % is pre-reset stale → flag the rollover."""
    now = _now()
    acct = {
        "five_hour_resets_at": _iso(now - timedelta(minutes=5)),
        "last_poll_ts": _iso(now - timedelta(minutes=12)),
        "current_5h_pct": 96,
    }
    assert cus._five_hour_rolled_since_poll(acct) is True


def test_not_rolled_for_idle_account_already_observed_zero():
    """Reset fired 70 min ago but we polled 3 min ago (after the reset) and saw
    0% → not stale, don't flag."""
    now = _now()
    acct = {
        "five_hour_resets_at": _iso(now - timedelta(minutes=70)),
        "last_poll_ts": _iso(now - timedelta(minutes=3)),
        "current_5h_pct": 0,
    }
    assert cus._five_hour_rolled_since_poll(acct) is False


def test_not_rolled_when_reset_in_future():
    now = _now()
    acct = {
        "five_hour_resets_at": _iso(now + timedelta(hours=2)),
        "last_poll_ts": _iso(now - timedelta(minutes=3)),
        "current_5h_pct": 50,
    }
    assert cus._five_hour_rolled_since_poll(acct) is False


def test_not_rolled_when_no_reset_timestamp():
    assert cus._five_hour_rolled_since_poll({"current_5h_pct": 0}) is False


def test_rolled_when_never_polled():
    """Reset is in the past and we have no last_poll_ts → can't have observed
    the post-reset state → treat as rolled/stale."""
    now = _now()
    acct = {"five_hour_resets_at": _iso(now - timedelta(minutes=2)), "current_5h_pct": 80}
    assert cus._five_hour_rolled_since_poll(acct) is True


def test_inference_zeroes_stale_high_rolled_account():
    now = _now()
    state = {"accounts": {"a": {
        "five_hour_resets_at": _iso(now - timedelta(minutes=5)),
        "last_poll_ts": _iso(now - timedelta(minutes=12)),
        "current_5h_pct": 96,
    }}}
    out = cus._apply_countdown_reset_inference(state, {"reset_inference": {"enabled": True}})
    a = state["accounts"]["a"]
    assert out == ["a"]
    assert a["current_5h_pct"] == 0.0
    assert a["five_hour_reset_inferred"] is True
    assert a["five_hour_pct_pre_reset"] == 96   # prior value stashed for display


def test_inference_leaves_freshly_polled_account_alone():
    now = _now()
    state = {"accounts": {"a": {
        "five_hour_resets_at": _iso(now - timedelta(minutes=70)),
        "last_poll_ts": _iso(now - timedelta(minutes=3)),   # polled after the reset
        "current_5h_pct": 0,
    }}}
    out = cus._apply_countdown_reset_inference(state, {"reset_inference": {"enabled": True}})
    assert out == []
    assert "five_hour_reset_inferred" not in state["accounts"]["a"]


def test_inference_cleared_when_fresh_poll_supersedes():
    now = _now()
    # previously inferred, but now we've polled after the reset → clear markers
    state = {"accounts": {"a": {
        "five_hour_resets_at": _iso(now - timedelta(minutes=70)),
        "last_poll_ts": _iso(now - timedelta(minutes=1)),
        "current_5h_pct": 12,
        "five_hour_reset_inferred": True,
        "five_hour_pct_pre_reset": 96,
    }}}
    cus._apply_countdown_reset_inference(state, {"reset_inference": {"enabled": True}})
    a = state["accounts"]["a"]
    assert "five_hour_reset_inferred" not in a
    assert "five_hour_pct_pre_reset" not in a


def test_inference_disabled_is_noop():
    now = _now()
    state = {"accounts": {"a": {
        "five_hour_resets_at": _iso(now - timedelta(minutes=5)),
        "last_poll_ts": _iso(now - timedelta(minutes=12)),
        "current_5h_pct": 96,
    }}}
    out = cus._apply_countdown_reset_inference(state, {"reset_inference": {"enabled": False}})
    assert out == []
    assert state["accounts"]["a"]["current_5h_pct"] == 96


def test_adaptive_sleep_shortens_to_just_after_reset():
    now = _now()
    state = {"accounts": {"a": {
        "five_hour_resets_at": _iso(now + timedelta(seconds=240)), "current_5h_pct": 50,
    }}}
    s = cus._adaptive_sleep_seconds(state, {"reset_inference": {"adaptive_repoll": True}}, 600)
    assert 255 <= s <= 265   # 240 + 20 buffer


def test_adaptive_sleep_unchanged_when_reset_is_far():
    now = _now()
    state = {"accounts": {"a": {
        "five_hour_resets_at": _iso(now + timedelta(hours=2)), "current_5h_pct": 50,
    }}}
    assert cus._adaptive_sleep_seconds(state, {"reset_inference": {"adaptive_repoll": True}}, 600) == 600


def test_adaptive_sleep_unchanged_when_no_ticking_clock():
    state = {"accounts": {"a": {"current_5h_pct": 0}}}
    assert cus._adaptive_sleep_seconds(state, {"reset_inference": {"adaptive_repoll": True}}, 600) == 600


def test_adaptive_sleep_floored():
    now = _now()
    state = {"accounts": {"a": {
        "five_hour_resets_at": _iso(now + timedelta(seconds=5)), "current_5h_pct": 50,
    }}}
    # 5 + 20 = 25, but floored at min_repoll_seconds (default 30)
    assert cus._adaptive_sleep_seconds(state, {"reset_inference": {"adaptive_repoll": True}}, 600) == 30


def test_adaptive_sleep_disabled_returns_base():
    now = _now()
    state = {"accounts": {"a": {
        "five_hour_resets_at": _iso(now + timedelta(seconds=60)), "current_5h_pct": 50,
    }}}
    assert cus._adaptive_sleep_seconds(state, {"reset_inference": {"adaptive_repoll": False}}, 600) == 600


def _run_all() -> int:
    import types
    tests = [v for k, v in globals().items()
             if k.startswith("test_") and isinstance(v, types.FunctionType)]
    failed = 0
    for t in tests:
        try:
            t()
            print(f"PASS  {t.__name__}")
        except Exception as e:  # noqa: BLE001
            failed += 1
            print(f"FAIL  {t.__name__}: {e}")
    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(_run_all())
