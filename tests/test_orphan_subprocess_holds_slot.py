"""Regression tests for the orphan-holds-slot bug (2026-07-10, GH #104 repro).

After a claude session closes, a subprocess it spawned — a `vite`/`next dev`
dev server, a `tail -f` — can reparent to init (ppid 1) while still carrying the
CLAUDE_CONFIG_DIR it inherited. That leftover keeps the slot's mount "in use"
per /proc forever, so:

  1. `cus slot gc` refused to reap the slot (its login family was never released).
  2. The pool/login-family gate counted the account as occupied on that slot, so
     `cus slot move <other-slot> <account>` refused with "no free login family".

The fix makes the gc-reap and pool-family DECISIONS SESSION-aware: a mount is
"live" only when a holder's comm is `claude`/`claude.exe` (the launcher execs
claude, so a live session always has such a process among the env holders). A
non-claude-comm holder whose claude ancestor is dead is an orphan and is ignored
by those two decisions. Every OTHER caller of mount_in_use/mount_pids (status
display, SOS detectors, swap-window detection) still means "any holder".

These tests pin:
  - `_is_claude_session_comm` — the pure comm discriminator.
  - `mount_session_pids` / `mount_has_live_session` — ignore a non-claude holder,
    honor a claude holder (mount_pids + _pid_comm monkeypatched, no real /proc).

Run standalone:  python3 tests/test_orphan_subprocess_holds_slot.py
Run under pytest: pytest tests/test_orphan_subprocess_holds_slot.py
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import cus  # noqa: E402


def test_is_claude_session_comm_pure() -> None:
    # The two comms a live claude SESSION top process reports (comm is
    # kernel-truncated to 15 chars; both fit).
    assert cus._is_claude_session_comm("claude") is True
    assert cus._is_claude_session_comm("claude.exe") is True
    # Orphan leftovers carry OTHER comms — a subagent bash, a dev server, a tail.
    for orphan in ("bash", "node", "vite", "python", "python3", "tail", "sleep", ""):
        assert cus._is_claude_session_comm(orphan) is False, orphan
    # cmdline substring matching was the UNRELIABLE approach we deliberately
    # rejected: a subagent bash's argv contains "claude" via the mount PATH.
    # comm is the executable name only, so this stays a bash, not a session.
    assert cus._is_claude_session_comm("bash") is False


def _patch_proc(monkeypatch_like, mount_to_pids: dict[str, list[int]], pid_to_comm: dict[int, str]) -> None:
    """Redirect mount_pids + _pid_comm to in-memory maps (no real /proc)."""
    cus.mount_pids = lambda m: list(mount_to_pids.get(str(m), []))  # type: ignore[assignment]
    cus._pid_comm = lambda pid: pid_to_comm.get(pid)  # type: ignore[assignment]


def test_mount_session_pids_ignores_orphan_holder() -> None:
    """A slot whose only holder is a non-claude orphan has NO live session."""
    orig_mount_pids, orig_pid_comm = cus.mount_pids, cus._pid_comm
    try:
        mount = Path("/home/rayi/claude-accounts/slot-11")
        # PID 5000 is an orphaned vite dev server that inherited the env var.
        _patch_proc(None, {str(mount): [5000]}, {5000: "vite"})
        assert cus.mount_session_pids(mount) == []
        assert cus.mount_has_live_session(mount) is False
        # ... but mount_in_use (unchanged, "any holder") still sees it live —
        # the whole point: status/SOS keep seeing the holder, only the two
        # DECISIONS became session-aware.
        assert cus.mount_in_use(mount) is True
    finally:
        cus.mount_pids, cus._pid_comm = orig_mount_pids, orig_pid_comm


def test_mount_session_pids_honors_claude_holder() -> None:
    """A slot with a live claude-comm holder IS session-occupied — even when
    orphaned descendants also hold the mount."""
    orig_mount_pids, orig_pid_comm = cus.mount_pids, cus._pid_comm
    try:
        mount = Path("/home/rayi/claude-accounts/slot-4")
        # 6000 = live claude session; 6001 = its subagent bash; 6002 = a vite it
        # spawned. All three inherit CLAUDE_CONFIG_DIR; only 6000 is the session.
        _patch_proc(None, {str(mount): [6001, 6000, 6002]},
                    {6000: "claude", 6001: "bash", 6002: "vite"})
        assert cus.mount_session_pids(mount) == [6000]
        assert cus.mount_has_live_session(mount) is True
        # The claude.exe node-binary variant also counts.
        _patch_proc(None, {str(mount): [6003]}, {6003: "claude.exe"})
        assert cus.mount_has_live_session(mount) is True
    finally:
        cus.mount_pids, cus._pid_comm = orig_mount_pids, orig_pid_comm


def test_occupied_slot_accounts_live_session_drops_orphan_only_slot() -> None:
    """The session-aware occupancy map — read by the pool-family DECISION —
    excludes an account whose only slot-holder is an orphan, so the account's
    login family reads as FREE and `cus slot move` no longer refuses (GH #104)."""
    import tempfile

    orig_mount_pids, orig_pid_comm, orig_slot_path = cus.mount_pids, cus._pid_comm, cus.slot_path
    try:
        state = {"slots": {
            "slot-11": {"account": "rayi3"},   # orphan-only (dead session)
            "slot-4": {"account": "rayi3"},    # genuinely live
        }}
        # Real temp dirs so the d.exists() gate inside the occupancy map passes;
        # the /proc side (holders + their comms) is the injected part under test.
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            (base / "slot-11").mkdir()
            (base / "slot-4").mkdir()
            cus.slot_path = lambda name: base / name  # type: ignore[assignment]
            # slot-11 held by an orphaned vite; slot-4 by a live claude session.
            cus.mount_pids = lambda m: {  # type: ignore[assignment]
                str(base / "slot-11"): [7000],
                str(base / "slot-4"): [7100],
            }.get(str(m), [])
            cus._pid_comm = lambda pid: {7000: "vite", 7100: "claude"}.get(pid)  # type: ignore[assignment]
            occ = cus.occupied_slot_accounts_live_session(state)
            # Only the genuinely-live slot-4 is counted for rayi3, so the account's
            # login family reads free and the slot-move refuse is lifted.
            assert occ.get("rayi3") == ["slot-4"], occ
    finally:
        cus.mount_pids, cus._pid_comm, cus.slot_path = orig_mount_pids, orig_pid_comm, orig_slot_path


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn()
        print(f"  ✓ {fn.__name__}")
    print(f"{len(fns)} passed")
