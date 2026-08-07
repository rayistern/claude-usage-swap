"""Tests for the GH #190 launch-time credential liveness gate (Mechanism 1).

THE HOLE: `cus launch` onto a slot that ALREADY holds the requested account
took a fast path — no execute_swap, no creds validation — and exec'd the
session onto whatever bytes sat in the slot's .credentials.json. Those bytes
can be well-SHAPED but DEAD (an expired access token whose refresh token fails
the OAuth grant with invalid_grant — the merkos dead-snapshot shape), so the
new session opened straight into "Not logged in · Run /login". Lane JOINs had
the same hole: a blank live lane happily accepted a new session.

This suite proves the gate's contract:
  (1) a HEALTHY same-account mount is zero-network and zero-swap (fast path
      unchanged);
  (2) a well-shaped-but-DEAD mount retires the slot's leased family
      (.dead-<date> rename + lease pop) and reinstalls via execute_swap,
      ending with a VALID mount;
  (3) dead mount + dead canonical + no family ⇒ ClickException (refusal),
      mount creds untouched;
  (4) a probe "unknown" verdict FAILS OPEN — launch proceeds as today;
  (5) launch_gate.enabled=False restores pre-#190 trust-the-slot behavior;
  (6) the probe is cooldown-cached — two launches inside the window make ONE
      grant call;
  (7) joining a BLANK live lane is refused (shape check only, never a probe);
      a healthy lane join is unchanged;
  (8) an expired-but-ALIVE mount is rotated in place (mount + leased family
      store both get the fresh generation) with NO reinstall.

Run standalone:  python3 tests/test_launch_liveness_gate.py
Run under pytest: pytest tests/test_launch_liveness_gate.py
"""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

import click
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import cus  # noqa: E402


# ---------------------------------------------------------------------------
# creds shapes (mirrors test_dead_snapshot_family_seed)
# ---------------------------------------------------------------------------

# Far-future expiry: a currently-VALID access token (2033-ish in ms).
_FUTURE = 2_000_000_000_000
# Long-past expiry: an EXPIRED access token — well-shaped (passes the #141
# blank-shape predicate) but forces the gate past its cheap path into the
# refresh-grant probe.
_PAST = 1_000


def _valid(access: str = "at-live", refresh: str = "rt-live", expires_at: int = _FUTURE) -> dict:
    return {"claudeAiOauth": {"accessToken": access, "refreshToken": refresh, "expiresAt": expires_at}}


def _expired(refresh: str) -> dict:
    """Well-SHAPED but suspect: present-but-expired access token, positive-but-
    past expiresAt. `_live_mount_creds_invalid` reads it as valid-shaped; only
    the refresh grant can tell dead from merely-expired."""
    return {"claudeAiOauth": {"accessToken": "at-expired", "refreshToken": refresh, "expiresAt": _PAST}}


def _blank() -> dict:
    """The #141 blank signature a failed in-place refresh leaves behind."""
    return {"claudeAiOauth": {"accessToken": "", "expiresAt": 0}}


class _Env:
    """Throwaway on-disk tree with every cus path constant repointed at it
    (copied from test_dead_snapshot_family_seed._Env: slots + families + echo
    capture + /proc live-mount mock)."""

    def __init__(self, accounts: dict[str, dict], active: str,
                 config: dict | None = None) -> None:
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
    """Fake `_oauth_refresh_grant(rt)` from {refresh_token: verdict-tuple}.
    Unmapped tokens raise so a test fails loudly on an unexpected probe.
    `calls` (optional) records every probed token for call-count assertions."""
    def _grant(rt):
        if calls is not None:
            calls.append(rt)
        if rt not in mapping:
            raise AssertionError(f"unexpected refresh-grant probe of {rt!r}")
        return mapping[rt]
    return _grant


def _no_grant():
    """A grant that must never fire (zero-network assertions)."""
    def _grant(rt):
        raise AssertionError(f"refresh-grant probe fired unexpectedly for {rt!r}")
    return _grant


def _swap_recorder(env: _Env, passthrough: bool = True):
    """Wrap cus.execute_swap with a call recorder; passthrough=False stubs it
    out entirely (raising RuntimeError like a refused install)."""
    calls: list[tuple] = []
    real = cus.execute_swap

    def _wrapped(target, trigger="manual", slot=None, **kw):
        calls.append((target, trigger, slot, kw))
        if passthrough:
            return real(target, trigger=trigger, slot=slot, **kw)
        raise RuntimeError("stubbed reinstall refusal")
    env.patch(cus, "execute_swap", _wrapped)
    return calls


# Gate-on config: the pool model is what makes family retire/claim meaningful.
_ILGATE = {"independent_logins": {"use_independent_logins": True}, "mode": "per_session"}


# ---------------------------------------------------------------------------
# (1) healthy same-account mount: zero network, zero swap
# ---------------------------------------------------------------------------

