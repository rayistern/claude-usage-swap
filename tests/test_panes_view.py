"""Tests for `cus panes` (GH #230) — the per-pane live view.

The command is READ-ONLY and derives everything from files already on disk, so
these tests build fake transcripts/registries in tmp_path and never touch real
credentials, real accounts or the live tmux server.

Covered (the issue's acceptance list):
  - a pane with live subagents (and their models)
  - a pane at a 429 wall, with the reset time — and a stale 429 that must NOT
    render as a current wall
  - a pane whose transcript is missing or unreadable
  - the attribution math (and that it is labelled an estimate)
  - the window boundary (in/out of window, and the tail-read boundary)
  - the per-message deduplication that stops one API response being counted
    once per content block
  - account headroom rendering "unknown" for a stale/missing observation

Added by the dual-review follow-up on PR #232 (2026-09-18) — each block is
named after the failure it locks out:
  - a live wall OLDER than --window (the read depth used to be the window)
  - the pane-state and account signals attesting a wall with no readable 429
  - a 429 from before the slot changed account not reading as a current wall
  - peer-registry identity: dead pid, RECYCLED pid (procStart mismatch),
    non-claude process, newest-entry-wins, malformed `tmux`
  - unmeasured panes: null share, excluded from the denominator, "-" not "0%"

Run standalone:  python3 tests/test_panes_view.py
Run under pytest: pytest tests/test_panes_view.py
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import cus  # noqa: E402

NOW = datetime(2026, 9, 18, 12, 0, 0, tzinfo=timezone.utc)
WINDOW_START = NOW - timedelta(minutes=30)


def iso(dt: datetime) -> str:
    return dt.isoformat().replace("+00:00", "Z")


def usage_line(ts: datetime, model: str, msg_id: str, *, block: int = 0,
               inp: int = 100, out: int = 50, cc: int = 0, cr: int = 0) -> str:
    """One assistant JSONL line. Claude Code emits one per content BLOCK, each
    repeating the same cumulative usage — `block` lets a test reproduce that."""
    return json.dumps({
        "type": "assistant", "timestamp": iso(ts), "apiBlockIndex": block,
        "uuid": f"{msg_id}-{block}", "requestId": f"req-{msg_id}",
        "message": {"role": "assistant", "id": msg_id, "model": model,
                    "usage": {"input_tokens": inp, "output_tokens": out,
                              "cache_creation_input_tokens": cc,
                              "cache_read_input_tokens": cr}},
    })


def agent_launch_line(ts: datetime, agent_id: str, model: str, desc: str) -> str:
    return json.dumps({
        "type": "user", "timestamp": iso(ts), "uuid": f"u-{agent_id}",
        "message": {"role": "user", "content": [{"type": "tool_result",
                                                 "tool_use_id": f"toolu_{agent_id}"}]},
        "toolUseResult": {"isAsync": True, "status": "async_launched",
                          "agentId": agent_id, "resolvedModel": model,
                          "description": desc},
    })


def agent_done_line(ts: datetime, agent_id: str, status: str = "completed") -> str:
    return json.dumps({
        "type": "user", "timestamp": iso(ts), "uuid": f"d-{agent_id}",
        "message": {"role": "user", "content":
                    f"<task-notification>\n<task-id>{agent_id}</task-id>\n"
                    f"<status>{status}</status>\n</task-notification>"},
    })


def wall_line(ts: datetime, resets_at: datetime, kind: str = "five_hour") -> str:
    return json.dumps({
        "type": "assistant", "timestamp": iso(ts), "uuid": f"w-{iso(ts)}",
        "isApiErrorMessage": True, "apiErrorStatus": 429, "error": "rate_limit",
        "quotaLimits": {"status": "rejected", "rateLimitType": kind,
                        "resetsAt": int(resets_at.timestamp())},
        "message": {"role": "assistant", "content": "rate limited"},
    })


# --------------------------------------------------------------------------
# window spec
# --------------------------------------------------------------------------

def test_parse_window_spec_units():
    assert cus.parse_window_spec("30m") == 30
    assert cus.parse_window_spec("2h") == 120
    assert cus.parse_window_spec("90s") == 1.5
    assert cus.parse_window_spec("1d") == 1440
    assert cus.parse_window_spec("45") == 45          # bare == minutes
    for bad in ("", "0m", "-5m", "abc"):
        try:
            cus.parse_window_spec(bad)
        except ValueError:
            continue
        raise AssertionError(f"{bad!r} should have been rejected")


# --------------------------------------------------------------------------
# transcript parsing: dedup, window boundary, models
# --------------------------------------------------------------------------

def test_usage_deduplicated_per_message_id():
    """One response split across 4 content-block lines must count ONCE.

    This is the bug that made the first cut of the parser report a 4x burn
    rate: every apiBlockIndex line repeats the same cumulative usage.
    """
    ts = NOW - timedelta(minutes=5)
    lines = [usage_line(ts, "claude-opus-5", "msg_A", block=b, inp=10, out=5, cr=1000)
             for b in range(4)]
    facts = cus.parse_transcript_facts(lines, WINDOW_START)
    assert facts["messages"] == 1
    assert facts["by_model"]["claude-opus-5"]["total"] == 1015
    assert facts["total"] == 1015


def test_window_boundary_excludes_older_lines():
    inside = usage_line(WINDOW_START + timedelta(seconds=1), "m1", "in", inp=1000, out=0)
    on_edge = usage_line(WINDOW_START, "m1", "edge", inp=200, out=0)
    outside = usage_line(WINDOW_START - timedelta(seconds=1), "m1", "out", inp=999999, out=0)
    facts = cus.parse_transcript_facts([outside, on_edge, inside], WINDOW_START)
    # the edge itself is IN (>= window_start); the line one second older is OUT
    assert facts["total"] == 1200
    assert set(facts["by_model"]) == {"m1"}


def test_multiple_models_tracked_separately():
    ts = NOW - timedelta(minutes=2)
    lines = [usage_line(ts, "claude-opus-5", "a", inp=100, out=10),
             usage_line(ts, "claude-fable-5-1", "b", inp=7, out=3, cc=40)]
    facts = cus.parse_transcript_facts(lines, WINDOW_START)
    assert facts["by_model"]["claude-opus-5"]["total"] == 110
    assert facts["by_model"]["claude-fable-5-1"]["total"] == 50


def test_unparseable_lines_are_skipped_not_guessed():
    ts = NOW - timedelta(minutes=1)
    facts = cus.parse_transcript_facts(
        ["{not json", "", "plain text", usage_line(ts, "m", "x", inp=5, out=5)], WINDOW_START)
    assert facts["total"] == 10


def test_cost_window_needs_a_prewindow_baseline():
    """Session-lifetime cost must never be reported as a 30-minute cost."""
    only_after = [json.dumps({"type": "cost-state", "timestamp": iso(NOW),
                              "totalCostUSD": 21.0})]
    assert cus.parse_transcript_facts(only_after, WINDOW_START)["cost_window_usd"] is None
    both = [json.dumps({"type": "cost-state",
                        "timestamp": iso(WINDOW_START - timedelta(minutes=1)),
                        "totalCostUSD": 20.0}),
            json.dumps({"type": "cost-state", "timestamp": iso(NOW), "totalCostUSD": 21.5})]
    assert cus.parse_transcript_facts(both, WINDOW_START)["cost_window_usd"] == 1.5


# --------------------------------------------------------------------------
# subagents
# --------------------------------------------------------------------------

def test_live_and_finished_subagents_paired():
    t = NOW - timedelta(minutes=10)
    lines = [agent_launch_line(t, "aaa111", "claude-fable-5-1", "QA wave"),
             agent_launch_line(t, "bbb222", "claude-opus-5", "Docs pass"),
             agent_done_line(NOW - timedelta(minutes=1), "bbb222")]
    facts = cus.parse_transcript_facts(lines, WINDOW_START)
    assert set(facts["agents_launched"]) == {"aaa111", "bbb222"}
    assert facts["agents_finished"] == {"bbb222": "completed"}
    assert facts["agents_launched"]["aaa111"]["model"] == "claude-fable-5-1"


# --------------------------------------------------------------------------
# 429 walls
# --------------------------------------------------------------------------

def test_wall_captured_with_reset_time():
    resets = NOW + timedelta(minutes=42)
    facts = cus.parse_transcript_facts(
        [wall_line(NOW - timedelta(minutes=3), resets)], WINDOW_START)
    w = facts["wall"]
    assert w["rate_limit_type"] == "five_hour"
    assert cus._panes_parse_ts(w["resets_at"]) == resets.replace(microsecond=0)
    assert cus._wall_active(w, NOW) is True


def test_stale_429_is_not_a_current_wall():
    """A day-old rejection deep in an idle pane's tail must not read as walled."""
    old = {"rate_limit_type": "five_hour",
           "resets_at": iso(NOW + timedelta(hours=2)),
           "observed_at": iso(NOW - timedelta(hours=23))}
    assert cus._wall_active(old, NOW) is False
    assert "23h ago" in cus._wall_text({"wall": old}, NOW)


