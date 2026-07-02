"""Tests for the 2026-07-02 differential poll cadence + per-model weekly tracking.

Pins the two features added by PR #86:

1. Differential cadence — the ACTIVE account polls on the fast
   `polling.active_interval_seconds`, idle accounts on the slow
   `polling.inactive_interval_seconds`, both falling back to the legacy flat
   `poll_interval_seconds` (backward-compat). Due-check fails OPEN (poll) on
   missing/unparseable timestamps. Motivated by the 2026-07-02 self-inflicted
   429 storm: flat 60s x 5 accounts throttled every account into poll-backoff.

2. Per-model WEEKLY tracking (fable/sonnet) — parsed from the usage API's
   `limits[]` model-scoped weekly entries plus the legacy seven_day_sonnet /
   seven_day_opus fields; persisted to state.json on successful polls only;
   folded into the weekly-cap gate ONLY when `per_model_weekly.gate_enabled`
   is true (default off = byte-for-byte old behavior).

Also pins the review fix from the PR #86 merge review: SOS Condition 4
(stale poll) judges staleness against each account's OWN cadence, not the
flat interval — otherwise a slow inactive cadence would permanently flag
every idle account as stale (false daemon-down SOS noise).
"""

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import cus  # noqa: E402


def _iso(dt):
    return dt.isoformat().replace("+00:00", "Z")


NOW = datetime.now(timezone.utc)

# Differential config: fast active, slow inactive, flat fallback distinct
# from both so tests can tell WHICH interval was picked.
DIFF_CFG = {
    "poll_interval_seconds": 300,
    "polling": {"active_interval_seconds": 45, "inactive_interval_seconds": 600},
    "thresholds": {"five_hour": True, "seven_day": True, "steps": [50, 75, 90]},
}
FLAT_CFG = {
    "poll_interval_seconds": 300,
    "thresholds": {"five_hour": True, "seven_day": True, "steps": [50, 75, 90]},
}


def _state(active="a", accounts=None):
    return {"active": active, "accounts": accounts or {}}


# --------------------------------------------------------------------------
# _account_poll_interval — class resolution + flat fallback
# --------------------------------------------------------------------------

def test_active_account_gets_fast_interval():
    st = _state(active="a", accounts={"a": {}, "b": {}})
    assert cus._account_poll_interval(st, DIFF_CFG, "a") == 45


def test_inactive_account_gets_slow_interval():
    st = _state(active="a", accounts={"a": {}, "b": {}})
    assert cus._account_poll_interval(st, DIFF_CFG, "b") == 600


def test_unset_differential_keys_fall_back_to_flat():
    # Backward-compat: an unmodified config polls everything on the flat
    # interval — identical to pre-2026-07-02 behavior.
    st = _state(active="a", accounts={"a": {}, "b": {}})
    assert cus._account_poll_interval(st, FLAT_CFG, "a") == 300
    assert cus._account_poll_interval(st, FLAT_CFG, "b") == 300


# --------------------------------------------------------------------------
# _account_poll_due — elapsed-vs-interval + fail-open
# --------------------------------------------------------------------------

def test_due_when_never_polled():
    st = _state(accounts={"a": {}})
    due, why = cus._account_poll_due(st, DIFF_CFG, "a")
    assert due and "no prior poll" in why


def test_due_fails_open_on_unparseable_timestamp():
    # A corrupt last_poll_ts must POLL (observe) rather than starve the
    # account of polling forever.
    st = _state(accounts={"a": {"last_poll_ts": "garbage"}})
    due, why = cus._account_poll_due(st, DIFF_CFG, "a")
    assert due and "unparseable" in why


def test_active_due_after_fast_interval_elapsed():
    st = _state(active="a", accounts={"a": {"last_poll_ts": _iso(NOW - timedelta(seconds=50))}})
    due, why = cus._account_poll_due(st, DIFF_CFG, "a")
    assert due and "active" in why


