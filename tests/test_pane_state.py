"""pane_state.py's classifier, pinned on captured pane text (2026-09-04).

The captures are real bottoms from this machine's panes on the day the helper was
written (identifiers trimmed), plus synthetic ones for the menus. Pure function under
test: no tmux, no /proc — liveness is an injected boolean.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "skills"))
from pane_state import bg_agents, classify  # noqa: E402

FOOTER = [
    "────────────────────────────────────",
    "  ⚠ cus: Independent login(s) past assumed refresh-token lifetime: slot-5->default",
    "  cus rayi1* 5h:↻reset(was 22%) 7d:32%·4h45m nxt:90% | 03 5h:? 7d:? nxt:90%",
]


def test_idle_with_unsent_draft():
    lines = ["  ⏺ Bash(git status)", "  ⎿ ok", "────", "❯ on 1 - give me the list agai",
             *FOOTER, "  ⏵⏵ bypass permissions on (shift+tab to cycle) · 1 feedback draft",
             "                          new task? /clear to"]
    assert classify(lines, True) == ("idle_with_draft", "on 1 - give me the list agai")


def test_agent_footer_is_reported_not_treated_as_busy():
    lines = ["────", "❯ ", *FOOTER, "  ⏵⏵ bypass permissions on (shift+tab to cycle) · ← 1 agent"]
    assert classify(lines, True) == ("idle", "")
    assert bg_agents(lines) == 1
    assert bg_agents(["❯ ", "  ⏵⏵ bypass permissions on (shift+tab to cycle)"]) == 0


def test_finished_tool_row_is_not_activity():
    lines = ["  ⏺ Bash(git status)", "  ⎿ On branch main", "────", "❯ ", *FOOTER]
    assert classify(lines, True)[0] == "idle"


def test_running_tool_row_is_working():
    lines = ["  ⏺ Bash(pytest -q)", "  ⎿ Running… (esc to interrupt)", "────", "❯ ", *FOOTER]
    assert classify(lines, True)[0] == "working"


def test_spinner_row_is_working():
    lines = ["  ✻ Thinking… (esc to interrupt)", "────", "❯ ", *FOOTER]
    assert classify(lines, True)[0] == "working"


def test_plain_idle():
    lines = ["  ⏺ Read(file.py)", "  ⎿ Read 40 lines", "────", "❯ ", *FOOTER,
             "  ⏵⏵ bypass permissions on (shift+tab to cycle)"]
    assert classify(lines, True) == ("idle", "")


def test_trust_folder_box_is_approval():
    lines = [" Claude Code'll be able to read, edit, and execute files here.", " Security guide",
             " ❯ 1. Yes, I trust this folder", "   2. No, exit", " Enter to confirm · Esc to cancel"]
    assert classify(lines, True)[0] == "approval"


def test_permission_box_is_approval():
    lines = ["  Bash command", "  pip install requests", "  Do you want to proceed?", "  ❯ 1. Yes",
             "    2. Yes, and don't ask again for pip commands", "    3. No", "  Esc to cancel"]
    assert classify(lines, True)[0] == "approval"


def test_soft_limit_block_above_prompt_is_limit_menu():
    lines = ["  ⎿ You've reached your Fable 5 limit. Run /usage-credits to continue or switch",
             "    models with /model", "────", "❯ ", *FOOTER]
    assert classify(lines, True)[0] == "limit_menu"


def test_rate_limit_menu():
    lines = ["  /rate-limit-options", "  ❯ 1. Stop and wait", "    2. Upgrade your plan", "❯ "]
    assert classify(lines, True)[0] == "limit_menu"


def test_login_menu():
    lines = ["  Login expired · Please run /login", "────", "❯ ", *FOOTER]
    assert classify(lines, True)[0] == "login_menu"


def test_exited_banner():
    lines = ["  Resume this session with: claude --resume 4f47a30f-a7f7-43e4", "rayi in ~ ❯"]
    assert classify(lines, True)[0] == "exited"


def test_dead_beats_everything():
    lines = ["  ✻ Thinking… (esc to interrupt)", "❯ "]
    assert classify(lines, False) == ("dead", "")


def test_shell_prompt_with_claude_gone_is_dead():
    assert classify(["rayi in 🌐 droplet in ~", "❯"], False)[0] == "dead"


def test_menu_row_is_not_the_input_line():
    # a numbered menu row starts with ❯ too; it must not be mistaken for an empty prompt
    lines = ["  Do you want to proceed?", "  ❯ 1. Yes", "    2. No"]
    state, draft = classify(lines, True)
    assert state == "approval" and draft == ""
