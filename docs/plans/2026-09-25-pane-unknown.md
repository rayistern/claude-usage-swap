# Pane `unknown` — shapes and where the fix lives

**Date:** 2026-09-25
**Branch:** `fix/pane-unknown-20260925` (from `be98e0b`)

## Before

`cus panes --json` on this box: **43** live panes. States: idle 21, working 11, unknown 9, idle_with_draft 2. No `limit_menu`.

Each `unknown` row was captured with `tmux capture-pane -p` (read only). The first classification used the checkout the shim resolves first (`~/repos/vibeCoding/skills/build-babysitter/pane_state.py`, commit `e3208ea8`, 2026-09-15). That checkout is **301** commits behind `origin/master`. The installed skill (`~/.claude/skills/build-babysitter/pane_state.py` → the serve worktree, `pane_state.py` at `8b3f52bb`, 2026-09-24) is a different file. Re-classification with that newer reader is in the correction below.

## Shapes

| Shape | Panes | True state | Why the reader says `unknown` |
|---|---|---|---|
| `🚨 cus SOS:` between the input rule and the `cus` / `⏵⏵` / `new task?` footer | 7 | idle (6, empty `❯`); idle_with_draft (1, unsent text on `❯`) | Footer regex is `^\s*cus\s`. The alarm emoji is not whitespace, so the bottom-up walk stops on the SOS line and never sees `❯`. |
| `⧉ review` on the last row, under `new task?` | 1 | idle (empty `❯`) | That chip matches neither footer nor rule, so the walk stops on row 0 and the whole status cluster stays in content. |
| Same SOS screens also show `Stop says …` / `UserPromptSubmit says …` and, on 2 of them, `How is Claude doing this session?` with `1: Bad / 2: Fine / 3: Good` | (inside the 7) | still idle or idle_with_draft | Hook lines sit in content above the prompt. The feedback rows are `1:` not `❯ 1.`, so they are not an approval box. They are not the cause. |
| One row was `unknown` in `cus panes` and `working` on the capture taken just after (`· Enchanting… (10s · …)`) | 1 | working | The live-row regex already matches that line once the footer peels. Treated as a race, not a shape. No fixture until it reproduces. |
| Validator lines, usage-limit menus (`You've hit your session limit`), Cursor TUI (`→ Add a follow-up`, model row, wrapped `cwd · branch` footer) | 0 | — | Not in this unknown set. No fixture without a capture. |

`cus.py` `collect_pane_row` copies `pane_row["state"]` through. `read_panes_from_reader` shells out to `skills/pane_state.py`, which is a shim that execs the vibeCoding file. Nothing in the call remaps a good verdict to `unknown`.

## Where the fix lives

> **Correction 2026-09-25 (same day, after re-reading the installed skill).** The paragraph below was written against the checkout the shim resolves first. That file does not match `🚨 cus`. The installed skill already does. Superseded text kept here so the first reading stays visible.

**Superseded:** both shapes need new footer patterns upstream, and the shim should extend the pattern until the skill absorbs it.

**Measured:** two different gaps.

1. **SOS rows — shim resolution, not a missing pattern.** `skills/pane_state.py` tries `~/repos/vibeCoding/skills/build-babysitter/pane_state.py` first and stops. That file is `e3208ea8` (2026-09-15), 301 commits behind `origin/master`, and its `_R["footer"]` has no `🚨`. The next candidate, `~/.claude/skills/build-babysitter/pane_state.py`, is a different file (`8b3f52bb`, 2026-09-24) whose `_R["footer"]` already includes `^\s*🚨\s+cus\b`. Re-classifying the 9 captures with that reader: idle 6, idle_with_draft 1, working 1, unknown 1. The 7 SOS captures are idle or idle_with_draft. A live `--all` on that reader: 45 rows, **1** unknown. The shim's own comment says the checkout and the skill link are the same file; on this box they are not, and the older one wins. Cus-side fix: when those two candidates are different files, run the newer one (mtime). Do not add an SOS alternative in the shim. `collect_pane_row` still copies `state` through.

2. **`⧉ review` chip — still unknown on the newest reader.** Footer and `footer_anchored` both miss `⧉`. One live pane, empty `❯`, true state idle. That pattern is the upstream change. This repo's shim does not grow a second classifier; the issue text below is what the desk files. A scrubbed fixture in this repo asserts idle for that shape against the reader the shim actually runs, so the gap stays visible until the skill lands it. If the after-count still shows that one pane as `unknown`, the row's reason is this chip, not a silent `unknown`.

Cursor, on the newest reader: **6** panes (5 working, 1 idle). None `unknown`. The idle one is 84 columns wide and its path footer still matches, so it is not the wrapped-footer case in vibeCoding #637 (open: an idle Cursor pane reads `unknown` when `cwd · branch` wraps). No live pane is in that case. No fixture for it.

## Upstream issue text (desk files this; do not file from this cell)

**Title:** pane reader: a trailing `⧉ review` chip leaves a live Claude Code pane `unknown`

**Body:**

`ClaudeCodeProfile._zones` peels footer lines from the bottom only while they match `_R["footer"]` or a rule. As of `8b3f52bb` (2026-09-24) that regex already includes `^\s*🚨\s+cus\b`, so a `cus SOS:` row is footer and a pane at `❯` under it reads `idle` or `idle_with_draft`. Do not re-file the SOS line.

What is still open: a last-row `⧉ review` chip under `new task? /clear`. It matches neither `_R["footer"]` nor `_R["footer_anchored"]`, the walk stops on row 0, and the pane reads `unknown`. The prompt above it is an empty `❯` and nothing is live, so the state is `idle`. Please treat a trailing `⧉` chip as a footer line. Hook lines (`Stop says`, `UserPromptSubmit says`) are content. The optional "How is Claude doing this session?" rows (`1: Bad`) are not an approval box.

One fixture. claude-usage-swap will assert the same shape against whichever reader its shim runs.

## Checklist

- [ ] Shim runs the newer reader when the vibeCoding checkout and the installed skill are different files.
- [ ] Fixture: SOS screen, empty prompt → idle (newest reader; scrubbed).
- [ ] Fixture: SOS screen, unsent draft → idle_with_draft (scrubbed).
- [ ] Fixture: trailing `⧉ review` chip, empty prompt → idle, marked xfail until the skill lands the pattern (suite stays green). The after-count may still show this one pane, and the row must say why.
- [ ] After-count: `python3 cus.py panes` from this worktree. Before: 43 panes, unknown 9 (idle 21, working 11, idle_with_draft 2). After: paste counts, not rows.
- [ ] Full suite: `pytest tests/ -q` passes.
- [ ] Draft PR. Installed `cus` is not reinstalled.

## Steps after this plan

1. Shim resolution only (newer file when the two candidates differ). Fixtures for the two SOS shapes. Scrub before writing.
2. Chip fixture that records the expected idle verdict. Upstream issue text stays in this plan for the desk; this cell does not file it and does not edit the skill.
3. `python3 cus.py panes` from this worktree; record counts. `pytest tests/ -q`.
4. Draft PR.
