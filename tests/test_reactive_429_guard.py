"""Regression tests for the 2026-06-23 "bouncing onto a full merkos" incident.

Incident: across an afternoon the active credential kept landing on `merkos`
while merkos was correctly polled at 80-100% 5h. Forensics (decisions.jsonl)
showed the polls were FRESH and accurate and `default` had 75-95% headroom every
time — so it was neither stale data nor exhaustion. Two root causes:

  A1. The PostToolUseFailure hook substring-matches "rate limit"/"usage limit"/
      etc. anywhere in a failed tool body, so a downstream API limit or a
      concurrency/RPM 429 from blasting parallel subagents tripped the reactive
      path while `default` sat at 4-23% 5h. An account at 8% is not budget-
      rate-limited; the swap was pointless and landed on a worse account.

  B.  With only two accounts and merkos full, pick_swap_target's degraded
      fallback ("swap somewhere rather than sit on a hot active") happily
      returned merkos at a correctly-polled 100%.

The fixes, pinned here:
  A1 — reactive.min_active_pct: a 429 only triggers a swap if the ACTIVE account
       is itself near its cap (default floor 50%).
  B  — never_swap_to_pct: a hard, no-fallback filter excluding any candidate at
       or above 100% (the picker returns None / stay-put instead).

Run standalone:  python3 tests/test_reactive_429_guard.py
Or under pytest: pytest tests/test_reactive_429_guard.py
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import cus  # noqa: E402


# Self-contained config mirroring the operator's live setup (smart strategy,
# two accounts) but independent of ~/claude-accounts/config.yaml for determinism.
CFG = {
    "strategy": "smart",
    "thresholds": {"five_hour": True, "seven_day": True, "steps": [80, 92]},
    "smart_strategy": {
        "five_hour_headroom_weight": 0.6, "seven_day_headroom_weight": 0.4,
        "hard_7d_cap_pct": 95, "allow_rate_limited_targets": True,
    },
    "swap_hysteresis": {"enabled": True, "min_improvement_pct": 5,
                        "min_seconds_between_swaps": 3000,
                        "min_seconds_between_reactive_swaps": 60},
    "reactive": {"enabled": True, "min_active_pct": 50},   # Fix A1
    "never_swap_to_pct": 100,                              # Fix B
}


def _a(h5, h7, nxt=80):
    # last_swap_ts=None so hysteresis never blocks — we probe the new guards.
    return {"current_5h_pct": h5, "current_7d_pct": h7,
            "next_swap_at_pct": nxt, "last_swap_ts": None}


def _pick(active, accts):
    return cus.pick_swap_target({"active": active, "accounts": accts}, CFG)


# --------------------------------------------------------------------------
# Fix B — never swap onto a 100% account
# --------------------------------------------------------------------------

def test_B_no_swap_onto_saturated_only_candidate():
    """The incident's worst moment: reactive 429 wants a target but the only
    candidate (merkos) is at a correctly-polled 100%. Picker must return None
    (stay put), NOT bounce onto the exhausted account."""
    accts = {"default": _a(23, 24), "merkos": _a(100, 22)}
    assert _pick("default", accts) is None, "must stay put when only target is 100%"


def test_B_does_not_overblock_below_full():
    """The sub-100% gray zone is left alone: merkos at 79% is still a valid
    target (e.g. for burn-before-reset). Fix B only blocks the hard wall."""
    accts = {"default": _a(23, 24), "merkos": _a(79, 22)}
    t = _pick("default", accts)
    assert t is not None and t.name == "merkos", f"merkos@79 should be pickable, got {t}"


def test_B_saturation_on_7d_window_also_blocks():
    """'Full' is either window: an account at 0% 5h but 100% 7d is still a wall."""
    accts = {"default": _a(40, 30), "merkos": _a(0, 100)}
    assert _pick("default", accts) is None, "100% on 7d must block too"


# --------------------------------------------------------------------------
# Fix A1 — reactive 429 only fires when the active account is near its cap
# --------------------------------------------------------------------------

def _reactive(active, accts, monkey_sid="deadbeef-0000-0000-0000-000000000000"):
    """Drive check_rate_limit_reactive with an injected fresh 429 on `active`'s
    session, bypassing the on-disk 429.log / sessions.log."""
    orig_read = cus._read_rate_limit_log_since
    orig_sess = cus._parse_sessions_log
    cus._read_rate_limit_log_since = lambda since: [{"session_id": monkey_sid, "match": "rate limit"}]
    cus._parse_sessions_log = lambda: [{"session_id": monkey_sid, "account": active}]
    try:
        state = {"active": active, "accounts": accts}  # no last_429_check_ts → fresh
        return cus.check_rate_limit_reactive(state, CFG)
    finally:
        cus._read_rate_limit_log_since = orig_read
        cus._parse_sessions_log = orig_sess


def test_A1_429_ignored_when_active_has_headroom():
    """The false-positive case: 429 on `default` while it's at 23% → ignore.
    An 8-23% account is not budget-rate-limited; the 429 is downstream noise."""
    accts = {"default": _a(23, 24), "merkos": _a(10, 22)}
    assert _reactive("default", accts) is None, "429 below min_active_pct must not swap"


def test_A1_429_still_reacts_when_active_near_cap():
    """The legitimate case the reactive path exists for: poll says 70% but a real
    429 hit before the next poll → still react and swap to a target with room."""
    accts = {"default": _a(70, 24), "merkos": _a(10, 22)}
    d = _reactive("default", accts)
    assert d is not None and d.gate == "reactive_429" and d.target == "merkos", \
        f"429 at 70% should react and swap to merkos, got {d}"
    assert d.reactive_entries and d.reactive_entries[0]["session_id"].startswith("deadbeef")


def test_A1_and_B_compose_no_swap_onto_full_even_when_active_hot():
    """Belt and suspenders: even if the active account IS near cap (A1 passes),
    Fix B still refuses to land on a 100% target — hold instead."""
    accts = {"default": _a(85, 24), "merkos": _a(100, 22)}
    assert _reactive("default", accts) is None, "A1 passes but B blocks the 100% target"


def test_global_reactive_ignores_event_from_prior_account_generation():
    state = {"active": "new", "accounts": {
        "new": _a(85, 20), "spare": _a(10, 10),
    }}
    event = {"ts": cus.now_iso(), "session_id": "sid", "match": "rate_limit",
             "source": "stopfailure", "account": "old"}
    assert cus.check_rate_limit_reactive(state, CFG, entries=[event]) is None
    assert not event.get("_retry"), "stale generation is settled rather than replayed"


def test_global_reactive_refuses_degraded_target_and_marks_retry():
    state = {"active": "hot", "accounts": {
        "hot": _a(95, 20), "also-hot": _a(85, 20),
    }}
    event = {"ts": cus.now_iso(), "session_id": "sid", "match": "rate_limit",
             "source": "stopfailure", "account": "hot"}
    assert cus.check_rate_limit_reactive(state, CFG, entries=[event]) is None
    assert event.get("_retry") is True


def test_global_reactive_hysteresis_persists_pending_event():
    sid = "sid"
    state = {"active": "hot", "accounts": {
        "hot": _a(95, 20), "spare": _a(10, 10),
    }}
    state["accounts"]["hot"]["last_swap_ts"] = cus.now_iso()
    original_read = cus._read_rate_limit_log_since
    cus._read_rate_limit_log_since = lambda since: [{
        "ts": cus.now_iso(), "session_id": sid, "match": "rate_limit",
        "source": "stopfailure", "account": "hot",
    }]
    try:
        assert cus.check_rate_limit_reactive(state, CFG) is None
        assert state["pending_429_entries"][0]["account"] == "hot"
    finally:
        cus._read_rate_limit_log_since = original_read


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
