"""Tests for the dead-snapshot heal-from-live-family (2026-07-10 rayi2 incident).

THE INCIDENT (verified live 2026-07-10): rayi2's canonical snapshot
(account-rayi2/.credentials.json) held a DEAD refresh token (the OAuth refresh
grant returned invalid_grant) while BOTH of its pooled login families were
live, valid, and refreshed every cycle — family-1 leased to slot-9, family-2 to
slot-5, both stores kept fresh by blank-preempt + the #109 pooled save-back.
Because every family was LEASED, has_free_login_family() was False and SOS
condition 10.6 emitted "[URGENT] relogin required" for DAYS, even though a
valid, identity-verified rayi2 token sat on disk the whole time. Root cause:
the pool model never refreshes the canonical snapshot (leased-lane save-backs
go to the family store by design), so the snapshot's own refresh lineage rots,
and no code path considered "reseed the snapshot from a live family".

This suite proves the heal and its guard stack:
  (1) a dead-flagged snapshot is reseeded byte-for-byte from the freshest
      valid family store; flags clear; a GH #79 backup is left behind; the
      CRED-AUDIT line fires without leaking raw tokens.
  (2) identity guard: a family whose /login identity contradicts the account's
      trusted anchor (meta.yaml) is REFUSED as a heal source (2026-07-06
      wrong-account clobber rule) — never save a clobbered/foreign token.
  (3) an expired family source is skipped (only a token that provably
      authenticates NOW may reseed).
  (4) idempotence: an already-valid snapshot is left byte-for-byte untouched
      (flags-only reconcile).
  (5) GH #77: a snapshot FRESHER than the best family source is never
      clobbered.
  (6) aliasing invariant (#104): a snapshot whose refresh token belongs to a
      family is dead-FOR-INSTALL without any token-rotating probe
      (_account_snapshot_dead), and the un-stale sweep heals instead of
      probing (a snapshot-side rotation would log the live lane out).
  (7) `_refresh_account_token` refuses to refresh an aliased snapshot (same
      rotation hazard), with zero network.
  (8) diagnose(): the 10.6 "relogin required" URGENT downgrades to a
      self-healing WARNING when a valid heal source exists; stays URGENT when
      none does.
  (9) the daemon pass `_auto_heal_dead_snapshots` heals + persists state.
 (10) shared-mount install of an aliased snapshot while its family is live on
      a lane is REFUSED (the #104 collide guard now also covers slot=None).

Run standalone:  python3 tests/test_snapshot_heal_from_live_family.py
Run under pytest: pytest tests/test_snapshot_heal_from_live_family.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import cus  # noqa: E402

# Reuse the proven throwaway-tree env (slots + families + echo capture + /proc
# mock) from the dead-snapshot seed suite rather than duplicating 100 lines —
# both suites exercise the same snapshot/family machinery.
from test_dead_snapshot_family_seed import (  # noqa: E402
    _Env, _valid, _dead_snapshot, _FUTURE, _PAST, _ILGATE,
)

# A second future expiry STRICTLY older than _FUTURE, for freshness-ordering
# tests (still comfortably valid "now").
_FUTURE_OLDER = _FUTURE - 3_600_000


def _write_meta(env: _Env, account: str, uuid: str | None = None, email: str | None = None) -> None:
    """Plant a trusted meta.yaml anchor for `account` (the identity source the
    save-back path never writes — see account_identity_anchor)."""
    cus.write_yaml(env.accounts_dir / f"account-{account}" / "meta.yaml",
                   {"oauth_account_uuid": uuid or f"uuid-{account}",
                    "oauth_email": email or f"{account}@x"})


def _write_family_identity(account: str, family_id: str, ident_of: str) -> None:
    """Plant the /login-recorded identity in a family dir's .claude.json."""
    d = cus.login_family_dir(account, family_id)
    d.mkdir(parents=True, exist_ok=True)
    (d / ".claude.json").write_text(json.dumps(
        {"oauthAccount": {"accountUuid": f"uuid-{ident_of}", "emailAddress": f"{ident_of}@x"}}))


