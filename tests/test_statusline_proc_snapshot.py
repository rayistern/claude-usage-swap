"""#255: `cus statusline` cost ~10 s of CPU per call because every
`mount_pids` call re-read every process's environ; one statusline call made
~157,000 environ reads. Third pass of PR #256 (the reviewers' structural
fix): every `mount_pids` / `mount_in_use` call OUTSIDE a hold is a fresh
scan of the table — an actor or a guard reads the table as it is now
without asking — and the READERS (statusline, status, sessions, panes, sos,
diagnose) wrap themselves in `_proc_window_held()`, which serves one scan
to every question in the block.

The oracle here is the OLD algorithm, inlined: one environ read per process
PER mount, the FIRST `CLAUDE_CONFIG_DIR=` winning. On a recorded process
table (a directory of `<pid>/environ` files) the new code must return the
same pids, in the same order, for every mount, and the collision verdicts
built on it must not change.
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
    holds oddities (an empty environ, an empty value, the variable twice)."""
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
    """The pre-#255 algorithm (`d52d2e4`), verbatim in shape: read every
    environ, per mount, the first `CLAUDE_CONFIG_DIR=` chunk winning."""
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


def _add(proc: Path, pid: int, cfg: str) -> Path:
    d = proc / str(pid)
    d.mkdir()
    (d / "environ").write_bytes(b"CLAUDE_CONFIG_DIR=" + cfg.encode() + b"\0")
    return d


@pytest.fixture
def reads(monkeypatch):
    """Count environ reads: one per pid per scan."""
    seen = []
    real = cus._pid_config_dir
    monkeypatch.setattr(cus, "_pid_config_dir", lambda pid: (seen.append(pid), real(pid))[1])
    return seen


def test_mount_pids_matches_the_old_algorithm_on_a_recorded_table(proc):
    mounts = [Path("/home/x/claude-accounts/slot-1"), Path("/home/x/claude-accounts/slot-1/"),
              Path("/home/x/claude-accounts/slot-2"), Path("/home/x/.claude"),
              Path("/home/x/claude-accounts/slot-9"), Path("/home/x/claude-accounts/slot-3"),
              Path("/nowhere")]
    for held in (False, True):
        with (cus._proc_window_held() if held else _no_hold()):
            for m in mounts:
                assert cus.mount_pids(m) == _old_mount_pids(proc, m), (m, held)
                assert cus.mount_in_use(m) == bool(_old_mount_pids(proc, m)), (m, held)
            assert cus._proc_config_dirs().get("") == [106] == _old_mount_pids(proc, "")
            assert 107 in cus.mount_pids(Path("/home/x/claude-accounts/slot-2"))
            assert 107 not in cus.mount_pids(Path("/home/x/claude-accounts/slot-9"))


class _no_hold:
    def __enter__(self):
        return None

    def __exit__(self, *exc):
        return False


def test_outside_a_hold_every_question_is_a_fresh_scan(proc, reads):
    """The inverted default: an actor never has to ask for freshness."""
    n_pids = len([p for p in proc.iterdir() if p.name.isdigit()])
    slot1 = Path("/home/x/claude-accounts/slot-1")
    cus.mount_in_use(slot1)
    cus.mount_in_use(slot1)
    cus.mount_pids(slot1)
    assert len(reads) == 3 * n_pids
    _add(proc, 777, str(slot1))
    assert 777 in cus.mount_pids(slot1)        # the pid that appeared is seen at once


def test_inside_a_hold_one_scan_serves_every_question(proc, reads):
    n_pids = len([p for p in proc.iterdir() if p.name.isdigit()])
    with cus._proc_window_held():
        for _ in range(50):
            for m in ("/home/x/claude-accounts/slot-1", "/home/x/claude-accounts/slot-2",
                      "/home/x/.claude", "/nowhere"):
                cus.mount_pids(Path(m))
        _add(proc, 777, "/home/x/claude-accounts/slot-1")
        assert 777 not in cus.mount_pids(Path("/home/x/claude-accounts/slot-1"))   # held
    assert len(reads) == n_pids                                                    # one scan
    assert 777 in cus.mount_pids(Path("/home/x/claude-accounts/slot-1"))           # released


