"""Tests for the dead-snapshot family-seed (2026-07-07 merkos incident).

THE INCIDENT (verified live): an account's canonical SNAPSHOT
(account-<name>/.credentials.json) can hold a DEAD refresh token — the OAuth
refresh grant returns invalid_grant — even while the account is perfectly usable
through a live LOGIN FAMILY (logins/<acct>/family-N/) that holds a valid token.
merkos was exactly this: its snapshot's refresh token was dead (the un-stale
sweep logs `op=unstale-refresh account=merkos decision=failed reason="refresh
grant dead"`) while lane tabby-2 ran on merkos via a valid family. The danger is
the SWAP install-point: installing the dead snapshot into a NEW lane mount blanks
that mount ("not logged in") on Claude Code's first refresh — exactly what
blanked chats1a when it was swapped onto merkos.

This suite proves:
  (a) swapping a lane onto an account with a DEAD snapshot but a VALID family
      seeds from the family (not the dead snapshot); the mount ends up VALID and
      a distinct family lease is recorded.
  (b) an account with a dead snapshot AND no valid family is excluded from
      pick_swap_target and surfaces the URGENT relogin SOS.
  (c) an account with a HEALTHY snapshot is unaffected (installs the snapshot as
      before; no refresh-grant probe, no family lease).
  (d) a live lane already running a valid family on a dead-snapshot account is
      never disturbed by the heal pass.
  (e) `_lane_heal_source` drops a DEAD snapshot from a plain lane's heal chain
      (returns None → escalate to relogin SOS) instead of looping heal→blank→heal.

Run standalone:  python3 tests/test_dead_snapshot_family_seed.py
Run under pytest: pytest tests/test_dead_snapshot_family_seed.py
"""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import cus  # noqa: E402


# ---------------------------------------------------------------------------
# creds shapes
# ---------------------------------------------------------------------------

# Far-future expiry: a currently-VALID access token (2033-ish in ms).
_FUTURE = 2_000_000_000_000
# Long-past expiry: an EXPIRED access token — the shape that forces
# _account_snapshot_dead past its cheap path and into the refresh-grant probe.
_PAST = 1_000


def _valid(access: str = "at-live", refresh: str = "rt-live", expires_at: int = _FUTURE) -> dict:
    return {"claudeAiOauth": {"accessToken": access, "refreshToken": refresh, "expiresAt": expires_at}}


def _dead_snapshot(refresh: str = "rt-dead") -> dict:
    """A well-SHAPED but DEAD snapshot: an expired-but-present access token and a
    positive-but-past expiresAt (so `_live_mount_creds_invalid` reads it as
    'valid-shaped'), whose refresh token fails the grant. This is the merkos
    shape that slips past the #141 blank-shape guards yet blanks the mount."""
    return {"claudeAiOauth": {"accessToken": "at-expired", "refreshToken": refresh, "expiresAt": _PAST}}


