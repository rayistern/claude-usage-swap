"""Tests for the find_live_sessions transcript-index fast path (GH #91).

Guards the load-bearing performance invariant behind the 2026-07-02 daemon
stall: find_live_sessions() must resolve transcripts via ONE scan of
~/.claude/projects (_transcript_index), never via per-session
_find_transcript() calls — whose glob fallback multiplied a ~13k-session
sessions.log against a ~300-dir projects tree into a measured 7.6-MINUTE
stall inside the daemon cycle, right after a swap (issue #91: "fast polling
can't help if the loop stalls").

Run standalone:  python3 tests/test_live_session_scan.py
Or under pytest: pytest tests/test_live_session_scan.py
"""

import json
import os
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

# cus.py lives at the repo root; make it importable when run from anywhere.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import cus  # noqa: E402


def _now_iso() -> str:
    """UTC timestamp in cus's sessions.log format (Z-suffixed, seconds
    precision) — used to write recent-looking log + transcript entries."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _setup_env(tmp: Path, n_stale: int = 50):
    """Point cus at a synthetic ~/.claude + sessions.log world.

    One genuinely-live session (recent transcript) + `n_stale` sessions whose
    transcripts don't exist anywhere — the population that used to trigger a
    full projects-tree glob EACH under the pre-#91 fallback.
    Returns (saved_globals, live_session_id).
    """
    claude_dir = tmp / ".claude"
    projects = claude_dir / "projects"
    proj_dir = projects / "-home-x-repo"
    proj_dir.mkdir(parents=True)

    live_sid = "live-session-0001"
    transcript = proj_dir / f"{live_sid}.jsonl"
    transcript.write_text(json.dumps({"timestamp": _now_iso()}) + "\n")

    sessions_log = tmp / "sessions.log"
    lines = [f"{_now_iso()},{live_sid},default,%1,/home/x/repo"]
    for i in range(n_stale):
        lines.append(f"2026-01-01T00:00:00Z,stale-{i:04d},default,%9,/home/x/gone")
    sessions_log.write_text("\n".join(lines) + "\n")

    saved = (cus.CLAUDE_DIR, cus.SESSIONS_LOG, cus.STOPS_LOG)
    cus.CLAUDE_DIR = claude_dir
    cus.SESSIONS_LOG = sessions_log
    cus.STOPS_LOG = tmp / "stops.log"  # absent — liveness rides on transcripts
    cus._TRANSCRIPT_INDEX_CACHE.clear()  # don't inherit a prior test's scan
    return saved, live_sid


def _restore(saved):
    """Undo _setup_env's monkeypatch. `saved` is the 3-tuple _setup_env
    returned: (CLAUDE_DIR, SESSIONS_LOG, STOPS_LOG), restored positionally."""
    cus.CLAUDE_DIR, cus.SESSIONS_LOG, cus.STOPS_LOG = saved


def test_live_session_found_via_index():
    """A session with a fresh transcript is detected without _find_transcript."""
    with tempfile.TemporaryDirectory() as td:
        saved, live_sid = _setup_env(Path(td))
        orig_find = cus._find_transcript
        # THE regression pin: any per-session fallback lookup fails loudly.
        cus._find_transcript = lambda *a, **k: (_ for _ in ()).throw(
            AssertionError("find_live_sessions must use _transcript_index, "
                           "not per-session _find_transcript (GH #91)")
        )
        try:
            live = cus.find_live_sessions()
            assert [s.session_id for s in live] == [live_sid]
            assert live[0].transcript_path and live[0].transcript_path.exists()
        finally:
            cus._find_transcript = orig_find
            _restore(saved)


def test_stale_sessions_cost_no_tree_scans():
    """Stale sessions (transcript gone) resolve as dead via one index build.

    Pin the O(1)-per-session property indirectly: with 500 stale entries the
    call must stay well under a second even on a slow box — the pre-#91
    behavior was ~20ms-per-glob × sessions, which this bound catches.
    """
    with tempfile.TemporaryDirectory() as td:
        saved, live_sid = _setup_env(Path(td), n_stale=500)
        try:
            t0 = time.monotonic()
            live = cus.find_live_sessions()
            elapsed = time.monotonic() - t0
            assert [s.session_id for s in live] == [live_sid]
            assert elapsed < 1.0, f"find_live_sessions took {elapsed:.2f}s for 501 sessions"
        finally:
            _restore(saved)


def test_transcript_index_shape():
    """_transcript_index maps every *.jsonl stem across all project dirs."""
    with tempfile.TemporaryDirectory() as td:
        saved, live_sid = _setup_env(Path(td))
        try:
            other = cus.CLAUDE_DIR / "projects" / "-home-x-other"
            other.mkdir()
            (other / "second-session.jsonl").write_text("{}\n")
            idx = cus._transcript_index()
            assert set(idx) == {live_sid, "second-session"}
            assert idx[live_sid].name == f"{live_sid}.jsonl"
        finally:
            _restore(saved)


def test_transcript_index_missing_projects_root():
    """No ~/.claude/projects at all → empty index, no crash."""
    with tempfile.TemporaryDirectory() as td:
        saved = (cus.CLAUDE_DIR, cus.SESSIONS_LOG, cus.STOPS_LOG)
        cus.CLAUDE_DIR = Path(td) / "nonexistent"
        cus._TRANSCRIPT_INDEX_CACHE.clear()
        try:
            assert cus._transcript_index() == {}
        finally:
            _restore(saved)


def test_resolve_transcript_prefers_session_cwd_over_duplicate():
    """Review finding 2026-07-02: a session id duplicated across two project
    dirs must resolve to the transcript in the SESSION'S OWN cwd, not to
    whichever the index glob happened to see last — else a stale duplicate
    shadows the live transcript and the session can be dropped / mis-swapped."""
    with tempfile.TemporaryDirectory() as td:
        saved, _ = _setup_env(Path(td))
        try:
            sid = "dup-session"
            own = cus.CLAUDE_DIR / "projects" / "-home-x-mine"
            other = cus.CLAUDE_DIR / "projects" / "-home-x-stale"
            own.mkdir(parents=True); other.mkdir(parents=True)
            own_t = own / f"{sid}.jsonl"; own_t.write_text("{}\n")
            (other / f"{sid}.jsonl").write_text("{}\n")
            cus._TRANSCRIPT_INDEX_CACHE.clear()
            idx = cus._transcript_index()
            # cwd-first wins regardless of which dir the index cached.
            resolved = cus._resolve_transcript(sid, "/home/x/mine", idx)
            assert resolved == own_t, f"expected the session's own-cwd transcript, got {resolved}"
            # No cwd → falls back to the index (arbitrary but present).
            assert cus._resolve_transcript(sid, "", idx) is not None
            # Unknown session, no cwd → None.
            assert cus._resolve_transcript("nope", "", idx) is None
        finally:
            _restore(saved)


def _reference_live_scan(account_filter=None):
    """Independent FULL-SCAN reference for find_live_sessions (GH #90/#95).

    Re-derives the live-session set from first principles with the
    pre-optimization per-session semantics: dedupe sessions.log by id
    (latest wins), then for each id do a LIVE cwd-first existence probe and
    an exhaustive per-id glob fallback — no shared index, no cached scan,
    no batched stats. Deliberately re-implemented here (calling no cus
    helper beyond the two log parsers) so a regression in the fast path
    can't hide inside a shared helper.

    Returns a set of (session_id, account, pane) triples — the identity of
    the live set, which is exactly what the GH #90/#95 "performance only,
    no semantics change" contract promises to preserve.
    """
    entries = cus._parse_sessions_log()
    stops = cus._latest_stops_per_session()
    now = datetime.now(timezone.utc)
    by_id = {}
    for e in entries:
        by_id[e["session_id"]] = e
    live = set()
    for sid, e in by_id.items():
        if account_filter and e["account"] != account_filter:
            continue
        is_live = False
        st = stops.get(sid)
        if st:
            try:
                st_dt = datetime.fromisoformat(st.replace("Z", "+00:00"))
                if (now - st_dt).total_seconds() < 3600:
                    is_live = True
            except ValueError:
                pass
        transcript = None
        if e["cwd"]:
            cand = (cus.CLAUDE_DIR / "projects"
                    / e["cwd"].replace("/", "-") / f"{sid}.jsonl")
            if cand.exists():
                transcript = cand
        if transcript is None:
            for hit in (cus.CLAUDE_DIR / "projects").glob(f"*/{sid}.jsonl"):
                transcript = hit  # any hit carries the same liveness signal
        if transcript is not None and transcript.exists():
            mtime = datetime.fromtimestamp(transcript.stat().st_mtime,
                                           tz=timezone.utc)
            if (now - mtime).total_seconds() < 3600:
                is_live = True
        if is_live:
            live.add((sid, e["account"], e["pane"]))
    return live


def test_fast_path_matches_full_scan_reference():
    """GH #90/#95 contract pin: the zero-syscall-per-id fast path must report
    the SAME live-session set as a naive full-scan reference, across every
    liveness shape the scanner distinguishes: live-by-transcript,
    live-by-stop (no transcript), stale-stop (not live), dead-stale
    transcript, transcript-missing, duplicate-id-across-project-dirs
    (cwd-first), empty-cwd index fallback, log-level duplicate entries
    (latest wins), and account filtering."""
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        saved, live_sid = _setup_env(tmp, n_stale=200)
        try:
            projects = cus.CLAUDE_DIR / "projects"
            now_iso = _now_iso()
            old_iso = "2026-01-01T00:00:00Z"
            lines = []

            # live-by-stop: no transcript anywhere, recent Stop entry —
            # plus a stale-stop session that must NOT count as live.
            lines.append(f"{now_iso},stop-only-01,acctA,%2,/home/x/repo")
            lines.append(f"{old_iso},stopped-long-ago,acctA,%2,/home/x/gone")
            cus.STOPS_LOG.write_text(
                f"{now_iso},stop-only-01\n"
                f"{old_iso},stopped-long-ago\n"
            )

            # dead: transcript exists but its mtime is 2h old (> 1h window).
            dead_dir = projects / "-home-x-dead"
            dead_dir.mkdir()
            dead_t = dead_dir / "dead-stale-01.jsonl"
            dead_t.write_text("{}\n")
            two_h_ago = time.time() - 7200
            os.utime(dead_t, (two_h_ago, two_h_ago))
            lines.append(f"{old_iso},dead-stale-01,acctB,%3,/home/x/dead")

            # duplicate id in TWO project dirs; session's own cwd must win.
            for d in ("-home-x-mine", "-home-x-other"):
                dd = projects / d
                dd.mkdir(exist_ok=True)
                (dd / "dup-live-01.jsonl").write_text("{}\n")
            lines.append(f"{now_iso},dup-live-01,acctA,%4,/home/x/mine")

            # empty cwd -> resolved purely via the by-stem index fallback.
            fb_dir = projects / "-home-x-fallback"
            fb_dir.mkdir()
            (fb_dir / "fallback-live-01.jsonl").write_text("{}\n")
            lines.append(f"{now_iso},fallback-live-01,acctB,%5,")

            # log-level duplicate for live_sid: this later line rebinds it
            # to acctA/%1 (dedupe keeps the LATEST entry) — both scanners
            # must agree on the rebinding, not just on the id.
            lines.append(f"{old_iso},{live_sid},acctA,%1,/home/x/repo")

            with cus.SESSIONS_LOG.open("a") as f:
                f.write("\n".join(lines) + "\n")

            cus._TRANSCRIPT_INDEX_CACHE.clear()
            got = {(s.session_id, s.account, s.pane)
                   for s in cus.find_live_sessions()}
            want = _reference_live_scan()
            assert got == want, f"fast path {got} != reference {want}"

            # Sanity: the crafted fixtures actually exercised each shape
            # (an empty==empty comparison would vacuously pass above).
            got_ids = {sid for sid, _, _ in got}
            assert {"stop-only-01", "dup-live-01",
                    "fallback-live-01", live_sid} <= got_ids
            assert "dead-stale-01" not in got_ids
            assert "stopped-long-ago" not in got_ids
            assert not any(s.startswith("stale-") for s in got_ids)

            # account_filter must agree with the reference too.
            cus._TRANSCRIPT_INDEX_CACHE.clear()
            got_a = {(s.session_id, s.account, s.pane)
                     for s in cus.find_live_sessions(account_filter="acctA")}
            assert got_a == _reference_live_scan(account_filter="acctA")

            # cwd-first pin survives the fast path: dup-live-01 resolves to
            # the session's OWN project dir, not the glob's last-seen twin.
            cus._TRANSCRIPT_INDEX_CACHE.clear()
            dup = [s for s in cus.find_live_sessions()
                   if s.session_id == "dup-live-01"][0]
            assert dup.transcript_path == projects / "-home-x-mine" / "dup-live-01.jsonl"
        finally:
            _restore(saved)


def test_history_sized_log_stays_fast():
    """GH #90/#95 scaling pin: per-unique-session-id cost must be
    syscall-free. 8k unique ids whose cwd points at a REAL project dir but
    whose transcripts are MISSING is exactly the population that cost one
    live stat each before the fix (~13.5k ids -> multi-second, 100%-CPU
    `cus status` on the 2026-07-02 incident box, growing forever with the
    append-only log). Bound is generous for slow CI boxes; the pre-fix code
    exceeded it several-fold and grew linearly with history."""
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        saved, live_sid = _setup_env(tmp, n_stale=0)
        try:
            # cwd /home/x/repo maps to the real -home-x-repo dir created by
            # _setup_env, so the cwd-candidate probe can't shortcut on a
            # missing parent directory.
            lines = [f"2026-01-01T00:00:00Z,hist-{i:05d},default,%9,/home/x/repo"
                     for i in range(8000)]
            with cus.SESSIONS_LOG.open("a") as f:
                f.write("\n".join(lines) + "\n")
            cus._TRANSCRIPT_INDEX_CACHE.clear()
            t0 = time.monotonic()
            live = cus.find_live_sessions()
            elapsed = time.monotonic() - t0
            assert [s.session_id for s in live] == [live_sid]
            assert elapsed < 1.5, (
                f"find_live_sessions took {elapsed:.2f}s for 8k historical "
                f"ids — per-id syscalls have crept back in (GH #90/#95)")
        finally:
            _restore(saved)


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"  ok: {name}")
    print("all tests passed")
