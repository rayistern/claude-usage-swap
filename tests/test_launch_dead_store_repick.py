"""`cus launch auto` re-picks past a DEAD legacy per-slot login store (2026-08-30).

THE BUG (follow-up to c03767b):
`cus launch auto` picks the best account + slot, then installs the creds via
`execute_swap`. c03767b added a guard in `_execute_swap_locked` that REFUSES to
install a slot's LEGACY per-slot login store when its OAuth refresh grant is
dead (invalid_grant — an unrotated `--from-existing` copy whose token family a
different live mount rotated away). Installing it would blank the mount and log
the session out (the 2026-08-10 slot-14->03 incident). The guard is correct,
but it raised a plain RuntimeError that propagated straight out of
`_launch_prepare` and crashed the whole `cus launch auto` with a traceback.

THE FIX:
The guard now raises `DeadLegacyStoreError` (a RuntimeError subclass, so every
existing `except RuntimeError` handler — daemon lane-move, `cus slot move`,
launch-gate reinstall — keeps catching it byte-for-byte). `_launch_prepare`'s
`auto` path catches THAT type specifically and EXCLUDES the failed slot, then
re-picks (reusing the GH #192 `acquire_slot(exclude=...)` machinery). An
explicit `--lane` still fails LOUD (the operator chose that slot); only `auto`
re-picks. When every slot is exhausted, `auto` fails loud with the guard's
remediation (`cus login-mount <account>`), not a raw traceback.

This suite proves that contract. `execute_swap` is stubbed to raise
`DeadLegacyStoreError` for the doomed slot(s) — that isolates the NEW control
flow (`_launch_prepare`'s re-pick loop) from the guard's already-covered probe
machinery (`_store_creds_dead` / `_oauth_refresh_grant`).

Run standalone:  python3 tests/test_launch_dead_store_repick.py
Run under pytest: pytest tests/test_launch_dead_store_repick.py
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

import click
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import cus  # noqa: E402


def _creds(refresh: str, expires_at: int = 2_000_000_000_000) -> dict:
    return {"claudeAiOauth": {"accessToken": f"at-{refresh}", "refreshToken": refresh,
                              "expiresAt": expires_at}}


def _identity(name: str) -> dict:
    return {"userID": f"uid-{name}",
            "oauthAccount": {"emailAddress": f"{name}@x", "accountUuid": f"uuid-{name}"}}


class _Env:
    """Temp HOME with three accounts (alpha/beta/gamma), gamma the shared-mount
    active. Mirrors tests/test_launch_stale_low_verify.py's _Env so
    `_launch_prepare` runs its full slot-acquire/install flow. `poll_account_usage`
    is mocked (the auto-pick verify loop calls it) and `mount_pids` returns []
    (nothing live → no GH #104 double-book blocks)."""

    def __init__(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        root = Path(self._tmp.name)
        self.claude_dir = root / ".claude"
        self.claude_json = root / ".claude.json"
        self.accounts_dir = root / "claude-accounts"

        (self.claude_dir / "projects").mkdir(parents=True)
        (self.claude_dir / "settings.json").write_text("{}")
        (self.claude_dir / ".credentials.json").write_text(json.dumps(_creds("rt-gamma")))
        self.claude_json.write_text(json.dumps({**_identity("gamma"), "mcpServers": {"m": {}}}))

        for name in ("alpha", "beta", "gamma"):
            d = self.accounts_dir / f"account-{name}"
            d.mkdir(parents=True)
            (d / ".credentials.json").write_text(json.dumps(_creds(f"rt-{name}")))
            (d / ".claude.json").write_text(json.dumps(_identity(name)))

        cus.write_json(self.accounts_dir / "state.json", {
            "active": "gamma",
            "accounts": {
                "alpha": {"next_swap_at_pct": 90, "current_5h_pct": 10.0, "current_7d_pct": 10.0},
                "beta": {"next_swap_at_pct": 90, "current_5h_pct": 20.0, "current_7d_pct": 15.0},
                "gamma": {"next_swap_at_pct": 90, "current_5h_pct": 5.0, "current_7d_pct": 5.0},
            },
            "swap_history": [],
        })

        self._saved = {k: getattr(cus, k) for k in
                       ("HOME", "CLAUDE_DIR", "CLAUDE_JSON", "CREDS_JSON", "ACCOUNTS_DIR",
                        "STATE_JSON", "CONFIG_YAML")}
        cus.HOME = root
        cus.CLAUDE_DIR = self.claude_dir
        cus.CLAUDE_JSON = self.claude_json
        cus.CREDS_JSON = self.claude_dir / ".credentials.json"
        cus.ACCOUNTS_DIR = self.accounts_dir
        cus.STATE_JSON = self.accounts_dir / "state.json"
        cus.CONFIG_YAML = self.accounts_dir / "config.yaml"

        self._saved_mount_pids = cus.mount_pids
        cus.mount_pids = lambda mount: []  # nothing live → no #104 blocks
        self._saved_poll = cus.poll_account_usage
        self._saved_env = os.environ.get("CLAUDE_CONFIG_DIR")
        os.environ.pop("CLAUDE_CONFIG_DIR", None)
        cus._OCCUPIED_SLOTS_CACHE.clear()

        # Fresh readings the auto-pick verify loop observes; all clean so the
        # picker's top choice is launched as-is (the verify loop is NOT what this
        # suite tests — it's covered by test_launch_stale_low_verify).
        def _fake_poll(name: str) -> "cus.AccountUsage":
            u = cus.AccountUsage.empty()
            u.five_hour = cus.UsageWindow(utilization=0.0, resets_at=None)
            u.seven_day = cus.UsageWindow(utilization=0.0, resets_at=None)
            return u

        cus.poll_account_usage = _fake_poll

        self.echoes: list[str] = []
        self._saved_echo = cus.click.echo
        cus.click.echo = lambda *a, **k: self.echoes.append(
            " ".join(str(x) for x in a) if a else "")

        self._patches: list[tuple[object, str, object]] = []

    def patch(self, obj: object, name: str, value: object) -> None:
        self._patches.append((obj, name, getattr(obj, name)))
        setattr(obj, name, value)

    def restore(self) -> None:
        for obj, name, value in reversed(self._patches):
            setattr(obj, name, value)
        cus.click.echo = self._saved_echo
        for k, v in self._saved.items():
            setattr(cus, k, v)
        cus.mount_pids = self._saved_mount_pids
        cus.poll_account_usage = self._saved_poll
        if self._saved_env is not None:
            os.environ["CLAUDE_CONFIG_DIR"] = self._saved_env
        else:
            os.environ.pop("CLAUDE_CONFIG_DIR", None)
        cus._OCCUPIED_SLOTS_CACHE.clear()
        self._tmp.cleanup()


def _dead_on_slots(env: _Env, doomed: set[str] | None, *,
                   doom_first: bool = False) -> list[tuple]:
    """Stub cus.execute_swap so it raises DeadLegacyStoreError for the doomed
    slot(s) and records every (account, trigger, slot) install it was asked for.

    `doomed`      : explicit set of slot NAMES whose install must fail.
    `doom_first`  : instead of naming slots up front, fail the FIRST slot ever
                    asked for and succeed for any DIFFERENT slot — used by the
                    re-pick test so it needn't predict acquire_slot's ordering.
    A success stub records the slot's account into state (what the real launch
    swap does) so post-install bookkeeping in _launch_prepare is realistic.
    """
    calls: list[tuple] = []
    doomed = set(doomed or ())

    def _stub(target, trigger="manual", slot=None, **kw):
        calls.append((target, trigger, slot))
        fail = slot in doomed
        if doom_first and not calls[:-1]:  # first call overall
            doomed.add(slot)
            fail = True
        if fail:
            raise cus.DeadLegacyStoreError(
                f"refusing to install '{target}' onto lane {slot}: its legacy per-slot login "
                f"store is DEAD (the OAuth refresh grant returns invalid_grant). Provision a "
                f"fresh family: `cus login-mount {target}`.")
        # Success: mimic the real swap recording the slot's occupant.
        st = cus.load_state()
        st.setdefault("slots", {}).setdefault(slot, {})["account"] = target
        cus.save_state(st)
        return st

    env.patch(cus, "execute_swap", _stub)
    return calls


# ---------------------------------------------------------------------------
# (0) the exception is a RuntimeError subclass — every existing handler keeps
#     catching it, so only the launch auto path's targeted catch is new.
# ---------------------------------------------------------------------------

def test_dead_legacy_store_error_is_runtimeerror_subclass():
    assert issubclass(cus.DeadLegacyStoreError, RuntimeError)
    # And an instance is caught by a plain `except RuntimeError` (the shape every
    # daemon / slot-move / launch-gate caller already uses).
    try:
        raise cus.DeadLegacyStoreError("boom")
    except RuntimeError as e:
        assert str(e) == "boom"
    else:  # pragma: no cover - the raise above always fires
        pytest.fail("DeadLegacyStoreError was not caught as a RuntimeError")


# ---------------------------------------------------------------------------
# (1) THE FIX: auto re-picks past a dead-legacy-store slot onto a healthy one
#     (before this change the guard's RuntimeError crashed cus launch auto).
# ---------------------------------------------------------------------------

def test_auto_repicks_past_dead_store_slot_onto_healthy():
    env = _Env()
    try:
        # The first slot acquire_slot hands out has a DEAD legacy store; any
        # re-picked (different) slot installs fine.
        calls = _dead_on_slots(env, doomed=None, doom_first=True)

        slot_name, slot_dir, account = cus._launch_prepare(
            "auto", cus.load_state(), cus.load_config())

        # Landed on a HEALTHY slot (the second one tried), not crashed.
        assert account in {"alpha", "beta", "gamma"}, account
        assert len(calls) >= 2, f"expected a re-pick after the dead-store slot, got {calls}"
        first_slot = calls[0][2]
        final_slot = calls[-1][2]
        assert final_slot != first_slot, f"must re-pick a DIFFERENT slot: {calls}"
        assert slot_name == final_slot, (slot_name, calls)
        # Every install was a plain launch swap onto a slot.
        assert all(c[1] == "launch" for c in calls), calls
        # The operator-facing re-pick line was emitted.
        assert any("legacy login store" in e and "DEAD" in e and first_slot in e
                   for e in env.echoes), env.echoes
    finally:
        env.restore()


# ---------------------------------------------------------------------------
# (2) explicit --lane on a dead-store slot FAILS LOUD (operator chose it) — no
#     re-pick, a clean ClickException carrying the guard's remediation.
# ---------------------------------------------------------------------------

def test_explicit_lane_on_dead_store_errors_no_repick():
    env = _Env()
    try:
        lane = "slot-1"
        # Whatever slot the explicit lane resolves to, its install is doomed.
        calls: list[tuple] = []

        def _stub(target, trigger="manual", slot=None, **kw):
            calls.append((target, trigger, slot))
            raise cus.DeadLegacyStoreError(
                f"refusing to install '{target}' onto lane {slot}: its legacy per-slot login "
                f"store is DEAD (invalid_grant). Provision a fresh family: `cus login-mount {target}`.")

        env.patch(cus, "execute_swap", _stub)

        with pytest.raises(click.ClickException) as ei:
            cus._launch_prepare("alpha", cus.load_state(), cus.load_config(), lane=lane)

        msg = str(ei.value)
        # Loud, clean failure (NOT a raw DeadLegacyStoreError traceback) that
        # preserves the guard's remediation.
        assert "legacy per-slot login" in msg and "login-mount" in msg, msg
        # --lane must NOT re-pick: exactly one install attempt, on the chosen lane.
        assert len(calls) == 1, f"--lane must not re-pick, got {calls}"
        assert calls[0][2] == lane, calls
    finally:
        env.restore()


# ---------------------------------------------------------------------------
# (3) auto with EVERY slot dead → fails loud with the same remediation, not a
#     raw traceback.
# ---------------------------------------------------------------------------

def test_auto_all_slots_dead_fails_loud_with_remediation():
    env = _Env()
    try:
        # Every install, on every slot, is refused.
        def _stub(target, trigger="manual", slot=None, **kw):
            raise cus.DeadLegacyStoreError(
                f"refusing to install '{target}' onto lane {slot}: dead legacy store.")

        env.patch(cus, "execute_swap", _stub)

        with pytest.raises(click.ClickException) as ei:
            cus._launch_prepare("auto", cus.load_state(), cus.load_config())

        msg = str(ei.value)
        assert "no launchable slot" in msg, msg
        assert "login-mount" in msg, msg  # the remediation, not a bare traceback
    finally:
        env.restore()


# ---------------------------------------------------------------------------
# (4) INTEGRATION: the REAL guard in _execute_swap_locked raises the SUBCLASS
#     (not a bare RuntimeError), which is what makes the auto catch above fire
#     in production. Drives a genuine empty-slot install onto a planted DEAD
#     legacy per-slot store with the pool gate on.
# ---------------------------------------------------------------------------

def _expired_store(refresh: str) -> dict:
    """Well-SHAPED but suspect: present-but-expired access token, positive-but-
    past expiresAt — `_live_mount_creds_invalid` reads it as valid-shaped, so
    `_store_creds_dead` must PROBE the refresh grant to tell dead from expired."""
    return {"claudeAiOauth": {"accessToken": "at-expired", "refreshToken": refresh, "expiresAt": 1000}}


def test_real_guard_raises_dead_legacy_store_error():
    env = _Env()
    try:
        # Pool gate ON: makes the legacy-login install source (and thus the
        # dead-store guard) reachable. Empty target-less slot = cus launch's
        # install primitive (no outgoing save-back).
        cus.write_yaml(cus.CONFIG_YAML,
                       {"mode": "per_session",
                        "independent_logins": {"use_independent_logins": True}})
        state = cus.load_state()
        slot, _sdir = cus.create_slot(state)  # account stays None (empty slot)
        cus.save_state(state)

        # Plant a LEGACY per-(account, slot) login store whose refresh grant is
        # dead — the unrotated `--from-existing` copy the guard defends against.
        store = cus.login_store_creds_path("alpha", slot)
        store.parent.mkdir(parents=True, exist_ok=True)
        store.write_text(json.dumps(_expired_store("rt-legacy-dead")))
        assert cus.has_independent_login("alpha", slot)

        # The refresh grant returns invalid_grant for the store's token → dead.
        cus._STORE_DEAD_PROBE.clear()
        env.patch(cus, "_oauth_refresh_grant",
                  lambda rt: ("dead", None) if rt == "rt-legacy-dead"
                  else (_ for _ in ()).throw(AssertionError(f"unexpected probe of {rt!r}")))

        with pytest.raises(cus.DeadLegacyStoreError) as ei:
            cus.execute_swap("alpha", trigger="launch", slot=slot)

        msg = str(ei.value)
        assert "legacy per-slot login" in msg and "login-mount alpha" in msg, msg
        # The mount was left untouched — a freshly-scaffolded empty slot never
        # got dead creds written into it (the guard refuses BEFORE the copy).
        mount = cus.mount_creds_path(cus.slot_path(slot))
        if mount.exists():
            assert cus._credential_refresh_token(cus.read_json(mount)) != "rt-legacy-dead"
    finally:
        env.restore()


if __name__ == "__main__":  # pragma: no cover - standalone runner
    sys.exit(pytest.main([__file__, "-q"]))
