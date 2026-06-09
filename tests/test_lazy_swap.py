"""Tests for the cache-aware lazy background swap gate (GH #56).

Guards the load-bearing invariant the design hinges on: a NON-deferrable
(urgent) swap — hard 7d cap / 5h saturation / reactive 429 — must NEVER be
held by the lazy cache gate, even when every session is cache-warm. If that
broke, the daemon could sit on a maxed account while warm sessions ride into a
rate-limit. Also covers the cheap short-circuits (hot-swap mode, lazy disabled)
and the warm-vs-cold payer detection.

Run standalone:  python3 tests/test_lazy_swap.py
Or under pytest: pytest tests/test_lazy_swap.py
"""

import sys
from pathlib import Path

# cus.py lives at the repo root; make it importable when run from anywhere.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import cus  # noqa: E402


def _session(pane: str = "%1", transcript: str = "/tmp/fake.jsonl") -> "cus.LiveSession":
    """A minimal live session whose transcript_path is truthy. Whether it's
    'warm' is decided by the monkeypatched cache_warm, not by reading the file.
    """
    return cus.LiveSession(
        session_id="deadbeef",
        account="default",
        pane=pane,
        cwd="/home/x/repo",
        started_at="2026-06-08T00:00:00Z",
        last_stop_at=None,
        transcript_path=Path(transcript),
    )


def _decision(deferrable: bool) -> "cus.SwapDecision":
    return cus.SwapDecision(target="default", reason="test", tier=2,
                            gate="ladder", deferrable=deferrable)


def _patch_sessions(monkeypatch_like, sessions, warm: bool):
    """Replace find_live_sessions + cache_warm on the cus module."""
    cus.find_live_sessions = lambda *a, **k: list(sessions)
    cus.cache_warm = lambda *a, **k: warm


def test_swapdecision_defaults():
    d = cus.SwapDecision(target="x", reason="r", tier=1)
    assert d.gate == "ladder"
    assert d.deferrable is True


def test_urgent_never_deferred_even_when_warm():
    """THE invariant: deferrable=False short-circuits before any session scan."""
    # If find_live_sessions is even consulted, fail loudly.
    cus.find_live_sessions = lambda *a, **k: (_ for _ in ()).throw(
        AssertionError("urgent swap must not scan sessions / consult cache")
    )
    cus.cache_warm = lambda *a, **k: True
    config = {"lazy_swap": {"enabled": True}, "hot_swap": {"enabled": False}}
    assert cus._lazy_warm_sessions(_decision(deferrable=False), config) == []


def test_hot_swap_mode_disables_lazy_gate():
    _patch_sessions(None, [_session()], warm=True)
    config = {"lazy_swap": {"enabled": True}, "hot_swap": {"enabled": True}}
    assert cus._lazy_warm_sessions(_decision(deferrable=True), config) == []


def test_lazy_disabled_swaps_now():
    _patch_sessions(None, [_session()], warm=True)
    config = {"lazy_swap": {"enabled": False}, "hot_swap": {"enabled": False}}
    assert cus._lazy_warm_sessions(_decision(deferrable=True), config) == []


def test_warm_sessions_defer():
    sessions = [_session("%1"), _session("%2")]
    _patch_sessions(None, sessions, warm=True)
    config = {"lazy_swap": {"enabled": True}, "hot_swap": {"enabled": False}}
    warm = cus._lazy_warm_sessions(_decision(deferrable=True), config)
    assert len(warm) == 2


def test_cold_sessions_swap_now():
    _patch_sessions(None, [_session("%1"), _session("%2")], warm=False)
    config = {"lazy_swap": {"enabled": True}, "hot_swap": {"enabled": False}}
    assert cus._lazy_warm_sessions(_decision(deferrable=True), config) == []


def _run_all() -> int:
    import types
    tests = [v for k, v in globals().items()
             if k.startswith("test_") and isinstance(v, types.FunctionType)]
    failed = 0
    for t in tests:
        # Reset patched module functions between tests (each test sets what it needs).
        try:
            t()
            print(f"PASS  {t.__name__}")
        except Exception as e:  # noqa: BLE001 — standalone runner wants the message
            failed += 1
            print(f"FAIL  {t.__name__}: {e}")
    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(_run_all())
