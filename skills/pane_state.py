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
  1. `$PANE_STATE_PY` when set — must be a file
  2. `~/repos/vibeCoding/skills/build-babysitter/pane_state.py`
  3. `<this repo>/../vibeCoding/skills/build-babysitter/pane_state.py` (sibling checkout)

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
             "clone github.com/rayistern/vibeCoding to ~/repos/vibeCoding (or next to this "
             "repo), or set PANE_STATE_PY to the file")


def candidates() -> list[str]:
    """Every path the shim will try, in order, whether or not it exists."""
    here = os.path.dirname(os.path.abspath(__file__))          # <cus repo>/skills
    out: list[str] = []
    env = os.environ.get("PANE_STATE_PY")
    if env:
        out.append(env)
    out.append(os.path.join(os.path.expanduser("~"), "repos", "vibeCoding", CANONICAL_REL))
    out.append(os.path.normpath(os.path.join(here, "..", "..", "vibeCoding", CANONICAL_REL)))
    # On the usual layout (~/repos/claude-usage-swap) the sibling path IS the home path;
    # keep the first occurrence so the error line does not list one place twice.
    seen: set[str] = set()
    return [c for c in out if not (c in seen or seen.add(c))]


def resolve() -> tuple[str | None, str | None]:
    """(path, error): the first existing candidate, or None plus why.

    An explicit `$PANE_STATE_PY` that is not a file is an error on its own — it means the
    operator pointed at the wrong place, and running some OTHER copy would hide that.
    """
    env = os.environ.get("PANE_STATE_PY")
    if env and not os.path.isfile(env):
        return None, f"PANE_STATE_PY is set but is not a file: {env}"
    for c in candidates():
        if os.path.isfile(c):
            return c, None
    return None, MOVED_MSG


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    target, err = resolve()
    if target is None:
        print(json.dumps({"error": err, "looked_in": candidates()}))
        return 3
    # exec, not subprocess: same pid, same stdout/stderr, same exit code — the caller
    # cannot tell the shim from the real thing, which is the whole point.
    os.execv(sys.executable, [sys.executable, target, *argv])
    return 0  # unreachable; keeps type-checkers honest


if __name__ == "__main__":
    sys.exit(main())
