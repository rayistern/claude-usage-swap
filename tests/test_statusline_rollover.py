"""Tests for GH #59 (statusline half) — the DEFAULT compact statusline must
flag a rolled-over 5h window live instead of showing the stale pre-reset %.

`_five_hour_rolled_since_poll` + the verbose-mode `↻reset(was X%)` badge and
the daemon-side countdown inference landed earlier (PRs #60/#61, covered by
tests/test_reset_rollover.py). The remaining gap closed here: COMPACT mode —
the default rendering — still printed the stale number bare (e.g. `5h:96%`
for up to a full poll interval after the window actually reset to ~0), and
let that stale number drive the near-swap ⚠ marker.

Run standalone:  python3 tests/test_statusline_rollover.py
Or under pytest: pytest tests/test_statusline_rollover.py
"""

import json
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

from click.testing import CliRunner

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import cus  # noqa: E402


def _iso(dt: datetime) -> str:
    return dt.isoformat().replace("+00:00", "Z")


def _now() -> datetime:
    return datetime.now(timezone.utc)


class _StatuslineEnv:
    """Throwaway state.json + config.yaml with cus path constants repointed,
    and a quiet diagnose() so SOS/warning early-returns don't preempt the
    output paths under test. Plain setattr + restore() (matching the other
    test files) so this also runs standalone without pytest fixtures."""

    def __init__(self, acct: dict):
        self._tmp = tempfile.TemporaryDirectory()
        root = Path(self._tmp.name)
        state_path = root / "state.json"
        state_path.write_text(json.dumps({"active": "a", "accounts": {"a": acct}}))
        config_path = root / "config.yaml"
        # color off → plain-text output the assertions can read directly
        config_path.write_text("statusline:\n  color: false\n")
        self._saved = {k: getattr(cus, k) for k in ("STATE_JSON", "CONFIG_YAML", "diagnose")}
        cus.STATE_JSON = state_path
        cus.CONFIG_YAML = config_path
        cus.diagnose = lambda state, config: []

    def restore(self):
        for k, v in self._saved.items():
            setattr(cus, k, v)
        self._tmp.cleanup()


def _rolled_acct(**extra) -> dict:
    """An account whose 5h window reset 5 minutes ago but whose last poll is
    12 minutes old — the exact stale-% window GH #59 is about."""
    now = _now()
    acct = {
        "current_5h_pct": 96,
        "current_7d_pct": 10,
        "next_swap_at_pct": 50,
        "five_hour_resets_at": _iso(now - timedelta(minutes=5)),
        "last_poll_ts": _iso(now - timedelta(minutes=12)),
    }
    acct.update(extra)
    return acct


def test_compact_flags_rollover_instead_of_stale_pct():
    """THE fix: compact shows ↻reset(was 96%) — never a bare stale 96%."""
    env = _StatuslineEnv(_rolled_acct())
    try:
        out = CliRunner().invoke(cus.statusline_cmd, ["--compact"]).output
        assert "5h:↻reset(was 96%)" in out
        assert "5h:96%" not in out
    finally:
        env.restore()


def test_compact_stale_pct_does_not_trip_warning_marker():
    """96% stored vs nxt:50 would normally render the ⚠ near-swap marker; a
    rolled window's real usage is ~0, so the stale number must not trip it
    (7d at 10% doesn't either)."""
    env = _StatuslineEnv(_rolled_acct())
    try:
        out = CliRunner().invoke(cus.statusline_cmd, ["--compact"]).output
        assert "⚠" not in out
    finally:
        env.restore()


def test_compact_prefers_daemon_stash_for_was_pct():
    """After the daemon's countdown inference zeroed current_5h_pct and
    stashed the pre-reset value, compact must show the stash (was 96%), not
    the zeroed live number."""
    env = _StatuslineEnv(_rolled_acct(current_5h_pct=0.0, five_hour_pct_pre_reset=96,
                                      five_hour_reset_inferred=True))
    try:
        out = CliRunner().invoke(cus.statusline_cmd, ["--compact"]).output
        assert "5h:↻reset(was 96%)" in out
    finally:
        env.restore()


def test_compact_normal_pct_when_not_rolled():
    """Freshly-polled account (poll after the reset) → plain % rendering,
    no rollover badge."""
    now = _now()
    env = _StatuslineEnv({
        "current_5h_pct": 42,
        "current_7d_pct": 10,
        "next_swap_at_pct": 50,
        "five_hour_resets_at": _iso(now + timedelta(hours=2)),
        "last_poll_ts": _iso(now - timedelta(minutes=3)),
    })
    try:
        out = CliRunner().invoke(cus.statusline_cmd, ["--compact"]).output
        assert "5h:42%" in out
        assert "↻reset" not in out
    finally:
        env.restore()


def test_compact_unknown_data_still_renders_question_marks():
    """token_stale etc. → the '?' early-return stays in charge; a rolled
    window with unreliable data must not fabricate a 'was X%' claim."""
    env = _StatuslineEnv(_rolled_acct(token_stale=True))
    try:
        out = CliRunner().invoke(cus.statusline_cmd, ["--compact"]).output
        assert "5h:?" in out
        assert "↻reset" not in out
    finally:
        env.restore()


def test_verbose_rollover_badge_unchanged():
    """Regression guard for the shared-label refactor: verbose mode keeps the
    exact badge it has shown since PR #60."""
    env = _StatuslineEnv(_rolled_acct())
    try:
        out = CliRunner().invoke(cus.statusline_cmd, ["--verbose"]).output
        assert "5h:↻reset(was 96%)" in out
    finally:
        env.restore()


def _run_all() -> int:
    import types
    tests = [v for k, v in globals().items()
             if k.startswith("test_") and isinstance(v, types.FunctionType)]
    failed = 0
    for t in tests:
        try:
            t()
            print(f"PASS  {t.__name__}")
        except Exception as e:  # noqa: BLE001
            failed += 1
            print(f"FAIL  {t.__name__}: {e}")
    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(_run_all())