def test_expired_wall_is_not_active():
    expired = {"rate_limit_type": "five_hour",
               "resets_at": iso(NOW - timedelta(minutes=1)),
               "observed_at": iso(NOW - timedelta(minutes=5))}
    assert cus._wall_active(expired, NOW) is False
    assert cus._wall_active(None, NOW) is False


# --------------------------------------------------------------------------
# attribution math
# --------------------------------------------------------------------------

def _row(pane, account, total):
    return {"pane": pane, "account": account, "tokens_window": {"total": total}}


def test_attribution_shares_sum_to_100_per_account():
    rows = [_row("%1", "rayi2", 750), _row("%2", "rayi2", 250),
            _row("%3", "rayi5", 400), _row("%4", None, 999)]
    cus.attribute_account_shares(rows)
    assert rows[0]["attribution"]["pane_pct_of_account_window"] == 75.0
    assert rows[1]["attribution"]["pane_pct_of_account_window"] == 25.0
    assert rows[0]["attribution"]["account_window_tokens"] == 1000
    assert rows[2]["attribution"]["pane_pct_of_account_window"] == 100.0
    # A pane with no resolved account has no denominator — null, never 100%.
    assert rows[3]["attribution"]["pane_pct_of_account_window"] is None


def test_attribution_is_labelled_an_estimate():
    """The honesty contract: the share must never be presented as measured."""
    rows = [_row("%1", "rayi2", 10)]
    cus.attribute_account_shares(rows)
    assert rows[0]["attribution"]["kind"] == "estimate"


def test_attribution_zero_account_tokens_is_null_not_divide_by_zero():
    rows = [_row("%1", "rayi2", 0), _row("%2", "rayi2", 0)]
    cus.attribute_account_shares(rows)
    assert all(r["attribution"]["pane_pct_of_account_window"] is None for r in rows)


# --------------------------------------------------------------------------
# account headroom honesty
# --------------------------------------------------------------------------

def test_headroom_known_when_freshly_observed():
    h = cus.account_headroom({"current_5h_pct": 40.0, "current_7d_pct": 10.0,
                              "last_observed_ts": iso(NOW - timedelta(minutes=2)),
                              "five_hour_resets_at": iso(NOW + timedelta(hours=1))}, NOW)
    assert h["known"] and h["headroom_5h_pct"] == 60.0 and h["headroom_7d_pct"] == 90.0


