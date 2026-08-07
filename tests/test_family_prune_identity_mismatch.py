"""Tests for the 2026-08-07 DEEP prune (`cus prune --deep`) — the family-
pollution follow-up to the GH #190 housekeeping scaffold.

Live incident proven against: account-03 was identity-clobbered to rayi2,
families minted during the clobber window (03/family-6/7/8) hold REAL rayi2
logins whose subscription was then cancelled — auth-alive, subscription-dead,
identity-wrong stores that claim-verify (#127) happily hands to lanes.

Contract proven here:
  (1) identity-mismatch: a family filed under account A holding account B's
      creds is REPORTED by default and retired `.dead-<date>` with
      execute=True; an identity-MATCHING healthy family is untouched;
  (2) a LEASED polluted family is reported (loudly) but NEVER probed and
      NEVER retired, even with execute=True — the evacuation pointer is the
      remedy, not a rename under a live mount;
  (3) subscription-disabled: a free family under a #191-flagged account is
      retired with ZERO network; the profile-probe path retires an unflagged
      store whose probe says "disabled"; the subscription_guard.enabled=False
      read-time gate suppresses both;
  (4) duplicate-generation: an unrotated bootstrap copy sharing the
      canonical's refresh token is retired — unless written <1h ago
      (possible in-flight --from-existing provision → report-only);
  (5) past-lifetime: a store minted beyond the assumed TTL is retired only
      when its grant probes DEAD; probe-unknown fails open to a report line;
  (6) config gates: housekeeping.deep_identity_mismatch=False skips the
      check; `cus prune` WITHOUT --deep never runs deep checks (opt-in);
  (7) legacy slot-keyed stores (logins/<acct>/slot-N) go through the same
      identity-mismatch retirement;
  (8) evacuation: a LIVE lane on a subscription-dead account yields a
      candidate row with a surviving target; the daemon sweep executes it
      via execute_swap (and honors no_execute / auto_evacuate=False /
      locked slots);
  (9) warm spares: a usable account below the warm-spare floor warns;
      a covered account does not.

Run standalone:  python3 tests/test_family_prune_identity_mismatch.py
Run under pytest: pytest tests/test_family_prune_identity_mismatch.py
"""

from __future__ import annotations

import json
import os
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest  # noqa: F401  (parity with sibling test modules)

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import cus  # noqa: E402

# Reuse the sibling module's throwaway on-disk env + creds shapes + grant
# fakes rather than copying them: _Env repoints every cus path constant at a
# tempdir and restores on .restore(), which is exactly what these tests need.
from test_prune_housekeeping import (  # noqa: E402
    _Env, _ILGATE, _expired, _grant_map, _no_grant, _valid,
)


def _ident(name: str) -> dict:
    """The oauthAccount identity _Env writes for account `name`."""
    return {"accountUuid": f"uuid-{name}", "emailAddress": f"{name}@x"}


def _plant_family_full(env: _Env, account: str, fam: str, creds: dict,
                       identity_of: str | None = None,
                       minted_ts: str | None = None,
                       backdate_creds_hours: float = 2.0) -> Path:
    """Plant a family WITH an identity .claude.json and optional provenance —
    the deep checks read both, unlike the creds-only plant_family helper.
    Creds mtime is backdated (default 2h) so the <1h freshness guard on the
    duplicate-generation check doesn't mask findings in tests."""
    env.plant_family(account, fam, creds)
    d = cus.login_family_dir(account, fam)
    if identity_of is not None:
        (d / ".claude.json").write_text(json.dumps({"oauthAccount": _ident(identity_of)}))
    if minted_ts is not None:
        (d / "provenance.json").write_text(json.dumps({"minted_ts": minted_ts}))
    path = cus.login_family_creds_path(account, fam)
    if backdate_creds_hours:
        t = time.time() - backdate_creds_hours * 3600
        os.utime(path, (t, t))
    return path