class _Env:
    """Throwaway on-disk tree with every cus path constant repointed at it, plus
    click.echo capture (for CRED-AUDIT assertions) and a /proc live-mount mock.
    Mirrors test_lane_mount_blank_heal._Env (slots + families) + the audit suite's
    echo capture."""

    def __init__(self, accounts: dict[str, dict], active: str,
                 flags: dict[str, dict] | None = None,
                 mode: str = "per_session", config: dict | None = None) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        root = Path(self._tmp.name)
        self.root = root
        self.claude_dir = root / ".claude"
        self.accounts_dir = root / "claude-accounts"
        (self.claude_dir / "projects").mkdir(parents=True)
        self.accounts_dir.mkdir(parents=True)

        # Shared mount stays HEALTHY so shared-mount checks never add noise.
        self.creds_json = self.claude_dir / ".credentials.json"
        self.creds_json.write_text(json.dumps(_valid("at-shared", "rt-shared")))
        self.claude_json = root / ".claude.json"
        self.claude_json.write_text(json.dumps(
            {"userID": f"uid-{active}",
             "oauthAccount": {"accountUuid": f"uuid-{active}", "emailAddress": f"{active}@x"}}))

        for name, creds in accounts.items():
            d = self.accounts_dir / f"account-{name}"
            d.mkdir()
            (d / ".credentials.json").write_text(json.dumps(creds))
            (d / ".claude.json").write_text(json.dumps(
                {"userID": f"uid-{name}",
                 "oauthAccount": {"accountUuid": f"uuid-{name}", "emailAddress": f"{name}@x"}}))

        flags = flags or {}
        self.state_json = self.accounts_dir / "state.json"
        self.state_json.write_text(json.dumps({
            "active": active,
            "accounts": {n: {"next_swap_at_pct": 50, "current_5h_pct": 0.0,
                             "current_7d_pct": 0.0, **flags.get(n, {})} for n in accounts},
            "slots": {},
            "swap_history": [],
        }))
        self.config_yaml = self.accounts_dir / "config.yaml"
        cus.write_yaml(self.config_yaml, config if config is not None else {"mode": mode})
        self.inbox_md = self.accounts_dir / "inbox.md"

        self._saved = {k: getattr(cus, k) for k in (
            "HOME", "CLAUDE_DIR", "CREDS_JSON", "CLAUDE_JSON", "ACCOUNTS_DIR",
            "STATE_JSON", "CONFIG_YAML", "INBOX_MD")}
        cus.HOME = root
        cus.CLAUDE_DIR = self.claude_dir
        cus.CREDS_JSON = self.creds_json
        cus.CLAUDE_JSON = self.claude_json
        cus.ACCOUNTS_DIR = self.accounts_dir
        cus.STATE_JSON = self.state_json
        cus.CONFIG_YAML = self.config_yaml
        cus.INBOX_MD = self.inbox_md

        self._saved_mount_pids = cus.mount_pids
        self.live_slots: set[str] = set()
        cus.mount_pids = lambda mount: [1] if Path(mount).name in self.live_slots else []
        cus._OCCUPIED_SLOTS_CACHE.clear()
        cus._reset_blank_tracking()

        self.echoes: list[str] = []
        self._saved_echo = cus.click.echo
        cus.click.echo = lambda *a, **k: self.echoes.append(
            " ".join(str(x) for x in a) if a else "")
        self._patches: list[tuple[object, str, object]] = []

    def patch(self, obj: object, name: str, value: object) -> None:
        self._patches.append((obj, name, getattr(obj, name)))
        setattr(obj, name, value)

    def make_slot(self, account: str | None, live: bool, mount_creds: dict | None,
                  family_id: str | None = None) -> str:
        state = cus.load_state()
        name, d = cus.create_slot(state)
        if mount_creds is not None:
            (d / ".credentials.json").write_text(json.dumps(mount_creds))
        (d / ".claude.json").write_text(json.dumps(
            {"oauthAccount": {"emailAddress": (f"{account}@x" if account else "empty@x")}}))
        state["slots"][name]["account"] = account
        if family_id:
            state["slots"][name]["login_family"] = f"{account}/{family_id}"
        cus.save_state(state)
        if live:
            self.live_slots.add(name)
        cus._OCCUPIED_SLOTS_CACHE.clear()
        return name

    def plant_family(self, account: str, family_id: str, creds: dict) -> None:
        d = cus.login_family_dir(account, family_id)
        d.mkdir(parents=True, exist_ok=True)
        cus.login_family_creds_path(account, family_id).write_text(json.dumps(creds))

    def slot_creds(self, slot: str) -> dict | None:
        p = cus.slot_path(slot) / ".credentials.json"
        return json.loads(p.read_text()) if p.exists() else None

    def snap_creds(self, name: str) -> dict:
        return json.loads((self.accounts_dir / f"account-{name}" / ".credentials.json").read_text())

    def audit_lines(self, op: str) -> list[str]:
        return [e for e in self.echoes if e.startswith(f"{cus.CRED_AUDIT_PREFIX} op={op}")]

    def restore(self) -> None:
        for obj, name, value in reversed(self._patches):
            setattr(obj, name, value)
        cus.click.echo = self._saved_echo
        for k, v in self._saved.items():
            setattr(cus, k, v)
        cus.mount_pids = self._saved_mount_pids
        cus._OCCUPIED_SLOTS_CACHE.clear()
        cus._reset_blank_tracking()
        self._tmp.cleanup()


def _grant_map(mapping: dict[str, tuple]):
    """Build a fake `_oauth_refresh_grant(rt)` from {refresh_token: verdict-tuple}.
    Unmapped tokens raise, so a test fails loudly if it probes something unexpected."""
    def _grant(rt):
        if rt not in mapping:
            raise AssertionError(f"unexpected refresh-grant probe of {rt!r}")
        return mapping[rt]
    return _grant


def _alive(access: str, refresh: str, expires_in: int = 3600):
    return ("alive", {"access_token": access, "refresh_token": refresh, "expires_in": expires_in})


_ILGATE = {"independent_logins": {"use_independent_logins": True}, "mode": "per_session"}


# ---------------------------------------------------------------------------
# (a) dead snapshot + valid family → seed the new lane from the family
# ---------------------------------------------------------------------------