def test_headroom_unknown_when_observation_is_stale():
    h = cus.account_headroom({"current_5h_pct": 0.0, "current_7d_pct": 0.0,
                              "last_observed_ts": iso(NOW - timedelta(hours=6))}, NOW)
    assert h["known"] is False and h["five_hour_pct"] is None
    assert "stale" in h["reason"]


def test_headroom_unknown_when_never_observed_or_missing():
    assert cus.account_headroom({"current_5h_pct": 0.0}, NOW)["known"] is False
    assert cus.account_headroom(None, NOW)["known"] is False
    assert cus.account_headroom({}, NOW)["five_hour_pct"] is None


# --------------------------------------------------------------------------
# tail reading
# --------------------------------------------------------------------------

def test_tail_reads_back_past_the_window(tmp_path):
    p = tmp_path / "t.jsonl"
    old = [usage_line(NOW - timedelta(hours=3) + timedelta(seconds=i), "m", f"old{i}")
           for i in range(400)]
    new = [usage_line(NOW - timedelta(minutes=i), "m", f"new{i}") for i in range(20, 0, -1)]
    p.write_text("\n".join(old + new) + "\n")
    lines, covered = cus.tail_lines(p, WINDOW_START)
    assert covered is True
    facts = cus.parse_transcript_facts(lines, WINDOW_START)
    assert facts["messages"] == 20        # only the in-window messages counted


def test_tail_cap_reports_partial_coverage(tmp_path):
    p = tmp_path / "t.jsonl"
    p.write_text("\n".join(usage_line(NOW - timedelta(seconds=i), "m", f"x{i}")
                           for i in range(2000)) + "\n")
    lines, covered = cus.tail_lines(p, NOW - timedelta(days=30), max_bytes=4096)
    assert covered is False               # caller must label totals as a floor
    assert lines


# --------------------------------------------------------------------------
# full row assembly (fake registry + fake projects tree)
# --------------------------------------------------------------------------

def _fake_env(monkeypatch, tmp_path):
    """Point cus at a throwaway CLAUDE_DIR/ACCOUNTS_DIR. No real creds anywhere."""
    claude = tmp_path / "dot-claude"
    (claude / "projects").mkdir(parents=True)
    monkeypatch.setattr(cus, "CLAUDE_DIR", claude)
    monkeypatch.setattr(cus, "ACCOUNTS_DIR", tmp_path / "accounts")
    monkeypatch.setattr(cus, "_pid_config_dir", lambda pid: None)
    monkeypatch.setattr(cus, "pane_mount_name", lambda pane, sock=None: "slot-7")
    return claude


STATE = {"slots": {"slot-7": {"account": "rayi5", "pool": "premium"}},
         "accounts": {"rayi5": {"current_5h_pct": 20.0, "current_7d_pct": 5.0}}}
CONFIG = {"session_locks": {"locked_slots": ["slot-7"]}}


def test_row_for_pane_with_live_subagents(monkeypatch, tmp_path):
    claude = _fake_env(monkeypatch, tmp_path)
    sid = "sess-1"
    proj = claude / "projects" / "-home-rayi-repos-demo"
    proj.mkdir(parents=True)
    t = proj / f"{sid}.jsonl"
    t.write_text("\n".join([
        usage_line(NOW - timedelta(minutes=5), "claude-opus-5", "m1", inp=1000, out=200),
        agent_launch_line(NOW - timedelta(minutes=8), "live01", "claude-fable-5-1", "QA wave"),
        agent_launch_line(NOW - timedelta(minutes=9), "done01", "claude-opus-5", "Docs"),
        agent_done_line(NOW - timedelta(minutes=2), "done01"),
    ]) + "\n")
    subs = proj / sid / "subagents"
    subs.mkdir(parents=True)
    (subs / "agent-live01.jsonl").write_text(
        usage_line(NOW - timedelta(minutes=3), "claude-fable-5-1", "s1", inp=500, out=100) + "\n")
    (subs / "agent-done01.jsonl").write_text(
        usage_line(NOW - timedelta(minutes=4), "claude-opus-5", "s2", inp=70, out=30) + "\n")

    registry = {"%9": {"pid": 4242, "session_id": sid,
                       "cwd": "/home/rayi/repos/demo", "claude_status": "busy"}}
    row = cus.collect_pane_row({"pane": "%9", "session": "demo1a", "pane_pid": 4241,
                                "state": "working"},
                               registry, STATE, CONFIG, WINDOW_START, NOW)
    assert row["transcript_status"] == "ok"
    assert row["slot"] == "slot-7" and row["account"] == "rayi5" and row["locked"] is True
    # live01 has no completion notice; done01 does.
    assert row["subagents_live"] == 1
    assert row["subagent_models"] == {"claude-fable-5-1": 1}
    # subagent spend is folded in — it is invisible in the parent transcript.
    assert row["tokens_window"]["subagent_total"] == 700
    assert row["tokens_window"]["total"] == 1200 + 700
    assert row["tokens_window"]["by_model"]["claude-fable-5-1"]["total"] == 600
    assert row["burn_tokens_per_min"] == round(1900 / 30.0, 1)


def test_row_for_walled_pane_carries_reset(monkeypatch, tmp_path):
    claude = _fake_env(monkeypatch, tmp_path)
    sid = "sess-wall"
    proj = claude / "projects" / "-home-rayi-repos-demo"
    proj.mkdir(parents=True)
    resets = NOW + timedelta(minutes=37)
    (proj / f"{sid}.jsonl").write_text(
        wall_line(NOW - timedelta(minutes=4), resets) + "\n")
    registry = {"%9": {"pid": 1, "session_id": sid, "cwd": "/home/rayi/repos/demo"}}
    row = cus.collect_pane_row({"pane": "%9", "session": "d", "state": "limit_menu"},
                               registry, STATE, CONFIG, WINDOW_START, NOW)
    assert row["wall"]["rate_limit_type"] == "five_hour"
    assert cus._wall_active(row["wall"], NOW) is True
    assert "↻0h37m" in cus._wall_text(row, NOW)


