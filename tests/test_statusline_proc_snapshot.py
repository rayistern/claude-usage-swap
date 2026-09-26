"""#255: `cus statusline` cost ~10 s of CPU per call because every
`mount_pids` call re-read every process's environ; one statusline call made
~165,000 environ reads. The fix scans the process table once per short
window and serves every `mount_pids` call from that map.

The oracle here is the OLD algorithm, inlined: one environ read per process
PER mount. On a recorded process table (a directory of `<pid>/environ`
files) the new code must return the same pids, in the same order, for every
mount, and the collision verdicts built on it must not change.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import cus  # noqa: E402


def _record_proc(root: Path, table: dict[int, str | None], extra: dict[int, bytes] | None = None):
    """A recorded process table: `<pid>/environ` per entry, NUL-separated like
    the kernel's; None means a process with no CLAUDE_CONFIG_DIR; `extra`
    holds unreadable-looking oddities (an empty environ, a non-pid entry)."""
    for pid, cfg in table.items():
        d = root / str(pid)
        d.mkdir()
        env = b"HOME=/home/x\0PATH=/usr/bin\0"
        if cfg is not None:
            env += b"CLAUDE_CONFIG_DIR=" + cfg.encode() + b"\0"
        env += b"LANG=C\0"
        (d / "environ").write_bytes(env)
    for pid, raw in (extra or {}).items():
        d = root / str(pid)
        d.mkdir()
        (d / "environ").write_bytes(raw)
    (root / "self").mkdir()          # not a pid: skipped by both algorithms
    (root / "cpuinfo").write_text("x")


def _old_mount_pids(root: Path, mount: Path | str) -> list[int]:
    """The pre-#255 algorithm, verbatim in shape: read every environ, per mount."""
    want = (mount if isinstance(mount, str) else str(mount)).rstrip("/")
    pids = []
    for p in list(root.iterdir()):
        if not p.name.isdigit():
            continue
        try:
            environ = (p / "environ").read_bytes()
        except OSError:
            continue
        val = None
        for chunk in environ.split(b"\0"):
            if chunk.startswith(b"CLAUDE_CONFIG_DIR="):
                val = chunk.split(b"=", 1)[1].decode("utf-8", "replace")
                break   # the FIRST occurrence, as `_pid_config_dir` returns on it
        if val is not None and val.rstrip("/") == want:
            pids.append(int(p.name))
    return pids


TABLE = {
    100: "/home/x/claude-accounts/slot-1",
    101: "/home/x/claude-accounts/slot-1/",      # trailing slash: the same mount
    102: "/home/x/claude-accounts/slot-2",
    103: None,
    104: "/home/x/.claude",
    2000: "/home/x/claude-accounts/slot-1",
    2001: None,
    30: "/home/x/claude-accounts/slot-9",
}
EXTRA = {105: b"", 106: b"CLAUDE_CONFIG_DIR=\0",   # empty environ; empty value
         # the variable twice: the FIRST wins, in the old code and the new
         107: b"CLAUDE_CONFIG_DIR=/home/x/claude-accounts/slot-2\0"
              b"CLAUDE_CONFIG_DIR=/home/x/claude-accounts/slot-9\0"}


@pytest.fixture
def proc(tmp_path, monkeypatch):
    root = tmp_path / "proc"
    root.mkdir()
    _record_proc(root, TABLE, EXTRA)
    monkeypatch.setattr(cus, "PROC_ROOT", root)
    cus._reset_proc_snapshot()
    yield root
    cus._reset_proc_snapshot()


def test_mount_pids_matches_the_old_algorithm_on_a_recorded_table(proc):
    mounts = [Path("/home/x/claude-accounts/slot-1"), Path("/home/x/claude-accounts/slot-1/"),
              Path("/home/x/claude-accounts/slot-2"), Path("/home/x/.claude"),
              Path("/home/x/claude-accounts/slot-9"), Path("/home/x/claude-accounts/slot-3"),
              Path("/nowhere")]
    for m in mounts:
        assert cus.mount_pids(m) == _old_mount_pids(proc, m), m
        assert cus.mount_in_use(m) == bool(_old_mount_pids(proc, m)), m
    # The empty-value environ (pid 106) maps the empty string, as the old code did
    # for a caller asking about the empty mount name; pid 107 sits under its
    # FIRST value, slot-2, in both.
    assert cus._proc_config_dirs().get("") == [106] == _old_mount_pids(proc, "")
    assert 107 in cus.mount_pids(Path("/home/x/claude-accounts/slot-2"))
    assert 107 not in cus.mount_pids(Path("/home/x/claude-accounts/slot-9"))


