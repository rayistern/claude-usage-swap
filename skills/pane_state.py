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
and `--table` / `--all` are exactly the reader's. It adds nothing of its own about
what a pane is doing.
Annotation 2026-09-25: it does choose WHICH copy to exec when several exist. That
choice is the resolution note below, not a second classifier.

Resolution order (vibeCoding D-queue #333, D4 — an explicit override that points at a
missing file is a configuration error and fails loudly rather than silently falling
back to a different copy):
  1. `$PANE_STATE_PY` when set — must be a file (`~` is expanded). It wins even when
     an older or newer copy exists elsewhere.
  2. `~/.claude/skills/build-babysitter/pane_state.py` — the installed skill
     (the copy an operator is running; the only place it exists under plugin
     packaging, vibeCoding #332)
  3. `~/repos/vibeCoding/skills/build-babysitter/pane_state.py` (a checkout, which
     can be behind the installed skill)
  4. `<this repo>/../vibeCoding/skills/build-babysitter/pane_state.py` (sibling checkout)
Candidates are deduplicated on their real path (2 and 3 were the same file when this
order was written; they are not, when the skill link points at another worktree).
When more than one of 2–4 exists as a different file, the earlier one in that
order runs (annotation 2026-09-25, below). A single existing file is used as-is. The same
realpath is one candidate, so there is nothing to compare.
A candidate that resolves to THIS shim (a botched restore, an override pointing here)
is refused with exit 3 instead of exec'ing itself forever (blind review F-F-1 / F-O-9).
If every existing candidate is this shim, that is still exit 3. A later real reader
is used instead of refusing the whole lookup.

Annotation 2026-09-25: D4 still names the candidates, and the override still wins and
still fails loud. The shim used to stop at the first existing file, which was the
checkout. That checkout can be behind the installed skill, and every live pane
under a `cus SOS:` line then read `unknown`.
Annotation 2026-09-25, later: two metadata keys were tried and both failed.
Commit time when both files had a commit let a fresh untracked copy win.
`(has_commit, commit_time, mtime)` then let a committed stale checkout beat a
plain or symlinked skill copy, because a git error ranks the same as untracked.
Neither commit time nor modification time is the reader's behaviour.
The rule now is the order above, with no git call and no mtime. The installed
skill wins over a checkout. `$PANE_STATE_PY` is how to point at a specific file.
When two different files exist, or a self-link is skipped, one stderr line
names the choice and the other paths. `cus panes` copies that line into
`reader_notice` (JSON) and prints it in text mode. A single file is silent.
What this cannot see: a newer checkout that was never installed, a stale
installed skill, a feature branch whose later commit is only a comment, and a
fresh untracked file sitting on the skill path. Those stay the installed
skill, and the stderr line is how the operator finds the other path.
When `$PANE_STATE_PY` points at this shim, `looked_in` is only that path.
Annotation 2026-09-25, later the same day: a cache of a metadata choice was
added and then removed. One cold resolve measured about 40 ms; under load the
same call measured 58–190 ms. The cache did not earn its place. There is no
git call left to time.

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
    out.append(os.path.join(home, ".claude", "skills", "build-babysitter", "pane_state.py"))
    out.append(os.path.join(home, "repos", "vibeCoding", CANONICAL_REL))
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


def _notice(found: list[str], chosen: str, saw_self: bool) -> None:
    """One stderr line when the pick could have gone another way. Silent when
    there is a single real reader and nothing was skipped."""
    if len(found) < 2 and not saw_self:
        return
    others = [p for p in found if os.path.realpath(p) != os.path.realpath(chosen)]
    parts = [f"pane_state: chose {chosen}"]
    if others:
        parts.append("also found " + ", ".join(others))
    if saw_self:
        parts.append("skipped a candidate that is this shim")
    print("; ".join(parts), file=sys.stderr)


def resolve() -> tuple[str | None, str | None, list[str]]:
    """(path, error, looked_in): the reader to exec, or None plus why and the
    paths that were actually considered.

    An explicit `$PANE_STATE_PY` that is not a file is an error on its own — it means
    the operator pointed at the wrong place, and running some OTHER copy would hide
    that — so only that path is reported as looked in. The override wins even when
    another copy is newer. A candidate that IS this shim (by real path) is not
    exec'd: os.execv on oneself is an infinite loop with no output, no exit and no
    new pid. When the only existing candidate is this shim, the error is SELF_MSG
    (exit 3). When none exist, the error is MOVED_MSG (exit 3).
    """
    env = os.environ.get("PANE_STATE_PY")
    if env:
        env = os.path.expanduser(env)
        if not os.path.isfile(env):
            return None, f"PANE_STATE_PY is set but is not a file: {env}", [env]
        if os.path.realpath(env) == os.path.realpath(__file__):
            return None, SELF_MSG, [env]
        return env, None, candidates()
    me = os.path.realpath(__file__)
    looked = candidates()
    found: list[str] = []
    saw_self = False
    for c in looked:
        if not os.path.isfile(c):
            continue
        if os.path.realpath(c) == me:
            saw_self = True
            continue
        found.append(c)
    if not found:
        return None, (SELF_MSG if saw_self else MOVED_MSG), looked
    chosen = found[0]
    _notice(found, chosen, saw_self)
    return chosen, None, looked


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