def test_row_for_missing_transcript_is_unknown_not_zero(monkeypatch, tmp_path):
    _fake_env(monkeypatch, tmp_path)
    registry = {"%9": {"pid": 1, "session_id": "nope", "cwd": "/home/rayi/repos/gone"}}
    row = cus.collect_pane_row({"pane": "%9", "session": "d", "state": "idle"},
                               registry, STATE, CONFIG, WINDOW_START, NOW)
    assert row["transcript_status"] == "missing"
    # None, not 0: an unread transcript is not a pane that spent nothing.
    assert row["unmeasured"] is True
    assert row["tokens_window"]["total"] is None
    assert row["burn_tokens_per_min"] is None
    assert row["subagents_live"] is None      # also read from the transcript
    # and the table must SAY it is unknown rather than print a blank model list
    payload = {"generated_at": iso(NOW), "window_minutes": 30,
               "window_start": iso(WINDOW_START), "panes": [row], "accounts": {}}
    cus.attribute_account_shares(payload["panes"])
    assert "usage unknown: missing" in cus.render_panes_table(payload)


def test_row_for_unreadable_transcript_is_flagged(monkeypatch, tmp_path):
    claude = _fake_env(monkeypatch, tmp_path)
    sid = "sess-perm"
    proj = claude / "projects" / "-home-rayi-repos-demo"
    proj.mkdir(parents=True)
    t = proj / f"{sid}.jsonl"
    t.write_text("{}\n")
    t.chmod(0o000)
    try:
        registry = {"%9": {"pid": 1, "session_id": sid, "cwd": "/home/rayi/repos/demo"}}
        row = cus.collect_pane_row({"pane": "%9", "session": "d", "state": "idle"},
                                   registry, STATE, CONFIG, WINDOW_START, NOW)
        if row["transcript_status"] == "ok":       # running as root: chmod is a no-op
            return
        assert row["transcript_status"].startswith("unreadable")
        assert row["unmeasured"] is True
        assert row["tokens_window"]["total"] is None
    finally:
        t.chmod(0o600)


def test_pane_with_no_registered_session_is_reported_not_dropped(monkeypatch, tmp_path):
    _fake_env(monkeypatch, tmp_path)
    row = cus.collect_pane_row({"pane": "%99", "session": "orphan", "state": "idle"},
                               {}, STATE, CONFIG, WINDOW_START, NOW)
    assert row["pane"] == "%99"
    assert row["transcript_status"] == "no-session-registered"


# --------------------------------------------------------------------------
# reader failure must never produce a table
# --------------------------------------------------------------------------

def test_missing_pane_reader_raises_rather_than_fabricating(monkeypatch, tmp_path):
    """The reader exits 3 when it can't find the canonical pane_state.py.
    A missing helper is STOP and escalate, never hand-scraping."""
    fake = tmp_path / "fake_reader.py"
    fake.write_text('import json,sys\n'
                    'print(json.dumps({"error":"moved","looked_in":[]}))\n'
                    'sys.exit(3)\n')
    monkeypatch.setenv("CUS_PANE_STATE_PY", str(fake))
    try:
        cus.read_panes_from_reader()
    except cus.PanesError as e:
        assert "missing" in str(e)
        return
    raise AssertionError("expected PanesError")


def test_pane_reader_tmux_failure_is_distinguished(monkeypatch, tmp_path):
    fake = tmp_path / "fake_reader.py"
    fake.write_text('import sys\nsys.stderr.write("tmux down\\n")\nsys.exit(2)\n')
    monkeypatch.setenv("CUS_PANE_STATE_PY", str(fake))
    try:
        cus.read_panes_from_reader()
    except cus.PanesError as e:
        assert "tmux unusable" in str(e)
        return
    raise AssertionError("expected PanesError")


# --------------------------------------------------------------------------
# --me resolution
# --------------------------------------------------------------------------

def _payload(rows, accounts=None):
    p = {"generated_at": iso(NOW), "window_minutes": 30,
         "window_start": iso(WINDOW_START), "panes": rows, "accounts": accounts or {}}
    cus.attribute_account_shares(rows)
    return p


def _full_row(pane, slot, account, total=1000):
    return {"pane": pane, "tmux_session": "s", "state": "working", "slot": slot,
            "account": account, "pool": "premium", "locked": False,
            "transcript_status": "ok", "window_covered": True, "subagents_live": 0,
            "subagent_models": {}, "subagents": [], "wall": None,
            "cost_window_usd": None, "burn_tokens_per_min": 33.3,
            "burn_new_tokens_per_min": 33.3,
            "tokens_window": {"total": total, "new": total, "subagent_total": 0,
                              "by_model": {"claude-opus-5": {"total": total, "input": total,
                                                             "output": 0, "cache_creation": 0,
                                                             "cache_read": 0}}}}


def test_me_resolves_by_tmux_pane():
    p = _payload([_full_row("%1", "slot-1", "rayi2"), _full_row("%2", "slot-2", "rayi5")])
    row, err = cus.resolve_me_pane(p, "%2", "/home/rayi/claude-accounts/slot-2")
    assert err is None and row["pane"] == "%2"


def test_me_falls_back_to_config_dir():
    p = _payload([_full_row("%1", "slot-1", "rayi2"), _full_row("%2", "slot-2", "rayi5")])
    row, err = cus.resolve_me_pane(p, None, "/home/rayi/claude-accounts/slot-2")
    assert err is None and row["pane"] == "%2"


