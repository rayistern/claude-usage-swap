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
    assert row["tokens_window"]["total"] == 0
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
        assert row["tokens_window"]["total"] == 0
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