def _fresh_caches() -> None:
    """Deep prune shares module-level cooldown caches with the launch gate and
    the #191 guard; clear them so verdicts can't leak across tests."""
    cus._STORE_DEAD_PROBE.clear()
    cus._reset_subscription_probe_cache()


def _no_profile_probe():
    def _probe(token):
        raise AssertionError(f"profile probe fired unexpectedly (token={token!r})")
    return _probe


def _flag_subscription_dead(account: str) -> None:
    state = cus.load_state()
    state["accounts"][account]["subscription_disabled"] = True
    cus.save_state(state)


# ---------------------------------------------------------------------------
# (1) identity-mismatch: report by default, retire on execute; match untouched
# ---------------------------------------------------------------------------

def test_identity_mismatch_family_reported_then_retired_match_untouched():
    env = _Env({"aa": _valid("at-a", "rt-a"), "bb": _valid("at-b", "rt-b")},
               active="bb", config=_ILGATE)
    try:
        _fresh_caches()
        # family-1 filed under aa but holding BB's identity — the 03/family-8
        # pollution shape. Distinct rt so no duplicate-generation hit.
        bad = _plant_family_full(env, "aa", "family-1", _valid("at-x", "rt-x"),
                                 identity_of="bb")
        # family-2 healthy and identity-MATCHING.
        good = _plant_family_full(env, "aa", "family-2", _valid("at-y", "rt-y"),
                                  identity_of="aa")
        env.patch(cus, "_oauth_refresh_grant", _no_grant())          # disk-only checks
        env.patch(cus, "_probe_subscription_profile", _no_profile_probe())
        state, config = cus.load_state(), cus.load_config()

        # Report-only: finding listed, nothing renamed.
        rows = cus._deep_prune_stores(state, config, execute=False, probe=False)
        mm = [r for r in rows if r["check"] == "identity-mismatch"]
        assert len(mm) == 1 and mm[0]["store"] == "family-1" and not mm[0]["leased"], rows
        assert mm[0]["retired"] is None and bad.exists(), \
            "default must be report-only — no rename"
        assert not [r for r in rows if r["store"] == "family-2"], \
            f"identity-matching family must produce no finding: {rows}"

        # Execute: polluted store retired, healthy store untouched.
        rows = cus._deep_prune_stores(state, config, execute=True, probe=False)
        mm = [r for r in rows if r["check"] == "identity-mismatch"]
        assert mm and mm[0]["retired"] and not bad.exists(), rows
        assert [p.name for p in bad.parent.iterdir() if ".dead-" in p.name]
        assert good.exists(), "identity-matching healthy family must never be touched"
    finally:
        env.restore()


# ---------------------------------------------------------------------------
# (2) LEASED polluted family: reported, never probed, never retired
# ---------------------------------------------------------------------------

def test_leased_mismatch_family_reported_never_probed_never_retired():
    env = _Env({"aa": _valid("at-a", "rt-a"), "bb": _valid("at-b", "rt-b")},
               active="bb", config=_ILGATE)
    try:
        _fresh_caches()
        env.make_slot("aa", live=True, mount_creds=_valid("at-l", "rt-leased-mnt"),
                      family_id="family-1")
        # Leased + polluted + EXPIRED creds: any probe would raise (rt not in
        # any grant map) — proving the leased store is never granted.
        bad = _plant_family_full(env, "aa", "family-1", _expired("rt-leased"),
                                 identity_of="bb")
        env.patch(cus, "_oauth_refresh_grant", _no_grant())
        env.patch(cus, "_probe_subscription_profile", _no_profile_probe())

        rows = cus._deep_prune_stores(cus.load_state(), cus.load_config(),
                                      execute=True, probe=True)
        mm = [r for r in rows if r["check"] == "identity-mismatch"]
        assert len(mm) == 1 and mm[0]["leased"] and mm[0]["retired"] is None, rows
        assert "slot move" in mm[0]["detail"], mm  # evacuation is the remedy
        assert bad.exists(), "a leased store must NEVER be retired"
    finally:
        env.restore()


