"""Regression tests: PLACEMENT (pick_launch_account) must apply the per-model
(Fable) weekly gate the SWAP path already has — including for STALE readings.

Incident (2026-07-14, the 03/sxe mis-placements): with nearly every account at
Fable 88-100%, every `cus launch auto` kept landing on account `03` — the one
account no slot occupied — whose Fable weekly was MAXED at 100% (past cap 97 /
target cap 95). Each new session immediately hit "You've reached your Fable 5
limit" (slots 2, 8, 13, 14 in one day). Two gaps:

  1. pick_swap_target grew a HARD per-model target filter on 2026-07-05, but
     pick_launch_account's RAW fallback tiers bypass pick_swap_target entirely:
     when the shimmed picker correctly HELD (every non-occupied candidate
     Fable-capped), the raw "lowest estimated usage" fallback re-admitted the
     capped account and placed the new session on it anyway.
  2. _max_model_weekly_from_acct's stale-guard (2026-07-05) reads a token_stale
     account's cached per-model as 0.0 — right for SWAP-target selection (never
     refuse a target on a number we couldn't reconfirm; the lane holds), but
     wrong for PLACEMENT: a new session routed onto an account whose last-known
     Fable is >= cap lands on a maxed account if the stale number was still
     true, and there is no current account to hold on.

Invariants pinned here:
  - Placement excludes any account whose LAST-KNOWN per-model weekly is at/over
    the target cap, fresh OR stale (trust_stale=True read).
  - When NO Fable-clean account exists anywhere (genuine fleet saturation),
    placement degrades to the LOWEST last-known Fable — annotated "[DEGRADED:"
    — rather than refusing to place or silently picking the maxed account.
  - Gate off (standard pool / unmodified installs) is byte-identical to the
    pre-fix behavior: the capped set is empty and every tier is unchanged.
  - The SWAP-side stale-guard is untouched: _max_model_weekly_from_acct's
    default call still reads a stale account as 0.0
    (test_premium_fanout_fable_cap pins the picker side of that).

Run standalone:  python3 tests/test_launch_fable_gate.py  (pytest-only: uses monkeypatch)
Or under pytest: pytest tests/test_launch_fable_gate.py
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import cus  # noqa: E402


def _a(h5, h7, nxt=90, model=None, **flags):
    acct = {"current_5h_pct": h5, "current_7d_pct": h7,
            "next_swap_at_pct": nxt, "last_swap_ts": None}
    if model is not None:
        acct["per_model_weekly_pct"] = model
    acct.update(flags)
    return acct


def _cfg(gate=True, lane_sharing=False):
    cfg = {
        "strategy": "lowest_usage",
        "thresholds": {"five_hour": True, "seven_day": True, "steps": [90, 96]},
        "smart_strategy": {"hard_7d_cap_pct": 80},
        "swap_hysteresis": {"enabled": False},
        "usage_growth_gate": {"enabled": False},
        "per_session": {"lane_sharing": lane_sharing},
    }
    if gate:
        cfg["per_model_weekly"] = {"gate_enabled": True, "models": ["fable"],
                                   "cap_pct": 97, "target_cap_pct": 95}
    return cfg


def _isolate(monkeypatch, live: set):
    """No real /proc or mount checks: stub live-occupancy to `live`."""
    monkeypatch.setattr(cus, "mount_in_use", lambda d: False)
    monkeypatch.setattr(cus, "_live_slot_accounts", lambda state: set(live))


# --------------------------------------------------------------------------
# trust_stale unit behavior (the new _max_model_weekly_from_acct mode).
# --------------------------------------------------------------------------

def test_trust_stale_reads_last_known_number():
    cfg = _cfg()
    stale = _a(1, 1, model={"Fable": 100.0}, token_stale=True)
    # Default (swap-side) call preserves the 2026-07-05 stale-guard: 0.0.
    assert cus._max_model_weekly_from_acct(stale, cfg) == 0.0
    # Placement-side call reads the last-known number regardless of staleness.
    assert cus._max_model_weekly_from_acct(stale, cfg, trust_stale=True) == 100.0
    # Gate off: 0.0 either way (the inert-on-unmodified-installs property).
    off = _cfg(gate=False)
    assert cus._max_model_weekly_from_acct(stale, off, trust_stale=True) == 0.0


# --------------------------------------------------------------------------
# THE INCIDENT: the only unoccupied account is Fable-maxed; clean accounts are
# all on live lanes. Placement must NOT mint a new mount on the maxed account.
# --------------------------------------------------------------------------

def test_launch_never_lands_on_fable_maxed_unoccupied_account(monkeypatch):
    """`o3` is the one free account (spread-optimal, 5h low) but Fable=100%.
    alpha/beta are Fable-clean on live lanes. With lane_sharing on, the launch
    must JOIN a clean lane rather than land fresh on the Fable-dead account —
    pre-fix the raw fallback returned o3 ("lowest estimated usage")."""
    _isolate(monkeypatch, live={"alpha", "beta"})
    state = {
        "active": None,
        "slots": {"slot-1": {"account": "alpha"}, "slot-2": {"account": "beta"}},
        "accounts": {
            "alpha": _a(40, 30, model={"Fable": 60.0}),
            "beta": _a(55, 35, model={"Fable": 70.0}),
            "o3": _a(2, 20, model={"Fable": 100.0}),
        },
    }
    t = cus.pick_launch_account(state, _cfg(lane_sharing=True))
    assert t is not None, "must still place"
    assert t.name != "o3", f"landed on the Fable-maxed account: {t.reason}"
    assert t.name == "alpha", f"expected lowest-usage clean lane join, got {t.name}"


def test_launch_stale_high_fable_excluded_from_placement(monkeypatch):
    """`o3` is idle with a STALE cached Fable=100% (token_stale) and stale-low
    aggregate numbers that make it the scoring winner. The swap-side stale-guard
    reads its per-model as 0.0, so pre-fix the shimmed picker chose it. For
    placement, stale-high must be excluded: the launch goes to the clean (but
    busier-looking) beta instead."""
    _isolate(monkeypatch, live=set())
    state = {
        "active": None,
        "slots": {},
        "accounts": {
            "o3": _a(1, 1, model={"Fable": 100.0}, token_stale=True),
            "beta": _a(50, 40, model={"Fable": 30.0}),
        },
    }
    t = cus.pick_launch_account(state, _cfg())
    assert t is not None and t.name == "beta", (
        f"stale-high Fable must not win placement: {t and t.name} ({t and t.reason})")


# --------------------------------------------------------------------------
# Genuine fleet-wide Fable saturation: degrade to least-bad, annotated —
# never refuse to place, never silently pick the maxed account.
# --------------------------------------------------------------------------

def test_launch_fleet_saturated_picks_least_bad_annotated(monkeypatch):
    """Every account is at/over the 95% target cap (o3=100 fresh, alpha=96
    stale). The launch must still place — on the LOWEST last-known Fable
    (alpha) — with a [DEGRADED: ...] annotation, not on the maxed o3."""
    _isolate(monkeypatch, live=set())
    state = {
        "active": None,
        "slots": {},
        "accounts": {
            "o3": _a(2, 20, model={"Fable": 100.0}),
            "alpha": _a(30, 25, model={"Fable": 96.0}, token_stale=True),
        },
    }
    t = cus.pick_launch_account(state, _cfg())
    assert t is not None, "fleet saturation must degrade, not refuse to place"
    assert t.name == "alpha", f"expected lowest last-known Fable, got {t.name}"
    assert "[DEGRADED:" in t.reason and "per-model weekly cap" in t.reason, t.reason


def test_launch_fleet_saturated_lane_share_least_bad(monkeypatch):
    """Saturated fleet where the lowest-Fable account is on a LIVE lane and
    lane_sharing is on: the degraded tier may join it (Fable 96 join beats a
    fresh mount on Fable-100 o3)."""
    _isolate(monkeypatch, live={"merkos"})
    state = {
        "active": None,
        "slots": {"slot-1": {"account": "merkos"}},
        "accounts": {
            "o3": _a(2, 20, model={"Fable": 100.0}),
            "merkos": _a(51, 55, model={"Fable": 96.0}),
        },
    }
    t = cus.pick_launch_account(state, _cfg(lane_sharing=True))
    assert t is not None and t.name == "merkos", (
        f"expected least-bad lane join, got {t and t.name} ({t and t.reason})")
    assert "[DEGRADED:" in t.reason, t.reason


def test_launch_saturated_fleet_without_lane_sharing_still_places(monkeypatch):
    """lane_sharing off + only free account is capped: still place (on it),
    annotated — matching the task's 'least-bad beats no-placement' rule."""
    _isolate(monkeypatch, live={"merkos"})
    state = {
        "active": None,
        "slots": {"slot-1": {"account": "merkos"}},
        "accounts": {
            "o3": _a(2, 20, model={"Fable": 100.0}),
            "merkos": _a(51, 55, model={"Fable": 88.0}),
        },
    }
    t = cus.pick_launch_account(state, _cfg(lane_sharing=False))
    assert t is not None and t.name == "o3", (
        f"only reachable account must still be placed on: {t and t.name}")
    assert "[DEGRADED:" in t.reason, t.reason


# --------------------------------------------------------------------------
# Gate off: byte-identical pre-fix behavior (inert on unmodified installs).
# --------------------------------------------------------------------------

def test_gate_off_placement_ignores_fable_unchanged(monkeypatch):
    """Same incident fleet with the gate OFF (standard pool / unmodified
    install): o3's Fable=100% is ignored and it wins on lowest usage, with the
    ordinary un-annotated reason — the pre-2026-07-14 behavior."""
    _isolate(monkeypatch, live=set())
    state = {
        "active": None,
        "slots": {},
        "accounts": {
            "o3": _a(2, 20, model={"Fable": 100.0}),
            "beta": _a(50, 40, model={"Fable": 30.0}),
        },
    }
    t = cus.pick_launch_account(state, _cfg(gate=False))
    assert t is not None and t.name == "o3", t and t.name
    assert "[DEGRADED:" not in t.reason, t.reason


if __name__ == "__main__":
    # monkeypatch-dependent tests run under pytest; only the pure unit here.
    test_trust_stale_reads_last_known_number()
    print("ok")
