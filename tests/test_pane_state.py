"""pane_state.py's classifier, pinned on pane text captured from this machine on
2026-09-04 (60 samples of live panes; identifiers trimmed) plus the negative cases the
two blind reviews of PR #194 constructed. Pure function under test — liveness is an
injected boolean; /proc and the state dir are exercised through monkeypatched readers.
"""
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "skills"))
import pane_state as ps  # noqa: E402
from pane_state import bg_counts, classify  # noqa: E402

RULE = "─" * 40
FOOTER_IDLE = [RULE, "  ⚠ cus: Independent login(s) past assumed refresh-token lifetime: slot-5->default",
               "  cus rayi1* 5h:22% 7d:32%·4h45m nxt:90% | 03 5h:? 7d:? nxt:90%",
               "  ⏵⏵ bypass permissions on (shift+tab to cycle) · ← for agents"]
FOOTER_AGENT = [RULE, "  ⏵⏵ bypass permissions on (shift+tab to cycle) · ← 1 agent"]
INPUT_EMPTY = [RULE, "❯ "]


def state(lines, alive=True):
    return classify(lines, alive)[0]


# ---- live activity, in the shapes the TUI actually renders today --------------------
@pytest.mark.parametrize("row", [
    "· Zigzagging… (12s · ↓ 1.2k tokens)",
    "* Unfurling… (3m 26s · ↓ 16.1k tokens)",
    "✽ Transmuting… (2m 10s · ↓ 8.5k tokens · still thinking with xhigh effort)",
    "✢ Prestidigitating… (4s · ↓ 300 tokens)",
    "✻ Metamorphosing… (1m 2s · almost done thinking with xhigh effort)",
    "✶ Unfurling… (3m 26s · ↓ 16.1k tokens)",
    "✻ Baking… (esc to interrupt)",
    "⠹ Running… (1s)",
])
def test_live_rows_are_working(row):
    assert state(["  ⏺ Bash(pytest -q)", row, *INPUT_EMPTY, *FOOTER_IDLE]) == "working"


def test_waiting_for_background_agent_is_working():
    lines = ["  ✻ Waiting for 1 background agent to finish", *INPUT_EMPTY, *FOOTER_IDLE]
    assert state(lines) == "working"
    assert bg_counts(lines)[1] == 1


def test_live_subagent_row_is_working():
    lines = ["  ◯ general-purpose  Sampling spinner glyphs on pane_state.py   21m 55s · ↓ 8.5k tokens",
             *INPUT_EMPTY, *FOOTER_AGENT]
    assert state(lines) == "working"
    assert bg_counts(lines) == (1, 0, 1)


def test_running_tool_row_is_working():
    assert state(["  ⏺ Bash(pytest -q)", "  ⎿ Running…", *INPUT_EMPTY, *FOOTER_IDLE]) == "working"


# ---- finished rows and prose are NOT activity --------------------------------------
@pytest.mark.parametrize("row", [
    "✻ Baked for 2m 7s · done 1:56 PM",
    "✻ Cooked for 1m 15s",
    "  ⏺ Bash(git status)",
    "● The live row carries '(esc to interrupt)' only while a turn runs.",
    "● I'll stop and wait for your go-ahead before merging.",
    "● rayi6 was not logged in, so the daemon rotated the mount.",
    "● It printed 'Resume this session with: claude --resume abcd' and exited.",
    "  grep /rate-limit-options watch.md",
])
def test_finished_and_prose_rows_are_idle(row):
    assert state([row, *INPUT_EMPTY, *FOOTER_IDLE]) == "idle"


def test_agent_footer_hint_vs_count():
    assert bg_counts(["❯ ", *FOOTER_IDLE])[0] == 0          # "← for agents" = none running
    assert bg_counts(["❯ ", *FOOTER_AGENT])[0] == 1
    assert state(["❯ ", *FOOTER_AGENT]) == "idle"            # a count alone is not busy-ness


# ---- boxes and menus, structural, in the active block ------------------------------
def test_trust_folder_box_is_approval():
    lines = [" Claude Code'll be able to read, edit, and execute files here.", " Security guide",
             " ❯ 1. Yes, I trust this folder", "   2. No, exit", " Enter to confirm · Esc to cancel"]
    assert state(lines) == "approval"


