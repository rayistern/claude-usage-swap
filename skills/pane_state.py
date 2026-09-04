#!/usr/bin/env python3
"""pane_state.py — one JSON line per tmux pane: is a Claude Code process there, and what is
its TUI doing right now (working / idle / idle with an unsent draft / approval box / limit
menu / login menu / exited / no_claude / dead / unknown)?

Why this exists
---------------
The transcript tells a watcher WHY a session stopped (usage wall, expired login, kill —
see context-dashboard's `session_metrics --live`), but two things only the pane can say:
whether there is a PROCESS to nudge at all (a session that exited to a bare shell and one
idling at an empty prompt are identical in the transcript — watch.md, 2026-07-10 lesson),
and whether the TUI is BLOCKED ON A HUMAN (a permission box, a trust-folder box, the
`/rate-limit-options` menu, the "Please run /login" menu — none of those are transcript
events until someone answers). The `/watch` skill carried these rules as prose recipes;
this script is those rules as one command, so a babysitter tick calls it instead of
re-deriving them. Born 2026-09-04 with the build-babysitter skill (vibeCoding#330);
rewritten the same day after two blind reviews found the first version keyed on captions
this machine's TUI does not emit and matched prose about the conditions it looks for.

What it reads
-------------
  * `tmux list-panes -a` → pane id, pane pid, current command, session name.
  * /proc → is the pane's command itself `claude`, or is there a descendant that is
    `claude`, or a `node` whose cmdline contains "claude"? A bare `node` (a dev server,
    an MCP server, jest) is NOT a Claude session.
  * `tmux capture-pane -p -S -40` → the visible tail, split into three zones, bottom-up:
    FOOTER (statusline, `⏵⏵ bypass permissions on … · ← for agents` / `← N agents`,
    `new task? /clear …`), a rule, the INPUT line (`❯ …`), a rule, then CONTENT. Menus
    and boxes are judged only in the ACTIVE BLOCK — the trailing content rows after the
    last finished row (`● …`, `⏺ …`, `✻ … done`) — so an answered box, a dismissed
    menu or the old `⎿ … limit` block higher up cannot re-trigger a verdict.
  * A per-pane scratch file (~/.cache/pane_state/<pane>.json) holding the last capture's
    fingerprint, so a pane whose bottom text hasn't changed across ticks reports
    `unchanged_for_s` (the "frozen still frame" case, watch.md 2026-07-10).

States (priority order — the first that matches wins; this list IS the code order)
------
  tmux_error        tmux could not be asked (absent, or the capture failed) — not a
                    verdict about the session; never act on it
  dead              no Claude process under the pane AND no Claude TUI on screen (a
                    shell prompt, or nothing). The ONLY state in which relaunching is
                    correct.
  no_claude         no Claude process found but a Claude TUI is still on screen — a
                    process that just died, or a liveness miss. Re-read; escalate if it
                    persists; never relaunch on first sight.
  working           a LIVE activity row: a spinner glyph (✻ ✽ ✶ ✳ ✢ · * or braille) +
                    a verb in progress + the timer parenthesis ("· Zigzagging… (12s ·
                    ↓ 1.2k tokens)"), "✻ Waiting for N background agent to finish", a
                    live subagent row ("◯ general-purpose … 21m 55s"), or "⎿ Running…".
                    A finished row ("✻ Brewed for 2m 7s · done 1:56 PM", "⏺ Bash(…)")
                    is NOT activity; prose containing these words is not either.
  limit_menu        the harness's rate-limit menu (`❯ 1. Stop and wait` / `2. Upgrade
                    your plan` / `/rate-limit-options`) — a numbered box too, but the
                    one a watcher may act on, so it is named before `approval`; the
                    soft-limit block (`⎿ You've reached your … limit`) is judged later,
                    after `login_menu`. Nothing running — swap + nudge territory, no
                    Escape without a fresh re-read
  approval          any other numbered box the human must answer: a `❯ 1.` row with a
                    sibling `2.` row or an "Esc to cancel / Enter to confirm" caption,
                    or a "Do you want to proceed?" row — in the active block. NEVER
                    answer it.
  login_menu        `Login expired · Please run /login` / `Not logged in` / OAuth expired
                    rendered as a block row in the active block — cus slot move + nudge
                    fixes a live pane; only an interactive /login is human-only
  exited            "Resume this session with: claude --resume …" while a Claude process
                    is still alive — winding down. Re-read; never type a relaunch into it.
  idle_with_draft   the input line holds text nobody submitted. `draft_signed` says
                    whether it starts with "[automated" — a watcher's own nudge whose
                    Enter never registered (press Enter only then, and only after it has
                    sat unchanged ≥ 30 s); anything else is a human mid-sentence: leave it.
  idle              a Claude TUI (footer marker present) with an empty input line and
                    nothing running
  unknown           a Claude process exists but no TUI marker is on screen (a shell
                    under a live node, a redraw in progress) — do not nudge

Usage
-----
  pane_state.py 2zajac2a %12 …     # tmux session names or pane ids (the watcher's list)
  pane_state.py --all              # every pane with a live Claude process — a DEAD pane
                                   # produces NO row here; trackers must name their panes
  pane_state.py --table            # human table (JSON lines is the default)
A named target that matches NO pane at all (gone or renamed) prints {"pane": <target>,
"state": "not_found"} — for a tracked build pane that means the pane is gone, never
healthy; a pane that exists but has no process is `dead`. `--all` with targets is an
error.
Exit code 0 unless tmux is unusable (2).
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import time
from dataclasses import asdict, dataclass

#: Visible lines captured from the bottom of the pane. The soft-limit block and a tool
#: row can sit a dozen lines above the input line, under the footer — 40 is far past
#: any observed footer + input + block, cheap to read.
CAPTURE_LINES = 40
#: Only the bottom N non-blank lines are judged; scrollback above is stale by definition.
JUDGE_LINES = 18
STATE_DIR = os.path.expanduser("~/.cache/pane_state")

# ---- line shapes ----------------------------------------------------------------------
# Spinner frames observed on this machine's TUI (60 samples, 2026-09-04): ✽ ✢ · ✻ * ✶,
# plus the ✳ / braille sets older builds used. A live row is glyph + verb + timer paren.
_SPIN = r"[✻✳✶✽✢·\*⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏]"
_R = {
    "footer": re.compile(r"⏵⏵|bypass permissions on|shift\+tab to cycle|new task\? /clear"
                         r"|^\s*cus\s+\S|^\s*⚠ cus:", re.I),
    "rule": re.compile(r"^\s*─{12,}\s*$"),
    "input": re.compile(r"^\s*❯(?P<draft>.*)$"),
    "shell_prompt": re.compile(r"^\S+ in .* in [~/]"),           # "rayi in 🌐 host in ~"
    "live_row": re.compile(rf"^\s*{_SPIN}\s+[A-Z][a-z]+…\s*\((\d|esc to interrupt)", re.U),
    "live_wait": re.compile(rf"^\s*{_SPIN}\s+Waiting for \d+ background agent", re.I),
    "live_agent": re.compile(r"^\s*◯\s+\S.*\d+[hms]\b"),
    "live_tool": re.compile(r"^\s*⎿\s*Running…"),
    "finished": re.compile(rf"^\s*(●|⏺|>|{_SPIN}\s+[A-Z][a-z]+ for \d+[hms])"),
    "menu_row1": re.compile(r"^\s*❯\s*1\.\s"),
    "menu_row2": re.compile(r"^\s*2\.\s"),
    "menu_caption": re.compile(r"esc to cancel|enter to confirm", re.I),
    "menu_question": re.compile(r"^\s*Do you want to (proceed|allow|make this edit)\?", re.I),
    "limit_menu": re.compile(r"^\s*(❯\s*)?\d+\.\s*(Stop and wait|Upgrade your plan)"
                             r"|^\s*/rate-limit-options", re.I),
    "limit_block": re.compile(r"^\s*⎿\s*You'?ve (reached|hit) your .{0,40}limit", re.I),
    "login": re.compile(r"^\s*(⎿\s*)?(Login expired|Not logged in|Please run /login"
                        r"|Failed to authenticate|OAuth session expired)", re.I),
    "exited": re.compile(r"^\s*Resume this session with:\s*claude --resume"),
    "bg_agents": re.compile(r"←\s*(\d+)\s+agents?\b", re.I),
    "bg_waiting": re.compile(r"Waiting for (\d+) background agent", re.I),
}
#: A watcher's own nudge signature — the only draft it may ever press Enter on.
SIGNED_DRAFT_PREFIX = "[automated"


@dataclass
class PaneState:
    pane: str
    session: str
    pane_pid: int
    current_command: str
    claude_alive: bool
    tui: bool                  # a Claude Code footer/rule marker is on screen
    state: str
    input_draft: str           # text sitting unsent in the input box ('' if none)
    draft_signed: bool         # the draft starts with SIGNED_DRAFT_PREFIX (a watcher's nudge)
    bg_agents: int             # "← N agents" in the footer (0 for "← for agents")
    waiting_for_agents: int    # "Waiting for N background agent(s)" on screen
    live_agent_rows: int       # live "◯ <agent> … Nm Ns" rows
    bottom: list[str]          # last 3 non-blank lines, stripped, for the human reading the tick
    unchanged_for_s: int       # seconds the judged tail has been identical (0 = changed)
    ts: str


def _zones(tail: list[str]) -> tuple[list[str], str | None, list[str]]:
    """Split the judged tail into (content, input_line, footer).

    Bottom-up: footer lines (statusline / bypass / new task) and rules, then the input
    line (the last ❯ line above the footer), then content. A ❯ that is the shell prompt
    (a "user in host in dir" line right above it) is not an input line."""
    i = len(tail) - 1
    while i >= 0 and (_R["footer"].search(tail[i]) or _R["rule"].match(tail[i])):
        i -= 1
    input_line = None
    if i >= 0 and _R["input"].match(tail[i]):
        above = tail[i - 1] if i >= 1 else ""
        # The TUI draws a rule directly above its input line; a menu row ("❯ 1. Yes")
        # never has one, and the shell prompt has a "user in host in dir" line instead.
        is_input = _R["rule"].match(above) is not None or (
            not _R["menu_row1"].match(tail[i]) and not _R["shell_prompt"].match(above))
        if is_input:
            input_line = tail[i]
            i -= 1
            while i >= 0 and _R["rule"].match(tail[i]):
                i -= 1
    return tail[: i + 1], input_line, tail[i + 1:]


def _active_block(content: list[str]) -> list[str]:
    """Trailing content rows after the last FINISHED row — what the TUI is showing now."""
    start = 0
    for k, ln in enumerate(content):
        if _R["finished"].match(ln) and not _R["live_row"].match(ln):
            start = k + 1
    return content[start:]


def classify(lines: list[str], claude_alive: bool) -> tuple[str, str, bool, bool]:
    """Pure classifier: (state, input_draft, tui, draft_signed) from the captured lines."""
    tail = [ln.rstrip() for ln in lines if ln.strip()][-JUDGE_LINES:]
    tui = any(_R["footer"].search(ln) or _R["rule"].match(ln) for ln in tail)
    content, input_line, _footer = _zones(tail)
    draft = ""
    if input_line is not None:
        m = _R["input"].match(input_line)
        draft = (m.group("draft") if m else "").strip()
    signed = draft.startswith(SIGNED_DRAFT_PREFIX)
    if not claude_alive:
        return ("no_claude" if tui else "dead"), "", tui, False
    live = any(_R["live_row"].match(ln) or _R["live_wait"].match(ln)
               or _R["live_agent"].match(ln) or _R["live_tool"].match(ln) for ln in tail)
    if live:
        return "working", draft, tui, signed
    block = _active_block(content)
    # boxes and menus: judged in the active block only, on harness-rendered line shapes
    def has(key: str) -> bool:
        return any(_R[key].search(ln) if key in ("menu_caption",) else _R[key].match(ln)
                   for ln in block)
    numbered = any(_R["menu_row1"].match(ln) for ln in block)
    sibling = any(_R["menu_row2"].match(ln) for ln in block) or has("menu_caption")
    if has("limit_menu"):
        return "limit_menu", draft, tui, signed
    if (numbered and sibling) or has("menu_question"):
        return "approval", draft, tui, signed
    if has("login"):
        return "login_menu", draft, tui, signed
    if has("limit_block"):
        return "limit_menu", draft, tui, signed
    if any(_R["exited"].match(ln) for ln in tail):
        return "exited", draft, tui, signed
    if input_line is not None and tui:
        return ("idle_with_draft" if draft else "idle"), draft, tui, signed
    return "unknown", draft, tui, signed


def bg_counts(lines: list[str]) -> tuple[int, int, int]:
    """(bg_agents from the footer, waiting_for_agents, live_agent_rows)."""
    tail = [ln for ln in lines if ln.strip()][-JUDGE_LINES:]
    agents = 0
    for ln in reversed(tail):
        m = _R["bg_agents"].search(ln)
        if m:
            agents = int(m.group(1))
            break
    waiting = 0
    for ln in tail:
        m = _R["bg_waiting"].search(ln)
        if m:
            waiting = int(m.group(1))
    live_rows = sum(1 for ln in tail if _R["live_agent"].match(ln))
    return agents, waiting, live_rows


# ----------------------------------------------------------------- tmux / proc plumbing
class TmuxError(RuntimeError):
    pass


def _run(cmd: list[str]) -> str:
    """stdout of a command; TmuxError when it could not run or failed."""
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, errors="replace", timeout=10)
    except (OSError, subprocess.SubprocessError) as e:
        raise TmuxError(str(e)) from e
    if r.returncode != 0:
        raise TmuxError(r.stderr.strip() or f"exit {r.returncode}")
    return r.stdout


def list_panes() -> list[dict]:
    out = _run(["tmux", "list-panes", "-a", "-F",
                "#{pane_id}\t#{pane_pid}\t#{pane_current_command}\t#{session_name}"])
    panes = []
    for ln in out.splitlines():
        parts = ln.split("\t")
        if len(parts) == 4:
            panes.append({"pane": parts[0], "pane_pid": int(parts[1] or 0),
                          "current_command": parts[2], "session": parts[3]})
    return panes


def _read(path: str) -> str:
    try:
        with open(path, "rb") as fh:
            return fh.read().decode("utf-8", "replace")
    except OSError:
        return ""


def _children(pid: int) -> list[int]:
    txt = _read(f"/proc/{pid}/task/{pid}/children")
    if txt:
        return [int(x) for x in txt.split()]
    try:
        out = subprocess.run(["pgrep", "-P", str(pid)], capture_output=True, text=True,
                             errors="replace", timeout=5).stdout
    except (OSError, subprocess.SubprocessError):
        return []
    return [int(x) for x in out.split() if x.isdigit()]


def _is_claude(pid: int) -> bool:
    """`claude` by name, or a `node` whose cmdline is the claude CLI — never a bare node."""
    comm = _read(f"/proc/{pid}/comm").strip()
    if comm == "claude":
        return True
    if comm == "node":
        return "claude" in _read(f"/proc/{pid}/cmdline").replace("\x00", " ")
    return False


def claude_alive_under(pane_pid: int, current_command: str = "", depth: int = 6) -> bool:
    """A Claude CLI process is the pane's own process or a descendant of it."""
    if pane_pid and _is_claude(pane_pid):
        return True
    frontier = [pane_pid]
    for _ in range(depth):
        nxt: list[int] = []
        for pid in frontier:
            for ch in _children(pid):
                if _is_claude(ch):
                    return True
                nxt.append(ch)
        frontier = nxt
        if not frontier:
            break
    return False