def test_the_hold_is_released_on_exception_and_restores_an_outer_hold(proc, reads):
    """F-O-9: released on any exception; nested, the outer hold comes back."""
    with pytest.raises(RuntimeError):
        with cus._proc_window_held():
            raise RuntimeError("boom")
    assert cus._PROC_HOLD is None
    with cus._proc_window_held():
        outer = cus._PROC_HOLD
        with cus._proc_window_held():
            assert cus._PROC_HOLD is outer      # nested: the outer scan is served, no new scan
        assert cus._PROC_HOLD is outer
        try:
            with cus._proc_window_held():
                raise RuntimeError("boom")
        except RuntimeError:
            pass
        assert cus._PROC_HOLD is outer
    assert cus._PROC_HOLD is None


def test_an_unreadable_table_reads_as_in_use_for_an_actor(proc, monkeypatch):
    """F-O-8: a scan that cannot list the table must not answer "idle" to
    anything that would act on it — `mount_in_use` answers True, `mount_pids`
    []; nothing is cached, so the next call reads the table again. A hold
    whose own scan failed answers the same way inside the block."""
    real_iterdir = Path.iterdir
    fail = [True]

    def flaky(self):
        if fail[0] and self == proc:
            raise OSError(24, "too many open files")
        return real_iterdir(self)

    monkeypatch.setattr(Path, "iterdir", flaky)
    shared = Path("/home/x/.claude")
    idle = Path("/home/x/claude-accounts/slot-3")
    assert cus.mount_in_use(idle) is True and cus.mount_pids(idle) == []
    assert cus.mount_in_use(shared) is True
    with cus._proc_window_held():
        assert cus._PROC_HOLD is cus._PROC_UNKNOWN
        assert cus.mount_in_use(idle) is True and cus.mount_pids(idle) == []
    fail[0] = False
    assert cus.mount_in_use(idle) is False and cus.mount_pids(shared) == [104]


def test_a_swapped_reader_or_root_is_honoured(proc, monkeypatch):
    """Other tests monkeypatch `_pid_config_dir`; with no cache there is
    nothing to leak, and a swapped reader is used at once."""
    assert cus.mount_in_use(Path("/home/x/.claude")) is True
    monkeypatch.setattr(cus, "_pid_config_dir", lambda pid: None)
    assert cus.mount_in_use(Path("/home/x/.claude")) is False
    monkeypatch.setattr(cus, "_pid_config_dir", lambda pid: "/home/x/.claude")
    assert cus.mount_in_use(Path("/home/x/.claude")) is True


@pytest.fixture
def slots(proc, tmp_path, monkeypatch):
    """Four slots under a tmp ACCOUNTS_DIR; the recorded pids point at them."""
    accounts = tmp_path / "accounts"
    monkeypatch.setattr(cus, "ACCOUNTS_DIR", accounts)
    monkeypatch.setattr(cus, "_OCCUPIED_SLOTS_CACHE", {})
    table = {}
    for name, acct in (("slot-1", "a"), ("slot-2", "b"), ("slot-3", "a"), ("slot-9", "c")):
        (accounts / name).mkdir(parents=True)     # slot_path(name) is ACCOUNTS_DIR / name
        table[name] = {"account": acct}
    for pid, cfg in TABLE.items():
        if cfg and "slot-" in cfg:
            new = str(accounts / Path(cfg.rstrip("/")).name)
            (proc / str(pid) / "environ").write_bytes(b"CLAUDE_CONFIG_DIR=" + new.encode() + b"\0")
    return {"slots": table}