def test_permission_box_is_approval():
    lines = ["  Bash command", "  pip install requests", "  Do you want to proceed?", "  ❯ 1. Yes",
             "    2. Yes, and don't ask again for pip commands", "    3. No", "  Esc to cancel"]
    assert state(lines) == "approval"


def test_answered_box_in_scrollback_is_not_approval():
    # the box was answered; the tool ran and finished (a ⏺ row closes the active block)
    lines = ["  Do you want to proceed?", "  ❯ 1. Yes", "    2. No", "  Esc to cancel",
             "  ⏺ Bash(pip install requests)", "  ⎿ ok", *INPUT_EMPTY, *FOOTER_IDLE]
    assert state(lines) == "idle"


def test_prose_stop_and_wait_above_a_real_box_is_still_approval():
    lines = ["● I'll stop and wait for the reviewer.", "  Do you want to proceed?",
             "  ❯ 1. Yes", "    2. No", "  Esc to cancel"]
    assert state(lines) == "approval"


def test_rate_limit_menu():
    lines = ["  /rate-limit-options", "  ❯ 1. Stop and wait", "    2. Upgrade your plan", "❯ "]
    assert state(lines) == "limit_menu"


def test_soft_limit_block_in_active_block():
    lines = ["  ⎿ You've reached your Fable 5 limit. Run /usage-credits to continue or switch",
             "    models with /model", *INPUT_EMPTY, *FOOTER_IDLE]
    assert state(lines) == "limit_menu"


def test_old_limit_block_behind_a_finished_turn_is_idle():
    lines = ["  ⎿ You've reached your Fable 5 limit. Run /usage-credits to continue",
             "● Resumed on the fresh account; carrying on.", "✻ Brewed for 41s · done 2:42 PM",
             *INPUT_EMPTY, *FOOTER_IDLE]
    assert state(lines) == "idle"


def test_live_spinner_outranks_stale_limit_block():
    lines = ["  ⎿ You've reached your Fable 5 limit. Run /usage-credits to continue",
             "· Zigzagging… (12s · ↓ 1.2k tokens)", *INPUT_EMPTY, *FOOTER_IDLE]
    assert state(lines) == "working"


def test_login_menu_rendered_by_harness():
    assert state(["  Login expired · Please run /login", *INPUT_EMPTY, *FOOTER_IDLE]) == "login_menu"
    assert state(["  ⎿ Not logged in · Please run /login", *INPUT_EMPTY, *FOOTER_IDLE]) == "login_menu"


def test_gh_auth_prose_is_not_a_login_menu():
    lines = ["  ⎿ You are not logged into any GitHub hosts. To log in, run: gh auth login",
             "● gh says we're not logged in; using the token instead.", *INPUT_EMPTY, *FOOTER_IDLE]
    assert state(lines) == "idle"


# ---- exited / dead / no_claude / shell --------------------------------------------
def test_exited_only_while_alive():
    banner = ["  Resume this session with: claude --resume abcd", "rayi in 🌐 host in ~", "❯"]
    assert state(banner, alive=True) == "exited"
    assert state(banner, alive=False) == "dead"


def test_shell_prompt_with_claude_gone_is_dead():
    assert state(["rayi in 🌐 claude-sandbox in ~", "❯"], alive=False) == "dead"
    assert state(["❯ cd repos", "rayi in 🌐 claude-sandbox in repos on  main [!] via 🐍 v3.12.3", "❯"],
                 alive=False) == "dead"


def test_shell_prompt_under_a_live_node_is_unknown_not_idle():
    # a dev server keeps a node child alive; the pane is a shell — never nudge it
    assert state(["rayi in 🌐 claude-sandbox in ~", "❯"], alive=True) == "unknown"
    assert state(["rayi in 🌐 claude-sandbox in repos", "❯ cd repos"], alive=True) == "unknown"


def test_tui_on_screen_but_no_process_is_no_claude():
    assert state(["  ⏺ Read(x)", *INPUT_EMPTY, *FOOTER_IDLE], alive=False) == "no_claude"


def test_dead_beats_activity_text():
    assert state(["· Zigzagging… (12s · ↓ 1.2k tokens)", "❯ "], alive=False) == "dead"