# ---------------------------------------------------------------------------
# (3) subscription-disabled: account-flag fast path, probe path, read gate
# ---------------------------------------------------------------------------

def test_subscription_flagged_account_retired_only_with_probe_confirmation():
    env = _Env({"aa": _valid("at-a", "rt-a"), "bb": _valid("at-b", "rt-b")},
               active="bb", config=_ILGATE)
    try:
        _fresh_caches()
        _flag_subscription_dead("aa")
        path = _plant_family_full(env, "aa", "family-1", _valid("at-x", "rt-x"),
                                  identity_of="aa")
        env.patch(cus, "_oauth_refresh_grant", _no_grant())

        # --no-probe: the flag ALONE must never retire (live lesson: 03's flag
        # was mis-attributed via drift; blind trust would have retired the one
        # fresh alive family). Report row present, marked non-actionable.
        env.patch(cus, "_probe_subscription_profile", _no_profile_probe())
        rows = cus._deep_prune_stores(cus.load_state(), cus.load_config(),
                                      execute=True, probe=False)
        sub = [r for r in rows if r["check"] == "subscription-disabled"]
        assert len(sub) == 1 and sub[0]["retired"] is None, rows
        assert not sub[0]["would_retire"] and "UNCONFIRMED" in sub[0]["detail"], sub
        assert path.exists()

        # Probe confirms "disabled" → retired, detail names the confirmation.
        cus._probe_subscription_profile = lambda token: (
            "disabled", {"organization_type": "claude_free"})
        _fresh_caches()
        rows = cus._deep_prune_stores(cus.load_state(), cus.load_config(),
                                      execute=True, probe=True)
        sub = [r for r in rows if r["check"] == "subscription-disabled"]
        assert len(sub) == 1 and sub[0]["retired"] and "confirms the #191" in sub[0]["detail"], rows
        assert not path.exists()
    finally:
        env.restore()


def test_subscription_stale_flag_probe_active_keeps_store():
    env = _Env({"aa": _valid("at-a", "rt-a"), "bb": _valid("at-b", "rt-b")},
               active="bb", config=_ILGATE)
    try:
        _fresh_caches()
        _flag_subscription_dead("aa")
        path = _plant_family_full(env, "aa", "family-1", _valid("at-x", "rt-x"),
                                  identity_of="aa")
        env.patch(cus, "_oauth_refresh_grant", _no_grant())
        env.patch(cus, "_probe_subscription_profile", lambda token: ("active", {}))
        rows = cus._deep_prune_stores(cus.load_state(), cus.load_config(),
                                      execute=True, probe=True)
        sub = [r for r in rows if r["check"] == "subscription-disabled"]
        assert len(sub) == 1 and sub[0]["retired"] is None, rows
        assert "STALE" in sub[0]["detail"], sub
        assert path.exists(), "a probe-ACTIVE store must be kept despite the flag"
    finally:
        env.restore()