def capture(pane: str) -> list[str]:
    return _run(["tmux", "capture-pane", "-p", "-t", pane, "-S", f"-{CAPTURE_LINES}"]).splitlines()


def _fingerprint(lines: list[str]) -> str:
    tail = [ln.rstrip() for ln in lines if ln.strip()][-JUDGE_LINES:]
    return hashlib.sha1("\n".join(tail).encode("utf-8", "replace")).hexdigest()


def _unchanged_for(pane: str, fp: str, now: float) -> int:
    """Seconds this fingerprint has been the same across runs (0 on change, first sight,
    or when the state dir is unusable — degrade, never crash)."""
    try:
        os.makedirs(STATE_DIR, exist_ok=True)
    except OSError:
        return 0
    path = os.path.join(STATE_DIR, pane.strip("%") + ".json")
    prev = {}
    try:
        with open(path) as fh:
            prev = json.load(fh)
    except (OSError, ValueError):
        pass
    since = float(prev.get("since", now)) if prev.get("fp") == fp else now
    try:
        with open(path + ".tmp", "w") as fh:
            json.dump({"fp": fp, "since": since}, fh)
        os.replace(path + ".tmp", path)
    except OSError:
        pass
    return int(now - since)


def read_pane(p: dict, now: float | None = None) -> PaneState:
    now = now or time.time()
    ts = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now))
    try:
        lines = capture(p["pane"])
    except TmuxError as e:
        return PaneState(pane=p["pane"], session=p["session"], pane_pid=p["pane_pid"],
                         current_command=p["current_command"], claude_alive=False, tui=False,
                         state="tmux_error", input_draft="", draft_signed=False, bg_agents=0,
                         waiting_for_agents=0, live_agent_rows=0, bottom=[str(e)[:110]],
                         unchanged_for_s=0, ts=ts)
    alive = claude_alive_under(p["pane_pid"], p["current_command"])
    state, draft, tui, signed = classify(lines, alive)
    agents, waiting, live_rows = bg_counts(lines)
    nonblank = [ln.strip() for ln in lines if ln.strip()]
    return PaneState(
        pane=p["pane"], session=p["session"], pane_pid=p["pane_pid"],
        current_command=p["current_command"], claude_alive=alive, tui=tui, state=state,
        input_draft=draft[:120], draft_signed=signed, bg_agents=agents,
        waiting_for_agents=waiting, live_agent_rows=live_rows,
        bottom=[ln[:110] for ln in nonblank[-3:]],
        unchanged_for_s=_unchanged_for(p["pane"], _fingerprint(lines), now), ts=ts,
    )


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0].replace("%", "%%"))
    ap.add_argument("targets", nargs="*", help="tmux session names or pane ids (e.g. %%12)")
    ap.add_argument("--all", action="store_true",
                    help="every pane with a live Claude process (dead panes produce no row)")
    ap.add_argument("--table", action="store_true", help="human table instead of JSON lines")
    a = ap.parse_args(argv)
    if a.all and a.targets:
        ap.error("--all cannot be combined with named targets (a tracker must name its panes)")
    try:
        panes = list_panes()
    except TmuxError as e:
        print(json.dumps({"error": f"tmux unavailable: {e}"}))
        return 2
    if a.all:
        chosen = [p for p in panes if claude_alive_under(p["pane_pid"], p["current_command"])]
    else:
        want = set(a.targets)
        chosen = [p for p in panes if p["pane"] in want or p["session"] in want]
        missing = want - {p["pane"] for p in chosen} - {p["session"] for p in chosen}
        for m in sorted(missing):
            print(json.dumps({"pane": m, "state": "not_found"}))
    states = [read_pane(p) for p in chosen]
    if a.table:
        print(f"{'pane':6s} {'session':20s} {'state':16s} {'alive':5s} {'tui':5s} "
              f"{'agents':6s} {'same_s':6s} bottom")
        for s in states:
            shown = s.input_draft or (s.bottom[-1] if s.bottom else "")
            print(f"{s.pane:6s} {s.session[:20]:20s} {s.state:16s} {str(s.claude_alive):5s} "
                  f"{str(s.tui):5s} {s.bg_agents:6d} {s.unchanged_for_s:6d} {shown[:60]}")
    else:
        for s in states:
            print(json.dumps(asdict(s), ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
