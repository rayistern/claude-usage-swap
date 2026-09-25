# Pane `unknown` — shapes and where the fix lives

**Date:** 2026-09-25
**Branch:** `fix/pane-unknown-20260925` (from `be98e0b`)
**Pull request:** [#251](https://github.com/rayistern/claude-usage-swap/pull/251)

## Before

One `cus panes --json` snapshot: **43** live panes. States: idle 21, working 11, unknown 9, idle_with_draft 2. No `limit_menu`.

Each `unknown` row was captured with `tmux capture-pane -p` (read only). The first classification used the checkout the shim tries first (`~/repos/vibeCoding/skills/build-babysitter/pane_state.py`, commit `e3208ea8`, 2026-09-15), **301** commits behind `origin/master`. The installed skill (`~/.claude/skills/build-babysitter/pane_state.py`, `pane_state.py` at `8b3f52bb`, 2026-09-24) is a different file. Re-classification with that newer reader is below.

## Shapes

| Shape | Panes | True state | Why the older reader says `unknown` |
|---|---|---|---|
| `🚨 cus SOS:` between the input rule and the `cus` / `⏵⏵` / `new task?` footer | 7 | idle (6, empty `❯`); idle_with_draft (1, unsent text on `❯`) | Footer regex is `^\s*cus\s`. The alarm emoji is not whitespace, so the bottom-up walk stops on the SOS line and never sees `❯`. |
| `⧉ review` on the last row, under `new task?` | 1 | idle (empty `❯`) | That chip matches neither footer nor rule, so the walk stops on row 0 and the whole status cluster stays in content. The newer reader still returns `unknown` for this shape. |
| Same SOS screens also show `Stop says …` / `UserPromptSubmit says …` and, on 2 of them, `How is Claude doing this session?` with `1: Bad / 2: Fine / 3: Good` | (inside the 7) | still idle or idle_with_draft | Hook lines sit in content above the prompt. The feedback rows are `1:` not `❯ 1.`, so they are not an approval box. They are not the cause. |
| One row was `unknown` in `cus panes` and `working` on the capture taken just after (`· Enchanting… (10s · …)`) | 1 | working | The live-row regex already matches that line once the footer peels. Treated as a race, not a shape. No fixture until it reproduces. |
| Validator lines, usage-limit menus (`You've hit your session limit`), Cursor TUI (`→ Add a follow-up`, model row, wrapped `cwd · branch` footer) | 0 | — | Not in this unknown set. No fixture without a capture. |

`cus.py` `collect_pane_row` copies `pane_row["state"]` through. `read_panes_from_reader` shells out to `skills/pane_state.py`, which execs the vibeCoding file. Nothing in the call remaps a good verdict to `unknown`.

## Where the fix lives

> **Correction 2026-09-25.** An earlier draft of this section said both shapes needed a new footer pattern, and that the shim should extend that pattern. The installed skill already matches `🚨 cus`. A later sentence in that draft said the shim should pick the newer file by mtime, and that this repo would carry a `⧉ review` fixture. The code does neither of those. A first implementation used commit time only when both files had a commit, and mtime otherwise. Review round 1 replaced that with one key, `(has_commit, commit_time, mtime)`, so a committed file never loses to mtime alone. Review round 2 showed that key treats a git error and a plain copy as untracked, so a committed stale checkout wins. The rule is now the documented path order: the installed skill, then the checkout, then the sibling. No git call. The chip fixture was left upstream. Superseded wording is not repeated below.

1. **SOS rows — shim resolution, not a missing pattern.** `skills/pane_state.py` tries `~/repos/vibeCoding/skills/build-babysitter/pane_state.py` first. That file is `e3208ea8` (2026-09-15) and its `_R["footer"]` has no `🚨`. The next candidate, `~/.claude/skills/build-babysitter/pane_state.py`, is a different file (`8b3f52bb`, 2026-09-24) whose `_R["footer"]` already includes `^\s*🚨\s+cus\b`. Re-classifying the 9 captures with that reader: idle 6, idle_with_draft 1, working 1, unknown 1. The shim used to stop at the first existing file. On a machine where the checkout and the installed skill are different files, the older one won. The shim now uses that path order and does not call git. The installed skill wins over a checkout. Two different files print one stderr line, and `cus panes` copies it to `reader_notice`. One existing file is used as-is. The same real path is one candidate. `$PANE_STATE_PY` still wins, including when it is older. No SOS pattern was added in the shim. `collect_pane_row` still copies `state` through. Shipped in `594e3de`. A cache of the choice was added in `4164bcd` and removed in `f5b2194`: a cold resolve is about 40 ms.

2. **`⧉ review` chip — still unknown on the newer reader.** Footer and `footer_anchored` both miss `⧉`. One live pane, empty `❯`, true state idle. The pattern belongs in the skill, not in a second classifier here. No fixture for it in this repo: CI has no reader, so an xfail here would not run, and a local regex would not test the skill. The proposed issue below asks the skill to add the pattern and one fixture.

Cursor, on the newer reader at the time of the capture: **6** panes (5 working, 1 idle). None `unknown`. The idle one was 84 columns wide and its path footer still matched, so it was not the wrapped-footer case in vibeCoding #637. No fixture for that case.

## Proposed upstream issue

Not opened by this pull request.

**Title:** pane reader: a trailing `⧉ review` chip leaves a live Claude Code pane `unknown`

**Body:**

`ClaudeCodeProfile._zones` peels footer lines from the bottom only while they match `_R["footer"]` or a rule. As of `8b3f52bb` (2026-09-24) that regex already includes `^\s*🚨\s+cus\b`, so a `cus SOS:` row is footer and a pane at `❯` under it reads `idle` or `idle_with_draft`. The SOS line does not need another change.

What is still open: a last-row `⧉ review` chip under `new task? /clear`. It matches neither `_R["footer"]` nor `_R["footer_anchored"]`, the walk stops on row 0, and the pane reads `unknown`. The prompt above it is an empty `❯` and nothing is live, so the state is `idle`. Please treat a trailing `⧉` chip as a footer line. Hook lines (`Stop says`, `UserPromptSubmit says`) are content. The optional "How is Claude doing this session?" rows (`1: Bad`) are not an approval box.

One fixture, in the skill's tests. A `READER_VERSION` constant on the reader would be a clearer comparison than commit time; this pull request does not add one.

## Checklist

- [x] Shim runs the installed skill when it and the vibeCoding checkout are different files. Evidence: this step's commit (path order, no git). Test: `test_installed_skill_wins_over_a_later_commit_in_the_checkout`.
- [x] Fixture: SOS screen, empty prompt → idle. Evidence: `tests/fixtures/pane_state/sos_idle.txt`, `708c38f`, `test_sos_footer_with_an_empty_prompt_is_idle`. Runs when a reader is installed; skips with a stated reason when none is (`f5b2194`).
- [x] Fixture: SOS screen, unsent draft → idle_with_draft. Evidence: `tests/fixtures/pane_state/sos_idle_with_draft.txt`, `708c38f`, `test_sos_footer_with_an_unsent_draft_is_idle_with_draft`.
- [x] No `⧉ review` fixture in this repo. Left upstream (section above). An xfail would not run in CI, which has no reader. After-count still shows one such pane: `python3 cus.py panes --json` from this worktree, 2026-09-25, 45 panes, unknown 1, and that capture's last row is the chip.
- [x] After-count. Before (`cus panes --json`, earlier snapshot): 43 panes, unknown 9 (idle 21, working 11, idle_with_draft 2). After (`python3 cus.py panes --json` from this worktree, later snapshot, not the same set of panes): 45 panes, idle 37, working 7, unknown 1. Recorded on [#251](https://github.com/rayistern/claude-usage-swap/pull/251).
- [x] Full suite, stated honestly. Local `pytest tests/ -q` on `2d2f53d`: 884 passed. The path-order commit's local run: 884 passed, 1 failed (`test_flat_config_polls_everyone_each_interval`). The docs-and-fixtures commit's local run was 883 passed and the same failure. That test passes on its own and fails the same way on `be98e0b` when the whole suite runs. An earlier full run on `f5b2194` failed the same test (879 passed). GitHub Actions on `f5b2194` is green: `gh pr checks 251` → test 3.11, 3.12, 3.13 pass (run `36099819413`). The round-2 disposition records runner CI green at `7cd9671`.
- [x] Draft pull request [#251](https://github.com/rayistern/claude-usage-swap/pull/251). The installed `cus` command was not reinstalled; the commits do not change an install path.

## What shipped

1. Shim resolution: the installed skill, then the checkout, then the sibling. No git call. Two files print which one was chosen, and `cus panes` shows that line. One cold resolve measured about 40 ms before the git calls were removed; under load the same call measured 58–190 ms, so 40 ms was one measurement, not a bound.
2. Two scrubbed SOS fixtures (`708c38f`), classified by the installed reader, skip when none is installed (`f5b2194`).
3. Reader-choice cache added (`4164bcd`) and removed (`f5b2194`).
4. Draft pull request #251.
