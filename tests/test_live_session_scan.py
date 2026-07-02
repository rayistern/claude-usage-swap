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
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

# cus.py lives at the repo root; make it importable when run from anywhere.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import cus  # noqa: E402


def _now_iso() -> str:
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
    return saved, live_sid


def _restore(saved):
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
        try:
            assert cus._transcript_index() == {}
        finally:
            _restore(saved)


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"  ok: {name}")
    print("all tests passed")
