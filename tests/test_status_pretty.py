"""Tests for `cus status --pretty` (rich renderer) and the shared
`_account_row` derivation both renderers consume.

Spec: docs/plans/2026-07-07-status-pretty.md

Run standalone:  python3 tests/test_status_pretty.py
Or under pytest: pytest tests/test_status_pretty.py
"""

import json
import sys
import tempfile
from pathlib import Path

from click.testing import CliRunner

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import cus  # noqa: E402


# --- _account_row: shared derivation ---------------------------------------


def test_account_row_flags_order_and_status_col():
    row = cus._account_row("a", {"token_stale": True, "rate_limited": True}, {}, 50)
    assert row["flags"] == ["RATE_LIMITED", "TOKEN_STALE"]
    assert row["status_col"] == "RATE_LIMITED,TOKEN_STALE"


def test_account_row_ok_when_no_flags():
    row = cus._account_row("a", {}, {}, 50)
    assert row["flags"] == []
    assert row["status_col"] == "ok"
    assert row["last"] == "never"


def test_account_row_disabled_comes_from_config():
    config = {"accounts": [{"name": "a", "disabled": True}, {"name": "b"}]}
    assert cus._account_row("a", {}, config, 50)["flags"] == ["DISABLED"]
    assert cus._account_row("b", {}, config, 50)["flags"] == []


def test_account_row_next_swap_only_when_off_first_step():
    assert cus._account_row("a", {"next_swap_at_pct": 75}, {}, 50)["next_swap_pct"] == 75
    assert cus._account_row("a", {"next_swap_at_pct": 50}, {}, 50)["next_swap_pct"] is None
    assert cus._account_row("a", {}, {}, 50)["next_swap_pct"] is None


def test_account_row_per_model_sorted_highest_first():
    acct = {"per_model_weekly_pct": {"sonnet": 10.0, "fable": 80.0}}
    assert cus._account_row("a", acct, {}, 50)["per_model"] == [("fable", 80.0), ("sonnet", 10.0)]


def test_account_row_est_only_on_divergence():
    # Deterministic despite now(): extrapolation is capped at
    # estimator.max_extrapolation_minutes (default 10), and the anchor ts is
    # far in the past — est = 50 + 2.0 * 10 = 70.
    acct = {
        "current_5h_pct": 50.0,
        "burn_rate_5h_pct_per_min": 2.0,
        "last_observed_ts": "2026-01-01T00:00:00Z",
    }
    row = cus._account_row("a", acct, {}, 50)
    assert row["est"] == {"5h": (70.0, 2.0)}
    # No burn rate measured → estimator falls back to polled → no est entry.
    assert cus._account_row("a", {"current_5h_pct": 50.0}, {}, 50)["est"] == {}


# --- --pretty: fixture env ---------------------------------------------------


class _PrettyEnv:
    """Throwaway state/config with every host-touching helper stubbed, so
    --pretty renders deterministically from the fixture alone."""

    STATE = {
        "active": "alpha",
        "accounts": {
            "alpha": {
                "current_5h_pct": 7.0,
                "current_7d_pct": 78.0,
                "last_swap_ts": "2026-01-01T00:00:00Z",
                "per_model_weekly_pct": {"Fable": 74.0},
            },
            "bravo": {
                "current_5h_pct": 82.0,
                "current_7d_pct": 30.0,
                "token_stale": True,
                "next_swap_at_pct": 75,
            },
            "charlie": {},
        },
        "swap_history": [
            {"ts": "2026-01-01T00:00:00Z", "from": "alpha", "to": "bravo",
             "trigger": "auto-ladder"},
        ],
        "slots": {"slot-1": {"account": "alpha"}},
    }
    CONFIG = (
        "mode: per_session\n"
        "accounts:\n"
        "  - name: alpha\n"
        "  - name: bravo\n"
        "  - name: charlie\n"
        "    disabled: true\n"
        "session_locks:\n"
        "  pinned:\n"
        "    sess-x: alpha\n"
    )

    def __init__(self, state: dict | None = None, config: str | None = None,
                 families: dict | None = None, with_slot_dir: bool = True,
                 sessions: list | None = None):
        self._tmp = tempfile.TemporaryDirectory()
        root = Path(self._tmp.name)
        (root / "state.json").write_text(json.dumps(state if state is not None else self.STATE))
        (root / "config.yaml").write_text(config if config is not None else self.CONFIG)
        self._families = families if families is not None else {"alpha": ["family-1", "family-2"]}
        self._sessions = sessions if sessions is not None else [
            cus.LiveSession(session_id="26f23a57beef", account="alpha",
                            pane="%1", cwd="/home/yaz/code/chabad-commons/GabAI",
                            started_at="2026-01-01T00:00:00Z",
                            last_stop_at=None, transcript_path=None),
            cus.LiveSession(session_id="5c0e416adead", account="alpha",
                            pane="%3", cwd="/home/yaz",
                            started_at="2026-01-01T00:00:00Z",
                            last_stop_at=None, transcript_path=None),
        ]
        slot_dirs = []
        if with_slot_dir:
            (root / "slot-1").mkdir()
            slot_dirs = [root / "slot-1"]
        stubs = {
            "STATE_JSON": root / "state.json",
            "CONFIG_YAML": root / "config.yaml",
            "diagnose": lambda state, config: [],
            "find_live_sessions": lambda *a, **k: self._sessions,
            "list_slot_dirs": lambda: slot_dirs,
            "mount_pids": lambda d: [11, 12] if d.name == "slot-1" else [],
            "pane_mount_name": lambda pane, tmux_socket=None: "slot-1" if pane == "%1" else None,
            "list_login_families": lambda a: self._families.get(a, []),
            "leased_families": lambda a, st: set(),
            "slot_leased_family": lambda st, slot: None,
            "has_independent_login": lambda a, slot: False,
            "list_provisioned_logins": lambda: [],
        }
        self._saved = {k: getattr(cus, k) for k in stubs}
        for k, v in stubs.items():
            setattr(cus, k, v)

    def restore(self):
        for k, v in self._saved.items():
            setattr(cus, k, v)
        self._tmp.cleanup()


