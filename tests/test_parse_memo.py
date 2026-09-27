"""The parse memo that lives inside a reader's hold (PR #259).

A statusline read 935 JSON files for 269 distinct paths — the same
`.claude.json` and `.credentials.json` parsed up to fifteen times within
one call. `read_json` / `read_yaml` now answer from a memo while a reader's
hold (`_proc_window_held`) is open, keyed on the file's (inode, mtime_ns,
size); outside a hold they are one open and one parse per call, exactly as
before. The contract that keeps every reader's output identical: inside a
hold the SAME object is handed to every reader of a file, so readers must
not mutate what they are given. The last two tests run every held reader
end to end and check that contract against a fresh parse.
"""
from __future__ import annotations

import copy
import functools
import json
import os
import re
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import cus  # noqa: E402

from click.testing import CliRunner   # noqa: E402
from test_prune_housekeeping import _Env, _valid   # noqa: E402


@pytest.fixture
def loads(monkeypatch):
    """Count real parses. The wrappers keep the loaders' names: the memo key
    carries `loader.__name__`, so a renamed wrapper would split the memo."""
    n = {"json": 0, "yaml": 0}
    real_json, real_yaml = cus._load_json, cus._load_yaml

    @functools.wraps(real_json)
    def _load_json(path):
        n["json"] += 1
        return real_json(path)

    @functools.wraps(real_yaml)
    def _load_yaml(path):
        n["yaml"] += 1
        return real_yaml(path)

    monkeypatch.setattr(cus, "_load_json", _load_json)
    monkeypatch.setattr(cus, "_load_yaml", _load_yaml)
    cus._reset_proc_snapshot()
    yield n
    cus._reset_proc_snapshot()


@pytest.fixture
def doc(tmp_path):
    p = tmp_path / "doc.json"
    p.write_text(json.dumps({"a": 1, "nested": {"b": [1, 2]}}))
    return p


def test_outside_a_hold_every_read_is_a_fresh_parse(doc, loads):
    assert cus._PARSE_MEMO is None
    first = cus.read_json(doc)
    second = cus.read_json(doc)
    third = cus.read_json(doc)
    assert loads["json"] == 3
    assert first == second == third == json.loads(doc.read_text())
    assert first is not second          # three parses, three objects


def test_inside_a_hold_one_parse_serves_every_repeat(doc, loads, monkeypatch):
    monkeypatch.setattr(cus, "_scan_proc", lambda: {})
    with cus._proc_window_held():
        objs = [cus.read_json(doc) for _ in range(5)]
    assert loads["json"] == 1
    assert all(o is objs[0] for o in objs)
    assert objs[0] == json.loads(doc.read_text())


def test_a_rewrite_inside_the_hold_is_seen(doc, loads, monkeypatch):
    """Three ways a file changes under a hold, each caught by the stat key:
    cus's own atomic write (new inode), an outsider's in-place write of a
    different size, and an in-place write of the SAME size with the mtime
    moved (what a real rewrite does one clock tick later)."""
    monkeypatch.setattr(cus, "_scan_proc", lambda: {})
    with cus._proc_window_held():
        assert cus.read_json(doc) == {"a": 1, "nested": {"b": [1, 2]}}
        cus.write_json(doc, {"a": 2})
        assert cus.read_json(doc) == {"a": 2}
        assert loads["json"] == 2
        doc.write_text(json.dumps({"a": 2, "more": True}))     # different size, same inode
        assert cus.read_json(doc) == {"a": 2, "more": True}
        assert loads["json"] == 3
        doc.write_text(json.dumps({"a": 3, "more": True}))     # same size, same inode
        st = os.stat(doc)
        os.utime(doc, ns=(st.st_atime_ns, st.st_mtime_ns + 1_000_000))
        assert cus.read_json(doc) == {"a": 3, "more": True}
        assert loads["json"] == 4
        assert cus.read_json(doc) == {"a": 3, "more": True}      # and the new entry is served
        assert loads["json"] == 4


