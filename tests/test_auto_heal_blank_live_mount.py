"""Tests for GH #141 — auto-healing a blanked live shared mount + preventing
swaps from blanking it.

Incident (3× on 2026-07-05): the live shared-mount creds
~/.claude/.credentials.json ended up BLANK (empty accessToken, expiresAt: 0) —
a fully logged-out file, not a merely stale token — after a shared-mount swap,
locking out every bare session on the shared mount. Detection already shipped
(#146/#148 → `_diagnose_live_mount_creds`); each recovery was a MANUAL
`cus restore-creds <active> --live`. This suite proves the two fixes:

  Part A (root cause) — a shared-mount swap NEVER leaves the live creds blank:
    * an early guard refuses a swap whose target SNAPSHOT is blank/expired,
      before any mutation (default/non-pool path), and
    * a definitive install-point guard right before the live atomic_copy refuses
      any blank install source.
  Part B (safety net) — the daemon self-heals a blanked live mount in-cycle by
    restoring the active account's newest usable credential source, and only
    falls through to the URGENT re-login SOS when no usable backup exists.

Run standalone:  python3 tests/test_auto_heal_blank_live_mount.py
Run under pytest: pytest tests/test_auto_heal_blank_live_mount.py
"""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import cus  # noqa: E402


def _valid(access: str = "at-live", refresh: str = "rt-live",
           expires_at: int = 2_000_000_000_000) -> dict:
    return {"claudeAiOauth": {"accessToken": access, "refreshToken": refresh,
                              "expiresAt": expires_at}}


def _blank() -> dict:
    """The exact 2026-07-05 incident signature: epoch expiresAt + empty token."""
    return {"claudeAiOauth": {"accessToken": "", "refreshToken": "", "expiresAt": 0}}


def _expired_zero(token: str = "at-x") -> dict:
    """Token present but expiresAt == 0 → invalid (the <=0 half of the guard)."""
    return {"claudeAiOauth": {"accessToken": token, "refreshToken": "rt-x", "expiresAt": 0}}


def _expired_negative(token: str = "at-x") -> dict:
    return {"claudeAiOauth": {"accessToken": token, "refreshToken": "rt-x", "expiresAt": -5}}


class _Env:
    """Throwaway on-disk tree with every cus path constant repointed at it, so
    execute_swap / the daemon auto-heal / diagnose run for real against temp
    files (no live-machine mutation). Combines the patterns of
    test_sos_blank_live_mount (config/state/diagnose) and test_creds_backup
    (execute_swap: INBOX_MD + migrate_account_dir stub).
    """

    def __init__(self, accounts: dict[str, dict], active: str,
                 live_creds: dict | None, mode: str = "global") -> None:
        self._tmp = tempfile.TemporaryDirectory()
        root = Path(self._tmp.name)
        self.root = root
        self.claude_dir = root / ".claude"
        self.accounts_dir = root / "claude-accounts"
        (self.claude_dir / "projects").mkdir(parents=True)
        self.accounts_dir.mkdir(parents=True)

        self.creds_json = self.claude_dir / ".credentials.json"
        if live_creds is not None:
            self.creds_json.write_text(json.dumps(live_creds))
        self.claude_json = root / ".claude.json"
        # Live identity == the active account (so no identity-mismatch SOS noise).
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
            "accounts": {n: {"next_swap_at_pct": 50,
                             "current_5h_pct": 0.0, "current_7d_pct": 0.0} for n in accounts},
            "slots": {},
            "swap_history": [],
        }))
        self.config_yaml = self.accounts_dir / "config.yaml"
        cus.write_yaml(self.config_yaml, {"mode": mode})
        self.inbox_md = self.accounts_dir / "inbox.md"

        self._saved = {k: getattr(cus, k) for k in (
            "HOME", "CLAUDE_DIR", "CREDS_JSON", "CLAUDE_JSON", "ACCOUNTS_DIR",
            "STATE_JSON", "CONFIG_YAML", "INBOX_MD", "migrate_account_dir")}
        cus.HOME = root
        cus.CLAUDE_DIR = self.claude_dir
        cus.CREDS_JSON = self.creds_json
        cus.CLAUDE_JSON = self.claude_json
        cus.ACCOUNTS_DIR = self.accounts_dir
        cus.STATE_JSON = self.state_json
        cus.CONFIG_YAML = self.config_yaml
        cus.INBOX_MD = self.inbox_md
        cus.migrate_account_dir = lambda d: {"action": "already_migrated"}

    def live(self) -> dict | None:
        if not self.creds_json.exists():
            return None
        return json.loads(self.creds_json.read_text())

    def snapshot_path(self, name: str) -> Path:
        return self.accounts_dir / f"account-{name}" / ".credentials.json"

    def make_backup(self, name: str, creds: dict) -> None:
        """Write `creds` as a rotated backup generation for `name` (via the
        real backup primitive, so timestamps sort correctly)."""
        p = self.snapshot_path(name)
        saved = p.read_text()
        p.write_text(json.dumps(creds))
        cus.backup_credentials_file(p)  # backs up the `creds` we just wrote
        p.write_text(saved)             # restore the snapshot to its prior value

    def state(self) -> dict:
        return json.loads(self.state_json.read_text())

    def restore(self) -> None:
        for k, v in self._saved.items():
            setattr(cus, k, v)
        self._tmp.cleanup()