def test_inactive_not_due_before_slow_interval():
    # 50s ago: past the ACTIVE interval but far short of the 600s inactive
    # one — the differential schedule must skip the idle account.
    st = _state(active="a", accounts={
        "a": {"last_poll_ts": _iso(NOW - timedelta(seconds=50))},
        "b": {"last_poll_ts": _iso(NOW - timedelta(seconds=50))},
    })
    due, why = cus._account_poll_due(st, DIFF_CFG, "b")
    assert not due and "inactive" in why


def test_inactive_due_after_slow_interval():
    st = _state(active="a", accounts={"b": {"last_poll_ts": _iso(NOW - timedelta(seconds=700))}})
    due, _ = cus._account_poll_due(st, DIFF_CFG, "b")
    assert due


def test_flat_config_polls_everyone_each_interval():
    # Backward-compat: with no polling.* keys, both roles are due after the
    # flat interval and not before.
    st = _state(active="a", accounts={
        "a": {"last_poll_ts": _iso(NOW - timedelta(seconds=310))},
        "b": {"last_poll_ts": _iso(NOW - timedelta(seconds=290))},
    })
    assert cus._account_poll_due(st, FLAT_CFG, "a")[0]
    assert not cus._account_poll_due(st, FLAT_CFG, "b")[0]


def test_5h_rollover_forces_due_despite_slow_cadence():
    """GH #59 composition (review amendment): _adaptive_sleep_seconds wakes
    the daemon just after the soonest 5h reset SO THAT the reset account gets
    repolled promptly. The due-gate must therefore treat 'window rolled over
    since last poll' as due regardless of cadence class — otherwise the early
    wake finds the account not-due, skips it, and the daemon flies on the
    countdown-inferred 0% for up to a whole inactive interval."""
    reset_at = NOW - timedelta(minutes=10)
    st = _state(active="a", accounts={"a": {}, "b": {
        "last_poll_ts": _iso(NOW - timedelta(minutes=20)),  # predates the reset
        "five_hour_resets_at": _iso(reset_at),
        "current_5h_pct": 96.0,
    }})
    due, why = cus._account_poll_due(st, DIFF_CFG, "b")
    assert due and "5h window reset" in why


def test_no_rollover_override_once_post_reset_state_observed():
    # The last poll happened AFTER the reset, so the stored 0% is real —
    # the slow inactive cadence applies again (no poll-storm after resets).
    reset_at = NOW - timedelta(minutes=10)
    st = _state(active="a", accounts={"a": {}, "b": {
        "last_poll_ts": _iso(NOW - timedelta(minutes=5)),  # after the reset
        "five_hour_resets_at": _iso(reset_at),
        "current_5h_pct": 0.0,
    }})
    due, _ = cus._account_poll_due(st, DIFF_CFG, "b")
    assert not due


# --------------------------------------------------------------------------
# _parse_per_model_weekly — API-shape parsing
# --------------------------------------------------------------------------

def test_parse_limits_model_scoped_weekly():
    data = {
        "five_hour": {"utilization": 10, "resets_at": "2026-07-02T10:00:00Z"},
        "seven_day": {"utilization": 40, "resets_at": "2026-07-08T00:00:00Z"},
        "limits": [
            {"group": "weekly", "scope": {"model": {"display_name": "Fable"}},
             "percent": 62.5, "resets_at": "2026-07-08T00:00:00Z"},
            # 5h/aggregate entries must be ignored: per-model is weekly-only.
            {"group": "five_hour", "scope": {"model": {"display_name": "Fable"}},
             "percent": 99},
            {"group": "weekly", "percent": 40},  # no model scope → aggregate, skip
        ],
    }
    out = cus._parse_per_model_weekly(data)
    assert set(out) == {"Fable"}
    assert out["Fable"].utilization == 62.5
    assert out["Fable"].resets_at == "2026-07-08T00:00:00Z"