def test_me_refuses_to_guess_on_a_shared_slot():
    """Two panes joined on one mount: guessing would hand a session another
    pane's headroom picture right before it fans out."""
    p = _payload([_full_row("%1", "slot-3", "rayi5"), _full_row("%2", "slot-3", "rayi5")])
    row, err = cus.resolve_me_pane(p, None, "/home/rayi/claude-accounts/slot-3")
    assert row is None and "share" in err and "%1" in err and "%2" in err


def test_me_errors_when_unresolvable():
    p = _payload([_full_row("%1", "slot-1", "rayi2")])
    row, err = cus.resolve_me_pane(p, None, None)
    assert row is None and "could not resolve" in err


def test_me_renders_unknown_headroom_loudly():
    rows = [_full_row("%1", "slot-1", "rayi2")]
    p = _payload(rows, {"rayi2": cus.account_headroom(
        {"current_5h_pct": 0.0, "current_7d_pct": 0.0,
         "last_observed_ts": iso(NOW - timedelta(hours=9))}, NOW)})
    text = cus.render_me(p, rows[0])
    assert "HEADROOM (rayi2): UNKNOWN" in text
    assert "0%" not in text.split("HEADROOM")[1].split("\n")[0]
    assert "estimate" in text


def test_me_renders_known_headroom_and_labels_the_share():
    rows = [_full_row("%1", "slot-1", "rayi2")]
    p = _payload(rows, {"rayi2": cus.account_headroom(
        {"current_5h_pct": 25.0, "current_7d_pct": 10.0,
         "last_observed_ts": iso(NOW - timedelta(minutes=1)),
         "five_hour_resets_at": iso(NOW + timedelta(hours=2))}, NOW)})
    text = cus.render_me(p, rows[0])
    assert "5h 75% left" in text and "7d 90% left" in text
    assert "not a measured percentage" in text


def test_table_labels_the_share_column_as_attribution():
    rows = [_full_row("%1", "slot-1", "rayi2")]
    text = cus.render_panes_table(_payload(rows))
    assert "SHARE*" in text
    assert "ATTRIBUTION" in text
    assert "NOT the account's usage" in text


def test_table_marks_stale_accounts_unknown():
    rows = [_full_row("%1", "slot-1", "rayi2")]
    p = _payload(rows, {"rayi2": cus.account_headroom(
        {"current_5h_pct": 0.0, "current_7d_pct": 0.0,
         "last_observed_ts": iso(NOW - timedelta(hours=9))}, NOW)})
    text = cus.render_panes_table(p)
    assert "unknown — last observation" in text


if __name__ == "__main__":
    import pytest
    raise SystemExit(pytest.main([__file__, "-q"]))


# ==========================================================================
# Dual-review follow-up, PR #232 (2026-09-18)
# ==========================================================================

# --------------------------------------------------------------------------
# Finding 1: wall detection must not be bounded by --window
# --------------------------------------------------------------------------

def _write_session(claude, sid, lines):
    proj = claude / "projects" / "-home-rayi-repos-demo"
    proj.mkdir(parents=True, exist_ok=True)
    t = proj / f"{sid}.jsonl"
    t.write_text("\n".join(lines) + "\n")
    return t


def _reg(sid):
    return {"%9": {"pid": 1, "session_id": sid, "cwd": "/home/rayi/repos/demo"}}


def _walled_45m_ago_transcript():
    """The Sonnet seat's reproduction, as a fixture: a 429 at t-45m with the
    reset still 15 minutes out, then >2MB of activity between t-45m and t-31m,
    a line exactly on the window edge, and light recent activity. The first
    1MB/2MB tail reads reach the 30m window WITHOUT reaching the 429."""
    pad = "x" * 1800
    filler = []
    for i in range(1300):                      # ~2.4MB, all OUTSIDE the 30m window
        ts = NOW - timedelta(minutes=44) + timedelta(seconds=i * 0.6)
        filler.append(json.dumps({"type": "user", "timestamp": iso(ts), "uuid": f"f{i}",
                                  "message": {"role": "user", "content": pad}}))
    return ([wall_line(NOW - timedelta(minutes=45), NOW + timedelta(minutes=15))]
            + filler
            + [usage_line(WINDOW_START, "claude-opus-5", "edge", inp=10, out=0),
               usage_line(NOW - timedelta(minutes=2), "claude-opus-5", "recent", inp=90, out=0)])


def test_wall_older_than_window_is_still_detected(monkeypatch, tmp_path):
    """THE regression. With --window 30m a wall from 45 minutes ago (reset still
    ahead) rendered as "-": tail_lines stopped as soon as it covered the token
    window, so the 429 was never read. A false "clear" is the one answer this
    command must not give before a fan-out."""
    claude = _fake_env(monkeypatch, tmp_path)
    lines = _walled_45m_ago_transcript()
    t = _write_session(claude, "sess-deepwall", lines)
    assert t.stat().st_size > 2 * cus.PANES_TAIL_START_BYTES   # fixture really is deep

    # Guard the fixture itself: reading only as far as the WINDOW misses the 429.
    shallow, covered = cus.tail_lines(t, WINDOW_START)
    assert covered is True
    assert cus.parse_transcript_facts(shallow, WINDOW_START)["wall"] is None

    row = cus.collect_pane_row({"pane": "%9", "session": "d", "state": "working"},
                               _reg("sess-deepwall"), STATE, CONFIG, WINDOW_START, NOW)
    assert row["wall"] is not None, "429 outside --window was not read"
    assert row["wall_active"] is True
    assert row["wall_evidence"] == ["transcript_429"]
    assert "WALL five_hour" in cus._wall_text(row, NOW) and "↻0h15m" in cus._wall_text(row, NOW)
    # ...and reading deeper must not leak pre-window tokens into the window sum.
    assert row["tokens_window"]["total"] == 100
    assert row["window_covered"] is True and row["wall_scan_covered"] is True