def _live_conditions(conditions):
    return [c for c in conditions if "shared mount ~/.claude/.credentials.json" in c.summary]


# ---------------------------------------------------------------------------
# (a) blanked live + valid active-account snapshot → daemon auto-restores it
# ---------------------------------------------------------------------------

def test_a_auto_heal_restores_from_active_snapshot_and_clears_sos():
    env = _Env({"merkos": _valid("at-snap", "rt-merkos")}, active="merkos",
               live_creds=_blank(), mode="global")
    try:
        # Precondition: the mount is diagnosed as blanked before healing.
        assert _live_conditions(cus.diagnose()), "expected the blank live mount to be flagged first"
        healed = cus._auto_heal_live_mount(cus.load_state(), cus.load_config())
        assert healed is True
        # Live file now valid and carries the active account's snapshot tokens.
        assert cus._live_mount_creds_invalid(env.live()) is False
        assert env.live()["claudeAiOauth"]["accessToken"] == "at-snap"
        # And the (previously firing) live-mount SOS is gone after the heal.
        assert _live_conditions(cus.diagnose()) == []
        # The blanked live file was preserved as a backup → heal is reversible.
        assert sorted(env.claude_dir.glob(".credentials.json.bak.*")), "expected a live backup"
    finally:
        env.restore()


def test_a_auto_heal_falls_back_to_newest_valid_backup_when_snapshot_blank():
    # Active snapshot ALSO blank, but a valid backup generation exists → heal
    # from the newest usable backup (the _newest_usable_creds_source fallback).
    env = _Env({"merkos": _blank()}, active="merkos", live_creds=_blank(), mode="global")
    try:
        env.make_backup("merkos", _valid("at-backup", "rt-backup"))
        healed = cus._auto_heal_live_mount(cus.load_state(), cus.load_config())
        assert healed is True
        assert env.live()["claudeAiOauth"]["accessToken"] == "at-backup"
        assert _live_conditions(cus.diagnose()) == []
    finally:
        env.restore()


# ---------------------------------------------------------------------------
# (b) blanked + no usable backup → no restore, URGENT SOS remains
# ---------------------------------------------------------------------------

def test_b_no_usable_backup_leaves_urgent_sos():
    # Both the live mount AND the active snapshot are blank, and there are no
    # backups → nothing usable to install; heal is a no-op and the URGENT
    # re-login SOS must still fire (the one case that needs a human).
    env = _Env({"merkos": _blank()}, active="merkos", live_creds=_blank(), mode="global")
    try:
        healed = cus._auto_heal_live_mount(cus.load_state(), cus.load_config())
        assert healed is False
        assert cus._live_mount_creds_invalid(env.live()) is True  # still blank
        conds = _live_conditions(cus.diagnose())
        assert len(conds) == 1 and conds[0].severity == "urgent"
    finally:
        env.restore()


# ---------------------------------------------------------------------------
# (c) shared-mount swap whose target is blank → does NOT blank the live mount
# ---------------------------------------------------------------------------

def test_c_swap_to_blank_target_refuses_and_preserves_live():
    env = _Env({"a": _valid("at-a", "rt-a"), "b": _blank()}, active="a",
               live_creds=_valid("at-a-live", "rt-a"), mode="global")
    try:
        raised = False
        try:
            cus.execute_swap("b")
        except RuntimeError as e:
            raised = True
            assert "GH #141" in str(e)
            assert "b" in str(e)
        assert raised, "swap to a blank-snapshot target must be refused"
        # Live mount untouched — still the outgoing (valid) account's creds.
        assert env.live()["claudeAiOauth"]["accessToken"] == "at-a-live"
        assert cus._live_mount_creds_invalid(env.live()) is False
        # Active account unchanged (the swap never committed).
        assert env.state()["active"] == "a"
        # The refusal happened before any mutating step → no swap journal left.
        assert not list(env.accounts_dir.glob("*.journal*")), "early guard must not leave a journal"
    finally:
        env.restore()


