"""Tests for the 429-detection hooks (A2, 2026-06-23).

Pins the two halves of the post-incident detection design:

  StopFailure (cus_stop_failure.sh) — the PRIMARY, reliable signal. A model-level
  Anthropic 429 fires StopFailure with a categorized `error` enum. We log only
  the budget-relevant categories (rate_limit, overloaded) and match the enum
  EXACTLY, so prose elsewhere in the event can't false-trigger.

  PostToolUseFailure (cus_post_tool_use_failure.sh) — hardened secondary. The
  original loose substring match ("rate limit" anywhere) caused the 2026-06-23
  false-swap incident. Now it matches only the precise API wire tokens and
  ignores matches confined to tool_input.

These shell out to the real hook scripts so the bash/grep/python logic is what's
under test, not a reimplementation.
"""

import json
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
HOOKS = REPO / "hooks"
STOP_FAILURE = HOOKS / "cus_stop_failure.sh"
PTUF = HOOKS / "cus_post_tool_use_failure.sh"


def _run(hook: Path, event: dict, accounts_dir: Path, env_overrides: dict | None = None) -> list[str]:
    """Pipe `event` JSON into `hook` and return the resulting 429.log lines."""
    env = {"CUS_ACCOUNTS_DIR": str(accounts_dir), "PATH": "/usr/bin:/bin:/usr/local/bin"}
    env.update(env_overrides or {})
    subprocess.run(
        ["bash", str(hook)],
        input=json.dumps(event),
        text=True,
        env=env,
        check=True,
    )
    log = accounts_dir / "429.log"
    return log.read_text().splitlines() if log.exists() else []


# --------------------------------------------------------------------------
# StopFailure — primary detector
# --------------------------------------------------------------------------

def test_stopfailure_logs_rate_limit(tmp_path):
    lines = _run(STOP_FAILURE, {
        "hook_event_name": "StopFailure", "session_id": "S1",
        "error": "rate_limit", "error_details": "429 Too Many Requests",
        "last_assistant_message": "API Error: Rate limit reached",
    }, tmp_path)
    assert len(lines) == 1
    assert lines[0].split(",")[1:3] == ["S1", "rate_limit"]
    assert lines[0].split(",")[3] == "stopfailure"


def test_stopfailure_logs_overloaded(tmp_path):
    lines = _run(STOP_FAILURE, {"hook_event_name": "StopFailure",
                                "session_id": "S2", "error": "overloaded"}, tmp_path)
    assert len(lines) == 1 and lines[0].split(",")[2] == "overloaded"


def test_stopfailure_logs_event_time_slot_and_account(tmp_path):
    (tmp_path / "state.json").write_text(json.dumps({
        "active": "bare-account",
        "slots": {"slot-2": {"account": "event-account"}},
    }))
    lines = _run(STOP_FAILURE, {
        "hook_event_name": "StopFailure", "session_id": "S-bound", "error": "rate_limit",
    }, tmp_path, {"CLAUDE_CONFIG_DIR": str(tmp_path / "slot-2")})
    assert lines[0].split(",")[4:6] == ["slot-2", "event-account"]


def test_stopfailure_ignores_other_error_types_even_with_rate_limit_prose(tmp_path):
    """authentication_failed is not budget-relevant — and prose mentioning 'rate
    limit' in error_details must not trip the exact-enum match."""
    lines = _run(STOP_FAILURE, {
        "hook_event_name": "StopFailure", "session_id": "S3",
        "error": "authentication_failed",
        "error_details": "unrelated rate limit prose in the details field",
    }, tmp_path)
    assert lines == []


# --------------------------------------------------------------------------
# PostToolUseFailure — hardened secondary
# --------------------------------------------------------------------------

def test_ptuf_ignores_downstream_prose(tmp_path):
    """The exact 2026-06-23 false positive: a Bash failure whose output mentions
    'rate limit' must NOT be logged (it's a downstream/tool limit, not ours)."""
    lines = _run(PTUF, {
        "hook_event_name": "PostToolUseFailure", "session_id": "S4",
        "tool_name": "Bash", "tool_input": {"command": "curl ..."},
        "error": "Command failed: server said rate limit exceeded, slow down",
    }, tmp_path)
    assert lines == []


def test_ptuf_ignores_token_only_in_tool_input(tmp_path):
    """Reading/editing a file that contains `rate_limit_error` (e.g. API-client
    code) must NOT trip — the token is in tool_input, not an actual API error."""
    lines = _run(PTUF, {
        "hook_event_name": "PostToolUseFailure", "session_id": "S5",
        "tool_name": "Read",
        "tool_input": {"file": "client.py with rate_limit_error handling"},
        "error": "File not found",
    }, tmp_path)
    assert lines == []


def test_ptuf_logs_real_api_token_in_error_with_toolname(tmp_path):
    """A genuine Anthropic error body leaking into a subagent's failure IS logged,
    tagged with the failing tool_name for forensics."""
    lines = _run(PTUF, {
        "hook_event_name": "PostToolUseFailure", "session_id": "S6",
        "tool_name": "Agent", "tool_input": {"prompt": "do x"},
        "error": 'subagent died: {"type":"error","error":{"type":"rate_limit_error"}}',
    }, tmp_path)
    assert len(lines) == 1
    parts = lines[0].split(",")
    assert parts[1] == "S6" and parts[2] == "rate_limit_error" and parts[3] == "Agent"


def test_ptuf_logs_event_time_slot_and_account(tmp_path):
    (tmp_path / "state.json").write_text(json.dumps({
        "active": "bare-account",
        "slots": {"slot-4": {"account": "bound-account"}},
    }))
    lines = _run(PTUF, {
        "hook_event_name": "PostToolUseFailure", "session_id": "S7",
        "tool_name": "Agent", "tool_input": {},
        "error": '{"type":"rate_limit_error"}',
    }, tmp_path, {"CLAUDE_CONFIG_DIR": str(tmp_path / "slot-4")})
    assert lines[0].split(",")[4:6] == ["slot-4", "bound-account"]


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
