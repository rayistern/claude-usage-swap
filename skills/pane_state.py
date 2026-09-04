#!/usr/bin/env python3
"""pane_state.py — one JSON line per tmux pane: is a Claude Code process there, and what is
its TUI doing right now (working / idle / idle with an unsent draft / approval box / limit
menu / login menu / exited / dead)?

Why this exists
---------------
The transcript tells a watcher WHY a session stopped (usage wall, expired login, kill —
see context-dashboard's `session_metrics --live`), but two things only the pane can say:
whether there is a PROCESS to nudge at all (a session that exited to a bare shell and one
idling at an empty prompt are identical in the transcript — watch.md, 2026-07-10 lesson),
and whether the TUI is BLOCKED ON A HUMAN (a permission box, a trust-folder box, the
`/rate-limit-options` menu, the "Please run /login" menu — none of those are transcript
events until someone answers). The `/watch` skill carried these rules as prose recipes
(`tmux capture-pane … | tail -18`, look for the `⎿` block above the prompt, check the
pane pid's children, diff against last cycle); this script is those rules as one command,
so a babysitter tick calls it instead of re-deriving them. Born 2026-09-04 with the
build-babysitter skill (vibeCoding#330).

What it reads
-------------
  * `tmux list-panes -a` → pane id, pane pid, current command, session name.
  * /proc → does the pane pid have a `claude`/`node` descendant? (no descendant and a
    shell as the current command = DEAD, relaunch; watch.md's `/proc` check).
  * `tmux capture-pane -p -S -40` → the visible tail. Claude Code's layout, bottom-up:
    footer lines (statusline, `⏵⏵ bypass permissions on … · ← N agents`, `new task?`),
    a rule, the INPUT line (`❯ …`), a rule, then content (`⏺ Bash(…)`, `✻ Thinking…
    (esc to interrupt)`, or a `⎿` limit block). So the input line is NOT the last line,
    and "empty prompt" must be judged on the `❯` line, not on the tail.
  * A per-pane scratch file (~/.cache/pane_state/<pane>.json) holding the last capture's
    fingerprint, so a pane whose bottom text hasn't changed across ticks reports
    `unchanged_for_s` — the "frozen still frame" case (watch.md, 2026-07-10).

States (priority order — the first that matches wins)
------
  dead              no claude/node process under the pane (shell prompt, or gone)
  exited            Claude printed "Resume this session with: claude --resume …" (process
                    still winding down or already gone)
  login_menu        "Please run /login" / "Not logged in" / OAuth expired — human only
  limit_menu        `/rate-limit-options` menu, "Stop and wait", or the Fable soft-limit
                    `⎿ You've reached your … limit` block — swap + nudge territory
  approval          a numbered yes/no box ("❯ 1. Yes …", "Enter to confirm · Esc to
                    cancel", "Do you want to proceed?") — NEVER answer these for the human
  working           a LIVE spinner row: "(esc to interrupt)", a braille spinner glyph, a
                    "✻ Baking…" verb still in progress, "Running…". Finished rows persist
                    in scrollback and are NOT activity — "⏺ Bash(…)" and "✻ Baked for
                    2m 7s · done 1:56 PM" alike; "← N agents" in the footer means
                    background agents exist and is reported as bg_agents, not busy-ness
  idle_with_draft   the input line holds text nobody submitted (a nudge whose Enter
                    never registered — watch.md, 2026-07-14: press Enter, re-read)
  idle              the input line is an empty `❯` and nothing is running
  unknown           a claude process exists but the tail matched nothing above

Usage
-----
  pane_state.py %12 %13            # explicit pane ids or tmux session names
  pane_state.py --all              # every pane with a live claude/node process
  pane_state.py --all --table      # human table (JSON lines is the default)
Exit code 0 always; the caller reads the `state` field.
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

#: How many visible lines to capture from the bottom of the pane. The soft-limit `⎿` block
#: and a tool row can sit a dozen lines above the input line, under the footer — 40 is far
#: past any observed footer + input + block, cheap to read.
CAPTURE_LINES = 40
#: Only the bottom N non-blank lines are judged; scrollback above is stale by definition.
JUDGE_LINES = 18
STATE_DIR = os.path.expanduser("~/.cache/pane_state")

_R = {
    "exited": re.compile(r"Resume this session with:\s*claude --resume", re.I),
    "login": re.compile(r"please run /login|not logged in|login expired|oauth session expired"
                        r"|failed to authenticate", re.I),
    "limit": re.compile(r"/rate-limit-options|stop and wait|upgrade your plan"
                        r"|(reached|hit) your .{0,40}limit|run /usage-credits", re.I),
    "approval": re.compile(r"^\s*❯\s*1\.\s|enter to confirm|esc to cancel|do you want to (proceed|allow"
                           r"|make this edit)|yes, and don'?t ask again|\(y/n\)", re.I),
    # LIVE activity only. A finished tool row keeps its ⏺ glyph in the scrollback, so ⏺
    # is not evidence; the ✻-family glyphs and braille dots are the spinner prefix of an
    # in-flight "Thinking…/Brewed for 12s/Running…" row and vanish when it completes.
    # "(esc to interrupt)" is the harness's own "I am busy" caption.
    # The ✻ row PERSISTS after the turn ("✻ Baked for 2m 7s · done 1:56 PM"); only its
    # live form carries "(esc to interrupt)" or a verb still in progress ("✻ Baking…").
    "working": re.compile(r"esc to interrupt|^\s*[⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏]\s"
                          r"|^\s*[✻✳✶✽✢]\s+[A-Za-z]+…|\brunning…", re.I),
    # Footer hint "· ← N agents": background agents EXIST (a Monitor, a subagent) — not
    # proof the main thread is busy. Reported separately as bg_agents.
    "bg_agents": re.compile(r"←\s*(\d+)\s+agents?\b", re.I),
    "input": re.compile(r"^\s*❯(?P<draft>.*)$"),
}


@dataclass
class PaneState:
    pane: str
    session: str
    pane_pid: int
    current_command: str
    claude_alive: bool
    state: str
    input_draft: str          # text sitting unsent in the input box ('' if none)
    bg_agents: int            # "← N agents" in the footer: background agents alive (not busy-ness)
    bottom: list[str]         # last 3 non-blank lines, for the human reading the tick
    unchanged_for_s: int      # seconds the judged tail has been identical (0 = changed)
    ts: str


def bg_agents(lines: list[str]) -> int:
    """Background agents named in the footer ('← 2 agents'), 0 when none."""
    for ln in reversed([ln for ln in lines if ln.strip()][-JUDGE_LINES:]):
        m = _R["bg_agents"].search(ln)
        if m:
            return int(m.group(1))
    return 0


def classify(lines: list[str], claude_alive: bool) -> tuple[str, str]:
    """Pure classifier: (state, input_draft) from the captured lines.

    `lines` is the raw capture (any length); only the bottom JUDGE_LINES non-blank lines
    are judged, in the priority order documented in the module docstring."""
    tail = [ln.rstrip() for ln in lines if ln.strip()][-JUDGE_LINES:]
    text = "\n".join(tail)
    if not claude_alive:
        return "dead", ""
    if _R["exited"].search(text):
        return "exited", ""
    if _R["login"].search(text):
        return "login_menu", ""
    if _R["limit"].search(text):
        return "limit_menu", ""
    # The input line: the LAST line starting with ❯ that is not a numbered menu row.
    draft = ""
    input_seen = False
    for ln in reversed(tail):
        m = _R["input"].match(ln)
        if m and not re.match(r"^\s*❯\s*\d+\.", ln):
            input_seen = True
            draft = m.group("draft").strip()
            break
    if any(_R["approval"].search(ln) for ln in tail):
        return "approval", draft
    if any(_R["working"].search(ln) for ln in tail):
        return "working", draft
    if input_seen:
        return ("idle_with_draft" if draft else "idle"), draft
    return "unknown", draft


# ----------------------------------------------------------------- tmux / proc plumbing
def _run(cmd: list[str]) -> str:
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=10).stdout
    except (OSError, subprocess.SubprocessError):
        return ""


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


def _children(pid: int) -> list[int]:
    try:
        with open(f"/proc/{pid}/task/{pid}/children") as fh:
            return [int(x) for x in fh.read().split()]
    except OSError:
        out = _run(["pgrep", "-P", str(pid)])
        return [int(x) for x in out.split() if x.isdigit()]


def _comm(pid: int) -> str:
    try:
        with open(f"/proc/{pid}/comm") as fh:
            return fh.read().strip()
    except OSError:
        return ""


def claude_alive_under(pane_pid: int, current_command: str, depth: int = 4) -> bool:
    """A claude/node process is the pane's command itself or a descendant of it."""
    if current_command in ("claude", "node"):
        return True
    frontier = [pane_pid]
    for _ in range(depth):
        nxt: list[int] = []
        for pid in frontier:
            for ch in _children(pid):
                if _comm(ch) in ("claude", "node"):
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
    return hashlib.sha1("\n".join(tail).encode()).hexdigest()


