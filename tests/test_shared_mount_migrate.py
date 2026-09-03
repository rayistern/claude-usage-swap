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

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))  # tests/ dir — sibling harness

import cus  # noqa: E402
# Reuse the on-disk swap sandbox from the dead-snapshot suite (slots, families,
# grant-map, click.echo capture) rather than duplicate ~130 lines of _Env.
from test_dead_snapshot_family_seed import (  # noqa: E402
    _Env, _dead_snapshot, _valid, _grant_map, _ILGATE)


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


def test_detect_dead_canonical_refresh():
    state = {"active": "rayi1", "accounts": {"rayi1": _dead_refresh()}}
    cfg = _cfg(accounts=("rayi1", "alpha"), guard={"enabled": True})
    r = cus._shared_mount_account_unhealthy("rayi1", state, cfg)
    assert r and "canonical refresh token is dead" in r


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
    """Replicate the hybrid-cycle migration decision exactly (detector → excl →
    pick_swap_target → target dead re-probe), returning the target name that WOULD
    be migrated onto, or None if it would URGENT-surface. Uses the REAL helpers;
    only _account_snapshot_dead is stubbed (its network probe) per dead_targets."""
    monkeypatch.setattr(cus, "_account_snapshot_dead",
                        lambda name, cfg=None, **k: name in dead_targets)
    shared_active = state["active"]
    if cus._shared_mount_account_unhealthy(shared_active, state, config) is None:
        return None
    slot_accts = cus._live_slot_accounts(state)
    tgt = cus.pick_swap_target(
        cus._state_excluding_accounts(state, shared_active, slot_accts), config)
    if tgt is not None and not cus._account_snapshot_dead(tgt.name, config):
        return tgt.name
    return None  # URGENT surface


def test_migrate_off_subended_to_healthy(monkeypatch):
    """mount on subscription-dead beta, alpha+gamma healthy → migrates to the
    best healthy target (gamma is fresher than alpha)."""
    state = {"active": "beta", "slots": {},
             "accounts": {"alpha": _healthy(90, 40), "beta": _sub_dead(),
                          "gamma": _healthy(20, 10)}}
    cfg = _cfg(guard={"enabled": True})
    assert _migrate_target(state, cfg, monkeypatch) == "gamma"


def test_migrate_off_dead_refresh(monkeypatch):
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


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