def test_one_scan_serves_every_mount_within_the_window(proc, monkeypatch):
    reads = []
    real = cus._pid_config_dir

    def counting(pid):
        reads.append(pid)
        return real(pid)

    monkeypatch.setattr(cus, "_pid_config_dir", counting)
    cus._reset_proc_snapshot()
    n_pids = len([p for p in proc.iterdir() if p.name.isdigit()])
    for _ in range(50):
        for m in ("/home/x/claude-accounts/slot-1", "/home/x/claude-accounts/slot-2",
                  "/home/x/.claude", "/nowhere"):
            cus.mount_pids(Path(m))
    assert len(reads) == n_pids, (len(reads), n_pids)   # 200 calls, one read per pid


def test_the_window_expires_and_fresh_forces_a_scan(proc, monkeypatch):
    clock = [1000.0]
    monkeypatch.setattr(cus, "_proc_clock", lambda: clock[0])   # the hook, not time.monotonic
    cus._reset_proc_snapshot()
    slot3 = Path("/home/x/claude-accounts/slot-3")
    assert cus.mount_pids(slot3) == []
    # A process appears after the scan: unseen within the window …
    d = proc / "777"
    d.mkdir()
    (d / "environ").write_bytes(b"CLAUDE_CONFIG_DIR=/home/x/claude-accounts/slot-3\0")
    assert cus.mount_pids(slot3) == []
    # … seen with `fresh=True`, and after the window on a plain call.
    assert cus.mount_pids(slot3, fresh=True) == [777]
    (d / "environ").unlink()
    d.rmdir()
    assert cus.mount_pids(slot3) == [777]
    clock[0] += cus._PROC_SNAPSHOT_TTL_SECONDS + 0.01
    assert cus.mount_pids(slot3) == []


def test_a_swapped_reader_gets_its_own_scan(proc, monkeypatch):
    """Tests elsewhere monkeypatch `_pid_config_dir`; the snapshot must not
    serve one test's table to the next within the window."""
    cus._reset_proc_snapshot()
    assert cus.mount_in_use(Path("/home/x/.claude")) is True
    monkeypatch.setattr(cus, "_pid_config_dir", lambda pid: None)
    assert cus.mount_in_use(Path("/home/x/.claude")) is False
    # The snapshot holds the reader it was built with and compares by
    # identity, so a new reader object — even one at a freed object's
    # address — cannot inherit the table (F-O-4 / F-S-3).
    monkeypatch.setattr(cus, "_pid_config_dir", lambda pid: "/home/x/.claude")
    assert cus.mount_in_use(Path("/home/x/.claude")) is True


def test_occupied_slot_accounts_verdicts_match_the_old_algorithm(proc, tmp_path, monkeypatch):
    accounts = tmp_path / "accounts"
    monkeypatch.setattr(cus, "ACCOUNTS_DIR", accounts)
    monkeypatch.setattr(cus, "_OCCUPIED_SLOTS_CACHE", {})
    slots = {}
    for name, acct in (("slot-1", "a"), ("slot-2", "b"), ("slot-3", "a"), ("slot-9", "c")):
        (accounts / name).mkdir(parents=True)     # slot_path(name) is ACCOUNTS_DIR / name
        slots[name] = {"account": acct}
    state = {"slots": slots}
    # `slot_path` under the patched ACCOUNTS_DIR; the recorded table names
    # mounts under /home/x — point the recorded pids at the tmp slot dirs.
    for pid, cfg in TABLE.items():
        if cfg:
            new = str(accounts / Path(cfg.rstrip("/")).name) if "slot-" in cfg else cfg
            (proc / str(pid) / "environ").write_bytes(b"CLAUDE_CONFIG_DIR=" + new.encode() + b"\0")
    cus._reset_proc_snapshot()
    got = cus.occupied_slot_accounts(state, max_age_seconds=0.0)
    expect: dict[str, list[str]] = {}
    for name, entry in sorted(slots.items()):
        d = cus.slot_path(name)
        if d.exists() and _old_mount_pids(proc, d):
            expect.setdefault(entry["account"], []).append(name)
    assert got == expect == {"a": ["slot-1"], "b": ["slot-2"], "c": ["slot-9"]}