def test_healthy_same_account_mount_no_probe_no_swap():
    env = _Env({"acct": _valid("at-a", "rt-a"), "other": _valid("at-o", "rt-o")},
               active="other", config=_ILGATE)
    try:
        slot = env.make_slot("acct", live=False, mount_creds=_valid("at-m", "rt-m"))
        env.patch(cus, "_oauth_refresh_grant", _no_grant())
        swaps = _swap_recorder(env)
        name, d, acct = cus._launch_prepare("acct", cus.load_state(), cus.load_config(), lane=slot)
        assert (name, acct) == (slot, "acct")
        assert swaps == [], "healthy fast path must not reinstall"
        # Mount untouched (same tokens).
        assert cus._credential_refresh_token(env.slot_creds(slot)) == "rt-m"
    finally:
        env.restore()


# ---------------------------------------------------------------------------
# (2) well-shaped-but-dead mount + leased family → retire + reinstall
# ---------------------------------------------------------------------------

def test_dead_mount_retires_family_and_reinstalls():
    env = _Env({"acct": _valid("at-canon", "rt-canon"), "other": _valid("at-o", "rt-o")},
               active="other", config=_ILGATE)
    try:
        slot = env.make_slot("acct", live=False, mount_creds=_expired("rt-mount-dead"),
                             family_id="family-1")
        env.plant_family("acct", "family-1", _expired("rt-mount-dead"))
        env.patch(cus, "_oauth_refresh_grant", _grant_map({"rt-mount-dead": ("dead", None)}))
        swaps = _swap_recorder(env)

        name, d, acct = cus._launch_prepare("acct", cus.load_state(), cus.load_config(), lane=slot)
        assert (name, acct) == (slot, "acct")

        # The dead family store was retired (renamed .dead-<date>), lease popped.
        fam_path = cus.login_family_creds_path("acct", "family-1")
        assert not fam_path.exists(), "dead family store must be renamed away"
        dead_siblings = [p.name for p in fam_path.parent.iterdir() if ".dead-" in p.name]
        assert dead_siblings, "expected a .dead-<YYYYMMDD> retirement rename"
        assert "login_family" not in cus.load_state()["slots"][slot]

        # A verified reinstall ran and the mount ended VALID (canonical tokens).
        assert swaps and swaps[0][1] == "launch-liveness-reinstall", swaps
        mount = env.slot_creds(slot)
        assert not cus._live_mount_creds_invalid(mount), mount
        assert cus._credential_refresh_token(mount) == "rt-canon", mount
    finally:
        env.restore()


# ---------------------------------------------------------------------------
# (3) dead mount + dead canonical + no family → refusal, mount untouched
# ---------------------------------------------------------------------------

def test_everything_dead_refuses_launch_mount_unchanged():
    env = _Env({"acct": _expired("rt-canon-dead"), "other": _valid("at-o", "rt-o")},
               active="other", config=_ILGATE)
    try:
        slot = env.make_slot("acct", live=False, mount_creds=_expired("rt-mount-dead"))
        before = (cus.slot_path(slot) / ".credentials.json").read_bytes()
        env.patch(cus, "_oauth_refresh_grant",
                  _grant_map({"rt-mount-dead": ("dead", None), "rt-canon-dead": ("dead", None)}))

        with pytest.raises(click.ClickException) as ei:
            cus._launch_prepare("acct", cus.load_state(), cus.load_config(), lane=slot)
        assert "launch gate (GH #190)" in str(ei.value), str(ei.value)
        # Mount creds bit-for-bit untouched — the refusal happened before any
        # install write.
        assert (cus.slot_path(slot) / ".credentials.json").read_bytes() == before
    finally:
        env.restore()


# ---------------------------------------------------------------------------
# (4) probe "unknown" → fail open, launch proceeds as today
# ---------------------------------------------------------------------------

def test_probe_unknown_fails_open_and_launches():
    env = _Env({"acct": _valid("at-canon", "rt-canon"), "other": _valid("at-o", "rt-o")},
               active="other", config=_ILGATE)
    try:
        slot = env.make_slot("acct", live=False, mount_creds=_expired("rt-maybe"))
        env.patch(cus, "_oauth_refresh_grant", _grant_map({"rt-maybe": ("unknown", None)}))
        swaps = _swap_recorder(env)
        name, d, acct = cus._launch_prepare("acct", cus.load_state(), cus.load_config(), lane=slot)
        assert (name, acct) == (slot, "acct")
        assert swaps == [], "an unverifiable mount must not trigger a reinstall (fail open)"
    finally:
        env.restore()


# ---------------------------------------------------------------------------
# (5) launch_gate.enabled: False → pre-#190 trust-the-slot behavior
# ---------------------------------------------------------------------------

def test_gate_disabled_dead_mount_still_launches():
    cfg = {**_ILGATE, "launch_gate": {"enabled": False}}
    env = _Env({"acct": _valid("at-canon", "rt-canon"), "other": _valid("at-o", "rt-o")},
               active="other", config=cfg)
    try:
        slot = env.make_slot("acct", live=False, mount_creds=_expired("rt-mount-dead"))
        env.patch(cus, "_oauth_refresh_grant", _no_grant())
        swaps = _swap_recorder(env)
        name, d, acct = cus._launch_prepare("acct", cus.load_state(), cus.load_config(), lane=slot)
        assert (name, acct) == (slot, "acct")
        assert swaps == [], "gate off must be bit-for-bit the old fast path"
        assert cus._credential_refresh_token(env.slot_creds(slot)) == "rt-mount-dead"
    finally:
        env.restore()