def _unchanged_for(pane: str, fp: str, now: float) -> int:
    """Seconds this fingerprint has been the same across runs (0 on change/first sight)."""
    os.makedirs(STATE_DIR, exist_ok=True)
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
    lines = capture(p["pane"])
    alive = claude_alive_under(p["pane_pid"], p["current_command"])
    state, draft = classify(lines, alive)
    nonblank = [ln.rstrip() for ln in lines if ln.strip()]
    return PaneState(
        pane=p["pane"], session=p["session"], pane_pid=p["pane_pid"],
        current_command=p["current_command"], claude_alive=alive, state=state,
        input_draft=draft[:120], bg_agents=bg_agents(lines),
        bottom=[ln[:110] for ln in nonblank[-3:]],
        unchanged_for_s=_unchanged_for(p["pane"], _fingerprint(lines), now),
        ts=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now)),
    )


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("targets", nargs="*", help="pane ids (%12) or tmux session names")
    ap.add_argument("--all", action="store_true", help="every pane with a live claude/node")
    ap.add_argument("--table", action="store_true", help="human table instead of JSON lines")
    a = ap.parse_args(argv)
    panes = list_panes()
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
        print(f"{'pane':6s} {'session':20s} {'state':16s} {'alive':5s} {'agents':6s} {'same_s':6s} bottom")
        for s in states:
            shown = s.input_draft or (s.bottom[-1] if s.bottom else "")
            print(f"{s.pane:6s} {s.session[:20]:20s} {s.state:16s} {str(s.claude_alive):5s} "
                  f"{s.bg_agents:6d} {s.unchanged_for_s:6d} {shown[:60]}")
    else:
        for s in states:
            print(json.dumps(asdict(s), ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