def test_parse_legacy_sonnet_opus_fields():
    data = {
        "seven_day_sonnet": {"utilization": 33, "resets_at": "2026-07-08T00:00:00Z"},
        "seven_day_opus": {"utilization": 71, "resets_at": None},
    }
    out = cus._parse_per_model_weekly(data)
    assert out["Sonnet"].utilization == 33.0
    assert out["Opus"].utilization == 71.0


def test_parse_limits_overrides_legacy_on_conflict():
    # limits[] is the live structured source; legacy fields only fill gaps.
    data = {
        "seven_day_sonnet": {"utilization": 33},
        "limits": [{"group": "weekly", "scope": {"model": {"display_name": "Sonnet"}},
                    "percent": 55}],
    }
    out = cus._parse_per_model_weekly(data)
    assert out["Sonnet"].utilization == 55.0


def test_parse_malformed_entries_never_raise():
    # The API shape is in flux; a schema tweak must not take polling down.
    data = {
        "limits": [
            None, 42, "x",
            {"group": "weekly", "scope": "not-a-dict", "percent": 10},
            {"group": "weekly", "scope": {"model": {"display_name": None}}, "percent": 10},
            {"group": "weekly", "scope": {"model": {"display_name": "Fable"}}},  # no percent
        ],
        "seven_day_sonnet": "not-a-dict",
    }
    assert cus._parse_per_model_weekly(data) == {}


# --------------------------------------------------------------------------
# gate resolution + folding into decisions
# --------------------------------------------------------------------------

def _usage(per_model=None, five_hour=30.0, seven_day=40.0):
    return cus.AccountUsage(
        five_hour=cus.UsageWindow(utilization=five_hour, resets_at=None),
        seven_day=cus.UsageWindow(utilization=seven_day, resets_at=None),
        per_model_weekly=per_model or {},
    )


GATE_ON = dict(DIFF_CFG, per_model_weekly={"gate_enabled": True, "models": []})
GATE_ALLOW = dict(DIFF_CFG, per_model_weekly={"gate_enabled": True, "models": ["Fable"]})
GATE_OFF = dict(DIFF_CFG, per_model_weekly={"gate_enabled": False, "models": []})


def test_gate_off_is_a_noop_on_current_max_pct():
    u = _usage(per_model={"Fable": cus.UsageWindow(utilization=95.0, resets_at=None)})
    # Gate off: only aggregate 5h/7d count — pre-2026-07-02 behavior.
    assert cus.current_max_pct(u, GATE_OFF) == 40.0
    assert cus.current_max_pct(u, DIFF_CFG) == 40.0  # key absent entirely


def test_ladder_signal_excludes_per_model_week():
    # Behavior change 2026-07-02 (supersedes the prior fold): current_max_pct
    # is the progressive-LADDER signal and no longer counts per-model weekly,
    # even with the gate on. A Fable week at 95% must NOT drag the ladder
    # signal up to 95% — that would trip a ladder swap at steps[0] long before
    # Fable's own cap. Per-model is enforced on the hard-cap path instead.
    u = _usage(per_model={"Fable": cus.UsageWindow(utilization=95.0, resets_at=None),
                          "Sonnet": cus.UsageWindow(utilization=20.0, resets_at=None)})
    assert cus.current_max_pct(u, GATE_ON) == 40.0   # aggregate 7d only
    assert cus.current_max_pct(u, GATE_OFF) == 40.0


def test_allowlist_filters_models_in_hard_cap_signal():
    # The models allowlist still governs WHICH model feeds the hard-cap signal
    # (_max_model_weekly_from_usage), which is where per-model now acts. Sonnet
    # is not allowlisted, so only fable's 50% counts.
    u = _usage(per_model={"Sonnet": cus.UsageWindow(utilization=95.0, resets_at=None),
                          "fable": cus.UsageWindow(utilization=50.0, resets_at=None)})
    assert cus._max_model_weekly_from_usage(u, GATE_ALLOW) == 50.0
    # ...and it does NOT leak into the ladder signal.
    assert cus.current_max_pct(u, GATE_ALLOW) == 40.0