def test_a_swap_onto_dead_snapshot_seeds_from_valid_family():
    env = _Env({"merkos": _dead_snapshot("rt-dead"), "other": _valid("at-o", "rt-o")},
               active="other", config=_ILGATE)
    try:
        # merkos snapshot refresh is DEAD; family-1's refresh is ALIVE.
        env.plant_family("merkos", "family-1", _valid("at-fam", "rt-fam"))
        env.patch(cus, "_oauth_refresh_grant",
                  _grant_map({"rt-dead": ("dead", None),
                              "rt-fam": _alive("at-fam-new", "rt-fam-new")}))
        # Empty target lane (a NEW lane being seeded onto merkos).
        slot = env.make_slot(account=None, live=False, mount_creds=None)
        cus.execute_swap("merkos", slot=slot)

        mount = env.slot_creds(slot)
        # The mount is VALID (not blanked) and carries the FAMILY's rotated token,
        # never the dead snapshot's rt-dead.
        assert not cus._live_mount_creds_invalid(mount), mount
        assert cus._credential_refresh_token(mount) == "rt-fam-new", mount
        assert mount["claudeAiOauth"]["refreshToken"] != "rt-dead"
        # A distinct family lease was recorded (#104-safe).
        assert cus.load_state()["slots"][slot]["login_family"] == "merkos/family-1"
        # The fallback audit line is emitted, naming the family it seeded from.
        seeded = env.audit_lines("snapshot-dead-family-fallback")
        assert seeded and "decision=seeded-from-family" in seeded[0], env.echoes
        assert "login_family=merkos/family-1" in seeded[0]
        assert "rt-dead" not in seeded[0] and "rt-fam" not in seeded[0], "raw token leaked"
    finally:
        env.restore()


# ---------------------------------------------------------------------------
# (b) dead snapshot + NO valid family → excluded from picker + relogin SOS
# ---------------------------------------------------------------------------

def test_b_dead_snapshot_no_family_excluded_from_picker():
    # 'dead' is flagged snapshot_refresh_dead with no family; 'healthy' is a clean
    # candidate. The picker must never choose 'dead'; it picks 'healthy'.
    env = _Env({"active": _valid("at-a", "rt-a"),
                "dead": _dead_snapshot("rt-dead"),
                "healthy": _valid("at-h", "rt-h")},
               active="active",
               flags={"dead": {"snapshot_refresh_dead": True, "current_5h_pct": 10.0},
                      "healthy": {"current_5h_pct": 10.0}},
               config={"strategy": "lowest_usage", **_ILGATE})
    try:
        target = cus.pick_swap_target(cus.load_state(), cus.load_config())
        assert target is not None and target.name == "healthy", target
    finally:
        env.restore()


def test_b_dead_snapshot_no_family_empties_pool_returns_none():
    env = _Env({"active": _valid("at-a", "rt-a"), "dead": _dead_snapshot("rt-dead")},
               active="active",
               flags={"dead": {"snapshot_refresh_dead": True, "current_5h_pct": 10.0}},
               config={"strategy": "lowest_usage", **_ILGATE})
    try:
        assert cus.pick_swap_target(cus.load_state(), cus.load_config()) is None
    finally:
        env.restore()


def test_b_dead_snapshot_WITH_family_still_selectable():
    # Same dead flag, but a free family exists → the install-point can seed from it,
    # so the account stays a valid target (NOT excluded).
    env = _Env({"active": _valid("at-a", "rt-a"), "dead": _dead_snapshot("rt-dead")},
               active="active",
               flags={"dead": {"snapshot_refresh_dead": True, "current_5h_pct": 10.0}},
               config={"strategy": "lowest_usage", **_ILGATE})
    try:
        env.plant_family("dead", "family-1", _valid("at-fam", "rt-fam"))
        target = cus.pick_swap_target(cus.load_state(), cus.load_config())
        assert target is not None and target.name == "dead", target
    finally:
        env.restore()


def test_b_dead_snapshot_no_family_emits_relogin_sos():
    env = _Env({"active": _valid("at-a", "rt-a"), "dead": _dead_snapshot("rt-dead")},
               active="active",
               flags={"dead": {"snapshot_refresh_dead": True}},
               config=_ILGATE)
    try:
        conds = cus.diagnose(cus.load_state(), cus.load_config())
        hits = [c for c in conds if c.affected == "dead"
                and "snapshot creds are dead" in c.summary and c.severity == "urgent"]
        assert hits, [(c.severity, c.summary) for c in conds]
        assert "cus relogin dead" in hits[0].action
    finally:
        env.restore()


def test_b_sweep_sets_dead_flag_from_grant_probe():
    # End-to-end mechanism: a token_stale account whose snapshot refresh is dead
    # gets `snapshot_refresh_dead` set by the idle-account un-stale sweep — the
    # signal the picker/SOS read (no separate network call).
    env = _Env({"active": _valid("at-a", "rt-a"), "merkos": _dead_snapshot("rt-dead")},
               active="active",
               flags={"merkos": {"token_stale": True}},
               config=_ILGATE)
    try:
        env.patch(cus, "_oauth_refresh_grant", _grant_map({"rt-dead": ("dead", None)}))
        state = cus.load_state()
        cus._sweep_unstale_idle_accounts(state, cus.load_config())
        assert state["accounts"]["merkos"].get("snapshot_refresh_dead") is True
    finally:
        env.restore()