def test_cus_own_write_forgets_the_path_at_once(doc, loads, monkeypatch):
    monkeypatch.setattr(cus, "_scan_proc", lambda: {})
    with cus._proc_window_held():
        cus.read_json(doc)
        assert any(k[1] == os.fspath(doc) for k in cus._PARSE_MEMO)
        cus.atomic_write_bytes(doc, b'{"a": 9}')
        assert not any(k[1] == os.fspath(doc) for k in cus._PARSE_MEMO)
        assert cus.read_json(doc) == {"a": 9}


def test_a_missing_file_raises_the_same_way_in_and_out_of_a_hold(tmp_path, loads, monkeypatch):
    monkeypatch.setattr(cus, "_scan_proc", lambda: {})
    gone = tmp_path / "gone.json"
    with pytest.raises(FileNotFoundError):
        cus.read_json(gone)
    with cus._proc_window_held():
        with pytest.raises(FileNotFoundError):
            cus.read_json(gone)
        assert cus._PARSE_MEMO == {}
    # Outside a hold the loader itself opened and raised (one call); inside,
    # the stat raised first and the loader was never reached.
    assert loads["json"] == 1


def test_a_failed_parse_leaves_nothing_memoised(doc, loads, monkeypatch):
    monkeypatch.setattr(cus, "_scan_proc", lambda: {})
    doc.write_text("{not json")
    with cus._proc_window_held():
        with pytest.raises(json.JSONDecodeError):
            cus.read_json(doc)
        assert cus._PARSE_MEMO == {}
        doc.write_text(json.dumps({"fixed": True}))
        assert cus.read_json(doc) == {"fixed": True}
    assert loads["json"] == 2


def test_the_memo_ends_with_the_hold_and_survives_a_nested_one(doc, loads, monkeypatch):
    monkeypatch.setattr(cus, "_scan_proc", lambda: {})
    with cus._proc_window_held():
        cus.read_json(doc)
        with cus._proc_window_held():                # nested: no new memo, no new parse
            cus.read_json(doc)
            assert loads["json"] == 1
        assert cus._PARSE_MEMO is not None           # the outer memo is still open
        cus.read_json(doc)
        assert loads["json"] == 1
    assert cus._PARSE_MEMO is None
    cus.read_json(doc)
    assert loads["json"] == 2


def test_the_memo_is_dropped_when_the_hold_exits_on_an_exception(doc, loads, monkeypatch):
    monkeypatch.setattr(cus, "_scan_proc", lambda: {})
    with pytest.raises(ValueError):
        with cus._proc_window_held():
            cus.read_json(doc)
            raise ValueError("boom")
    assert cus._PARSE_MEMO is None and cus._PROC_HOLD is None


def test_json_and_yaml_readers_of_one_path_do_not_collide(tmp_path, loads, monkeypatch):
    monkeypatch.setattr(cus, "_scan_proc", lambda: {})
    p = tmp_path / "both.json"
    p.write_text('{"a": 1}')
    with cus._proc_window_held():
        assert cus.read_json(p) == {"a": 1}
        assert cus.read_yaml(p) == {"a": 1}
        assert cus.read_yaml(p) == {"a": 1}
        assert (loads["json"], loads["yaml"]) == (1, 1)
        assert {k[0] for k in cus._PARSE_MEMO} == {"_load_json", "_load_yaml"}


def test_an_empty_yaml_file_reads_as_an_empty_dict_both_ways(tmp_path, loads, monkeypatch):
    monkeypatch.setattr(cus, "_scan_proc", lambda: {})
    p = tmp_path / "empty.yaml"
    p.write_text("")
    assert cus.read_yaml(p) == {}
    with cus._proc_window_held():
        assert cus.read_yaml(p) == {}
        assert cus.read_yaml(p) == {}
    assert loads["yaml"] == 2


# ── the contract, checked against every held reader ─────────────────────

_READERS = (["status"], ["sessions"], ["panes"], ["statusline"], ["sos"], ["slot", "list"])