def test_c_swap_to_valid_target_still_works():
    # Regression: the guard must not block a legitimate swap.
    env = _Env({"a": _valid("at-a", "rt-a"), "b": _valid("at-b", "rt-b")}, active="a",
               live_creds=_valid("at-a-live", "rt-a"), mode="global")
    try:
        cus.execute_swap("b")
        assert env.state()["active"] == "b"
        assert env.live()["claudeAiOauth"]["accessToken"] == "at-b"
    finally:
        env.restore()


# ---------------------------------------------------------------------------
# (d) healthy live mount → auto-heal is a no-op
# ---------------------------------------------------------------------------

def test_d_healthy_mount_auto_heal_is_noop():
    env = _Env({"merkos": _valid("at-snap", "rt-merkos")}, active="merkos",
               live_creds=_valid("at-live", "rt-merkos"), mode="global")
    try:
        before = env.creds_json.read_bytes()
        healed = cus._auto_heal_live_mount(cus.load_state(), cus.load_config())
        assert healed is False
        assert env.creds_json.read_bytes() == before  # byte-for-byte untouched
        # No spurious backup written for a healthy mount.
        assert list(env.claude_dir.glob(".credentials.json.bak.*")) == []
    finally:
        env.restore()


def test_d_per_session_mode_never_heals():
    # In per_session the shared mount is observe-only/authless → a blank there
    # is expected and must NOT be healed (mirrors _diagnose_live_mount_creds).
    env = _Env({"merkos": _valid("at-snap", "rt-merkos")}, active="merkos",
               live_creds=_blank(), mode="per_session")
    try:
        healed = cus._auto_heal_live_mount(cus.load_state(), cus.load_config())
        assert healed is False
        assert cus._live_mount_creds_invalid(env.live()) is True  # left blank on purpose
    finally:
        env.restore()


# ---------------------------------------------------------------------------
# (e) the install-point guard rejects empty / expiresAt<=0 payloads
# ---------------------------------------------------------------------------

def test_e_install_guard_rejects_all_blank_shapes():
    for shape_name, target_creds in (
        ("empty-accessToken", _blank()),
        ("expiresAt==0", _expired_zero()),
        ("expiresAt<0", _expired_negative()),
    ):
        env = _Env({"a": _valid("at-a", "rt-a"), "b": target_creds}, active="a",
                   live_creds=_valid("at-a-live", "rt-a"), mode="global")
        try:
            raised = False
            try:
                cus.execute_swap("b")
            except RuntimeError as e:
                raised = True
                assert "GH #141" in str(e)
            assert raised, f"install guard must reject the {shape_name} target"
            # The live mount was never written with the blank payload.
            assert cus._live_mount_creds_invalid(env.live()) is False, shape_name
            assert env.live()["claudeAiOauth"]["accessToken"] == "at-a-live"
        finally:
            env.restore()


def test_e_newest_usable_source_prefers_snapshot_over_backup():
    # _newest_usable_creds_source: snapshot is always newer than any backup
    # (backups predate it), so a valid snapshot wins.
    env = _Env({"merkos": _valid("at-snap", "rt-merkos")}, active="merkos",
               live_creds=_blank(), mode="global")
    try:
        env.make_backup("merkos", _valid("at-old-backup", "rt-old"))
        src = cus._newest_usable_creds_source("merkos")
        assert src == env.snapshot_path("merkos")
    finally:
        env.restore()


def test_e_newest_usable_source_none_when_all_invalid():
    env = _Env({"merkos": _blank()}, active="merkos", live_creds=_blank(), mode="global")
    try:
        env.make_backup("merkos", _expired_zero())  # backup also unusable
        assert cus._newest_usable_creds_source("merkos") is None
    finally:
        env.restore()


if __name__ == "__main__":
    import traceback

    funcs = [v for k, v in sorted(globals().items())
             if k.startswith("test_") and callable(v)]
    failed = 0
    for fn in funcs:
        try:
            fn()
            print(f"  ok   {fn.__name__}")
        except Exception:  # noqa: BLE001
            failed += 1
            print(f"  FAIL {fn.__name__}")
            traceback.print_exc()
    print(f"\n{len(funcs) - failed}/{len(funcs)} passed")
    sys.exit(1 if failed else 0)