def _boom_grant(rt):
    raise AssertionError(f"unexpected token-rotating refresh-grant probe (fp of {rt[:4]}…)")


# ---------------------------------------------------------------------------
# (1) happy path: dead snapshot + valid leased live family → reseeded
# ---------------------------------------------------------------------------

def test_heal_reseeds_dead_snapshot_from_leased_live_family():
    env = _Env({"rayi2": _dead_snapshot("rt-snap-dead"), "other": _valid("at-o", "rt-o")},
               active="other", config=_ILGATE,
               flags={"rayi2": {"snapshot_refresh_dead": True}})
    try:
        _write_meta(env, "rayi2")
        env.plant_family("rayi2", "family-1", _valid("at-fam", "rt-fam"))
        _write_family_identity("rayi2", "family-1", "rayi2")
        env.make_slot(account="rayi2", live=True,
                      mount_creds=_valid("at-fam", "rt-fam"), family_id="family-1")
        # Heal must be pure-disk: any refresh-grant probe is a #104 rotation bug.
        env.patch(cus, "_oauth_refresh_grant", _boom_grant)

        state = cus.load_state()
        res = cus.heal_snapshot_from_live_family("rayi2", state, cus.load_config())

        assert res["healed"] and res["action"] == "healed" and res["family"] == "family-1", res
        # Snapshot is now a byte-copy of the family store (the alias the #104
        # guards recognize), flags are reconciled, and a GH #79 backup exists.
        store = json.loads(cus.login_family_creds_path("rayi2", "family-1").read_text())
        assert env.snap_creds("rayi2") == store
        assert "snapshot_refresh_dead" not in state["accounts"]["rayi2"]
        acct_dir = env.accounts_dir / "account-rayi2"
        assert any(p.name.startswith(".credentials.json.bak") for p in acct_dir.iterdir()), \
            "GH #79 backup missing"
        wrote = [l for l in env.audit_lines("snapshot-heal-from-family") if "decision=wrote" in l]
        assert wrote and "login_family=rayi2/family-1" in wrote[0], env.echoes
        assert "rt-fam" not in wrote[0] and "at-fam" not in wrote[0], "raw token leaked into audit"
    finally:
        env.restore()


# ---------------------------------------------------------------------------
# (2) identity guard: foreign-identity family is refused as a heal source
# ---------------------------------------------------------------------------

def test_heal_refuses_family_with_foreign_identity():
    env = _Env({"rayi2": _dead_snapshot("rt-snap-dead")}, active="rayi2", config=_ILGATE,
               flags={"rayi2": {"snapshot_refresh_dead": True}})
    try:
        _write_meta(env, "rayi2")  # trusted anchor: rayi2
        env.plant_family("rayi2", "family-1", _valid("at-evil", "rt-evil"))
        _write_family_identity("rayi2", "family-1", "evil")  # /login landed on WRONG account
        before = env.snap_creds("rayi2")

        state = cus.load_state()
        res = cus.heal_snapshot_from_live_family("rayi2", state, cus.load_config())

        assert not res["healed"] and res["action"] == "no-source", res
        assert env.snap_creds("rayi2") == before, "refused heal must not touch the snapshot"
        assert state["accounts"]["rayi2"].get("snapshot_refresh_dead") is True, \
            "flag must survive a refused heal (relogin SOS stays reachable)"
        refused = [l for l in env.audit_lines("snapshot-heal-from-family")
                   if "decision=refused-identity" in l]
        assert refused, env.echoes
    finally:
        env.restore()


# ---------------------------------------------------------------------------
# (3) expired family source is not a heal source
# ---------------------------------------------------------------------------