def test_subscription_flagged_expired_store_grant_refresh_then_confirm():
    """The full honest chain for the live rayi2-family shape: flagged account,
    EXPIRED access token — deep prune grant-refreshes the free store (rotation
    persisted), then profile-probes the fresh token, then retires on
    "disabled"."""
    env = _Env({"aa": _valid("at-a", "rt-a"), "bb": _valid("at-b", "rt-b")},
               active="bb", config=_ILGATE)
    try:
        _fresh_caches()
        _flag_subscription_dead("aa")
        path = _plant_family_full(env, "aa", "family-1", _expired("rt-old"),
                                  identity_of="aa")
        grant_calls: list[str] = []
        env.patch(cus, "_oauth_refresh_grant",
                  _grant_map({"rt-old": ("alive", {"access_token": "at-minted",
                                                   "refresh_token": "rt-minted",
                                                   "expires_in": 3600})},
                             calls=grant_calls))
        probed: list[str] = []

        def _probe(token):
            probed.append(token)
            return "disabled", {"organization_type": "claude_free"}
        env.patch(cus, "_probe_subscription_profile", _probe)

        rows = cus._deep_prune_stores(cus.load_state(), cus.load_config(),
                                      execute=True, probe=True)
        sub = [r for r in rows if r["check"] == "subscription-disabled"]
        assert len(sub) == 1 and sub[0]["retired"], rows
        assert grant_calls == ["rt-old"], grant_calls
        assert probed == ["at-minted"], "profile probe must use the FRESH minted token"
        assert not path.exists()
        # The retired file holds the ROTATED generation (persisted pre-rename).
        retired = [p for p in path.parent.iterdir() if ".dead-" in p.name]
        assert retired and json.loads(retired[0].read_text())[
            "claudeAiOauth"]["refreshToken"] == "rt-minted"
    finally:
        env.restore()


def test_subscription_probe_disabled_unflagged_family_retired():
    env = _Env({"aa": _valid("at-a", "rt-a"), "bb": _valid("at-b", "rt-b")},
               active="bb", config=_ILGATE)
    try:
        _fresh_caches()
        path = _plant_family_full(env, "aa", "family-1", _valid("at-x", "rt-x"),
                                  identity_of="aa")
        env.patch(cus, "_oauth_refresh_grant", _no_grant())
        probed: list[str] = []

        def _probe(token):
            probed.append(token)
            return "disabled", {"organization_type": "claude_free"}
        env.patch(cus, "_probe_subscription_profile", _probe)

        rows = cus._deep_prune_stores(cus.load_state(), cus.load_config(),
                                      execute=True, probe=True)
        sub = [r for r in rows if r["check"] == "subscription-disabled"]
        assert len(sub) == 1 and sub[0]["retired"], rows
        assert probed == ["at-x"], "exactly one profile probe, with the store's token"
        assert not path.exists()
    finally:
        env.restore()


def test_subscription_guard_disabled_config_suppresses_both_paths():
    cfg = {**_ILGATE, "subscription_guard": {"enabled": False}}
    env = _Env({"aa": _valid("at-a", "rt-a"), "bb": _valid("at-b", "rt-b")},
               active="bb", config=cfg)
    try:
        _fresh_caches()
        _flag_subscription_dead("aa")  # stale flag must be IGNORED with guard off
        path = _plant_family_full(env, "aa", "family-1", _valid("at-x", "rt-x"),
                                  identity_of="aa")
        env.patch(cus, "_oauth_refresh_grant", _no_grant())
        env.patch(cus, "_probe_subscription_profile", _no_profile_probe())
        rows = cus._deep_prune_stores(cus.load_state(), cus.load_config(),
                                      execute=True, probe=True)
        assert not [r for r in rows if r["check"] == "subscription-disabled"], rows
        assert path.exists()
    finally:
        env.restore()


# ---------------------------------------------------------------------------
# (4) duplicate-generation: canonical-copy retired; <1h-fresh reported only
# ---------------------------------------------------------------------------