def test_effective_pct_excludes_per_model_week():
    # Dict-level twin of the ladder signal: also no longer folds per-model
    # weekly (2026-07-02). The hard-cap target filter in pick_swap_target
    # excludes model-exhausted candidates on its own.
    acct = {"current_5h_pct": 10.0, "current_7d_pct": 20.0,
            "per_model_weekly_pct": {"Fable": 88.0}}
    assert cus._account_effective_pct(acct, GATE_ON) == 20.0
    assert cus._account_effective_pct(acct, GATE_OFF) == 20.0


def test_pick_swap_target_excludes_model_week_exhausted_target_when_gated():
    # Target 'b' has aggregate headroom but its Fable week is exhausted; with
    # the gate on it must not be picked over 'c'. With the gate off the old
    # aggregate-only filter applies and 'b' (lower aggregate) wins.
    accounts = {
        "a": {"current_5h_pct": 90.0, "current_7d_pct": 50.0, "next_swap_at_pct": 50},
        "b": {"current_5h_pct": 5.0, "current_7d_pct": 10.0, "next_swap_at_pct": 50,
              "per_model_weekly_pct": {"Fable": 97.0}},
        "c": {"current_5h_pct": 10.0, "current_7d_pct": 30.0, "next_swap_at_pct": 50},
    }
    st = _state(active="a", accounts=accounts)
    cfg_on = dict(GATE_ON, strategy="lowest_usage")
    cfg_off = dict(GATE_OFF, strategy="lowest_usage")
    assert cus.pick_swap_target(st, cfg_on).name == "c"
    assert cus.pick_swap_target(st, cfg_off).name == "b"


def test_decide_swap_forces_off_active_when_model_week_exhausted():
    # Active 'a' has aggregate 7d headroom (40%) but its Fable week is at 96%
    # — with the gate on, the hard-7d-cap trigger must fire.
    accounts = {
        "a": {"current_5h_pct": 30.0, "current_7d_pct": 40.0, "next_swap_at_pct": 90},
        "b": {"current_5h_pct": 5.0, "current_7d_pct": 10.0, "next_swap_at_pct": 50},
    }
    st = _state(active="a", accounts=accounts)
    usage = {"a": _usage(per_model={"Fable": cus.UsageWindow(utilization=96.0, resets_at=None)})}
    cfg_on = dict(GATE_ON, strategy="smart",
                  smart_strategy={"hard_7d_cap_pct": 80},
                  swap_hysteresis={"enabled": False})
    cfg_off = dict(GATE_OFF, strategy="smart",
                   smart_strategy={"hard_7d_cap_pct": 80},
                   swap_hysteresis={"enabled": False})
    decision = cus.decide_swap(st, cfg_on, usage)
    assert decision is not None and decision.gate == "hard_7d_cap" and decision.target == "b"
    # Gate off: same numbers, no swap (aggregate 40% < 80% cap, ladder at 90%).
    assert cus.decide_swap(st, cfg_off, usage) is None


# --------------------------------------------------------------------------
# update_state_with_usage — persistence semantics
# --------------------------------------------------------------------------

def test_per_model_pct_persisted_on_success(monkeypatch):
    monkeypatch.setattr(cus, "load_config", lambda: dict(DIFF_CFG))
    st = _state(accounts={"a": {}})
    u = _usage(per_model={"Fable": cus.UsageWindow(utilization=62.5, resets_at=None)})
    cus.update_state_with_usage(st, {"a": u})
    assert st["accounts"]["a"]["per_model_weekly_pct"] == {"Fable": 62.5}