def test_heal_skips_expired_family_source():
    env = _Env({"rayi2": _dead_snapshot("rt-snap-dead")}, active="rayi2", config=_ILGATE,
               flags={"rayi2": {"snapshot_refresh_dead": True}})
    try:
        env.plant_family("rayi2", "family-1", _valid("at-fam", "rt-fam", expires_at=_PAST))
        before = env.snap_creds("rayi2")
        state = cus.load_state()
        res = cus.heal_snapshot_from_live_family("rayi2", state, cus.load_config())
        assert not res["healed"] and res["action"] == "no-source", res
        assert env.snap_creds("rayi2") == before
    finally:
        env.restore()


# ---------------------------------------------------------------------------
# (4) idempotence: an already-valid snapshot is flags-only reconciled
# ---------------------------------------------------------------------------

def test_heal_already_valid_snapshot_is_read_only():
    env = _Env({"rayi2": _valid("at-snap", "rt-snap")}, active="rayi2", config=_ILGATE,
               flags={"rayi2": {"snapshot_refresh_dead": True}})  # stale flag, healthy file
    try:
        env.plant_family("rayi2", "family-1", _valid("at-fam", "rt-fam"))
        before = env.snap_creds("rayi2")
        state = cus.load_state()
        res = cus.heal_snapshot_from_live_family("rayi2", state, cus.load_config())
        assert res["healed"] and res["action"] == "already-valid", res
        assert env.snap_creds("rayi2") == before, "already-valid heal must be byte-for-byte read-only"
        assert "snapshot_refresh_dead" not in state["accounts"]["rayi2"]
    finally:
        env.restore()


# ---------------------------------------------------------------------------
# (5) GH #77: a fresher snapshot is never clobbered by an older family copy
# ---------------------------------------------------------------------------

def test_gh77_fresher_snapshot_not_clobbered():
    # Blank-ACCESS snapshot with a FUTURE expiresAt newer than the family's: the
    # already-valid branch can't take it (blank access), and GH #77 must refuse
    # the older family copy rather than clobber the fresher file.
    snap = {"claudeAiOauth": {"accessToken": "", "refreshToken": "rt-snap", "expiresAt": _FUTURE}}
    env = _Env({"rayi2": snap}, active="rayi2", config=_ILGATE,
               flags={"rayi2": {"snapshot_refresh_dead": True}})
    try:
        env.plant_family("rayi2", "family-1", _valid("at-fam", "rt-fam", expires_at=_FUTURE_OLDER))
        before = env.snap_creds("rayi2")
        state = cus.load_state()
        res = cus.heal_snapshot_from_live_family("rayi2", state, cus.load_config())
        assert not res["healed"] and res["action"] == "skipped-stale", res
        assert env.snap_creds("rayi2") == before
    finally:
        env.restore()


# ---------------------------------------------------------------------------
# (6) aliasing invariant: no token-rotating probe of an aliased snapshot
# ---------------------------------------------------------------------------

def test_account_snapshot_dead_aliased_true_without_probe():
    # Snapshot's refresh token IS family-1's (post-heal shape). Expired access
    # would normally force the refresh-grant probe — the probe must NOT run
    # (it would rotate the live family's single-use token, #104).
    env = _Env({"rayi2": _valid("at-old", "rt-shared-fam", expires_at=_PAST)},
               active="rayi2", config=_ILGATE)
    try:
        env.plant_family("rayi2", "family-1", _valid("at-fam", "rt-shared-fam"))
        env.patch(cus, "_oauth_refresh_grant", _boom_grant)
        assert cus._account_snapshot_dead("rayi2", cus.load_config(), force=True) is True
    finally:
        env.restore()


def test_account_snapshot_dead_alias_beats_valid_access_cheap_path():
    # Even a currently-VALID aliased snapshot is dead-FOR-INSTALL: installing it
    # double-books the family. (A valid NON-aliased snapshot stays installable.)
    env = _Env({"rayi2": _valid("at-snap", "rt-shared-fam"),
                "clean": _valid("at-c", "rt-c-own")},
               active="rayi2", config=_ILGATE)
    try:
        env.plant_family("rayi2", "family-1", _valid("at-fam", "rt-shared-fam"))
        env.plant_family("clean", "family-1", _valid("at-cf", "rt-c-fam"))
        env.patch(cus, "_oauth_refresh_grant", _boom_grant)
        assert cus._account_snapshot_dead("rayi2", cus.load_config(), force=True) is True
        assert cus._account_snapshot_dead("clean", cus.load_config(), force=True) is False
    finally:
        env.restore()


