# Pane `unknown` — shapes and where the fix lives

**Date:** 2026-09-25
**Branch:** `fix/pane-unknown-20260925` (from `be98e0b`)

## Before

`cus panes --json` on this box: **43** live panes. States: idle 21, working 11, unknown 9, idle_with_draft 2. No `limit_menu`.

Each `unknown` row was captured with `tmux capture-pane -p` (read only) and classified with the canonical reader (`vibeCoding/skills/build-babysitter/pane_state.py`, `ClaudeCodeProfile`).

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

**Upstream**, in `ClaudeCodeProfile`'s footer pattern (`pane_state.py` in the build-babysitter skill). The zone walk peels only a contiguous footer from the bottom. Both shapes are footer lines the regex does not name:

- a line containing `cus SOS:` (emoji prefix allowed)
- a trailing `⧉` chip under the status cluster (the observed text was `review`)

After those lines peel with the existing `cus` / `⚠ cus:` / `⏵⏵` / `new task?` lines, the `❯` line is found and the current priority order already returns idle, idle_with_draft, or working. Do not treat the optional feedback widget as `approval`.

**Cus's own copy** has no classifier (shim since 2026-09-04). **The call** does not need a new mapping. This repo still has to pass `python cus.py panes` from this worktree, because the installed `cus` must not be reinstalled and the vibeCoding file must not be edited from here. The cus-side change is therefore in the shim: extend that footer pattern on the imported reader, then run it, and keep a fixture per shape under `tests/` so the verdicts stay idle / idle_with_draft. When the skill absorbs the same pattern, the shim goes back to a pure exec. Issue text for that absorption is below; the desk files it.

## Upstream issue text (desk files this; do not file from this cell)

**Title:** pane reader: cus SOS line and the review chip leave a live Claude Code pane `unknown`

**Body:**

`ClaudeCodeProfile._zones` peels footer lines from the bottom only while they match `_R["footer"]` or a rule. `_R["footer"]` already includes `⏵⏵`, `bypass permissions on`, `new task? /clear`, `^\s*cus\s`, and `^\s*⚠ cus:`.

Two lines now sit in that cluster and match neither:

1. `🚨 cus SOS: …` — the alarm emoji is not whitespace, so `^\s*cus\s` misses it. The walk stops, `❯` is never found, and a pane with an empty prompt (or an unsent draft) reads `unknown`.
2. A last-row `⧉ review` chip under `new task? /clear`. The walk stops on row 0.

Please treat both as footer lines (a `cus SOS:` match that allows a prefix, and a trailing `⧉` chip). A pane at `❯` with nothing live stays `idle`; text on `❯` stays `idle_with_draft`; a live spinner row stays `working`. The optional "How is Claude doing this session?" rows (`1: Bad`) are not an approval box. Hook lines (`Stop says`, `UserPromptSubmit says`) are content and need no new state.

Add one fixture per shape. claude-usage-swap will carry the same expectation on its shim until this lands, then drop the shim patch.

## Steps after this plan

1. Shim: extend the footer pattern; fixtures for the SOS gap (idle), the SOS gap with a draft (idle_with_draft), and the `⧉ review` chip (idle). Scrub captures before they are written.
2. Run this worktree's `python cus.py panes` and record counts (no row text) for the PR. Installed `cus` stays on the old checkout.
3. Draft PR. Suite green.