def test_wall_detection_is_independent_of_the_window_value(monkeypatch, tmp_path):
    """Same transcript, three --window values: the wall verdict must not move."""
    claude = _fake_env(monkeypatch, tmp_path)
    _write_session(claude, "sess-w", _walled_45m_ago_transcript())
    for minutes in (1, 30, 120):
        row = cus.collect_pane_row({"pane": "%9", "session": "d", "state": "working"},
                                   _reg("sess-w"), STATE, CONFIG,
                                   NOW - timedelta(minutes=minutes), NOW)
        assert row["wall_active"] is True, f"--window {minutes}m lost the wall"


def test_me_warns_off_a_pane_walled_before_the_window(monkeypatch, tmp_path):
    claude = _fake_env(monkeypatch, tmp_path)
    _write_session(claude, "sess-me", _walled_45m_ago_transcript())
    row = cus.collect_pane_row({"pane": "%9", "session": "d", "state": "working"},
                               _reg("sess-me"), STATE, CONFIG, WINDOW_START, NOW)
    text = cus.render_me(_payload([row]), row)
    assert "do NOT fan out here" in text and "transcript_429" in text


def test_limit_menu_state_attests_a_wall_without_any_429(monkeypatch, tmp_path):
    """The Opus seat's live case (%19, 2026-09-18): the reader said limit_menu
    and the table said "(429 3h ago)". The pane state alone must be enough."""
    claude = _fake_env(monkeypatch, tmp_path)
    _write_session(claude, "sess-menu",
                   [usage_line(NOW - timedelta(hours=7), "claude-opus-5", "m", inp=5, out=5)])
    row = cus.collect_pane_row({"pane": "%9", "session": "d", "state": "limit_menu"},
                               _reg("sess-menu"), STATE, CONFIG, WINDOW_START, NOW)
    assert row["wall"] is None
    assert row["wall_active"] is True and row["wall_evidence"] == ["pane_limit_menu"]
    assert cus._wall_text(row, NOW).startswith("WALL")


def test_exhausted_account_attests_a_wall_even_with_a_stale_reading(monkeypatch, tmp_path):
    """5h at 100% with the reset still ahead is a wall however old the reading:
    usage inside a window only goes up. A reading from BEFORE the reset is not."""
    claude = _fake_env(monkeypatch, tmp_path)
    _write_session(claude, "sess-acct",
                   [usage_line(NOW - timedelta(minutes=3), "claude-opus-5", "m", inp=5, out=5)])
    walled = {"slots": STATE["slots"], "accounts": {"rayi5": {
        "current_5h_pct": 100.0, "current_7d_pct": 40.0,
        "five_hour_resets_at": iso(NOW + timedelta(hours=2)),
        "last_observed_ts": iso(NOW - timedelta(hours=3))}}}          # stale on purpose
    row = cus.collect_pane_row({"pane": "%9", "session": "d", "state": "idle"},
                               _reg("sess-acct"), walled, CONFIG, WINDOW_START, NOW)
    assert row["wall_active"] is True
    assert row["wall_evidence"] == ["account_5h_exhausted"]
    assert "↻2h00m" in cus._wall_text(row, NOW)

    already_reset = {"slots": STATE["slots"], "accounts": {"rayi5": {
        "current_5h_pct": 100.0, "current_7d_pct": 40.0,
        "five_hour_resets_at": iso(NOW - timedelta(minutes=1))}}}
    row = cus.collect_pane_row({"pane": "%9", "session": "d", "state": "idle"},
                               _reg("sess-acct"), already_reset, CONFIG, WINDOW_START, NOW)
    assert row["wall_active"] is False


def test_unreadable_pane_at_the_limit_menu_is_still_walled(monkeypatch, tmp_path):
    """No transcript must not mean no wall verdict."""
    _fake_env(monkeypatch, tmp_path)
    row = cus.collect_pane_row({"pane": "%99", "session": "orphan", "state": "limit_menu"},
                               {}, STATE, CONFIG, WINDOW_START, NOW)
    assert row["transcript_status"] == "no-session-registered"
    assert row["wall_active"] is True


def test_429_from_before_the_slot_changed_account_is_not_a_current_wall(monkeypatch, tmp_path):
    """Live on 2026-09-18: %2 rendered WALL against an account at 45% because
    its slot had been moved off the exhausted account after the 429. Positive
    swap evidence discounts the transcript signal — and ONLY that signal."""
    claude = _fake_env(monkeypatch, tmp_path)
    _write_session(claude, "sess-moved",
                   [wall_line(NOW - timedelta(minutes=40), NOW + timedelta(minutes=20))])
    moved = dict(STATE, swap_history=[
        {"slot": "slot-7", "from": "rayi9", "to": "rayi5", "trigger": "reactive-429",
         "ts": iso(NOW - timedelta(minutes=35))}])
    row = cus.collect_pane_row({"pane": "%9", "session": "d", "state": "working"},
                               _reg("sess-moved"), moved, CONFIG, WINDOW_START, NOW)
    assert row["wall_active"] is False
    assert row["wall_429_discounted"]
    assert "ago)" in cus._wall_text(row, NOW)
    # The other signals are untouched by the discount: still at the menu => walled.
    row = cus.collect_pane_row({"pane": "%9", "session": "d", "state": "limit_menu"},
                               _reg("sess-moved"), moved, CONFIG, WINDOW_START, NOW)
    assert row["wall_active"] is True and row["wall_evidence"] == ["pane_limit_menu"]
    # A move of a DIFFERENT slot, or one BEFORE the 429, discounts nothing.
    for entry in ({"slot": "slot-8", "from": "a", "to": "b", "ts": iso(NOW - timedelta(minutes=35))},
                  {"slot": "slot-7", "from": "a", "to": "b", "ts": iso(NOW - timedelta(minutes=50))}):
        row = cus.collect_pane_row({"pane": "%9", "session": "d", "state": "working"},
                                   _reg("sess-moved"), dict(STATE, swap_history=[entry]),
                                   CONFIG, WINDOW_START, NOW)
        assert row["wall_active"] is True


