"""Tests for GH #190 Mechanism 2: same-account `cus slot move` = verify-and-reinstall.

Pre-#190, `cus slot move <slot> <its-own-account>` short-circuited to
"already on X, nothing to do" — useless as a heal gesture when the slot's
mount creds had died (the exact moment an operator reaches for the command).
Now a same-account move liveness-checks the mount: healthy ⇒ the old fast
no-op; provably DEAD ⇒ retire the slot's dead family lease and reinstall the
account from a verified source through execute_swap's full guard stack.

Contract proven here:
  (1) healthy same-account move: "nothing to do", zero grant calls, zero
      credential writes;
  (2) well-shaped-dead mount + leased family: family retired (.dead-<date>),
      execute_swap ran, mount ends VALID;
  (3) dead mount + dead canonical + a free family: the reinstall seeds from
      the verified family and records the lease;
  (4) dead mount + everything dead: exit 1 with the install-refusal text,
      mount creds untouched;
  (5) --dry-run on an expired mount: prints the VERIFY plan, zero grant
      calls, zero writes (a probe ROTATES the token — previews must never);
  (6) locked slot + dead mount: refused without --force, heals with it;
  (7) a DIFFERENT-account move is byte-identical to today (regression).

Run standalone:  python3 tests/test_slot_move_same_account.py
Run under pytest: pytest tests/test_slot_move_same_account.py
"""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import cus  # noqa: E402


# ---------------------------------------------------------------------------
# creds shapes (mirrors test_launch_liveness_gate / test_dead_snapshot_family_seed)
# ---------------------------------------------------------------------------

_FUTURE = 2_000_000_000_000  # currently-VALID access token (2033-ish, ms)
_PAST = 1_000                # well-shaped but EXPIRED — forces the probe path


def _valid(access: str = "at-live", refresh: str = "rt-live", expires_at: int = _FUTURE) -> dict:
    return {"claudeAiOauth": {"accessToken": access, "refreshToken": refresh, "expiresAt": expires_at}}


def _expired(refresh: str) -> dict:
    return {"claudeAiOauth": {"accessToken": "at-expired", "refreshToken": refresh, "expiresAt": _PAST}}


class _Env:
    """Throwaway on-disk tree with cus path constants repointed (same pattern
    as test_launch_liveness_gate._Env)."""

    def __init__(self, accounts: dict[str, dict], active: str,
                 config: dict | None = None) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        root = Path(self._tmp.name)
        self.root = root
        self.claude_dir = root / ".claude"
        self.accounts_dir = root / "claude-accounts"
        (self.claude_dir / "projects").mkdir(parents=True)
        self.accounts_dir.mkdir(parents=True)

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

        self.state_json = self.accounts_dir / "state.json"
        self.state_json.write_text(json.dumps({
            "active": active,
            "accounts": {n: {"next_swap_at_pct": 50, "current_5h_pct": 0.0,
                             "current_7d_pct": 0.0} for n in accounts},
            "slots": {},
            "swap_history": [],
        }))
        self.config_yaml = self.accounts_dir / "config.yaml"
        cus.write_yaml(self.config_yaml, config if config is not None else {"mode": "per_session"})
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

    def set_config(self, cfg: dict) -> None:
        cus.write_yaml(cus.CONFIG_YAML, cfg)

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


def _grant_map(mapping: dict[str, tuple], calls: list | None = None):
    def _grant(rt):
        if calls is not None:
            calls.append(rt)
        if rt not in mapping:
            raise AssertionError(f"unexpected refresh-grant probe of {rt!r}")
        return mapping[rt]
    return _grant


def _no_grant():
    def _grant(rt):
        raise AssertionError(f"refresh-grant probe fired unexpectedly for {rt!r}")
    return _grant


def _swap_recorder(env: _Env):
    """Wrap cus.execute_swap with a passthrough call recorder."""
    calls: list[tuple] = []
    real = cus.execute_swap

    def _wrapped(target, trigger="manual", slot=None, **kw):
        calls.append((target, trigger, slot, kw))
        return real(target, trigger=trigger, slot=slot, **kw)
    env.patch(cus, "execute_swap", _wrapped)
    return calls


def _move(slot: str, account: str, dry_run: bool = False, force: bool = False) -> None:
    """Drive the command's callback directly (SystemExit surfaces to the test)."""
    cus.slot_move_cmd.callback(slot, account, dry_run, force)


_ILGATE = {"independent_logins": {"use_independent_logins": True}, "mode": "per_session"}


# ---------------------------------------------------------------------------
# (1) healthy same-account move: nothing to do, zero network, zero writes
# ---------------------------------------------------------------------------