def _rich_env() -> _Env:
    """Four accounts, two pooled families each, six slots (leased, unleased,
    live and idle): on this tree a statusline makes ~109 reads for ~25
    parses, so the memo is exercised, not brushed (PR #259 pass 1, F-O-2 —
    the first fixture gave it three hits)."""
    names = ("alpha", "beta", "gamma", "delta")
    env = _Env({n: _valid(f"at-{n}", f"rt-{n}") for n in names}, active="alpha",
               config={"mode": "per_session",
                       "independent_logins": {"use_independent_logins": True, "pool_size": 2}})
    for n in names:
        for i in (1, 2):
            env.plant_family(n, f"family-{i}", _valid(f"at-{n}-f{i}", f"rt-{n}-f{i}"))
    env.make_slot("alpha", live=True, mount_creds=_valid("at-alpha-f1", "rt-alpha-f1"), family_id="family-1")
    env.make_slot("alpha", live=True, mount_creds=_valid("at-alpha-f2", "rt-alpha-f2"), family_id="family-2")
    env.make_slot("beta", live=True, mount_creds=_valid("at-beta-f1", "rt-beta-f1"), family_id="family-1")
    env.make_slot("beta", live=False, mount_creds=_valid("at-beta", "rt-beta"))
    env.make_slot("gamma", live=True, mount_creds=_valid("at-gamma", "rt-gamma"))
    env.make_slot("delta", live=False, mount_creds=_valid("at-delta-f1", "rt-delta-f1"), family_id="family-1")
    return env


def test_no_held_reader_mutates_a_memoised_parse(loads):
    """Every held reader, end to end, under one outer hold so the memo
    persists across them. Each object is snapshotted (deep copy) the first
    time the memo hands it out, and at hold exit every object still equals
    its snapshot — including objects evicted since (a cus write forgets the
    path; an outside rewrite re-parses), which a comparison against the
    disk would never see (PR #259 pass 1, F-O-1). And the memo served
    repeats: parses are well under reads."""
    env = _rich_env()
    reads = {"n": 0}
    handed: dict[int, tuple[object, object]] = {}      # id(obj) -> (obj, snapshot at hand-out)
    real_read, real_parsed = cus.read_json, cus._parsed

    def counting_read(path):
        reads["n"] += 1
        return real_read(path)

    def snapshotting_parsed(path, loader):
        obj = real_parsed(path, loader)
        if cus._PARSE_MEMO is not None and id(obj) not in handed:
            handed[id(obj)] = (obj, copy.deepcopy(obj))
        return obj
    try:
        env.patch(cus, "read_json", counting_read)
        env.patch(cus, "_parsed", snapshotting_parsed)
        cus._reset_proc_snapshot()
        runner = CliRunner()
        with cus._proc_window_held():
            for argv in _READERS:
                r = runner.invoke(cus.cli, argv)
                assert r.exit_code in (0, 1) and "process-table hold" not in (r.output + str(r.exception)), \
                    (argv, r.output[-400:], r.exception)
            cus.diagnose(cus.load_state(), cus.load_config())
            mutated = [obj for obj, snap in handed.values() if obj != snap]
        assert mutated == [], mutated[:3]
        assert len(handed) >= 25, len(handed)
        assert reads["n"] >= 3 * loads["json"] > 0, (reads, loads)
    finally:
        env.restore()
        cus._reset_proc_snapshot()


def test_statusline_output_is_identical_with_and_without_the_memo(loads):
    env = _rich_env()
    try:
        lines: list[str] = []
        env.patch(cus.click, "echo", lambda msg="", *a, **k: lines.append(str(msg)))
        cus._reset_proc_snapshot()
        runner = CliRunner()

        def run() -> str:
            del lines[:]
            r = runner.invoke(cus.cli, ["statusline"])
            assert r.exit_code in (0, 1), (r.output, r.exception)
            return re.sub(r"\d{1,2}:\d{2}", "HH:MM", "\n".join(lines) + r.output)

        with_memo = run()
        memo_parses = loads["json"]
        env.patch(cus, "_parsed", lambda path, loader: loader(path))    # the memo bypassed
        without = run()
        assert with_memo == without
        assert with_memo.strip()
        # The run WITH the memo parsed well under half of what the run
        # without did (37 against 109 on this fixture) — the comparison
        # covered a memo that was doing its job.
        without_parses = loads["json"] - memo_parses
        assert without_parses >= 2 * memo_parses, (memo_parses, without_parses)
    finally:
        env.restore()
        cus._reset_proc_snapshot()