def test_token_window_coverage_is_not_relabelled_by_the_deeper_wall_scan(monkeypatch, tmp_path):
    """Hitting the byte cap short of the 5h wall lookback must not turn a fully
    covered 30m token window into a "floor"."""
    claude = _fake_env(monkeypatch, tmp_path)
    monkeypatch.setattr(cus, "PANES_TAIL_MAX_BYTES", 64 * 1024)
    pad = "y" * 900
    old = [json.dumps({"type": "user", "timestamp": iso(NOW - timedelta(hours=2) + timedelta(seconds=i)),
                       "uuid": f"o{i}", "message": {"role": "user", "content": pad}})
           for i in range(400)]                                        # ~400KB, 2h old
    recent = [usage_line(NOW - timedelta(minutes=40), "m", "pre", inp=7, out=0),
              usage_line(NOW - timedelta(minutes=5), "m", "in", inp=50, out=0)]
    t = _write_session(claude, "sess-cap", old + recent)
    lines, scan_covered = cus.tail_lines(t, NOW - timedelta(hours=5), max_bytes=64 * 1024)
    assert scan_covered is False                                       # cap binds on the scan
    oldest = cus._panes_oldest_ts(lines)
    assert oldest is not None and oldest <= WINDOW_START               # ...but the window is covered


# --------------------------------------------------------------------------
# Finding 2: peer-registry identity (dead pid, RECYCLED pid)
# --------------------------------------------------------------------------

def _registry_dir(monkeypatch, tmp_path):
    claude = tmp_path / "dot-claude"
    (claude / cus.SESSIONS_SUBDIR).mkdir(parents=True)
    monkeypatch.setattr(cus, "CLAUDE_DIR", claude)
    return claude / cus.SESSIONS_SUBDIR


def _entry(d, name, mtime=None, **fields):
    p = d / name
    p.write_text(json.dumps(fields))
    if mtime is not None:
        import os as _os
        _os.utime(p, (mtime, mtime))
    return p


def _dead_pid():
    import subprocess as _sp
    proc = _sp.Popen([sys.executable, "-c", "pass"])
    proc.wait()
    return proc.pid


def test_registry_drops_a_recycled_pid(monkeypatch, tmp_path):
    """THE regression. /proc/<pid> existing proves only that SOME process holds
    the number. Here the pid is genuinely alive (it is this test process) and
    even looks like claude — but the entry's procStart is another process's, so
    it must be dropped, not bound to the pane."""
    import os as _os
    d = _registry_dir(monkeypatch, tmp_path)
    monkeypatch.setattr(cus, "_proc_cmdline_is_claude", lambda pid: True)
    me = _os.getpid()
    real_start = cus._proc_start_ticks(me)
    assert real_start and real_start.isdigit()            # real /proc, not a mock
    _entry(d, "stale.json", pid=me, sessionId="dead-session", cwd="/x",
           tmux="s:@1.%7", procStart=str(int(real_start) - 12345))
    assert cus.read_peer_registry() == {}

    # Same pid, the RIGHT procStart: accepted. Proves the drop above was the
    # identity check and not the fixture being rejected for another reason.
    _entry(d, "live.json", pid=me, sessionId="live-session", cwd="/x",
           tmux="s:@1.%7", procStart=real_start)
    reg = cus.read_peer_registry()
    assert reg["%7"]["session_id"] == "live-session" and reg["%7"]["pid"] == me


def test_recycled_pid_cannot_attribute_another_sessions_spend(monkeypatch, tmp_path):
    """End to end: the dead session's transcript holds real tokens. With the
    stale entry dropped the pane reports UNKNOWN usage — never those tokens
    against the live pane's account."""
    import os as _os
    claude = _fake_env(monkeypatch, tmp_path)
    (claude / cus.SESSIONS_SUBDIR).mkdir(parents=True)
    monkeypatch.setattr(cus, "_proc_cmdline_is_claude", lambda pid: True)
    _write_session(claude, "dead-session",
                   [usage_line(NOW - timedelta(minutes=5), "claude-opus-5", "big", inp=900000, out=0)])
    me = _os.getpid()
    _entry(claude / cus.SESSIONS_SUBDIR, "stale.json", pid=me, sessionId="dead-session",
           cwd="/home/rayi/repos/demo", tmux="s:@1.%9",
           procStart=str(int(cus._proc_start_ticks(me)) + 999))
    row = cus.collect_pane_row({"pane": "%9", "session": "d", "state": "working"},
                               cus.read_peer_registry(), STATE, CONFIG, WINDOW_START, NOW)
    assert row["transcript_status"] == "no-session-registered"
    assert row["session_id"] is None and row["tokens_window"]["total"] is None
    cus.attribute_account_shares([row])
    assert row["attribution"]["pane_pct_of_account_window"] is None


def test_registry_drops_a_dead_pid(monkeypatch, tmp_path):
    d = _registry_dir(monkeypatch, tmp_path)
    monkeypatch.setattr(cus, "_proc_cmdline_is_claude", lambda pid: True)
    _entry(d, "dead.json", pid=_dead_pid(), sessionId="s", cwd="/x",
           tmux="s:@1.%7", procStart="1")
    assert cus.read_peer_registry() == {}


