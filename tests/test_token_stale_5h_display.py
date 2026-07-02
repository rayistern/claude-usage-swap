"""Tests for the token_stale 5h last-known display.

Background: `token_stale` (GH #13) fires when an INACTIVE account's stored
access token has aged out — we skip polling it, but the refresh token (and
therefore the account) is still fully usable. Before this change, every
display surface (`status`, compact/verbose statusline) blanked BOTH
current_5h_pct and current_7d_pct to "?" under token_stale, even though the
underlying values are preserved (see `update_state_with_usage` Branch 0) and
even though GH #59's reset-inference (`_five_hour_rolled_since_poll` /
`_apply_countdown_reset_inference`) already keeps `current_5h_pct` honest
across a 5h reset boundary independent of whether polling itself succeeded.

This change surfaces the last-known 5h value under token_stale, marked as
not-yet-reconfirmed (dim + trailing '~'), instead of blanking it. 7d has no
equivalent reset-inference and stays "?".

Run standalone:  python3 tests/test_token_stale_5h_display.py
Or under pytest: pytest tests/test_token_stale_5h_display.py
"""

import json
import sys
import tempfile
from pathlib import Path

from click.testing import CliRunner

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import cus  # noqa: E402


class _Env:
    """Throwaway state.json + config.yaml with cus path constants repointed,
    and a quiet diagnose() so SOS/warning early-returns don't preempt the
    output paths under test."""

    def __init__(self, acct: dict, color: bool = False):
        self._tmp = tempfile.TemporaryDirectory()
        root = Path(self._tmp.name)
        state_path = root / "state.json"
        state_path.write_text(json.dumps({"active": "a", "accounts": {"a": acct}, "swap_history": []}))
        config_path = root / "config.yaml"
        config_path.write_text(f"statusline:\n  color: {'true' if color else 'false'}\n")
        self._saved = {k: getattr(cus, k) for k in ("STATE_JSON", "CONFIG_YAML", "diagnose", "find_live_sessions")}
        cus.STATE_JSON = state_path
        cus.CONFIG_YAML = config_path
        cus.diagnose = lambda state, config: []
        # `status` also scans real sessions.log/transcripts on the host via
        # find_live_sessions() — irrelevant to what's under test here (and
        # slow on a machine with a lot of session history), so stub it out.
        cus.find_live_sessions = lambda *a, **k: []

    def restore(self):
        for k, v in self._saved.items():
            setattr(cus, k, v)
        self._tmp.cleanup()


def _stale_acct(**extra) -> dict:
    """token_stale account, NOT rolled (5h window still ticking), 82% used."""
    acct = {
        "current_5h_pct": 82,
        "current_7d_pct": 30,
        "next_swap_at_pct": 90,
        "token_stale": True,
    }
    acct.update(extra)
    return acct


# --- Unit-level: the two decision helpers ---------------------------------


def test_pct_is_unknown_5h_known_7d_unknown_under_token_stale_alone():
    acct = {"token_stale": True}
    assert cus._pct_is_unknown(acct, "current_5h_pct") is False
    assert cus._pct_is_unknown(acct, "current_7d_pct") is True
    assert cus._pct_is_unknown(acct, None) is True  # unspecified key stays conservative


def test_pct_is_unknown_5h_still_blocked_by_a_harder_flag():
    """token_stale plus a genuinely blocking flag must still hide 5h — the
    last-known-value carve-out is for token_stale ALONE."""
    for flag in ("rate_limited", "token_expired", "poll_error"):
        acct = {"token_stale": True, flag: True}
        assert cus._pct_is_unknown(acct, "current_5h_pct") is True, flag


def test_pct_is_stale_known_only_for_5h_under_token_stale_alone():
    assert cus._pct_is_stale_known({"token_stale": True}, "current_5h_pct") is True
    assert cus._pct_is_stale_known({"token_stale": True}, "current_7d_pct") is False
    assert cus._pct_is_stale_known({}, "current_5h_pct") is False
    assert cus._pct_is_stale_known({"token_stale": True, "rate_limited": True}, "current_5h_pct") is False


# --- `cus status` -----------------------------------------------------------


def test_status_shows_last_known_5h_marked_stale_and_hides_7d():
    env = _Env(_stale_acct())
    try:
        out = CliRunner().invoke(cus.status).output
        lines = [l for l in out.splitlines() if l.startswith("a ")]
        assert len(lines) == 1, out
        assert "82.0~" in lines[0]
        assert "?" in lines[0]  # the 7d column
        assert "TOKEN_STALE" in lines[0]
    finally:
        env.restore()


def test_status_hides_5h_too_when_a_harder_flag_is_also_set():
    env = _Env(_stale_acct(rate_limited=True))
    try:
        out = CliRunner().invoke(cus.status).output
        lines = [l for l in out.splitlines() if l.startswith("a ")]
        assert len(lines) == 1, out
        assert "82.0~" not in lines[0]
    finally:
        env.restore()


# --- statusline (compact + verbose) -----------------------------------------


def test_statusline_compact_shows_last_known_5h_marked_stale():
    env = _Env(_stale_acct())
    try:
        out = CliRunner().invoke(cus.statusline_cmd, ["--compact"]).output
        assert "5h:82%~" in out
        assert "7d:?" in out
    finally:
        env.restore()


def test_statusline_verbose_shows_last_known_5h_marked_stale():
    env = _Env(_stale_acct())
    try:
        out = CliRunner().invoke(cus.statusline_cmd, ["--verbose"]).output
        assert "5h:82%~" in out
        assert "7d:?" in out
        assert "STALE" in out
    finally:
        env.restore()


def test_statusline_compact_both_blank_when_harder_flag_present():
    env = _Env(_stale_acct(rate_limited=True))
    try:
        out = CliRunner().invoke(cus.statusline_cmd, ["--compact"]).output
        assert "5h:?" in out
        assert "7d:?" in out
        assert "82%" not in out
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
