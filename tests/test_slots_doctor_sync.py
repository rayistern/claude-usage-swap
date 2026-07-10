"""Tests for per_session Phase 1: slot scaffolding, doctor --fix-dirs, sync-config.

Plan: docs/plans/2026-07-02-per-session-accounts.md (Phases 1.2 + 1.3).

Covers:
  - scaffold_mount_dir: canonical layout created, idempotent
  - create_slot: lowest-free-index allocation, state registration
  - doctor_mount: settings.json stub fold + relink, missing-symlink heal,
    real-dir merge into shared target, idempotency (re-run = no findings)
  - sync_mount_claude_json: non-account keys synced, identity keys preserved
  - saveback_mount_credentials: GH #3 owner routing + GH #77 freshness skip
  - gc_slot: in-use refusal, reap + save-back, stale-entry drop

Run standalone:  python3 tests/test_slots_doctor_sync.py
Run under pytest: pytest tests/test_slots_doctor_sync.py
"""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import cus  # noqa: E402


def _creds(refresh: str, expires_at: int = 2_000_000_000_000) -> dict:
    return {"claudeAiOauth": {"accessToken": f"at-{refresh}", "refreshToken": refresh, "expiresAt": expires_at}}


class _Env:
    """Throwaway on-disk tree mirroring the production layout.

    Same monkeypatch-constants pattern as test_swap_lock_journal._Env so the
    file stays standalone-runnable (no pytest fixtures).
    """

    def __init__(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        root = Path(self._tmp.name)
        self.claude_dir = root / ".claude"
        self.claude_json = root / ".claude.json"
        self.accounts_dir = root / "claude-accounts"
        self.state_json = self.accounts_dir / "state.json"

        # Canonical shared tree: a few shared dirs + settings files.
        for sub in ("projects", "plugins", "commands", "session-env"):
            (self.claude_dir / sub).mkdir(parents=True)
        (self.claude_dir / "settings.json").write_text(json.dumps({"statusLine": {"type": "command"}, "hooks": {"Stop": []}}))
        (self.claude_dir / "settings.local.json").write_text("{}")
        self.claude_json.write_text(json.dumps({
            "userID": "uid-live", "oauthAccount": {"emailAddress": "live@x"},
            "mcpServers": {"gym": {"command": "gym"}}, "numStartups": 42,
        }))

        # Two account dirs with distinct refresh-token lineages.
        for name, rt in (("alpha", "rt-alpha"), ("beta", "rt-beta")):
            d = self.accounts_dir / f"account-{name}"
            d.mkdir(parents=True)
            (d / ".credentials.json").write_text(json.dumps(_creds(rt)))
            (d / ".claude.json").write_text(json.dumps({"userID": f"uid-{name}", "oauthAccount": {"emailAddress": f"{name}@x"}}))

        cus.write_json(self.state_json, {
            "active": "alpha",
            "accounts": {"alpha": {"next_swap_at_pct": 50}, "beta": {"next_swap_at_pct": 50}},
            "swap_history": [],
        })

        self._saved = {k: getattr(cus, k) for k in
                       ("HOME", "CLAUDE_DIR", "CLAUDE_JSON", "CREDS_JSON", "ACCOUNTS_DIR", "STATE_JSON", "CONFIG_YAML")}
        cus.HOME = root
        cus.CLAUDE_DIR = self.claude_dir
        cus.CLAUDE_JSON = self.claude_json
        cus.CREDS_JSON = self.claude_dir / ".credentials.json"
        cus.ACCOUNTS_DIR = self.accounts_dir
        cus.STATE_JSON = self.state_json
        cus.CONFIG_YAML = self.accounts_dir / "config.yaml"
        # Slot occupancy comes from /proc in production; tests control it.
        self._saved_mount_pids = cus.mount_pids
        cus.mount_pids = lambda mount: []
        # The gc-reap in-use gate is session-aware (orphan-holds-slot bug,
        # 2026-07-10): a holder counts only if its comm is claude. Default the
        # fake holders to a claude comm so a test that plants a holder to mean
        # "live session" reads as one; a test exercising the orphan case can
        # override cus._pid_comm to a non-claude comm.
        self._saved_pid_comm = cus._pid_comm
        cus._pid_comm = lambda pid: "claude"

    def restore(self) -> None:
        for k, v in self._saved.items():
            setattr(cus, k, v)
        cus.mount_pids = self._saved_mount_pids
        cus._pid_comm = self._saved_pid_comm
        self._tmp.cleanup()


def test_scaffold_mount_dir_idempotent():
    env = _Env()
    try:
        mount = env.accounts_dir / "slot-1"
        actions = cus.scaffold_mount_dir(mount)
        assert (mount / "projects").is_symlink()
        assert (mount / "settings.json").is_symlink()
        assert (mount / ".claude.json").exists()
        # Only links whose target exists get created (no dangling "todos" etc.)
        assert not (mount / "todos").exists()
        assert actions, "first scaffold reports actions"
        assert cus.scaffold_mount_dir(mount) == [], "re-scaffold is a no-op"
    finally:
        env.restore()


def test_create_slot_allocates_lowest_free_index():
    env = _Env()
    try:
        state = cus.load_state()
        n1, _ = cus.create_slot(state)
        n2, _ = cus.create_slot(state)
        assert (n1, n2) == ("slot-1", "slot-2")
        # New slots carry a fresh reservation (protects an in-flight launch);
        # clear it so gc treats slot-1 as reapable (simulates the reservation
        # having expired / the session having come and gone).
        for e in state["slots"].values():
            e.pop("reserved_until", None)
        # gc slot-1, next create reuses index 1. Persist the gc'd pop —
        # create_slot reloads state from disk under the lock, so the drop must
        # be on disk (real callers save_state after gc, as slot_gc_cmd does).
        result = cus.gc_slot("slot-1", state)
        assert result["action"] == "reaped"
        cus.save_state(state)
        n3, _ = cus.create_slot(state)
        assert n3 == "slot-1"
        assert set(state["slots"]) == {"slot-1", "slot-2"}
    finally:
        env.restore()


def test_doctor_folds_settings_stub_and_heals():
    env = _Env()
    try:
        # account-alpha mimics the production drift: stub settings.json,
        # missing symlinks (only .credentials.json + .claude.json present).
        alpha = env.accounts_dir / "account-alpha"
        (alpha / "settings.json").write_text(json.dumps({"theme": "dark", "hooks": {"Stop": ["clobber"]}}))

        findings = cus.doctor_mount(alpha, fix=False)
        assert findings, "dry-run reports the drift"
        # Dry run changed nothing:
        assert not (alpha / "projects").exists()

        cus.doctor_mount(alpha, fix=True)
        assert (alpha / "settings.json").is_symlink()
        assert (alpha / "projects").is_symlink()
        shared = json.loads((env.claude_dir / "settings.json").read_text())
        assert shared["theme"] == "dark", "novel stub key folded into shared"
        assert shared["hooks"] == {"Stop": []}, "conflicting key: shared value wins"
        stub_baks = list(alpha.glob("settings.json.stub-bak.*"))
        assert len(stub_baks) == 1, "original stub preserved (preserve-the-log)"

        assert cus.doctor_mount(alpha, fix=True) == [], "healed mount re-runs clean"
    finally:
        env.restore()


def test_doctor_merges_real_dir_into_shared():
    env = _Env()
    try:
        alpha = env.accounts_dir / "account-alpha"
        # Real session-env dir with content (the account-03 production case).
        real = alpha / "session-env"
        real.mkdir()
        (real / "uuid-1").mkdir()
        (real / "uuid-1" / "env.json").write_text("{}")

        cus.doctor_mount(alpha, fix=True)
        assert (alpha / "session-env").is_symlink()
        assert (env.claude_dir / "session-env" / "uuid-1" / "env.json").exists(), "content moved to shared"
    finally:
        env.restore()


def test_doctor_leaves_colliding_real_dir():
    env = _Env()
    try:
        alpha = env.accounts_dir / "account-alpha"
        (env.claude_dir / "session-env" / "uuid-9").mkdir()
        real = alpha / "session-env"
        real.mkdir()
        (real / "uuid-9").mkdir()  # collides with shared

        cus.doctor_mount(alpha, fix=True)
        assert not (alpha / "session-env").is_symlink(), "colliding dir left for operator judgment"
        findings = cus.doctor_mount(alpha, fix=False)
        assert any("session-env" == f["entry"] for f in findings), "still reported on re-run"
    finally:
        env.restore()


def test_sync_mount_claude_json_preserves_identity():
    env = _Env()
    try:
        state = cus.load_state()
        _, d = cus.create_slot(state)
        cj = cus.mount_claude_json_path(d)
        cus.write_json(cj, {"userID": "uid-slot", "oauthAccount": {"emailAddress": "slot@x"}, "numStartups": 1})

        result = cus.sync_mount_claude_json(d, json.loads(env.claude_json.read_text()))
        assert result["changed"]
        merged = json.loads(cj.read_text())
        assert merged["mcpServers"] == {"gym": {"command": "gym"}}, "non-account key synced"
        assert merged["numStartups"] == 42, "canonical wins on non-account conflicts"
        assert merged["userID"] == "uid-slot", "identity preserved"
        assert merged["oauthAccount"] == {"emailAddress": "slot@x"}

        again = cus.sync_mount_claude_json(d, json.loads(env.claude_json.read_text()))
        assert not again["changed"], "second sync is a no-op"
    finally:
        env.restore()


def test_saveback_routes_and_guards():
    env = _Env()
    try:
        state = cus.load_state()
        mount = env.accounts_dir / "slot-1"
        cus.scaffold_mount_dir(mount)

        # Case 1: live creds match expected account → saved.
        (mount / ".credentials.json").write_text(json.dumps(_creds("rt-alpha", expires_at=3_000_000_000_000)))
        r = cus.saveback_mount_credentials(mount, "alpha", state)
        assert r["action"] == "saved" and r["account"] == "alpha"
        snap = json.loads((env.accounts_dir / "account-alpha" / ".credentials.json").read_text())
        assert snap["claudeAiOauth"]["expiresAt"] == 3_000_000_000_000

        # Case 2: GH #3 — live creds belong to beta, expected alpha → routed to beta.
        (mount / ".credentials.json").write_text(json.dumps(_creds("rt-beta", expires_at=3_000_000_000_000)))
        r = cus.saveback_mount_credentials(mount, "alpha", state)
        assert r["action"] == "saved_to_owner" and r["account"] == "beta"

        # Case 3: GH #77 — snapshot fresher than live → skipped.
        (mount / ".credentials.json").write_text(json.dumps(_creds("rt-alpha", expires_at=1_000_000_000_000)))
        r = cus.saveback_mount_credentials(mount, "alpha", state)
        assert r["action"] == "skipped" and "fresher" in r["detail"]

        # Case 4: logout-shaped live file → skipped (invalid).
        (mount / ".credentials.json").write_text("{}")
        r = cus.saveback_mount_credentials(mount, "alpha", state)
        assert r["action"] == "skipped"
    finally:
        env.restore()


def test_gc_slot_refuses_in_use_then_reaps():
    env = _Env()
    try:
        state = cus.load_state()
        name, d = cus.create_slot(state)
        (d / ".credentials.json").write_text(json.dumps(_creds("rt-alpha", expires_at=3_000_000_000_000)))
        state["slots"][name]["account"] = "alpha"

        cus.mount_pids = lambda mount: [12345]
        r = cus.gc_slot(name, state)
        assert r["action"] == "refused_in_use"
        assert d.exists()

        cus.mount_pids = lambda mount: []
        # A fresh reservation also blocks gc (in-flight-launch guard) — verify
        # that, then clear it to exercise the actual reap.
        assert cus.gc_slot(name, state)["action"] == "refused_reserved"
        state["slots"][name].pop("reserved_until", None)
        r = cus.gc_slot(name, state)
        assert r["action"] == "reaped"
        assert r["saveback"]["action"] == "saved"
        assert not d.exists()
        assert name not in state["slots"]

        # Stale state entry with no dir: dropped quietly.
        state.setdefault("slots", {})["slot-9"] = {"account": None}
        r = cus.gc_slot("slot-9", state)
        assert r["action"] == "dropped_stale_entry"
    finally:
        env.restore()


def test_gc_slot_reaps_over_orphan_nonsession_holder():
    """Orphan-holds-slot bug (2026-07-10): a slot whose ONLY holder is a
    non-claude orphan (a dev server / tail that outlived its session and kept the
    inherited CLAUDE_CONFIG_DIR) has no live session, so gc must REAP it — not
    refuse forever — and report the stepped-over orphan pids."""
    env = _Env()
    try:
        state = cus.load_state()
        name, d = cus.create_slot(state)
        (d / ".credentials.json").write_text(json.dumps(_creds("rt-alpha", expires_at=3_000_000_000_000)))
        state["slots"][name]["account"] = "alpha"

        # A leftover vite dev server (pid 4242) still holds the mount, but its
        # comm is NOT claude → not a live session. Clear the fresh create_slot
        # reservation first so the in-flight-launch guard (which still fires even
        # when only orphans hold the slot) doesn't mask the reap under test.
        state["slots"][name].pop("reserved_until", None)
        cus.mount_pids = lambda mount: [4242]
        cus._pid_comm = lambda pid: "vite"
        r = cus.gc_slot(name, state)
        assert r["action"] == "reaped", r
        assert r.get("orphan_pids") == [4242], r
        assert not d.exists()
        assert name not in state["slots"]
    finally:
        env.restore()


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn()
        print(f"  ✓ {fn.__name__}")
    print(f"{len(fns)} passed")
