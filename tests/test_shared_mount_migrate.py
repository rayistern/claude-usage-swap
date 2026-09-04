"""Tests for AUTO-MIGRATION of the shared bare ~/.claude mount off an unusable
account (2026-09-03, the account-03 subscription-ended incident).

THE HOLE: cus's subscription_guard already auto-DETECTS a subscription-ended
account and auto-EXCLUDES it from FUTURE rotation (test_subscription_disabled),
and decide_swap Trigger 0 / the evacuate sweep move live LANES off a disabled
account. But nothing moved the already-live SHARED bare mount off its account
when THAT account died — so every bare session kept riding it and errored
"organization has disabled Claude subscription access for Claude Code" (03's
subscription ended) or "Login expired" (rayi1's canonical refresh was dead) until
an operator moved the mount by hand. The shared mount was the one uncovered mount.

The contract pinned here:
  (1) `_shared_mount_account_unhealthy` flags the shared-mount account when it is
      subscription-ended / operator-disabled (via _disabled_accounts) OR its
      canonical refresh is dead (the cached snapshot_refresh_dead flag) — and
      returns None for a healthy account;
  (2) the hybrid cycle, when its account is unhealthy and no other swap already
      claims the mount, picks a HEALTHY target with pick_swap_target and — crucially
      — re-probes that target with _account_snapshot_dead, so it never installs a
      dead canonical onto the shared mount (which has no login-family seed path and
      would BLANK — the rayi1 trap);
  (3) when no healthy slot-free target exists it does NOT silently hold — it leaves
      the decision None so the URGENT surface fires (free a slot / relogin);
  (4) the install primitive itself REFUSES to switch the shared mount (slot=None)
      onto a dead-canonical account rather than blanking it — closing the trap for
      the manual `cus switch` path too;
  (5) subscription_guard.auto_migrate_shared_mount=False restores exclude-only
      behavior; the gate is independent of the master guard.

Run standalone:  python3 tests/test_shared_mount_migrate.py
Run under pytest: pytest tests/test_shared_mount_migrate.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))  # tests/ dir — sibling harness

import cus  # noqa: E402
# Reuse the on-disk swap sandbox from the dead-snapshot suite (slots, families,
# grant-map, click.echo capture) rather than duplicate ~130 lines of _Env.
from test_dead_snapshot_family_seed import (  # noqa: E402
    _Env, _FUTURE, _dead_snapshot, _valid, _grant_map, _ILGATE)


def _point_live_mount(monkeypatch, tmp_path, creds):
    """Point cus.CREDS_JSON (the live shared ~/.claude/.credentials.json) at a temp
    file so the FIX 1 (F-F-1) live-mount self-heal check is hermetic. `creds=None`
    leaves the file absent → the detector reads it as unusable (genuinely stuck)."""
    p = tmp_path / ".credentials.json"
    if creds is not None:
        p.write_text(json.dumps(creds))
    monkeypatch.setattr(cus, "CREDS_JSON", p)
    return p


# ---------------------------------------------------------------------------
# helpers (minimal, mirror test_subscription_disabled)
# ---------------------------------------------------------------------------

def _healthy(h5=10.0, h7=10.0):
    return {"current_5h_pct": h5, "current_7d_pct": h7,
            "next_swap_at_pct": 80, "last_swap_ts": None}


def _sub_dead():
    return {"current_5h_pct": 0.0, "current_7d_pct": 0.0, "next_swap_at_pct": 80,
            "last_swap_ts": None, "subscription_disabled": True, "rate_limited": True}


def _dead_refresh():
    return {"current_5h_pct": 0.0, "current_7d_pct": 0.0, "next_swap_at_pct": 80,
            "last_swap_ts": None, "snapshot_refresh_dead": True}


def _cfg(accounts=("alpha", "beta", "gamma"), guard=None, disabled=()):
    accts = [{"name": n, "priority": 1,
              **({"disabled": True} if n in disabled else {})} for n in accounts]
    cfg = {"strategy": "smart", "accounts": accts,
           "thresholds": {"five_hour": True, "seven_day": True, "steps": [80, 90]},
           "swap_hysteresis": {"enabled": False}, "usage_growth_gate": {"enabled": False}}
    if guard is not None:
        cfg["subscription_guard"] = guard
    return cfg


# ---------------------------------------------------------------------------
# (5) the gate flag
# ---------------------------------------------------------------------------

def test_gate_default_on():
    assert cus._auto_migrate_shared_mount_enabled({}) is True


def test_gate_explicit_off():
    assert cus._auto_migrate_shared_mount_enabled(
        {"subscription_guard": {"auto_migrate_shared_mount": False}}) is False


def test_gate_independent_of_master_guard():
    # dead-refresh migration must work even with the subscription master guard off.
    assert cus._auto_migrate_shared_mount_enabled(
        {"subscription_guard": {"enabled": False, "auto_migrate_shared_mount": True}}) is True


# ---------------------------------------------------------------------------
# (1) the unhealthy detector
# ---------------------------------------------------------------------------

def test_detect_operator_disabled():
    state = {"active": "03", "accounts": {"03": _healthy()}}
    cfg = _cfg(accounts=("03", "alpha"), disabled=("03",))
    assert "disabled" in (cus._shared_mount_account_unhealthy("03", state, cfg) or "")


def test_detect_subscription_disabled_flag():
    state = {"active": "x", "accounts": {"x": _sub_dead()}}
    cfg = _cfg(accounts=("x", "alpha"), guard={"enabled": True})
    r = cus._shared_mount_account_unhealthy("x", state, cfg)
    assert r and "subscription-ended" in r


def test_detect_dead_canonical_refresh(monkeypatch, tmp_path):
    # FIX 2026-09-04 (F-F-1): a dead canonical is "unhealthy" only when the LIVE
    # mount ALSO can't self-heal. Here the live mount file is absent → genuinely
    # stuck → flagged (the rayi1 trap: dead canonical + no live mount to heal from).
    _point_live_mount(monkeypatch, tmp_path, None)  # missing live mount → unusable
    state = {"active": "rayi1", "accounts": {"rayi1": _dead_refresh()}}
    cfg = _cfg(accounts=("rayi1", "alpha"), guard={"enabled": True})
    r = cus._shared_mount_account_unhealthy("rayi1", state, cfg)
    assert r and "canonical refresh token is dead" in r


def test_dead_refresh_but_live_mount_self_heals_is_none(monkeypatch, tmp_path):
    # FIX 2026-09-04 (F-F-1) REGRESSION: snapshot_refresh_dead set on the shared-
    # active account BUT the live mount holds a valid refresh token → it self-heals
    # on the next request, so the detector returns None (NO spurious migrate). This
    # is merkos's steady state: the canonical goes refresh-dead by construction
    # (bare sessions rotate the live token) while the live family stays fine. Before
    # this fix mode (b) fired on the flag alone and would churn the whole bare fleet
    # off a healthy account every cycle.
    _point_live_mount(monkeypatch, tmp_path, _valid("at-live", "rt-live"))
    state = {"active": "rayi1", "accounts": {"rayi1": _dead_refresh()}}
    cfg = _cfg(accounts=("rayi1", "alpha"), guard={"enabled": True})
    assert cus._shared_mount_account_unhealthy("rayi1", state, cfg) is None


def test_dead_refresh_and_live_mount_no_refresh_is_unhealthy(monkeypatch, tmp_path):
    # The other rayi1-trap variant: dead canonical AND a live mount with a token but
    # NO refresh token to mint a new access token from → genuinely stuck → flagged.
    _point_live_mount(monkeypatch, tmp_path,
                      {"claudeAiOauth": {"accessToken": "at", "expiresAt": _FUTURE}})
    state = {"active": "rayi1", "accounts": {"rayi1": _dead_refresh()}}
    cfg = _cfg(accounts=("rayi1", "alpha"), guard={"enabled": True})
    r = cus._shared_mount_account_unhealthy("rayi1", state, cfg)
    assert r and "canonical refresh token is dead" in r


def test_operator_disabled_unconditional_even_with_healthy_live_mount(monkeypatch, tmp_path):
    # Mode (a) stays UNCONDITIONAL (FIX 1): an org-disabled account 429s every
    # request even with a perfectly healthy live mount, so it is flagged regardless
    # of the live-mount state — no self-heal escape hatch for mode (a).
    _point_live_mount(monkeypatch, tmp_path, _valid("at-live", "rt-live"))
    state = {"active": "03", "accounts": {"03": _healthy()}}
    cfg = _cfg(accounts=("03", "alpha"), disabled=("03",))
    assert "disabled" in (cus._shared_mount_account_unhealthy("03", state, cfg) or "")


def test_detect_healthy_is_none():
    state = {"active": "alpha", "accounts": {"alpha": _healthy()}}
    assert cus._shared_mount_account_unhealthy("alpha", state, _cfg()) is None


def test_detect_none_account_is_none():
    assert cus._shared_mount_account_unhealthy(None, {"accounts": {}}, _cfg()) is None


def test_detect_guard_off_ignores_sub_flag():
    # with the master guard off, a lingering subscription_disabled flag must NOT
    # be treated as unhealthy (read-time gating — same as the picker/SOS).
    state = {"active": "x", "accounts": {"x": _sub_dead()}}
    cfg = _cfg(accounts=("x", "alpha"), guard={"enabled": False})
    assert cus._shared_mount_account_unhealthy("x", state, cfg) is None


# ---------------------------------------------------------------------------
# (2)+(3) the migration decision: pick a healthy target, re-probe it, or surface
# ---------------------------------------------------------------------------

def _migrate_target(state, config, monkeypatch, *, dead_targets=()):
    """Drive the migration decision through the REAL helpers (detector +
    `_pick_shared_mount_migration_target`, which now owns the exclude → pick →
    dead-canonical re-pick loop), returning the target name that WOULD be migrated
    onto, or None if it would URGENT-surface. Only _account_snapshot_dead is stubbed
    (its network probe) per dead_targets — no hand-copied replica of the cycle logic
    (F-F-3): this calls the exact functions the cycle calls."""
    monkeypatch.setattr(cus, "_account_snapshot_dead",
                        lambda name, cfg=None, **k: name in dead_targets)
    shared_active = state["active"]
    if cus._shared_mount_account_unhealthy(shared_active, state, config) is None:
        return None
    tgt = cus._pick_shared_mount_migration_target(
        state, config, cus._live_slot_accounts(state),
        lambda name: cus._account_snapshot_dead(name, config))
    return tgt.name if tgt is not None else None  # None → URGENT surface


def test_migrate_off_subended_to_healthy(monkeypatch):
    """mount on subscription-dead beta, alpha+gamma healthy → migrates to the
    best healthy target (gamma is fresher than alpha)."""
    state = {"active": "beta", "slots": {},
             "accounts": {"alpha": _healthy(90, 40), "beta": _sub_dead(),
                          "gamma": _healthy(20, 10)}}
    cfg = _cfg(guard={"enabled": True})
    assert _migrate_target(state, cfg, monkeypatch) == "gamma"


def test_migrate_off_dead_refresh(monkeypatch, tmp_path):
    # Live mount can't self-heal (absent) so the dead-refresh mode (b) genuinely
    # fires (FIX 1), and a healthy gamma is available → migrate to gamma.
    _point_live_mount(monkeypatch, tmp_path, None)
    state = {"active": "rayi1", "slots": {},
             "accounts": {"rayi1": _dead_refresh(), "gamma": _healthy(20, 10)}}
    cfg = _cfg(accounts=("rayi1", "gamma"), guard={"enabled": True})
    assert _migrate_target(state, cfg, monkeypatch) == "gamma"


def test_no_migrate_when_only_target_is_dead_canonical(monkeypatch):
    """The one available target has a DEAD canonical (rayi1 trap) — do NOT migrate
    onto it (would blank the mount); URGENT-surface instead."""
    state = {"active": "beta", "slots": {},
             "accounts": {"beta": _sub_dead(), "gamma": _healthy(20, 10)}}
    cfg = _cfg(accounts=("beta", "gamma"), guard={"enabled": True})
    assert _migrate_target(state, cfg, monkeypatch, dead_targets=("gamma",)) is None


def test_no_migrate_when_no_healthy_target(monkeypatch):
    """Every non-active account is subscription-dead → no target → surface."""
    state = {"active": "beta", "slots": {},
             "accounts": {"beta": _sub_dead(), "gamma": _sub_dead()}}
    cfg = _cfg(accounts=("beta", "gamma"), guard={"enabled": True})
    assert _migrate_target(state, cfg, monkeypatch) is None


def test_healthy_mount_never_migrates(monkeypatch):
    state = {"active": "alpha", "slots": {},
             "accounts": {"alpha": _healthy(20, 10), "gamma": _healthy(30, 20)}}
    assert _migrate_target(state, _cfg(), monkeypatch) is None


def test_migration_target_repicks_past_dead_canonical(monkeypatch):
    """FIX 2026-09-04 (F-F-2): the picker admits a dead-snapshot-with-free-family
    account, which can OUTRANK a healthy one — but installing it would blank the
    shared mount. The migration must re-pick PAST it to the live target behind it,
    not URGENT-surface. Here `pick_swap_target` prefers gamma (dead-canonical) then
    delta; the loop rejects gamma and lands on delta."""
    state = {"active": "beta", "slots": {},
             "accounts": {"beta": _sub_dead(), "gamma": _healthy(20, 10),
                          "delta": _healthy(30, 20)}}
    cfg = _cfg(accounts=("beta", "gamma", "delta"), guard={"enabled": True})

    def _fake_pick(st, c):
        # Prefer gamma, then delta — but only among accounts still in the (trimmed)
        # shim, so excluding gamma on the re-pick surfaces delta.
        accts = st.get("accounts", {})
        for n in ("gamma", "delta"):
            if n in accts and n != st.get("active"):
                return cus.SwapTarget(name=n, reason="pick")
        return None
    monkeypatch.setattr(cus, "pick_swap_target", _fake_pick)
    tgt = cus._pick_shared_mount_migration_target(
        state, cfg, cus._live_slot_accounts(state), lambda n: n == "gamma")
    assert tgt is not None and tgt.name == "delta", tgt


# ---------------------------------------------------------------------------
# (4) install primitive refuses a dead-canonical shared-mount switch
# ---------------------------------------------------------------------------

def test_execute_swap_refuses_dead_canonical_on_shared_mount():
    """slot=None (shared mount) onto a DEAD-canonical account must RAISE rather
    than blank the mount — the exact rayi1 trap (2026-09-03). The lane path
    family-seeds a dead canonical (test_dead_snapshot_family_seed test_a); the
    shared mount has NO seed path, so it must refuse. Refuse is a clean no-op:
    the live mount is left on its prior (good) creds, never blanked."""
    env = _Env({"dead": _dead_snapshot("rt-dead"), "other": _valid("at-o", "rt-o")},
               active="other", config=_ILGATE)
    try:
        env.patch(cus, "_oauth_refresh_grant", _grant_map({"rt-dead": ("dead", None)}))
        with pytest.raises(RuntimeError, match="BLANK"):
            cus.execute_swap("dead")          # slot defaults to None → shared mount
        mount = cus.read_json(env.creds_json)
        assert cus._credential_refresh_token(mount) == "rt-shared", mount
        assert not cus._live_mount_creds_invalid(mount), "mount must NOT be blanked"
        assert env.audit_lines("shared-mount-dead-snapshot"), env.echoes
    finally:
        env.restore()


# ---------------------------------------------------------------------------
# (2)+(3) at the REAL _hybrid_cycle level (F-F-3 = F-O-2): the prior tests drove
# a hand-copied replica of the decision path; these call the actual shipped block
# so a regression in the cycle wiring (inverted gate, broken exclusion, wrong
# SwapDecision fields, missing/duplicate hold record) fails the suite. Only the
# SEAMS the reviews named are monkeypatched — pick_swap_target, _account_snapshot_dead,
# decide_swap, decide_slot_swaps — everything between them (the gate, the
# global_decision-is-None preemption, the exclusion set, the SwapDecision
# composition, the flow into _execute_global_mount_swap, the blocked-hold logging)
# is the real code. Reactive is disabled for determinism (no rate-limit-log read).
# ---------------------------------------------------------------------------

def _cycle_cfg(*, auto_migrate=True, guard_enabled=True):
    return {
        "mode": "hybrid",
        "strategy": "smart",
        "thresholds": {"five_hour": True, "seven_day": True, "steps": [80, 90]},
        "subscription_guard": {"enabled": guard_enabled,
                               "auto_migrate_shared_mount": auto_migrate},
        "reactive": {"enabled": False},        # skip the rate-limit-log read → deterministic
        "lazy_swap": {"enabled": False},       # no cache-warm defer surprises
        "swap_hysteresis": {"enabled": False},
        "usage_growth_gate": {"enabled": False},
        "accounts": [{"name": n, "priority": 1} for n in ("beta", "gamma", "delta")],
    }


def _run_hybrid_cycle(monkeypatch, config, *, decide_swap_result=None,
                      dead_targets=(), pick_order=("gamma",)):
    """Drive the REAL cus._hybrid_cycle(no_execute=True), capturing every decision
    record it emits (via _log_decision). Returns the record list."""
    records: list[dict] = []
    monkeypatch.setattr(cus, "_log_decision", lambda rec: records.append(rec))
    # No slot logic in these tests; the shared-mount migrate block is the subject.
    monkeypatch.setattr(cus, "decide_slot_swaps", lambda *a, **k: [])
    monkeypatch.setattr(cus, "decide_swap", lambda *a, **k: decide_swap_result)
    monkeypatch.setattr(cus, "_account_snapshot_dead",
                        lambda name, cfg=None, **k: name in dead_targets)

    def _fake_pick(st, c):
        # Return the first pick_order account still present in the (excl-trimmed)
        # state shim, so the re-pick loop can walk past an excluded dead target.
        accts = st.get("accounts", {})
        for n in pick_order:
            if n in accts and n != st.get("active"):
                return cus.SwapTarget(name=n, reason="pick")
        return None
    monkeypatch.setattr(cus, "pick_swap_target", _fake_pick)
    cus._hybrid_cycle(cus.load_state(), config, {}, no_execute=True)
    return records


def test_cycle_migrates_off_subended_to_healthy_target(monkeypatch):
    """(1) genuinely-unhealthy (sub-ended) shared mount + healthy slot-free target
    + no competing decision ⇒ a shared_mount_migrate swap decision (tier 3, forced)
    onto the healthy target."""
    cfg = _cycle_cfg()
    env = _Env({"beta": _valid("at-b", "rt-b"), "gamma": _valid("at-g", "rt-g")},
               active="beta",
               flags={"beta": {"subscription_disabled": True, "rate_limited": True}},
               config=cfg)
    try:
        records = _run_hybrid_cycle(monkeypatch, cfg, pick_order=("gamma",))
        migrate = [r for r in records if r["gate"] == "shared_mount_migrate"]
        assert migrate, [(r["action"], r["gate"]) for r in records]
        assert migrate[0]["action"] == "would_swap"
        assert migrate[0]["target"] == "gamma"
        assert migrate[0]["tier"] == 3, migrate[0]
    finally:
        env.restore()


def test_cycle_repicks_past_dead_canonical_target(monkeypatch):
    """(1b / FIX 2 through the real cycle) the top pick is dead-canonical → the
    cycle re-picks past it onto the live target behind it, still emitting a
    shared_mount_migrate decision (no false URGENT surface)."""
    cfg = _cycle_cfg()
    env = _Env({"beta": _valid("at-b", "rt-b"), "gamma": _valid("at-g", "rt-g"),
                "delta": _valid("at-d", "rt-d")},
               active="beta",
               flags={"beta": {"subscription_disabled": True, "rate_limited": True}},
               config=cfg)
    try:
        records = _run_hybrid_cycle(monkeypatch, cfg,
                                    pick_order=("gamma", "delta"), dead_targets=("gamma",))
        migrate = [r for r in records if r["gate"] == "shared_mount_migrate"]
        assert migrate and migrate[0]["target"] == "delta", \
            [(r["action"], r["gate"], r.get("target")) for r in records]
    finally:
        env.restore()


def test_cycle_gate_off_does_not_migrate(monkeypatch):
    """(2) auto_migrate_shared_mount=False ⇒ no migrate (exclude-only behavior); a
    plain no_moves hold is logged instead of a shared-mount migrate."""
    cfg = _cycle_cfg(auto_migrate=False)
    env = _Env({"beta": _valid("at-b", "rt-b"), "gamma": _valid("at-g", "rt-g")},
               active="beta",
               flags={"beta": {"subscription_disabled": True, "rate_limited": True}},
               config=cfg)
    try:
        records = _run_hybrid_cycle(monkeypatch, cfg, pick_order=("gamma",))
        assert not any(r["gate"].startswith("shared_mount_migrate") for r in records), records
        assert any(r["gate"] == "no_moves" for r in records), records
    finally:
        env.restore()


def test_cycle_competing_decision_not_overridden(monkeypatch):
    """(3) a competing global/bare swap decision is present ⇒ the migrate block does
    NOT fire (gated on global_decision is None); the competing decision is what the
    cycle acts on."""
    cfg = _cycle_cfg()
    competing = cus.SwapDecision(target="gamma", reason="ladder step", tier=2,
                                 gate="ladder", deferrable=True)
    env = _Env({"beta": _valid("at-b", "rt-b"), "gamma": _valid("at-g", "rt-g")},
               active="beta",
               flags={"beta": {"subscription_disabled": True, "rate_limited": True}},
               config=cfg)
    try:
        records = _run_hybrid_cycle(monkeypatch, cfg, decide_swap_result=competing)
        assert not any(r["gate"].startswith("shared_mount_migrate") for r in records), records
        ws = [r for r in records if r["action"] == "would_swap"]
        assert ws and ws[0]["gate"] == "ladder" and ws[0]["target"] == "gamma", records
    finally:
        env.restore()


def test_cycle_no_target_logs_blocked_hold_once(monkeypatch):
    """(4) unhealthy shared mount + no healthy target ⇒ exactly one
    shared_mount_migrate_blocked hold, and NOT also a redundant no_moves hold
    (F-F-6 dedup)."""
    cfg = _cycle_cfg()
    env = _Env({"beta": _valid("at-b", "rt-b"), "gamma": _valid("at-g", "rt-g")},
               active="beta",
               flags={"beta": {"subscription_disabled": True, "rate_limited": True}},
               config=cfg)
    try:
        # pick_order=() → the picker finds no target → the migrate block URGENT-holds.
        records = _run_hybrid_cycle(monkeypatch, cfg, pick_order=())
        blocked = [r for r in records if r["gate"] == "shared_mount_migrate_blocked"]
        assert blocked and blocked[0]["action"] == "hold", records
        assert not any(r["gate"] == "no_moves" for r in records), \
            "no_moves must be suppressed when a shared-migrate hold was logged"
    finally:
        env.restore()


# ---------------------------------------------------------------------------
# (F-F-4 = F-O-1) diagnose() surfaces the blocked state as an URGENT SOSCondition
# ---------------------------------------------------------------------------

def test_diagnose_surfaces_stuck_shared_mount_no_target():
    """A sub-ended shared-mount account with NO healthy target ⇒ diagnose() emits an
    URGENT SOSCondition (an operator watching `cus sos` / SOS.md sees it), not just a
    daemon-stdout line the incident showed nobody was tailing."""
    cfg = _cycle_cfg()
    # Only beta (sub-ended, active) and gamma (also sub-ended) → no healthy target.
    env = _Env({"beta": _valid("at-b", "rt-b"), "gamma": _valid("at-g", "rt-g")},
               active="beta",
               flags={"beta": {"subscription_disabled": True, "rate_limited": True},
                      "gamma": {"subscription_disabled": True, "rate_limited": True}},
               config=cfg)
    try:
        conds = cus.diagnose(cus.load_state(), cfg)
        hits = [c for c in conds if c.severity == "urgent"
                and "shared bare mount stuck" in c.summary]
        assert hits, [(c.severity, c.summary) for c in conds]
        assert "cus slot move" in hits[0].action
    finally:
        env.restore()


def test_diagnose_silent_when_healthy_target_exists():
    """The same sub-ended shared mount but WITH a healthy slot-free target ⇒ NO
    urgent shared-mount SOS: the daemon will auto-migrate on its next cycle, so no
    operator action is needed (the condition fires only when the migrate is blocked)."""
    cfg = _cycle_cfg()
    env = _Env({"beta": _valid("at-b", "rt-b"), "gamma": _valid("at-g", "rt-g")},
               active="beta",
               flags={"beta": {"subscription_disabled": True, "rate_limited": True},
                      "gamma": {"current_5h_pct": 10.0, "current_7d_pct": 10.0}},
               config=cfg)
    try:
        conds = cus.diagnose(cus.load_state(), cfg)
        assert not any("shared bare mount stuck" in c.summary for c in conds), \
            [(c.severity, c.summary) for c in conds]
    finally:
        env.restore()


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