def test_unstale_sweep_heals_aliased_snapshot_instead_of_probing():
    env = _Env({"rayi2": _valid("at-old", "rt-shared-fam", expires_at=_PAST)},
               active="rayi2", config=_ILGATE,
               flags={"rayi2": {"token_stale": True}})
    try:
        env.plant_family("rayi2", "family-1", _valid("at-fam", "rt-shared-fam"))
        env.patch(cus, "_oauth_refresh_grant", _boom_grant)
        cus._UNSTALE_ATTEMPT_MS.clear()
        state = cus.load_state()
        ok = cus._unstale_account_snapshot("rayi2", state, cus.load_config(), force=True)
        assert ok is True
        # Healed from the family store, not probed: the snapshot now carries the
        # family's valid access token and the stale flag is gone.
        store = json.loads(cus.login_family_creds_path("rayi2", "family-1").read_text())
        assert env.snap_creds("rayi2") == store
        assert "token_stale" not in state["accounts"]["rayi2"]
        skipped = [l for l in env.audit_lines("unstale-refresh") if "decision=skipped-aliased" in l]
        assert skipped, env.echoes
    finally:
        env.restore()


# ---------------------------------------------------------------------------
# (7) _refresh_account_token refuses the aliased snapshot, zero network
# ---------------------------------------------------------------------------

def test_refresh_account_token_skips_aliased_snapshot():
    env = _Env({"rayi2": _valid("at-old", "rt-shared-fam", expires_at=_PAST)},
               active="rayi2", config=_ILGATE)
    try:
        env.plant_family("rayi2", "family-1", _valid("at-fam", "rt-shared-fam"))

        def _no_network(*a, **k):
            raise AssertionError("aliased-snapshot refresh must not reach the network")
        env.patch(cus.urllib.request, "urlopen", _no_network)
        assert cus._refresh_account_token("rayi2") is False
    finally:
        env.restore()


# ---------------------------------------------------------------------------
# (8) diagnose: URGENT relogin downgrades to self-healing WARNING
# ---------------------------------------------------------------------------

def _rayi2_snapshot_conditions(state, config):
    return [c for c in cus.diagnose(state, config)
            if c.affected == "rayi2" and "snapshot creds are dead" in c.summary]


def test_diagnose_warns_autoheal_when_live_family_can_vouch():
    env = _Env({"rayi2": _dead_snapshot("rt-snap-dead"), "other": _valid("at-o", "rt-o")},
               active="other", config=_ILGATE,
               flags={"rayi2": {"snapshot_refresh_dead": True}})
    try:
        # family-1 valid but LEASED to a live slot → has_free_login_family False,
        # the exact 2026-07-10 rayi2 shape that used to scream URGENT for days.
        env.plant_family("rayi2", "family-1", _valid("at-fam", "rt-fam"))
        env.make_slot(account="rayi2", live=True,
                      mount_creds=_valid("at-fam", "rt-fam"), family_id="family-1")
        conds = _rayi2_snapshot_conditions(cus.load_state(), cus.load_config())
        assert conds and conds[0].severity == "warning", conds
        assert "auto-heal" in conds[0].summary and "no relogin needed" in conds[0].summary
    finally:
        env.restore()


def test_diagnose_stays_urgent_when_no_valid_family_source():
    env = _Env({"rayi2": _dead_snapshot("rt-snap-dead"), "other": _valid("at-o", "rt-o")},
               active="other", config=_ILGATE,
               flags={"rayi2": {"snapshot_refresh_dead": True}})
    try:
        # Only family is EXPIRED and leased → nothing can vouch; genuine relogin.
        env.plant_family("rayi2", "family-1", _valid("at-fam", "rt-fam", expires_at=_PAST))
        env.make_slot(account="rayi2", live=True,
                      mount_creds=_valid("at-fam", "rt-fam", expires_at=_PAST),
                      family_id="family-1")
        conds = _rayi2_snapshot_conditions(cus.load_state(), cus.load_config())
        assert conds and conds[0].severity == "urgent", conds
        assert "relogin required" in conds[0].summary
    finally:
        env.restore()