def test_occupied_slot_accounts_verdicts_match_the_old_algorithm(proc, slots):
    got = cus.occupied_slot_accounts(slots, max_age_seconds=0.0)
    expect: dict[str, list[str]] = {}
    for name, entry in sorted(slots["slots"].items()):
        d = cus.slot_path(name)
        if d.exists() and _old_mount_pids(proc, d):
            expect.setdefault(entry["account"], []).append(name)
    assert got == expect == {"a": ["slot-1"], "b": ["slot-2"], "c": ["slot-9"]}


# ── the reviewers' seven acting / guard sites ─────────────────────────────
#
# Each asks `mount_in_use` with no hold active, so each sees a pid that
# appears between two calls. Where the site's own fixture is small it is
# driven directly; the sites whose fixtures are the launch pipeline
# (`acquire_slot` via `_slot_busy`, `_launch_prepare --lane`,
# `_sync_slot_json`, sync-config) rest on `_slot_busy` and on the property
# above (no hold → every question is a fresh scan), since none of them
# runs inside a reader's hold — the only holders are the six readers.

def test_site_slot_busy_and_acquire_path_see_a_new_pid(proc, slots):
    assert cus._slot_busy("slot-3", {}) is False
    _add(proc, 777, str(cus.slot_path("slot-3")))
    assert cus._slot_busy("slot-3", {}) is True


def test_site_gc_slot_refuses_a_mount_that_just_became_live(proc, slots):
    _add(proc, 777, str(cus.slot_path("slot-3")))
    res = cus.gc_slot("slot-3", dict(slots), force=False)
    assert res["action"] == "refused_in_use" and res["pids"] == [777], res


def test_site_release_dead_leases_skips_a_slot_that_just_became_live(proc, slots, monkeypatch):
    """`_release_dead_leases(execute=True)` pops the lease of an idle slot
    whose store is gone; a pid that appears between two calls keeps the
    lease (a live mount's lease is its session's generation record)."""
    monkeypatch.setattr(cus, "_locked_slots", lambda config: set())
    monkeypatch.setattr(cus, "slot_leased_family", lambda state, name: ("a", "family-1"))
    monkeypatch.setattr(cus, "login_family_creds_path", lambda acct, fam: Path("/nonexistent/creds"))
    state = {"slots": {"slot-3": {"account": "a", "login_family": "family-1"}}}
    assert cus._release_dead_leases(dict(state, slots={"slot-3": dict(state["slots"]["slot-3"])}),
                                    {}, execute=False) == ["slot-3: a/family-1"]
    _add(proc, 777, str(cus.slot_path("slot-3")))
    st = {"slots": {"slot-3": {"account": "a", "login_family": "family-1"}}}
    assert cus._release_dead_leases(st, {}, execute=True) == []
    assert st["slots"]["slot-3"]["login_family"] == "family-1"


def test_site_slot_mount_creds_dead_disables_the_probe_on_a_live_mount(proc, slots, monkeypatch):
    """`_slot_mount_creds_dead`'s probe rotates a token; it must be off for a
    mount that became live between two calls."""
    calls = []
    monkeypatch.setattr(cus, "_store_creds_dead",
                        lambda path, label, config, *, allow_probe: (calls.append(allow_probe), False)[1])
    d = cus.slot_path("slot-3")
    cus._slot_mount_creds_dead("slot-3", d, "a", dict(slots), {}, allow_probe=True)
    _add(proc, 777, str(d))
    cus._slot_mount_creds_dead("slot-3", d, "a", dict(slots), {}, allow_probe=True)
    assert calls == [True, False]


def test_site_deep_prune_stores_asks_without_a_hold(proc, slots, monkeypatch):
    """`_deep_prune_stores` (prune --execute) holds back a probe or retire when
    the legacy slot is live; its `mount_in_use` runs with no hold active, so
    it is a fresh scan. Pinned by the property that no hold is installed
    while it runs (the function is not a reader) and by the fresh-scan
    property above; its own fixture is the store layout, not driven here."""
    holds = []
    real = cus._proc_config_dirs
    monkeypatch.setattr(cus, "_proc_config_dirs", lambda: (holds.append(cus._PROC_HOLD), real())[1])
    cus.mount_in_use(cus.slot_path("slot-3"))
    assert holds == [None]