def test_registry_drops_a_live_pid_that_is_not_claude(monkeypatch, tmp_path):
    """procStart absent (older Claude Code) => the cmdline check carries the
    identity alone, and this pytest process is not claude. Runs the REAL check."""
    import os as _os
    d = _registry_dir(monkeypatch, tmp_path)
    _entry(d, "notclaude.json", pid=_os.getpid(), sessionId="s", cwd="/x", tmux="s:@1.%7")
    assert cus._proc_cmdline_is_claude(_os.getpid()) is False
    assert cus.read_peer_registry() == {}


def test_registry_newest_entry_wins_for_a_relaunched_pane(monkeypatch, tmp_path):
    import os as _os
    d = _registry_dir(monkeypatch, tmp_path)
    monkeypatch.setattr(cus, "_proc_cmdline_is_claude", lambda pid: True)
    me, start = _os.getpid(), cus._proc_start_ticks(_os.getpid())
    _entry(d, "a.json", mtime=1_000_000, pid=me, sessionId="older", cwd="/x",
           tmux="s:@1.%7", procStart=start)
    _entry(d, "b.json", mtime=2_000_000, pid=me, sessionId="newer", cwd="/x",
           tmux="s:@1.%7", procStart=start)
    assert cus.read_peer_registry()["%7"]["session_id"] == "newer"


def test_registry_skips_malformed_entries(monkeypatch, tmp_path):
    import os as _os
    d = _registry_dir(monkeypatch, tmp_path)
    monkeypatch.setattr(cus, "_proc_cmdline_is_claude", lambda pid: True)
    me, start = _os.getpid(), cus._proc_start_ticks(_os.getpid())
    (d / "garbage.json").write_text("{not json")
    _entry(d, "no-tmux.json", pid=me, sessionId="s", cwd="/x", procStart=start)
    _entry(d, "no-pane.json", pid=me, sessionId="s", cwd="/x", tmux="s:@1", procStart=start)
    _entry(d, "bad-pid.json", pid="abc", sessionId="s", cwd="/x", tmux="s:@1.%7", procStart=start)
    (d / "secret.key").write_text("never-read")             # not *.json: never opened
    assert cus.read_peer_registry() == {}


# --------------------------------------------------------------------------
# Finding 3: unmeasured is unknown, not 0%
# --------------------------------------------------------------------------

def _unmeasured_row(pane, account, status="missing"):
    r = _full_row(pane, "slot-x", account, total=0)
    r.update({"transcript_status": status, "unmeasured": True, "subagents_live": None,
              "burn_tokens_per_min": None, "burn_new_tokens_per_min": None,
              "tokens_window": {"total": None, "new": None, "by_model": {},
                                "subagent_total": None}})
    return r


def test_unmeasured_pane_has_null_share_and_is_excluded_from_the_denominator():
    rows = [_full_row("%1", "slot-1", "rayi3", total=600),
            _full_row("%2", "slot-2", "rayi3", total=400),
            _unmeasured_row("%3", "rayi3")]
    cus.attribute_account_shares(rows)
    a1, a2, a3 = (r["attribution"] for r in rows)
    assert a3["pane_pct_of_account_window"] is None and a3["unmeasured"] is True
    assert (a1["pane_pct_of_account_window"], a2["pane_pct_of_account_window"]) == (60.0, 40.0)
    assert a1["account_window_tokens"] == 1000
    # every row on the account says the picture is incomplete
    assert a1["unmeasured_panes_on_account"] == a3["unmeasured_panes_on_account"] == 1


def test_legacy_zero_total_row_with_a_bad_transcript_is_still_unmeasured():
    """Belt: a row that arrives with total=0 but transcript_status != ok (the
    pre-fix shape) must not sneak back in as a measured 0%."""
    rows = [_full_row("%1", "slot-1", "rayi3", total=500),
            {"pane": "%2", "account": "rayi3", "transcript_status": "missing",
             "tokens_window": {"total": 0}}]
    cus.attribute_account_shares(rows)
    assert rows[1]["attribution"]["pane_pct_of_account_window"] is None
    assert rows[0]["attribution"]["unmeasured_panes_on_account"] == 1


def test_table_renders_unmeasured_as_dash_never_zero_percent():
    rows = [_full_row("%1", "slot-1", "rayi3", total=1000), _unmeasured_row("%3", "rayi3")]
    p = _payload(rows, {"rayi3": dict(cus.account_headroom(None, NOW), unmeasured_panes=1)})
    text = cus.render_panes_table(p)
    line = next(ln for ln in text.splitlines() if ln.startswith("%3"))
    assert "0%" not in line and " 0 " not in line
    assert "usage unknown: missing" in line
    assert "1 pane(s) on rayi3 UNMEASURED" in text


def test_me_says_tokens_unknown_not_zero_for_an_unmeasured_pane():
    row = _unmeasured_row("%3", "rayi3")
    text = cus.render_me(_payload([row]), row)
    assert "tokens UNKNOWN (not zero)" in text
    assert "0 tokens" not in text


def test_capped_pane_marks_every_share_on_its_account_approximate():
    capped = _full_row("%1", "slot-1", "rayi3", total=700)
    capped["window_covered"] = False
    rows = [capped, _full_row("%2", "slot-2", "rayi3", total=300),
            _full_row("%4", "slot-4", "rayi2", total=50)]
    text = cus.render_panes_table(_payload(rows))
    assert rows[1]["attribution"]["approximate"] is True
    assert rows[2]["attribution"]["approximate"] is False     # other account untouched
    assert "70%~" in text and "30%~" in text and "100%~" not in text