# ---------------------------------------------------------------------------
# (6) probe cooldown: two launches inside the window = ONE grant call
# ---------------------------------------------------------------------------

def test_probe_cooldown_caches_dead_verdict():
    env = _Env({"acct": _valid("at-canon", "rt-canon"), "other": _valid("at-o", "rt-o")},
               active="other", config=_ILGATE)
    try:
        slot = env.make_slot("acct", live=False, mount_creds=_expired("rt-mount-dead"))
        grant_calls: list[str] = []
        env.patch(cus, "_oauth_refresh_grant",
                  _grant_map({"rt-mount-dead": ("dead", None)}, calls=grant_calls))
        # Stub the reinstall as a refusal so the mount STAYS dead — isolates the
        # cooldown behavior from the (separately-tested) reinstall.
        _swap_recorder(env, passthrough=False)

        for _ in range(2):
            with pytest.raises(click.ClickException):
                cus._launch_prepare("acct", cus.load_state(), cus.load_config(), lane=slot)
        assert grant_calls == ["rt-mount-dead"], (
            f"expected exactly one probe inside the cooldown window, got {grant_calls}")
    finally:
        env.restore()


# ---------------------------------------------------------------------------
# (7) lane join: blank live lane refused (shape only); healthy join unchanged
# ---------------------------------------------------------------------------

_LANE_SHARE = {**_ILGATE, "per_session": {"lane_sharing": True}}


def test_lane_join_blank_live_lane_refused():
    env = _Env({"acct": _valid("at-canon", "rt-canon"), "other": _valid("at-o", "rt-o")},
               active="other", config=_LANE_SHARE)
    try:
        lane = env.make_slot("acct", live=True, mount_creds=_blank())
        env.patch(cus, "_oauth_refresh_grant", _no_grant())  # shape check ONLY, never a probe
        with pytest.raises(click.ClickException) as ei:
            cus._launch_prepare("acct", cus.load_state(), cus.load_config())
        assert f"refusing to join lane {lane}" in str(ei.value), str(ei.value)
    finally:
        env.restore()


def test_lane_join_healthy_lane_unchanged():
    env = _Env({"acct": _valid("at-canon", "rt-canon"), "other": _valid("at-o", "rt-o")},
               active="other", config=_LANE_SHARE)
    try:
        lane = env.make_slot("acct", live=True, mount_creds=_valid("at-lane", "rt-lane"))
        env.patch(cus, "_oauth_refresh_grant", _no_grant())
        name, d, acct = cus._launch_prepare("acct", cus.load_state(), cus.load_config())
        assert (name, acct) == (lane, "acct")
        assert cus._credential_refresh_token(env.slot_creds(lane)) == "rt-lane"
    finally:
        env.restore()


# ---------------------------------------------------------------------------
# (8) expired-but-ALIVE mount: rotate in place (mount + family), no reinstall
# ---------------------------------------------------------------------------

def test_expired_but_alive_mount_rotates_and_savebacks_no_reinstall():
    env = _Env({"acct": _valid("at-canon", "rt-canon"), "other": _valid("at-o", "rt-o")},
               active="other", config=_ILGATE)
    try:
        slot = env.make_slot("acct", live=False, mount_creds=_expired("rt-exp"),
                             family_id="family-1")
        # Family store deliberately OLDER than the rotation the probe will mint,
        # so the save-back's freshness guard lets the fresh generation land.
        env.plant_family("acct", "family-1", _expired("rt-exp"))
        env.patch(cus, "_oauth_refresh_grant",
                  _grant_map({"rt-exp": ("alive", {"access_token": "at-new",
                                                   "refresh_token": "rt-new",
                                                   "expires_in": 3600})}))
        swaps = _swap_recorder(env)

        name, d, acct = cus._launch_prepare("acct", cus.load_state(), cus.load_config(), lane=slot)
        assert (name, acct) == (slot, "acct")
        assert swaps == [], "an alive mount must not be reinstalled"

        # Rotated pair persisted into the MOUNT...
        mount = env.slot_creds(slot)
        assert cus._credential_refresh_token(mount) == "rt-new", mount
        assert not cus._live_mount_creds_invalid(mount)
        # ...AND into the leased family store (save-back), so the freshest
        # generation isn't stranded on the mount alone.
        fam = json.loads(cus.login_family_creds_path("acct", "family-1").read_text())
        assert cus._credential_refresh_token(fam) == "rt-new", fam
        # Lease intact — nothing was retired on the alive path.
        assert cus.load_state()["slots"][slot]["login_family"] == "acct/family-1"
    finally:
        env.restore()


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in fns:
        fn()
        print(f"ok {fn.__name__}")
    print(f"{len(fns)} tests passed")