def test_duplicate_generation_bootstrap_copy_retired_fresh_reported_only():
    env = _Env({"aa": _valid("at-a", "rt-a"), "bb": _valid("at-b", "rt-b")},
               active="bb", config=_ILGATE)
    try:
        _fresh_caches()
        # Same refresh token as aa's CANONICAL — the unrotated --from-existing
        # copy (the live 03/slot-14 b990a8… shape). Fresh mtime first.
        path = _plant_family_full(env, "aa", "family-1", _valid("at-a", "rt-a"),
                                  identity_of="aa", backdate_creds_hours=0)
        env.patch(cus, "_oauth_refresh_grant", _no_grant())
        env.patch(cus, "_probe_subscription_profile", _no_profile_probe())
        state, config = cus.load_state(), cus.load_config()

        rows = cus._deep_prune_stores(state, config, execute=True, probe=False)
        dup = [r for r in rows if r["check"] == "duplicate-generation"]
        assert len(dup) == 1 and dup[0]["retired"] is None, rows
        assert "in-flight" in dup[0]["detail"], dup
        assert path.exists(), "<1h-fresh duplicate must be report-only"

        # Backdate past the freshness window → retired on execute.
        t = time.time() - 7200
        os.utime(path, (t, t))
        rows = cus._deep_prune_stores(state, config, execute=True, probe=False)
        dup = [r for r in rows if r["check"] == "duplicate-generation"]
        assert len(dup) == 1 and dup[0]["retired"], rows
        assert "canonical account-aa" in dup[0]["detail"], dup
        assert not path.exists()
    finally:
        env.restore()


# ---------------------------------------------------------------------------
# (5) past-lifetime: probe-dead retired; probe-unknown kept (fail open)
# ---------------------------------------------------------------------------

def _minted_days_ago(days: float) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()


def test_past_lifetime_probe_dead_retired():
    env = _Env({"aa": _valid("at-a", "rt-a"), "bb": _valid("at-b", "rt-b")},
               active="bb", config=_ILGATE)
    try:
        _fresh_caches()
        path = _plant_family_full(env, "aa", "family-1", _expired("rt-old"),
                                  identity_of="aa", minted_ts=_minted_days_ago(40))
        env.patch(cus, "_oauth_refresh_grant", _grant_map({"rt-old": ("dead", None)}))
        env.patch(cus, "_probe_subscription_profile", _no_profile_probe())
        rows = cus._deep_prune_stores(cus.load_state(), cus.load_config(),
                                      execute=True, probe=True)
        pl = [r for r in rows if r["check"] == "past-lifetime"]
        assert len(pl) == 1 and pl[0]["retired"] and "grant is dead" in pl[0]["detail"], rows
        assert not path.exists()
    finally:
        env.restore()


def test_past_lifetime_probe_unknown_fails_open_kept():
    env = _Env({"aa": _valid("at-a", "rt-a"), "bb": _valid("at-b", "rt-b")},
               active="bb", config=_ILGATE)
    try:
        _fresh_caches()
        path = _plant_family_full(env, "aa", "family-1", _expired("rt-old"),
                                  identity_of="aa", minted_ts=_minted_days_ago(40))
        env.patch(cus, "_oauth_refresh_grant", _grant_map({"rt-old": ("unknown", None)}))
        env.patch(cus, "_probe_subscription_profile", _no_profile_probe())
        rows = cus._deep_prune_stores(cus.load_state(), cus.load_config(),
                                      execute=True, probe=True)
        pl = [r for r in rows if r["check"] == "past-lifetime"]
        assert len(pl) == 1 and pl[0]["retired"] is None, rows
        assert "unverifiable" in pl[0]["detail"], pl
        assert path.exists(), "probe-unknown must fail open — never retire on a blip"
    finally:
        env.restore()


# ---------------------------------------------------------------------------
# (6) gates: per-check config gate; `cus prune` without --deep is opt-out
# ---------------------------------------------------------------------------

def test_config_gate_deep_identity_mismatch_off_skips_check():
    cfg = {**_ILGATE, "housekeeping": {"deep_identity_mismatch": False}}
    env = _Env({"aa": _valid("at-a", "rt-a"), "bb": _valid("at-b", "rt-b")},
               active="bb", config=cfg)
    try:
        _fresh_caches()
        path = _plant_family_full(env, "aa", "family-1", _valid("at-x", "rt-x"),
                                  identity_of="bb")
        env.patch(cus, "_oauth_refresh_grant", _no_grant())
        env.patch(cus, "_probe_subscription_profile",
                  lambda token: ("active", {}))  # probe may fire; must say active
        rows = cus._deep_prune_stores(cus.load_state(), cus.load_config(),
                                      execute=True, probe=True)
        assert not [r for r in rows if r["check"] == "identity-mismatch"], rows
        assert path.exists()
    finally:
        env.restore()