# ---- drafts ------------------------------------------------------------------------
def test_unsigned_draft_is_a_human_at_the_keyboard():
    lines = ["  ⏺ Bash(git status)", "  ⎿ ok", RULE, "❯ K this was a waste of time, let's",
             *FOOTER_IDLE]
    st, draft, tui, signed = classify(lines, True)
    assert (st, draft, tui, signed) == ("idle_with_draft", "K this was a waste of time, let's", True, False)


def test_signed_draft_is_the_watchers_own_nudge():
    lines = [RULE, "❯ [automated build-babysitter — not Rayi] retry the last step", *FOOTER_IDLE]
    st, draft, _tui, signed = classify(lines, True)
    assert st == "idle_with_draft" and signed is True


def test_draft_that_looks_like_a_menu_row_is_a_draft():
    lines = [RULE, "❯ 1. do the first thing then report", *FOOTER_IDLE]
    assert state(lines) == "idle_with_draft"


def test_menu_row_is_not_the_input_line():
    lines = ["  Do you want to proceed?", "  ❯ 1. Yes", "    2. No"]
    st, draft, _t, _s = classify(lines, True)
    assert st == "approval" and draft == ""


def test_plain_idle_and_unsent_draft_from_real_capture():
    lines = ["  ⏺ Bash(git status)", "  ⎿ ok", RULE, "❯ on 1 - give me the list agai", *FOOTER_IDLE,
             "                          new task? /clear to save 12.3k tokens"]
    assert classify(lines, True)[:2] == ("idle_with_draft", "on 1 - give me the list agai")
    assert state(["  ⏺ Read(file.py)", "  ⎿ Read 40 lines", *INPUT_EMPTY, *FOOTER_IDLE]) == "idle"


# ---- liveness through /proc, robustness ---------------------------------------------
def test_is_claude_requires_the_claude_cli(monkeypatch):
    fake = {
        "/proc/1/comm": "node\n", "/proc/1/cmdline": "node\x00/srv/app/server.js\x00",
        "/proc/2/comm": "node\n", "/proc/2/cmdline": "node\x00/home/x/.npm/claude/cli.js\x00",
        "/proc/3/comm": "claude\n",
        "/proc/9/task/9/children": "1 2\n",
    }
    monkeypatch.setattr(ps, "_read", lambda path: fake.get(path, ""))
    assert ps._is_claude(1) is False       # a bare node: dev server
    assert ps._is_claude(2) is True        # node running the claude cli
    assert ps._is_claude(3) is True
    assert ps.claude_alive_under(9, "bash") is True
    monkeypatch.setattr(ps, "_read", lambda path: {"/proc/9/task/9/children": "1\n",
                                                     "/proc/1/comm": "node\n",
                                                     "/proc/1/cmdline": "node\x00server.js\x00"}.get(path, ""))
    assert ps.claude_alive_under(9, "bash") is False


def test_unchanged_for_degrades_when_state_dir_unusable(tmp_path, monkeypatch):
    monkeypatch.setattr(ps, "STATE_DIR", str(tmp_path / "a-file-not-a-dir"))
    (tmp_path / "a-file-not-a-dir").write_text("x")
    assert ps._unchanged_for("%1", "fp", 100.0) == 0
    monkeypatch.setattr(ps, "STATE_DIR", str(tmp_path / "ok"))
    assert ps._unchanged_for("%1", "fp", 100.0) == 0
    assert ps._unchanged_for("%1", "fp", 160.0) == 60
    assert ps._unchanged_for("%1", "fp2", 200.0) == 0


def test_tmux_failure_is_a_tmux_error_row(monkeypatch):
    def boom(cmd):
        raise ps.TmuxError("no server running")
    monkeypatch.setattr(ps, "_run", boom)
    s = ps.read_pane({"pane": "%1", "pane_pid": 1, "current_command": "bash", "session": "x"}, now=1.0)
    assert s.state == "tmux_error" and s.claude_alive is False


def test_all_and_targets_are_mutually_exclusive(capsys):
    with pytest.raises(SystemExit):
        ps.main(["--all", "%12"])


def test_help_does_not_crash(capsys):
    with pytest.raises(SystemExit) as e:
        ps.main(["--help"])
    assert e.value.code == 0
    assert "session names" in capsys.readouterr().out