# ---------------------------------------------------------------------------
# (c) healthy snapshot → unchanged behavior (installs snapshot, no probe)
# ---------------------------------------------------------------------------

def test_c_healthy_snapshot_installs_snapshot_no_probe():
    env = _Env({"good": _valid("at-good", "rt-good"), "other": _valid("at-o", "rt-o")},
               active="other", config=_ILGATE)
    try:
        # A VALID access token (future expiry) → _account_snapshot_dead's cheap path
        # returns not-dead WITHOUT a grant probe; assert the grant is never called.
        env.patch(cus, "_oauth_refresh_grant",
                  lambda rt: (_ for _ in ()).throw(AssertionError("healthy snapshot must not be probed")))
        slot = env.make_slot(account=None, live=False, mount_creds=None)
        cus.execute_swap("good", slot=slot)

        mount = env.slot_creds(slot)
        # Installed the SNAPSHOT verbatim (the pre-existing copy path).
        assert cus._credential_refresh_token(mount) == "rt-good", mount
        # No family lease, no dead-snapshot fallback.
        assert "login_family" not in cus.load_state()["slots"][slot]
        assert env.audit_lines("snapshot-dead-family-fallback") == []
        # The normal swap-install audit line still fires (account snapshot source).
        assert any("account=good" in ln and "account snapshot" in ln
                   for ln in env.audit_lines("swap-install")), env.echoes
    finally:
        env.restore()


# ---------------------------------------------------------------------------
# (d) a live lane on a dead-snapshot account (via valid family) is not disturbed
# ---------------------------------------------------------------------------

def test_d_live_lane_via_family_not_disturbed_by_heal():
    env = _Env({"merkos": _dead_snapshot("rt-dead"), "active": _valid("at-a", "rt-a")},
               active="active", config=_ILGATE)
    try:
        env.plant_family("merkos", "family-1", _valid("at-fam", "rt-fam"))
        # slot-1 is LIVE on merkos via family-1 with a VALID mount — the working lane.
        slot = env.make_slot(account="merkos", live=True,
                             mount_creds=_valid("at-fam", "rt-fam"), family_id="family-1")
        before = env.slot_creds(slot)
        # The heal pass must be a no-op for a healthy lane (nothing blanked).
        healed = cus._auto_heal_live_lanes(cus.load_state(), cus.load_config())
        assert healed == [], healed
        assert env.slot_creds(slot) == before, "healthy live lane mount was disturbed"
    finally:
        env.restore()


# ---------------------------------------------------------------------------
# (e) _lane_heal_source drops a DEAD snapshot from a plain lane's heal chain
# ---------------------------------------------------------------------------

def test_e_plain_lane_heal_source_drops_dead_snapshot():
    env = _Env({"merkos": _dead_snapshot("rt-dead"), "active": _valid("at-a", "rt-a")},
               active="active", config=_ILGATE)
    try:
        env.patch(cus, "_oauth_refresh_grant", _grant_map({"rt-dead": ("dead", None)}))
        # A PLAIN lane (no family lease) on merkos; no last-valid shadow exists.
        slot = env.make_slot(account="merkos", live=True, mount_creds=_valid("at-m", "rt-m"))
        # With the snapshot DEAD and no shadow, the heal source is None (escalate to
        # the relogin SOS) — NOT the dead snapshot (which would loop heal→blank→heal).
        src = cus._lane_heal_source(slot, "merkos", cus.load_state(), cus.load_config())
        assert src is None, src
    finally:
        env.restore()


def test_e_plain_lane_heal_source_uses_healthy_snapshot():
    # Control: a HEALTHY snapshot is still returned as the plain-lane heal source.
    env = _Env({"good": _valid("at-good", "rt-good"), "active": _valid("at-a", "rt-a")},
               active="active", config=_ILGATE)
    try:
        env.patch(cus, "_oauth_refresh_grant",
                  lambda rt: (_ for _ in ()).throw(AssertionError("healthy snapshot must not be probed")))
        slot = env.make_slot(account="good", live=True, mount_creds=_valid("at-m", "rt-m"))
        src = cus._lane_heal_source(slot, "good", cus.load_state(), cus.load_config())
        assert src == cus.account_creds_path("good"), src
    finally:
        env.restore()


def _run_all() -> int:
    import types
    tests = [v for k, v in sorted(globals().items())
             if k.startswith("test_") and isinstance(v, types.FunctionType)]
    failed = 0
    for t in tests:
        try:
            t()
            print(f"PASS  {t.__name__}")
        except Exception as e:  # noqa: BLE001
            failed += 1
            print(f"FAIL  {t.__name__}: {type(e).__name__}: {e}")
    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(_run_all())
