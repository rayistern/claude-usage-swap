#!/usr/bin/env python3
"""pane_state.py — SHIM. The pane reader moved to vibeCoding on 2026-09-04.

The canonical module is now `~/repos/vibeCoding/skills/build-babysitter/pane_state.py`
(rayistern/vibeCoding, `skills/build-babysitter/`): the build-babysitter skill is the
reader's only consumer and has to work on a machine without cus — cus is that skill's
OPTIONAL account layer (slots, swaps, logins, the watchdog), not a dependency (Rayi,
2026-09-04: "can someone w/o cus run this babysitting?"). The classifier's history up to
the move is in THIS repo: `git log ae11bea -- skills/pane_state.py` (rev ee1c99b is the
blind-review rewrite, PR #194; ae11bea the docs sync).

Why a shim instead of a deletion: `watch.md` § "Read panes through skills/pane_state.py",
the watchdog session's tick, and every recipe that typed the old path keep working
unchanged — this file execs the canonical copy with the same argv, so stdout, exit codes
and `--table` / `--all` are exactly the reader's. It adds nothing of its own.

Resolution order (vibeCoding D-queue #333, D4 — an explicit override that points at a
missing file is a configuration error and fails loudly rather than silently falling
back to a different copy):
  1. `$PANE_STATE_PY` when set — must be a file (`~` is expanded)
  2. `~/repos/vibeCoding/skills/build-babysitter/pane_state.py`
  3. `~/.claude/skills/build-babysitter/pane_state.py` — the skill's installed location
     (a link on this box; the only place it exists under plugin packaging, vibeCoding #332)
  4. `<this repo>/../vibeCoding/skills/build-babysitter/pane_state.py` (sibling checkout)
Candidates are deduplicated on their real path (2 and 3 are the same file on this box).
A candidate that resolves to THIS shim (a botched restore, an override pointing here)
is refused with exit 3 instead of exec'ing itself forever (blind review F-F-1 / F-O-9).

On a miss it prints ONE JSON line `{"error": …, "looked_in": […]}` and exits 3 — distinct
from the reader's own exit 2 ("tmux unusable"), so a watcher can tell "reader missing"
from "tmux down" — and never fabricates a pane row: a missing helper is STOP and
escalate, never hand-scraping (build-babysitter SKILL.md § Setup, step 2).
"""
from __future__ import annotations

import json
import os
import sys

CANONICAL_REL = os.path.join("skills", "build-babysitter", "pane_state.py")
MOVED_MSG = ("pane_state.py moved to vibeCoding/skills/build-babysitter/ on 2026-09-04; "
             "clone or pull github.com/rayistern/vibeCoding at ~/repos/vibeCoding (or next to "
             "this repo), link the skill at ~/.claude/skills/build-babysitter, or set "
             "PANE_STATE_PY to the file")
SELF_MSG = ("pane_state.py resolved to this shim itself — the canonical copy is missing or "
            "the candidate is a link back here; not exec'ing myself. " + MOVED_MSG)


def candidates() -> list[str]:
    """Every path the shim will try, in order, whether or not it exists."""
    here = os.path.dirname(os.path.realpath(__file__))         # <cus repo>/skills
    home = os.path.expanduser("~")
    out: list[str] = []
    env = os.environ.get("PANE_STATE_PY")
    if env:
        out.append(os.path.expanduser(env))
    out.append(os.path.join(home, "repos", "vibeCoding", CANONICAL_REL))
    out.append(os.path.join(home, ".claude", "skills", "build-babysitter", "pane_state.py"))
    out.append(os.path.normpath(os.path.join(here, "..", "..", "vibeCoding", CANONICAL_REL)))
    # On the usual layout several candidates are one file (the sibling path IS the home
    # path; ~/.claude/skills/build-babysitter is a link into it) — keep the first
    # spelling of each real path so the error line does not list one place twice.
    seen: set[str] = set()
    uniq: list[str] = []
    for c in out:
        key = os.path.realpath(c)
        if key not in seen:
            seen.add(key)
            uniq.append(c)
    return uniq


def resolve() -> tuple[str | None, str | None, list[str]]:
    """(path, error, looked_in): the first existing candidate, or None plus why and
    the paths that were actually considered.

    An explicit `$PANE_STATE_PY` that is not a file is an error on its own — it means
    the operator pointed at the wrong place, and running some OTHER copy would hide
    that — so only that path is reported as looked in. A candidate that IS this shim
    (by real path) is skipped over as an error rather than exec'd: os.execv on oneself
    is an infinite loop with no output, no exit and no new pid.
    """
    env = os.environ.get("PANE_STATE_PY")
    if env:
        env = os.path.expanduser(env)
        if not os.path.isfile(env):
            return None, f"PANE_STATE_PY is set but is not a file: {env}", [env]
    me = os.path.realpath(__file__)
    looked = candidates()
    for c in looked:
        if os.path.isfile(c):
            if os.path.realpath(c) == me:
                return None, SELF_MSG, looked
            return c, None, looked
    return None, MOVED_MSG, looked


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    target, err, looked = resolve()
    if target is None:
        print(json.dumps({"error": err, "looked_in": looked}))
        return 3
    # exec, not subprocess: same pid, same stdout/stderr, same exit code — the caller
    # cannot tell the shim from the real thing, which is the whole point.
    os.execv(sys.executable, [sys.executable, target, *argv])
    return 0  # unreachable; keeps type-checkers honest


if __name__ == "__main__":
    sys.exit(main())
