"""Tests for `cus sessions` pure logic (GH #137 diagnostics view).

Covers the daemon-less, I/O-free helpers behind the command so the drift +
binding-constraint logic is exercised without tmux/proc:
  - _session_binding: 5h/7d hard cap, premium per-model weekly gate, standard-
    pool gate bypass, ladder-step warn, headroom, token/rate blockers.
  - build_session_rows: DRIFT flag (sessions.log label vs resolved mount) and
    bare/unresolved handling.
  - detect_slot_orphans: slots with live pids and no owning pane.

Run standalone:  python3 tests/test_sessions_view.py
Run under pytest: pytest tests/test_sessions_view.py
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import cus  # noqa: E402


def _config(**overrides) -> dict:
    """per_session config with the Fable weekly gate ON at cap 97%, mirroring
    the live setup that surfaced #137 (premium slots evacuate a Fable-capped
    account; standard slots keep it)."""
    cfg = cus.deep_merge(cus.DEFAULT_CONFIG, {
        "mode": "per_session",
        "thresholds": {"steps": [50, 75, 90]},
        "per_model_weekly": {"gate_enabled": True, "cap_pct": 97},
    })
    return cus.deep_merge(cfg, overrides)


def _acct(five=0.0, seven=0.0, per_model=None, **flags) -> dict:
    a = {"current_5h_pct": five, "current_7d_pct": seven}
    if per_model is not None:
        a["per_model_weekly_pct"] = per_model
    a.update(flags)
    return a


# --------------------------------------------------------------------------
# _session_binding
# --------------------------------------------------------------------------

def test_binding_5h_hard_cap():
    sev, txt = cus._session_binding(_acct(five=100.0), "premium", _config())
    assert sev == "blocked"
    assert "5h" in txt and "hard cap" in txt


def test_binding_premium_model_gate_blocks():
    sev, txt = cus._session_binding(_acct(five=10.0, per_model={"Fable": 98.0}), "premium", _config())
    assert sev == "blocked"
    assert "Fable" in txt and "premium gate" in txt


def test_binding_standard_pool_ignores_model_gate():
    # Same Fable=98% account, but on a STANDARD lane: not blocked, and the
    # number is surfaced-but-ignored rather than hidden (two-dimensional
    # exhaustion — standard-model work doesn't touch the exhausted model).
    sev, txt = cus._session_binding(_acct(five=10.0, per_model={"Fable": 98.0}), "standard", _config())
    assert sev == "ok"
    assert "ignored" in txt and "Fable" in txt


def test_binding_ladder_step_warn():
    sev, txt = cus._session_binding(_acct(five=78.0), "premium", _config())
    assert sev == "warn"
    assert "ladder step 75%" in txt


def test_binding_headroom_ok():
    sev, txt = cus._session_binding(_acct(five=22.0, seven=3.0, per_model={"Fable": 3.0}), "premium", _config())
    assert sev == "ok"
    assert "headroom" in txt


def test_binding_token_expired_beats_percentages():
    # A cached 0% must NOT read as healthy when the token is dead.
    sev, txt = cus._session_binding(_acct(five=0.0, token_expired=True), "premium", _config())
    assert sev == "blocked"
    assert "token expired" in txt


def test_binding_gate_off_when_disabled():
    # Same Fable=98% but gate disabled in config → the model number does not
    # block; only the aggregate ladder/5h can.
    cfg = _config(per_model_weekly={"gate_enabled": False, "cap_pct": 97})
    sev, _txt = cus._session_binding(_acct(five=10.0, per_model={"Fable": 98.0}), "premium", cfg)
    assert sev == "ok"


# --------------------------------------------------------------------------
# build_session_rows — DRIFT detection (#137)
# --------------------------------------------------------------------------

def _state() -> dict:
    return {
        "active": "merkos",
        "accounts": {
            "rayi2": _acct(five=5.0, seven=10.0),
            "merkos": _acct(five=50.0, seven=20.0),
        },
        "slots": {"slot-3": {"account": "rayi2", "pool": "premium"}},
    }


def test_row_flags_drift_when_log_disagrees_with_mount():
    # sessions.log said 'merkos' (launch-time label) but the live mount resolves
    # to rayi2 — the exact #137 signature.
    rows = cus.build_session_rows([{
        "session_id": "abc123", "pane": "%4", "cwd": "/x",
        "logged_account": "merkos", "mount": "slot-3",
        "resolved_account": "rayi2", "source": "disk-identity",
    }], _state(), _config())
    assert len(rows) == 1
    r = rows[0]
    assert r["drift"] is True
    assert r["account"] == "rayi2"
    assert r["logged_account"] == "merkos"
    assert r["slot"] == "slot-3"
    assert r["pool"] == "premium"


def test_row_no_drift_when_aligned():
    rows = cus.build_session_rows([{
        "session_id": "abc123", "pane": "%4", "cwd": "/x",
        "logged_account": "rayi2", "mount": "slot-3",
        "resolved_account": "rayi2", "source": "state-slot",
    }], _state(), _config())
    assert rows[0]["drift"] is False


def test_row_bare_session_never_drifts():
    # A bare/global pane has no mount to compare — "follows global creds" is by
    # design, NOT the #137 defect, so it must not be flagged.
    rows = cus.build_session_rows([{
        "session_id": "abc123", "pane": "%9", "cwd": "/x",
        "logged_account": "merkos", "mount": None,
        "resolved_account": None, "source": "unresolved",
    }], _state(), _config())
    assert rows[0]["drift"] is False
    assert rows[0]["bare"] is True


# --------------------------------------------------------------------------
# detect_slot_orphans
# --------------------------------------------------------------------------

def test_orphan_detected_when_pids_but_no_pane():
    orphans = cus.detect_slot_orphans({"slot-3": 2, "slot-5": 1}, {"slot-5"})
    assert orphans == [{"slot": "slot-3", "pids": 2}]


def test_no_orphans_when_every_slot_has_a_pane():
    orphans = cus.detect_slot_orphans({"slot-3": 1}, {"slot-3"})
    assert orphans == []


# ---------------------------------------------------------------------------
# _session_row_sort_key + pane_session_and_title (PR #206 readable-sessions;
# both blind reviewers flagged these as untested — F-O-3 / F-F-2, 2026-09-09).
# ---------------------------------------------------------------------------

def test_sort_key_groups_by_account_then_name():
    labels = {("%9", None): ("zsess", ""), ("%10", None): ("asess", "")}
    rows = [
        {"account": "acctB", "pane": "%1", "tmux_socket": None},
        {"account": "acctA", "pane": "%9", "tmux_socket": None},
        {"account": "acctA", "pane": "%10", "tmux_socket": None},
    ]
    ordered = sorted(rows, key=lambda r: cus._session_row_sort_key(r, labels))
    # acctA before acctB; within acctA, name 'asess' (%10) before 'zsess' (%9)
    assert [r["pane"] for r in ordered] == ["%10", "%9", "%1"]


def test_sort_key_numeric_pane_tiebreak():
    # same account + same name → pane sorts NUMERICALLY (%9 before %10, not str)
    labels = {("%9", None): ("x", ""), ("%10", None): ("x", "")}
    r9 = {"account": "a", "pane": "%9", "tmux_socket": None}
    r10 = {"account": "a", "pane": "%10", "tmux_socket": None}
    assert cus._session_row_sort_key(r9, labels) < cus._session_row_sort_key(r10, labels)


def test_sort_key_missing_account_and_name_sort_last():
    labels = {("%1", None): ("named", "")}
    r_named = {"account": "acct", "pane": "%1", "tmux_socket": None}
    r_noacct = {"account": None, "pane": "%2", "tmux_socket": None}
    ordered = sorted([r_noacct, r_named], key=lambda r: cus._session_row_sort_key(r, labels))
    assert ordered[0] is r_named  # None account ('~') sorts last


def test_sort_key_per_server_pane_not_collapsed():
    # identical pane id on two tmux servers must resolve to DISTINCT labels
    # (F-O-2/F-F-3: keying by pane alone would collapse them).
    labels = {("%3", "/sockA"): ("alpha", ""), ("%3", "/sockB"): ("beta", "")}
    rA = {"account": "a", "pane": "%3", "tmux_socket": "/sockA"}
    rB = {"account": "a", "pane": "%3", "tmux_socket": "/sockB"}
    assert cus._session_row_sort_key(rA, labels)[1] == "alpha"
    assert cus._session_row_sort_key(rB, labels)[1] == "beta"


def _fake_completed(stdout: str):
    class _R:
        pass
    r = _R()
    r.stdout = stdout
    return r


def test_pane_session_and_title_parses_tab():
    from unittest.mock import patch
    with patch.object(cus, "tmux_is_available", lambda: True), \
         patch.object(cus.subprocess, "run", lambda *a, **k: _fake_completed("7cc1a\tCredit building DB\n")):
        assert cus.pane_session_and_title("%1", None) == ("7cc1a", "Credit building DB")


def test_pane_session_and_title_empty_fields():
    from unittest.mock import patch
    with patch.object(cus, "tmux_is_available", lambda: True), \
         patch.object(cus.subprocess, "run", lambda *a, **k: _fake_completed("\t\n")):
        assert cus.pane_session_and_title("%1", None) == ("", "")


def test_pane_session_and_title_swallows_subprocess_error():
    from unittest.mock import patch
    def _boom(*a, **k):
        raise cus.subprocess.SubprocessError("tmux failed")
    with patch.object(cus, "tmux_is_available", lambda: True), \
         patch.object(cus.subprocess, "run", _boom):
        assert cus.pane_session_and_title("%1", None) == ("", "")


def test_pane_session_and_title_no_tmux_shortcircuits():
    # pane 'no-tmux' returns ('','') without any tmux call (guard short-circuit)
    assert cus.pane_session_and_title("no-tmux", None) == ("", "")


if __name__ == "__main__":
    import traceback
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    failed = 0
    for fn in fns:
        try:
            fn()
            print(f"ok   {fn.__name__}")
        except Exception:
            failed += 1
            print(f"FAIL {fn.__name__}")
            traceback.print_exc()
    print(f"\n{len(fns) - failed}/{len(fns)} passed")
    sys.exit(1 if failed else 0)