def test_per_model_pct_preserved_on_poll_error(monkeypatch):
    # Error branches preserve the prior dict — same preserve-on-error
    # semantics as current_*_pct (stale beats absent for target selection).
    monkeypatch.setattr(cus, "load_config", lambda: dict(DIFF_CFG))
    st = _state(accounts={"a": {"per_model_weekly_pct": {"Fable": 62.5}}})
    err = cus.AccountUsage(raw={"error": "boom"})
    cus.update_state_with_usage(st, {"a": err})
    assert st["accounts"]["a"]["per_model_weekly_pct"] == {"Fable": 62.5}


def test_per_model_pct_cleared_when_api_reports_none(monkeypatch):
    # A successful poll with no model-scoped limits clears the dict — an
    # expired/removed model cap must not gate forever on a stale value.
    monkeypatch.setattr(cus, "load_config", lambda: dict(DIFF_CFG))
    st = _state(accounts={"a": {"per_model_weekly_pct": {"Fable": 62.5}}})
    cus.update_state_with_usage(st, {"a": _usage(per_model={})})
    assert st["accounts"]["a"]["per_model_weekly_pct"] == {}


# --------------------------------------------------------------------------
# SOS Condition 4 — staleness judged per-account cadence (merge-review fix)
# --------------------------------------------------------------------------

def _stale_conditions(state, config):
    return [c for c in cus.diagnose(state=state, config=config)
            if c.summary.startswith("Stale usage data")]


def test_idle_account_on_slow_cadence_is_not_stale():
    # 25 min since last poll: >4x the flat 300s interval, but well within 4x
    # the 600s inactive cadence. Pre-fix this fired a false "daemon down"
    # warning on every idle account; post-fix it must stay quiet.
    st = _state(active="a", accounts={
        "a": {"last_poll_ts": _iso(NOW - timedelta(seconds=60)), "next_swap_at_pct": 50},
        "b": {"last_poll_ts": _iso(NOW - timedelta(minutes=25)), "next_swap_at_pct": 50},
    })
    assert _stale_conditions(st, DIFF_CFG) == []


def test_active_account_stale_past_4x_fast_cadence():
    # Active on a 45s cadence, silent for 10 min (>4x45s) → real staleness.
    st = _state(active="a", accounts={
        "a": {"last_poll_ts": _iso(NOW - timedelta(minutes=10)), "next_swap_at_pct": 50},
    })
    conds = _stale_conditions(st, DIFF_CFG)
    assert len(conds) == 1 and "a" in conds[0].summary


def test_flat_config_staleness_unchanged():
    # Backward-compat: no polling.* keys → 4x flat interval, as before.
    st = _state(active="a", accounts={
        "a": {"last_poll_ts": _iso(NOW - timedelta(minutes=25)), "next_swap_at_pct": 50},
    })
    conds = _stale_conditions(st, FLAT_CFG)
    assert len(conds) == 1


# --------------------------------------------------------------------------
# per_model_weekly.cap_pct — separate threshold for the model gate
# --------------------------------------------------------------------------

def _cap_cfg(cap_pct, hard_cap=80, steps=None):
    """Gate on, model cap set explicitly, aggregate hard cap independent."""
    return dict(
        GATE_ON,
        strategy="smart",
        smart_strategy={"hard_7d_cap_pct": hard_cap},
        per_model_weekly={"gate_enabled": True, "models": [], "cap_pct": cap_pct},
        swap_hysteresis={"enabled": False},
        thresholds={"five_hour": True, "seven_day": True, "steps": steps or [50, 75, 90]},
    )


def test_model_cap_pct_none_inherits_hard_cap():
    assert cus._model_weekly_cap_for_config(_cap_cfg(None, hard_cap=80)) == 80
    assert cus._model_weekly_cap_for_config(_cap_cfg(90, hard_cap=80)) == 90