def test_healthy_same_account_move_is_fast_noop():
    env = _Env({"acct": _valid("at-canon", "rt-canon"), "other": _valid("at-o", "rt-o")},
               active="other", config=_ILGATE)
    try:
        slot = env.make_slot("acct", live=False, mount_creds=_valid("at-m", "rt-m"))
        mount_before = (cus.slot_path(slot) / ".credentials.json").read_bytes()
        canon_before = (env.accounts_dir / "account-acct" / ".credentials.json").read_bytes()
        env.patch(cus, "_oauth_refresh_grant", _no_grant())
        swaps = _swap_recorder(env)

        _move(slot, "acct")

        assert any("nothing to do" in e for e in env.echoes), env.echoes
        assert swaps == []
        assert (cus.slot_path(slot) / ".credentials.json").read_bytes() == mount_before
        assert (env.accounts_dir / "account-acct" / ".credentials.json").read_bytes() == canon_before
    finally:
        env.restore()


# ---------------------------------------------------------------------------
# (2) well-shaped dead + lease → family retired, reinstall ran, mount valid
# ---------------------------------------------------------------------------

def test_dead_mount_with_lease_retires_family_and_reinstalls():
    env = _Env({"acct": _valid("at-canon", "rt-canon"), "other": _valid("at-o", "rt-o")},
               active="other", config=_ILGATE)
    try:
        slot = env.make_slot("acct", live=False, mount_creds=_expired("rt-mount-dead"),
                             family_id="family-1")
        env.plant_family("acct", "family-1", _expired("rt-mount-dead"))
        env.patch(cus, "_oauth_refresh_grant", _grant_map({"rt-mount-dead": ("dead", None)}))
        swaps = _swap_recorder(env)

        _move(slot, "acct")

        # Verify-and-reinstall announced; the dead family store retired.
        assert any("verify-and-reinstall" in e for e in env.echoes), env.echoes
        fam_path = cus.login_family_creds_path("acct", "family-1")
        assert not fam_path.exists()
        assert any(".dead-" in p.name for p in fam_path.parent.iterdir())
        # execute_swap ran the reinstall; mount ends valid on canonical tokens.
        assert swaps and swaps[0][1] == "manual-slot-move", swaps
        assert swaps[0][3].get("force_reinstall") is True, swaps
        mount = env.slot_creds(slot)
        assert not cus._live_mount_creds_invalid(mount), mount
        assert cus._credential_refresh_token(mount) == "rt-canon", mount
        assert "login_family" not in cus.load_state()["slots"][slot]
    finally:
        env.restore()


# ---------------------------------------------------------------------------
# (3) dead mount + dead canonical + a FREE family → seeded from family + lease
# ---------------------------------------------------------------------------

def test_dead_mount_dead_canonical_seeds_from_free_family():
    env = _Env({"acct": _expired("rt-canon-dead"), "other": _valid("at-o", "rt-o")},
               active="other", config=_ILGATE)
    try:
        slot = env.make_slot("acct", live=False, mount_creds=_expired("rt-mount-dead"))
        env.plant_family("acct", "family-1", _valid("at-fam", "rt-fam"))
        env.patch(cus, "_oauth_refresh_grant", _grant_map({
            "rt-mount-dead": ("dead", None),
            "rt-canon-dead": ("dead", None),
            "rt-fam": ("alive", {"access_token": "at-fam-new",
                                 "refresh_token": "rt-fam-new", "expires_in": 3600}),
        }))

        _move(slot, "acct")

        # Seeded from the verified family (rotated tokens), lease recorded.
        mount = env.slot_creds(slot)
        assert not cus._live_mount_creds_invalid(mount), mount
        assert cus._credential_refresh_token(mount) == "rt-fam-new", mount
        assert cus.load_state()["slots"][slot]["login_family"] == "acct/family-1"
    finally:
        env.restore()


# ---------------------------------------------------------------------------
# (4) dead mount + everything dead → exit 1, refusal text, mount unchanged
# ---------------------------------------------------------------------------

def test_everything_dead_refuses_with_exit_1_mount_unchanged():
    env = _Env({"acct": _expired("rt-canon-dead"), "other": _valid("at-o", "rt-o")},
               active="other", config=_ILGATE)
    try:
        slot = env.make_slot("acct", live=False, mount_creds=_expired("rt-mount-dead"))
        before = (cus.slot_path(slot) / ".credentials.json").read_bytes()
        env.patch(cus, "_oauth_refresh_grant", _grant_map({
            "rt-mount-dead": ("dead", None), "rt-canon-dead": ("dead", None)}))

        with pytest.raises(SystemExit) as ei:
            _move(slot, "acct")
        assert ei.value.code == 1
        # execute_swap's own dead-snapshot refusal, surfaced verbatim.
        err = [e for e in env.echoes if "ERROR:" in e]
        assert err and "canonical snapshot credentials are DEAD" in err[0], env.echoes
        assert (cus.slot_path(slot) / ".credentials.json").read_bytes() == before
    finally:
        env.restore()


# ---------------------------------------------------------------------------
# (5) --dry-run on an expired mount: VERIFY plan, zero grant calls, zero writes
# ---------------------------------------------------------------------------