def test_readers_hold_and_the_detector_is_served_the_held_scan(proc, slots, reads, monkeypatch):
    """`diagnose` is a reader: wrapped in the hold, its guards' questions
    (each a `max_age_seconds=0` occupancy read) are served one scan."""
    assert cus.diagnose.__wrapped__ is not None            # decorated with the hold
    for fn in (cus.status, cus.sessions_cmd, cus.panes_cmd, cus.statusline_cmd, cus.sos_cmd):
        assert getattr(fn, "callback", fn).__wrapped__ is not None, fn
    n_pids = len([p for p in proc.iterdir() if p.name.isdigit()])
    with cus._proc_window_held():
        for _ in range(12):
            with cus._proc_window_held():       # a nested reader takes no scan
                cus.occupied_slot_accounts(slots, max_age_seconds=0.0)
    assert len(reads) == n_pids


# ── pass 3 of PR #256: the discipline as a check, and the remaining sites ─
#
# `_assert_no_hold` at every acting entry point turns "no actor runs inside
# a reader's hold" from a rule into a runtime check. The readers are run
# end to end with the check on; each acting site is driven with a pid
# appearing between two calls (through the suite's `_Env`, whose
# `mount_pids` double is consulted on every call — there is no cache
# anywhere any more, so a double or the real table behave alike here).

import json                       # noqa: E402
from click.testing import CliRunner   # noqa: E402

from test_prune_housekeeping import _Env, _valid   # noqa: E402


def test_an_actor_inside_a_hold_raises(proc, slots):
    with cus._proc_window_held():
        for what, call in (("gc_slot", lambda: cus.gc_slot("slot-3", dict(slots), force=False)),
                           ("acquire_slot", lambda: cus.acquire_slot(dict(slots))),
                           ("_release_dead_leases", lambda: cus._release_dead_leases(dict(slots), {}, execute=True)),
                           ("_deep_prune_stores", lambda: cus._deep_prune_stores(dict(slots), {}, execute=True, probe=False)),
                           ("_execute_swap_locked", lambda: cus._execute_swap_locked("a", "test"))):
            with pytest.raises(RuntimeError, match="inside a process-table hold"):
                call()
    # Report-only prune and lease release are readers' business and may run held.
    with cus._proc_window_held():
        assert cus._release_dead_leases(dict(slots), {}, execute=False) == []


def test_every_reader_runs_with_the_check_on():
    """Each decorated reader, end to end, on a throwaway tree with a live and
    an idle slot: none reaches an actor (the check would raise), and each
    exits cleanly."""
    env = _Env({"alpha": _valid("at-a", "rt-a"), "beta": _valid("at-b", "rt-b")}, active="alpha")
    try:
        env.make_slot("alpha", live=True, mount_creds=_valid("at-a", "rt-a"))
        env.make_slot("beta", live=False, mount_creds=_valid("at-b", "rt-b"))
        cus._reset_proc_snapshot()
        runner = CliRunner()
        for argv in (["status"], ["sessions"], ["panes"], ["statusline"], ["sos"], ["slot", "list"]):
            r = runner.invoke(cus.cli, argv)
            assert r.exit_code in (0, 1) and "process-table hold" not in (r.output + str(r.exception)), (argv, r.output[-400:], r.exception)
        assert cus._PROC_HOLD is None
        with cus._proc_window_held():
            cus.diagnose(cus.load_state(), cus.load_config())
    finally:
        env.restore()