def test_a_failed_scan_answers_empty_once_and_caches_nothing(proc, monkeypatch):
    """F-S-2: a transient `iterdir` failure must not read as "every mount
    idle" for a second — the call answers empty (the old `mount_pids`'s
    answer), nothing is cached, and the next call reads the table."""
    cus._reset_proc_snapshot()
    shared = Path("/home/x/.claude")
    real_iterdir = Path.iterdir
    fail = [True]

    def flaky(self):
        if fail[0] and self == proc:
            fail[0] = False
            raise OSError(24, "too many open files")
        return real_iterdir(self)

    monkeypatch.setattr(Path, "iterdir", flaky)
    assert cus.mount_pids(shared) == []          # this call: empty, as before
    assert cus._PROC_SNAPSHOT is None            # nothing cached from the failure
    assert cus.mount_pids(shared) == [104]       # the next call reads the table


def test_acting_paths_read_the_table_now(proc, monkeypatch, tmp_path):
    """F-S-1 / F-O-1: an actor takes `_proc_config_dirs(fresh=True)` and then
    asks; `occupied_slot_accounts(max_age_seconds=0)` and `gc_slot`'s
    refusal see a process that appeared after the window's scan; a plain
    reader does not until the window passes. `mount_in_use` stays
    one-argument (the test doubles elsewhere are one-argument lambdas)."""
    clock = [1000.0]
    monkeypatch.setattr(cus, "_proc_clock", lambda: clock[0])
    cus._reset_proc_snapshot()
    accounts = tmp_path / "accounts"
    monkeypatch.setattr(cus, "ACCOUNTS_DIR", accounts)
    monkeypatch.setattr(cus, "_OCCUPIED_SLOTS_CACHE", {})
    (accounts / "slot-3").mkdir(parents=True)
    slot3 = cus.slot_path("slot-3")
    state = {"slots": {"slot-3": {"account": "a"}}}
    assert cus.mount_in_use(slot3) is False                       # scan taken now
    d = proc / "777"
    d.mkdir()
    (d / "environ").write_bytes(b"CLAUDE_CONFIG_DIR=" + str(slot3).encode() + b"\0")
    assert cus.mount_in_use(slot3) is False                       # a reader: the window
    cus._proc_config_dirs(fresh=True)                             # an actor: now
    assert cus.mount_in_use(slot3) is True
    (d / "environ").unlink()
    d.rmdir()
    assert cus.mount_in_use(slot3) is True                        # the fresh scan is the window's now
    assert cus.occupied_slot_accounts(state, max_age_seconds=0.0) == {}   # ground truth: gone
    d.mkdir()
    (d / "environ").write_bytes(b"CLAUDE_CONFIG_DIR=" + str(slot3).encode() + b"\0")
    res = cus.gc_slot("slot-3", dict(state), force=False)
    assert res["action"] == "refused_in_use" and res["pids"] == [777], res


def test_a_reader_holds_one_scan_across_the_guards(proc, monkeypatch, tmp_path):
    """The detector inside `diagnose` runs the credential-safety guards for
    every lane; each asks for the table now. Inside `_proc_window_held`
    they are all served the one scan the reader took — twelve checks, one
    scan — and outside the hold an actor still gets a new scan."""
    cus._reset_proc_snapshot()
    reads = []
    real_reader = cus._pid_config_dir
    monkeypatch.setattr(cus, "_pid_config_dir", lambda pid: (reads.append(pid), real_reader(pid))[1])
    accounts = tmp_path / "accounts"
    monkeypatch.setattr(cus, "ACCOUNTS_DIR", accounts)
    monkeypatch.setattr(cus, "_OCCUPIED_SLOTS_CACHE", {})
    (accounts / "slot-3").mkdir(parents=True)
    state = {"slots": {"slot-3": {"account": "a"}}}
    with cus._proc_window_held():
        n = len(reads)
        for _ in range(12):
            cus.occupied_slot_accounts(state, max_age_seconds=0.0)
            cus._proc_config_dirs(fresh=True)
        assert len(reads) == n                                    # held: no rescan
    cus.occupied_slot_accounts(state, max_age_seconds=0.0)
    assert len(reads) > n                                         # outside: a scan
    assert cus._PROC_HOLD is None