def test_dry_run_expired_prints_verify_and_makes_no_writes():
    env = _Env({"acct": _valid("at-canon", "rt-canon"), "other": _valid("at-o", "rt-o")},
               active="other", config=_ILGATE)
    try:
        slot = env.make_slot("acct", live=False, mount_creds=_expired("rt-x"),
                             family_id="family-1")
        env.plant_family("acct", "family-1", _valid("at-fam", "rt-fam"))
        mount_before = (cus.slot_path(slot) / ".credentials.json").read_bytes()
        state_before = cus.STATE_JSON.read_bytes()
        env.patch(cus, "_oauth_refresh_grant", _no_grant())  # a preview must NEVER probe
        swaps = _swap_recorder(env)

        _move(slot, "acct", dry_run=True)

        assert any("VERIFY" in e for e in env.echoes), env.echoes
        assert swaps == []
        assert (cus.slot_path(slot) / ".credentials.json").read_bytes() == mount_before
        assert cus.STATE_JSON.read_bytes() == state_before
        assert cus.login_family_creds_path("acct", "family-1").exists(), "dry-run must not retire"
    finally:
        env.restore()


def test_dry_run_healthy_prints_noop():
    env = _Env({"acct": _valid("at-canon", "rt-canon"), "other": _valid("at-o", "rt-o")},
               active="other", config=_ILGATE)
    try:
        slot = env.make_slot("acct", live=False, mount_creds=_valid("at-m", "rt-m"))
        env.patch(cus, "_oauth_refresh_grant", _no_grant())
        _move(slot, "acct", dry_run=True)
        assert any("NOOP" in e and "healthy" in e for e in env.echoes), env.echoes
    finally:
        env.restore()


# ---------------------------------------------------------------------------
# (6) locked slot + dead mount: refuse without --force, heal with it
# ---------------------------------------------------------------------------

def test_locked_slot_dead_mount_refuses_then_heals_with_force():
    env = _Env({"acct": _valid("at-canon", "rt-canon"), "other": _valid("at-o", "rt-o")},
               active="other", config=_ILGATE)
    try:
        slot = env.make_slot("acct", live=False, mount_creds=_expired("rt-mount-dead"))
        env.set_config({**_ILGATE, "session_locks": {"locked_slots": [slot]}})
        grant_calls: list[str] = []
        env.patch(cus, "_oauth_refresh_grant",
                  _grant_map({"rt-mount-dead": ("dead", None)}, calls=grant_calls))

        # Without --force: the lock wins (locks are user intent) — exit 1, dead
        # mount untouched.
        with pytest.raises(SystemExit) as ei:
            _move(slot, "acct")
        assert ei.value.code == 1
        assert any("locked" in e for e in env.echoes), env.echoes
        assert cus._credential_refresh_token(env.slot_creds(slot)) == "rt-mount-dead"

        # With --force: heals (probe verdict comes from the cooldown cache —
        # still exactly one grant call across both attempts).
        _move(slot, "acct", force=True)
        mount = env.slot_creds(slot)
        assert not cus._live_mount_creds_invalid(mount), mount
        assert cus._credential_refresh_token(mount) == "rt-canon", mount
        assert grant_calls == ["rt-mount-dead"], grant_calls
    finally:
        env.restore()


# ---------------------------------------------------------------------------
# (7) different-account move: byte-identical to today (regression)
# ---------------------------------------------------------------------------

def test_different_account_move_unchanged():
    env = _Env({"acct": _valid("at-a", "rt-a"), "dest": _valid("at-d", "rt-d"),
                "bystander": _valid("at-b", "rt-b")},
               active="bystander", config=_ILGATE)
    try:
        slot = env.make_slot("acct", live=False, mount_creds=_valid("at-a", "rt-a"))
        env.patch(cus, "_oauth_refresh_grant", _no_grant())
        swaps = _swap_recorder(env)

        _move(slot, "dest")

        assert any(f"moved {slot}: acct -> dest" in e for e in env.echoes), env.echoes
        # Plain execute_swap with today's arguments: no force_reinstall, ladder
        # bump intact (the outgoing account's threshold advanced 50 -> 75).
        assert swaps and swaps[0][:3] == ("dest", "manual-slot-move", slot), swaps
        assert swaps[0][3].get("force_reinstall") is False, swaps
        assert swaps[0][3].get("bump_ladder") is True, swaps
        mount = env.slot_creds(slot)
        assert cus._credential_refresh_token(mount) == "rt-d", mount
        state = cus.load_state()
        assert state["slots"][slot]["account"] == "dest"
        assert state["accounts"]["acct"]["next_swap_at_pct"] == 75, "ladder bump must be unchanged"
    finally:
        env.restore()


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in fns:
        fn()
        print(f"ok {fn.__name__}")
    print(f"{len(fns)} tests passed")