def test_decide_swap_model_cap_pct_trips_independently_of_hard_cap():
    # Aggregate 7d 40% is under the 80% hard cap; Fable week 85% is under a
    # 90% model cap → hold. Raise Fable to 92% → the model cap (not the
    # aggregate one) force-swaps. Ladder is parked at 95 so only the hard-cap
    # trigger can fire.
    accounts = {
        "a": {"current_5h_pct": 30.0, "current_7d_pct": 40.0, "next_swap_at_pct": 95},
        "b": {"current_5h_pct": 5.0, "current_7d_pct": 10.0, "next_swap_at_pct": 50},
    }
    cfg = _cap_cfg(90, hard_cap=80)
    st = _state(active="a", accounts=accounts)
    usage_under = {"a": _usage(per_model={"Fable": cus.UsageWindow(utilization=85.0, resets_at=None)})}
    assert cus.decide_swap(st, cfg, usage_under) is None
    usage_over = {"a": _usage(per_model={"Fable": cus.UsageWindow(utilization=92.0, resets_at=None)})}
    decision = cus.decide_swap(st, cfg, usage_over)
    assert decision is not None and decision.gate == "hard_7d_cap" and decision.target == "b"


def test_pick_swap_target_filters_on_model_cap_not_hard_cap():
    # Sole candidate 'b' has Fable at 85% (aggregate 10%). Under a 90% model
    # cap it is a SAFE pick (no degraded fallback); once the model cap drops
    # to 80% it is filtered and only survives via the degraded "no targets
    # below cap" fallback. Ladder steps parked at [95] so the would-re-trip
    # filter stays out of the way — this isolates the cap filter itself.
    accounts = {
        "a": {"current_5h_pct": 90.0, "current_7d_pct": 50.0, "next_swap_at_pct": 95},
        "b": {"current_5h_pct": 5.0, "current_7d_pct": 10.0, "next_swap_at_pct": 95,
              "per_model_weekly_pct": {"Fable": 85.0}},
    }
    st = _state(active="a", accounts=accounts)
    loose = dict(_cap_cfg(90, steps=[95]), strategy="lowest_usage")
    tight = dict(_cap_cfg(80, steps=[95]), strategy="lowest_usage")
    picked_loose = cus.pick_swap_target(st, loose)
    picked_tight = cus.pick_swap_target(st, tight)
    assert picked_loose.name == "b" and "[DEGRADED:" not in picked_loose.reason
    assert picked_tight.name == "b" and "no targets below 7d cap" in picked_tight.reason


def test_per_model_does_not_ladder_below_its_cap():
    # The user's exact ask: with steps [80, 90] and Fable capped at 97, Fable
    # climbing to 85% or 92% must NOT trigger a ladder swap — the account
    # switches (and leaves rotation) ONLY at 97%. Aggregate 7d is low the whole
    # time, so nothing but per-model could trip. Ladder step sits at 80.
    accounts = {
        "a": {"current_5h_pct": 10.0, "current_7d_pct": 15.0, "next_swap_at_pct": 80},
        "b": {"current_5h_pct": 5.0, "current_7d_pct": 10.0, "next_swap_at_pct": 50},
    }
    cfg = _cap_cfg(97, hard_cap=80, steps=[80, 90])
    st = _state(active="a", accounts=accounts)
    # Fable at 85% and 92%: over steps[0]=80 but under the 97 cap → HOLD
    # (pre-2026-07-02 this laddered at 80%).
    for fable in (85.0, 92.0):
        u = {"a": _usage(five_hour=10.0, seven_day=15.0,
                         per_model={"Fable": cus.UsageWindow(utilization=fable, resets_at=None)})}
        assert cus.decide_swap(st, cfg, u) is None, f"Fable {fable}% must not ladder below cap"
    # Fable at 97%: hard-cap force-swap fires.
    u = {"a": _usage(five_hour=10.0, seven_day=15.0,
                     per_model={"Fable": cus.UsageWindow(utilization=97.0, resets_at=None)})}
    d = cus.decide_swap(st, cfg, u)
    assert d is not None and d.gate == "hard_7d_cap" and d.target == "b"