def test_prune_cmd_without_deep_never_runs_deep_checks():
    env = _Env({"aa": _valid("at-a", "rt-a"), "bb": _valid("at-b", "rt-b")},
               active="bb", config=_ILGATE)
    try:
        _fresh_caches()
        path = _plant_family_full(env, "aa", "family-1", _valid("at-x", "rt-x"),
                                  identity_of="bb")
        env.patch(cus, "_oauth_refresh_grant", _no_grant())
        env.patch(cus, "_probe_subscription_profile", _no_profile_probe())
        # Old callback shape (no deep kwarg) — proves backward compatibility
        # AND that --execute without --deep can't touch a polluted store.
        cus.prune_cmd.callback(execute=True, reseed=None, no_probe=True)
        assert path.exists(), "without --deep the polluted store must be untouched"
        assert not any("Deep store checks" in e for e in env.echoes), env.echoes
    finally:
        env.restore()


def test_prune_cmd_deep_reports_then_retires():
    env = _Env({"aa": _valid("at-a", "rt-a"), "bb": _valid("at-b", "rt-b")},
               active="bb", config=_ILGATE)
    try:
        _fresh_caches()
        path = _plant_family_full(env, "aa", "family-1", _valid("at-x", "rt-x"),
                                  identity_of="bb")
        env.patch(cus, "_oauth_refresh_grant", _no_grant())
        env.patch(cus, "_probe_subscription_profile", _no_profile_probe())

        cus.prune_cmd.callback(execute=False, reseed=None, no_probe=True, deep=True)
        assert any("Deep store checks" in e for e in env.echoes), env.echoes
        assert any("would retire" in e and "identity-mismatch" in e
                   for e in env.echoes), env.echoes
        assert path.exists()

        env.echoes.clear()
        cus.prune_cmd.callback(execute=True, reseed=None, no_probe=True, deep=True)
        assert any("retired to" in e for e in env.echoes), env.echoes
        assert not path.exists()
    finally:
        env.restore()


# ---------------------------------------------------------------------------
# (7) legacy slot-keyed store: identity-mismatch retirement
# ---------------------------------------------------------------------------

def test_legacy_store_identity_mismatch_retired():
    env = _Env({"aa": _valid("at-a", "rt-a"), "bb": _valid("at-b", "rt-b")},
               active="bb", config=_ILGATE)
    try:
        _fresh_caches()
        # logins/aa/slot-3: a legacy per-(account, slot) store carrying BB's
        # identity. slot-3 has no live slot dir → safe to judge.
        d = cus.login_store_dir("aa", "slot-3")
        d.mkdir(parents=True)
        cus.login_store_creds_path("aa", "slot-3").write_text(
            json.dumps(_valid("at-x", "rt-x")))
        (d / ".claude.json").write_text(json.dumps({"oauthAccount": _ident("bb")}))
        env.patch(cus, "_oauth_refresh_grant", _no_grant())
        env.patch(cus, "_probe_subscription_profile", _no_profile_probe())

        rows = cus._deep_prune_stores(cus.load_state(), cus.load_config(),
                                      execute=True, probe=False)
        mm = [r for r in rows if r["check"] == "identity-mismatch"]
        assert len(mm) == 1 and mm[0]["legacy"] and mm[0]["store"] == "slot-3", rows
        assert mm[0]["retired"] and not cus.login_store_creds_path("aa", "slot-3").exists()
    finally:
        env.restore()


# ---------------------------------------------------------------------------
# (8) evacuation: candidates + daemon-sweep execution semantics
# ---------------------------------------------------------------------------