def test_site_deep_prune_stores_holds_back_when_the_legacy_slot_becomes_live():
    """`_deep_prune_stores(execute=True)` retires a legacy store whose
    namesake slot is idle; a pid that appears between two calls holds it
    back (a live mount MAY hold that store's generation)."""
    env = _Env({"alpha": _valid("at-a", "rt-a")}, active="alpha",
               config={"mode": "per_session", "independent_logins": {"use_independent_logins": True}})
    try:
        slot = env.make_slot("alpha", live=False, mount_creds=_valid("at-a", "rt-a"))
        legacy = cus.login_family_dir("alpha", slot)
        legacy.mkdir(parents=True, exist_ok=True)
        cus.login_family_creds_path("alpha", slot).write_text(json.dumps(_valid("at-old", "rt-old")))
        env.patch(cus, "_oauth_refresh_grant", lambda rt: ("unknown", None))
        cus._OCCUPIED_SLOTS_CACHE.clear()
        first = [r for r in cus._deep_prune_stores(cus.load_state(), cus.load_config(), execute=False, probe=False)
                 if r["store"] == slot]
        env.live_slots.add(slot)            # the pid appears between the two calls
        cus._OCCUPIED_SLOTS_CACHE.clear()
        second = [r for r in cus._deep_prune_stores(cus.load_state(), cus.load_config(), execute=False, probe=False)
                  if r["store"] == slot]
        assert first != second or not first, (first, second)
        assert all("live" in r.get("problem", "").lower() or r.get("leased") for r in second) or not second, second
        assert cus.login_family_creds_path("alpha", slot).exists()
    finally:
        env.restore()


def test_site_launch_prepare_lane_refuses_a_lane_that_became_live_on_another_account():
    env = _Env({"alpha": _valid("at-a", "rt-a"), "beta": _valid("at-b", "rt-b")}, active="alpha",
               config={"mode": "per_session"})
    try:
        lane = env.make_slot("beta", live=False, mount_creds=_valid("at-b", "rt-b"))
        state, config = cus.load_state(), cus.load_config()
        cus._launch_prepare("alpha", state, config, lane=lane, dry_run=True)   # idle: accepted
        env.live_slots.add(lane)                                                # beta's pid appears
        with pytest.raises(cus.click.ClickException, match="is live on 'beta'"):
            cus._launch_prepare("alpha", cus.load_state(), config, lane=lane, dry_run=True)
    finally:
        env.restore()


def test_site_sync_config_skips_a_slot_that_became_live():
    env = _Env({"alpha": _valid("at-a", "rt-a")}, active="alpha")
    try:
        slot = env.make_slot("alpha", live=False, mount_creds=_valid("at-a", "rt-a"))
        canonical = cus.CLAUDE_DIR / ".claude.json"
        canonical.write_text(json.dumps({"theme": "dark"}))
        lines: list[str] = []
        env.patch(cus.click, "echo", lambda msg="", *a, **k: lines.append(str(msg)))  # the env silences echo
        cus.sync_config_cmd.callback(from_path=str(canonical), dry_run=True)
        first = list(lines)
        lines.clear()
        env.live_slots.add(slot)
        cus.sync_config_cmd.callback(from_path=str(canonical), dry_run=True)
        assert any("would update" in ln for ln in first) and not any("skipped" in ln for ln in first), first
        assert any(f"{slot}: live session — skipped" in ln for ln in lines), lines
    finally:
        env.restore()


def test_displays_say_unknown_when_the_table_cannot_be_read(proc, slots, monkeypatch):
    real_iterdir = Path.iterdir
    monkeypatch.setattr(Path, "iterdir", lambda self: (_ for _ in ()).throw(OSError(24, "EMFILE")) if self == proc else real_iterdir(self))
    assert cus._mount_pids_or_unknown(cus.slot_path("slot-1")) is None
    monkeypatch.setattr(cus, "list_slot_dirs", lambda: [cus.slot_path("slot-1")])
    r = CliRunner().invoke(cus.cli, ["slot", "list"])
    assert "unknown (process table unreadable)" in r.output, r.output
    # The occupancy cache keeps nothing computed from an unreadable table.
    cus._OCCUPIED_SLOTS_CACHE.clear()
    assert cus.occupied_slot_accounts(slots) == {"a": ["slot-1", "slot-3"], "b": ["slot-2"], "c": ["slot-9"]}
    assert cus._OCCUPIED_SLOTS_CACHE == {}
