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
  2. `~/repos/vibeCoding/skills/build-babysitter/pane_state.py`
  3. `~/.claude/skills/build-babysitter/pane_state.py` — the skill's installed location
     (a link on this box; the only place it exists under plugin packaging, vibeCoding #332)
  4. `<this repo>/../vibeCoding/skills/build-babysitter/pane_state.py` (sibling checkout)
Candidates are deduplicated on their real path (2 and 3 were the same file when this
order was written; they are not, on a box whose skill link points at another worktree).
When more than one of 2–4 exists as a different file, the newer one runs
(annotation 2026-09-25, below). A single existing file is used as-is. The same
realpath is one candidate, so there is nothing to compare.
A candidate that resolves to THIS shim (a botched restore, an override pointing here)
is refused with exit 3 instead of exec'ing itself forever (blind review F-F-1 / F-O-9).
If every existing candidate is this shim, that is still exit 3. A later real reader
is used instead of refusing the whole lookup.

Annotation 2026-09-25: D4 still names the candidates, and the override still wins and
still fails loud. The shim used to stop at the first existing file. A checkout at
candidate 2 that is behind the installed skill then hid a reader that already
treated the cus SOS footer line as footer, and every live pane under that line read
`unknown`. Each candidate gets one key, computed once:
`(has_commit, commit_time, mtime)`. A file with a commit sorts above a file
with none, so modification time alone never outranks a committed reader.
`commit_time` is `git log -1 --format=%ct` (0 when git cannot say). `mtime`
is only the third field: it breaks a tie, and it decides when neither file
has a commit. An equal key keeps the earlier candidate.
Limits, plainly: the key is the last commit that touched the file, not the
behaviour in the bytes, and the shim then execs the working tree. A dirty
checkout therefore runs its uncommitted text while ranking by the commit.
An uncommitted fix on an older commit loses to a later commit. A later
commit that only edits a comment, or a rebase, amend, or cherry-pick, can
make an older reader look newer. `$PANE_STATE_PY` is how to point at a
specific file. Each git call waits at most 1 second. At most three
candidates remain after dedup, so a hung git costs at most 3 seconds.
The override does not call git.
When two or more real readers exist, or a candidate that is this shim is
skipped and a real reader remains, one line on stderr names the choice
and the other paths. The pick is not silent.
When `$PANE_STATE_PY` points at this shim, `looked_in` is only that path.
Before this change the same error listed every candidate.
Annotation 2026-09-25, later the same day: a cache of that choice was added and
then removed. A cold resolve is about 40 ms, and the cache file plus a shared
`.tmp` write did not earn their place.

On a miss it prints ONE JSON line `{"error": …, "looked_in": […]}` and exits 3 — distinct
from the reader's own exit 2 ("tmux unusable"), so a watcher can tell "reader missing"
from "tmux down" — and never fabricates a pane row: a missing helper is STOP and
escalate, never hand-scraping (build-babysitter SKILL.md § Setup, step 2).
"""
from __future__ import annotations

import json
import os
import subprocess
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


#: One git log per candidate. Three candidates after dedup is the worst case
#: (3 seconds) if every call hangs. The override path does not call git.
GIT_LOG_TIMEOUT_S = 1


def _git_commit_time(path: str) -> int | None:
    """Unix time of the last commit that touched `path`, or None when git cannot say.

    None covers: no git binary, not a worktree, an untracked file, a non-zero
    exit, or an empty answer. None means "no commit" in the comparison key,
    not "compare modification times instead".
    """
    try:
        proc = subprocess.run(
            ["git", "-C", os.path.dirname(path), "log", "-1", "--format=%ct", "--",
             os.path.basename(path)],
            capture_output=True, text=True, timeout=GIT_LOG_TIMEOUT_S,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        return None
    stamp = proc.stdout.strip()
    if not stamp.isdigit():
        return None
    return int(stamp)


def _rank(path: str) -> tuple[int, int, float]:
    """One comparison key. Higher is preferred. See the module docstring.

    `(has_commit, commit_time, mtime)`. `has_commit` is 0 or 1, so a committed
    file always sorts above an untracked one. `mtime` cannot reverse that.
    """
    commit_time = _git_commit_time(path)
    try:
        mtime = os.path.getmtime(path)
    except OSError:
        mtime = 0.0
    if commit_time is None:
        return (0, 0, mtime)
    return (1, commit_time, mtime)


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
    best = _rank(chosen)
    for challenger in found[1:]:
        rank = _rank(challenger)
        if rank > best:
            chosen = challenger
            best = rank
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