def test_evacuation_candidate_and_sweep_executes_via_execute_swap():
    cfg = {**_ILGATE, "housekeeping": {"daemon_sweep": True}}
    env = _Env({"aa": _valid("at-a", "rt-a"), "bb": _valid("at-b", "rt-b")},
               active="bb", config=cfg)
    try:
        _fresh_caches()
        _flag_subscription_dead("aa")
        live = env.make_slot("aa", live=True, mount_creds=_valid("at-l", "rt-l"))
        env.make_slot("aa", live=False, mount_creds=_valid("at-i", "rt-i"))  # idle: no rescue
        state, config = cus.load_state(), cus.load_config()

        cands = cus._evacuation_candidates(state, config)
        assert len(cands) == 1, f"only the LIVE lane needs evacuation: {cands}"
        assert cands[0]["slot"] == live and cands[0]["account"] == "aa"
        assert cands[0]["target"] == "bb" and cands[0]["plan"] in ("snapshot", "claim")

        moves: list[tuple] = []
        env.patch(cus, "execute_swap",
                  lambda target, trigger="manual", slot=None, **kw:
                  moves.append((target, trigger, slot)) or {})
        env.patch(cus, "_oauth_refresh_grant", _no_grant())

        # no_execute threads through: report line only, no swap.
        msgs = cus._sweep_evacuate_subscription_dead(state, config, no_execute=True)
        assert any("would evacuate" in m for m in msgs), msgs
        assert moves == []

        msgs = cus._sweep_evacuate_subscription_dead(state, config)
        assert any("evacuated" in m and "walk-back" in m for m in msgs), msgs
        assert moves == [("bb", "housekeeping-evacuate", live)], moves
    finally:
        env.restore()


def test_evacuation_gates_auto_evacuate_off_and_locked_slot():
    env = _Env({"aa": _valid("at-a", "rt-a"), "bb": _valid("at-b", "rt-b")},
               active="bb", config=_ILGATE)
    try:
        _fresh_caches()
        _flag_subscription_dead("aa")
        live = env.make_slot("aa", live=True, mount_creds=_valid("at-l", "rt-l"))
        state = cus.load_state()

        # auto_evacuate off → sweep does nothing even with a candidate.
        cfg_off = {**cus.load_config(), "housekeeping": {"auto_evacuate": False}}
        env.patch(cus, "execute_swap",
                  lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not swap")))
        assert cus._sweep_evacuate_subscription_dead(state, cfg_off) == []

        # Locked slot → not even a candidate.
        cfg_lock = {**cus.load_config(), "session_locks": {"locked_slots": [live]}}
        assert cus._evacuation_candidates(state, cfg_lock) == []
    finally:
        env.restore()


# ---------------------------------------------------------------------------
# (9) warm-spare floor: short pool warns; covered pool doesn't
# ---------------------------------------------------------------------------

def test_warm_spare_warnings_short_vs_covered():
    env = _Env({"aa": _valid("at-a", "rt-a"), "bb": _valid("at-b", "rt-b")},
               active="bb", config=_ILGATE)
    try:
        _fresh_caches()
        # aa: its only family is LEASED by a live slot → zero warm spares.
        env.make_slot("aa", live=True, mount_creds=_valid("at-l", "rt-l"),
                      family_id="family-1")
        _plant_family_full(env, "aa", "family-1", _valid("at-l", "rt-l"),
                           identity_of="aa")
        # bb: one FREE healthy family → covered.
        _plant_family_full(env, "bb", "family-1", _valid("at-f", "rt-f"),
                           identity_of="bb")
        msgs = cus._warm_spare_warnings(cus.load_state(), cus.load_config())
        assert len(msgs) == 1 and msgs[0].startswith("aa:"), msgs
        assert "login-mount aa" in msgs[0], msgs
    finally:
        env.restore()


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in fns:
        fn()
        print(f"ok {fn.__name__}")
    print(f"{len(fns)} tests passed")