def _pretty(env_kwargs: dict | None = None, columns: str = "160") -> str:
    env = _PrettyEnv(**(env_kwargs or {}))
    try:
        res = CliRunner().invoke(cus.status, ["--pretty"], env={"COLUMNS": columns},
                                 catch_exceptions=False)
        return res.output
    finally:
        env.restore()


# --- --pretty: header + accounts table --------------------------------------


def test_pretty_header_mode_and_ladder():
    out = _pretty()
    assert "per_session" in out
    assert "bare-launch: alpha" in out
    assert "50% → 75% → 90%" in out


def test_pretty_accounts_table_core():
    out = _pretty()
    assert "● alpha" in out                      # active marker replaces ' *'
    assert "Fable 74%" in out                    # per-model column
    assert " ago" in out                         # relative last swap
    assert "never" in out                        # bravo/charlie never swapped
    assert "█" in out                            # bars at 160 cols


def test_pretty_staleness_markers_survive():
    out = _pretty()
    assert "82%~" in out                         # stale-known 5h keeps ~
    assert "?" in out                            # unknown 7d keeps ?
    assert "TOKEN_STALE" in out


def test_pretty_disabled_and_next_swap():
    out = _pretty()
    assert "DISABLED" in out
    assert "next@75%" in out


def test_pretty_pool_column_and_gate_caption():
    out = _pretty()
    assert "2/2 free" in out
    assert "gate OFF" in out                     # pools exist, gate not enabled


def test_pretty_plain_mode_untouched_by_flag_addition():
    env = _PrettyEnv()
    try:
        out = CliRunner().invoke(cus.status, catch_exceptions=False).output
        assert "●" not in out
        assert "alpha *" in out                  # plain active marker survives
    finally:
        env.restore()


# --- --pretty: lanes & sessions, locks, swaps --------------------------------


def test_pretty_lane_row_merges_sessions():
    out = _pretty()
    assert "slot-1" in out
    assert "live 2 pids" in out
    assert "26f23a57" in out                    # session id on the lane row
    assert "GabAI" in out                       # cwd tail survives truncation
    assert "pool avail" in out                  # login annotation carried over


def test_pretty_bare_pseudo_lane():
    out = _pretty()
    assert "(bare)" in out
    assert "5c0e416a" in out                    # the %3 session groups under it


def test_pretty_locks_panel():
    out = _pretty()
    assert "Locks" in out
    assert "pinned: sess-x → alpha" in out


def test_pretty_recent_swaps():
    out = _pretty()
    assert "Recent swaps (1 of 1)" in out
    assert "alpha → bravo" in out
    assert "auto-ladder" in out


def test_pretty_orphaned_slot_mount_keeps_slot_grounding():
    """A session mounted on a slot whose dir vanished must stay grounded to
    its slot name (yellow 'slot dir missing'), not fall into '(global)'."""
    env = _PrettyEnv(sessions=[
        cus.LiveSession(session_id="deadbeef0123", account="alpha",
                        pane="%9", cwd="/home/yaz",
                        started_at="2026-01-01T00:00:00Z",
                        last_stop_at=None, transcript_path=None),
    ])
    try:
        cus.pane_mount_name = lambda pane, tmux_socket=None: "slot-9"
        out = CliRunner().invoke(cus.status, ["--pretty"], env={"COLUMNS": "160"},
                                 catch_exceptions=False).output
        assert "slot-9" in out
        assert "slot dir missing" in out
        assert "(global)" not in out
    finally:
        env.restore()


# --- --pretty: width adaptation & section hiding -----------------------------


def _max_line_len(out: str) -> int:
    return max((len(l) for l in out.splitlines()), default=0)


def test_pretty_wide_has_bars():
    out = _pretty(columns="160")
    assert "█" in out or "░" in out
    assert _max_line_len(out) <= 160


def test_pretty_medium_drops_bars_keeps_model_column():
    out = _pretty(columns="100")
    assert "█" not in out and "░" not in out
    assert "7d by model" in out
    assert _max_line_len(out) <= 100


def test_pretty_narrow_folds_model_column():
    out = _pretty(columns="60")
    assert "7d by model" not in out
    assert "Fable 74%" in out                   # folded under the account name
    assert _max_line_len(out) <= 60


def test_pretty_minimal_state_hides_sections():
    minimal = {"active": "a", "accounts": {"a": {}}, "swap_history": []}
    out = _pretty({"state": minimal, "config": "mode: per_session\n",
                   "families": {}, "with_slot_dir": False, "sessions": []})
    assert "Lanes" not in out
    assert "Recent swaps" not in out
    assert "Locks" not in out
    assert "gate OFF" not in out
    assert "bare-launch: a" in out               # header still renders


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