# ---------------------------------------------------------------------------
# (9) daemon pass: heal + persist
# ---------------------------------------------------------------------------

def test_auto_heal_dead_snapshots_heals_and_persists():
    env = _Env({"rayi2": _dead_snapshot("rt-snap-dead"), "other": _valid("at-o", "rt-o")},
               active="other", config=_ILGATE,
               flags={"rayi2": {"snapshot_refresh_dead": True}})
    try:
        env.plant_family("rayi2", "family-1", _valid("at-fam", "rt-fam"))
        env.make_slot(account="rayi2", live=True,
                      mount_creds=_valid("at-fam", "rt-fam"), family_id="family-1")
        healed = cus._auto_heal_dead_snapshots(cus.load_state(), cus.load_config())
        assert healed == ["rayi2"], healed
        # Persisted: a FRESH state read shows the flag gone (the pass save_states).
        assert "snapshot_refresh_dead" not in cus.load_state()["accounts"]["rayi2"]
        store = json.loads(cus.login_family_creds_path("rayi2", "family-1").read_text())
        assert env.snap_creds("rayi2") == store
    finally:
        env.restore()


def test_auto_heal_dead_snapshots_no_execute_is_dry():
    env = _Env({"rayi2": _dead_snapshot("rt-snap-dead"), "other": _valid("at-o", "rt-o")},
               active="other", config=_ILGATE,
               flags={"rayi2": {"snapshot_refresh_dead": True}})
    try:
        env.plant_family("rayi2", "family-1", _valid("at-fam", "rt-fam"))
        before = env.snap_creds("rayi2")
        healed = cus._auto_heal_dead_snapshots(cus.load_state(), cus.load_config(), no_execute=True)
        assert healed == []
        assert env.snap_creds("rayi2") == before
        assert cus.load_state()["accounts"]["rayi2"].get("snapshot_refresh_dead") is True
    finally:
        env.restore()


# ---------------------------------------------------------------------------
# (10) shared-mount install of an aliased snapshot is refused (#104)
# ---------------------------------------------------------------------------

def test_shared_mount_swap_refuses_aliased_snapshot_while_family_live():
    # Post-heal shape: rayi2's snapshot is a byte-copy of family-1, and family-1
    # is LIVE on a lane. Installing the snapshot into ~/.claude would double-book
    # the family (next rotation logs one side out) — must refuse, leaving the
    # shared mount's current creds untouched.
    env = _Env({"rayi2": _valid("at-fam", "rt-shared-fam"), "other": _valid("at-o", "rt-o")},
               active="other", config=_ILGATE)
    try:
        env.plant_family("rayi2", "family-1", _valid("at-fam", "rt-shared-fam"))
        env.make_slot(account="rayi2", live=True,
                      mount_creds=_valid("at-fam", "rt-shared-fam"), family_id="family-1")
        before = json.loads(env.creds_json.read_text())
        try:
            cus.execute_swap("rayi2")  # slot=None → the shared mount
            raise AssertionError("shared-mount install of an aliased snapshot must refuse")
        except RuntimeError as e:
            assert "refresh-token family" in str(e), e
        assert json.loads(env.creds_json.read_text()) == before, \
            "refusal must be a clean no-op for the shared mount"
        refused = [l for l in env.audit_lines("family-collision-refuse")]
        assert refused and "mount=shared-mount" in refused[0], env.echoes
    finally:
        env.restore()


if __name__ == "__main__":
    fns = [(n, f) for n, f in sorted(globals().items())
           if n.startswith("test_") and callable(f)]
    for name, fn in fns:
        fn()
        print(f"  ✓ {name}")
    print(f"{len(fns)} passed.")
