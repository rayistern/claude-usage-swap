# `cus status --pretty` — rich, width-adaptive status view

**Date:** 2026-07-07
**Status:** Spec approved, awaiting implementation plan

## Problem

`cus status` output is dense plain text: manually padded f-string columns, ISO
timestamps, and load-bearing information squeezed into bracket-note suffixes
(`[next-swap: 75%]`, `[5h est 84% @ +1.2%/min]`) and sub-lines. It is hard to
scan at a glance and does not adapt to terminal width.

## Decisions (from brainstorming)

- **One-shot render.** `--pretty` prints once, sized to the current terminal
  width, then exits. No refresh loop, no alt-screen. (Composable with
  `watch`/tmux; a `--watch` wrapper could be added later but is out of scope.)
- **Re-skin + restructure.** Pretty mode may reorganize content for
  readability (columns instead of bracket notes, merged sections, bars).
  Plain mode stays **byte-identical** to today.
- **rich, lazily imported.** Add `rich>=13.0` to the uv script-header
  dependencies. All rich imports live inside the pretty render path so the
  daemon / statusline hot paths pay zero import cost.

## Code structure

1. **uv header:** add `"rich>=13.0"` to the inline `dependencies` list.
   (First `cus` run after the edit fetches it once via uv; run `cus --version`
   immediately after editing to warm the cache.)
2. **Shared row derivation.** Extract the per-account derivation currently
   inlined in `status()` into a pure helper:

   ```python
   _account_row(name, acct, config, first_step) -> dict
   # keys: flags (list[str]), status_col, est (per-window dict or None),
   #        next_swap_pct (or None when == first_step), per_model (dict),
   #        last_swap_ts, is_disabled
   ```

   Both the plain loop and the pretty renderer consume it. Nothing else about
   the plain path changes; existing status tests
   (`test_token_stale_5h_display`, `test_disabled_accounts`, …) guard
   byte-fidelity.
3. **Flag:** `status()` gains `@click.option("--pretty", is_flag=True,
   help="Rich, width-adaptive tables")`. When set, delegate to
   `_render_status_pretty(state, config)` (new function adjacent to
   `status()`); otherwise run the existing code unchanged.
4. **Staleness semantics carry over exactly** (2026-07-05 incident — these
   markers are load-bearing): `?` for unknown values, dim `NN~` for
   stale-known, per-model numbers marked stale under exactly the same
   condition as the aggregate 7d (`_pct_is_unknown(acct, "current_7d_pct")`).
   Pretty mode renders them via `rich` styles but keeps the literal `?` / `~`
   characters so meaning survives `NO_COLOR` and piping.

## Layout

One `rich.Console()` render. Sections top to bottom:

### Header line (no box)

```
per_session · bare-launch: yaz-myjli-com · ladder 50 → 75 → 90%
```

Mode-dependent phrasing mirrors the plain header (per_session / hybrid /
global). Ladder steps come from `thresholds.steps` as today.

### Accounts table

```
┃ Account          ┃ 5h            ┃ 7d            ┃ 7d by model ┃ Status   ┃ Last swap ┃ Pool     ┃
│ ● yaz-myjli-com  │ ▂▂░░░░░░  7%  │ ▆▆▆▆▆░░  78%  │ Fable 74%   │ ok       │ 8h ago    │ 4/4 free │
```

- Active account marked `●` (replaces the ` *` suffix).
- **Usage bars colored by the ladder steps themselves**, read from config:
  green below the first step, yellow at/above it, red at/above the last step
  (defaults 50/90). The colors literally mean "how close to a swap".
- `Last swap` is relative (`8h ago`) via the existing `_fmt_duration`; `never`
  stays `never`. The ISO timestamp remains available in plain mode.
- **Pool column** folds in the "Login pools" section: `4/4 free`, styled
  yellow when family count is below `independent_logins.pool_size`; empty for
  accounts with no pool. The standalone Login-pools section does not appear in
  pretty mode (the gate-OFF warning line moves under the table as a caption
  when applicable).
- **Estimator divergence** renders inside the pct cell, dim:
  `78% ↗84 +1.2/m` (same ≥1.0 pct divergence trigger as plain mode).
- **Mid-ladder next-swap** renders in the Status cell: `next@75%` (same
  "only when advanced off the first step" rule).
- Flags keep their names (`DISABLED`, `TOKEN_EXPIRED`, `RATE_LIMITED`,
  `POLL_ERROR`, `TOKEN_STALE`); `ok` green, `DISABLED` dim red, others yellow.
- Per-model column sorted highest-first as today; stale per-model values dim
  with trailing `~`.

### Lanes & sessions (merged table)

One row per lane (slot dir on disk), replacing the separate Lanes and
Live-sessions sections:

```
│ Lane    │ Account                    │ State       │ Notes        │ Sessions                     │
│ slot-1  │ yaz-tefillinconnection-org │ live 5 pids │ pool avail   │ 26f23a57 %1 …commons/GabAI   │
│ (bare)  │ yaz-myjli-com              │ —           │ observe-only │ 5c0e416a %3 /home/yaz        │
```

- Session cell: `id[:8] pane cwd`, one per line for multi-session lanes; cwd
  **left-truncated** (`…tail`) since tails are the distinguishing part.
- Sessions are matched to lanes via the same ground truth `status()` uses
  today (`pane_mount_name` in per_session mode; recorded/machine-active
  account otherwise). Bare-mount sessions group under a `(bare)` pseudo-lane
  row, yellow, as the plain `bare` flag is today.
- Lock (`🔒locked`), pool/login annotations (`pool family-1`, `pool avail`,
  `indep-login ✓/MISSING`, `copy`), orphan-dir and state-entry-without-dir
  warnings all carry over into the Notes cell with their current colors.
- Non-per_session modes keep the drift note (`(start:<account>)`) in the
  session cell.

### Locks panel

Same content as plain mode (pins, locked slots, never_restart patterns);
rendered as a small list, only when non-empty.

### Recent swaps table

`8h ago │ from → to │ trigger` — last 5 of N as today, trigger dim.

## Width adaptation

Breakpoints keyed off `Console().width`:

- **≥ 110 cols:** everything, bars included.
- **80–109:** drop the bar glyphs (numbers keep their threshold colors); rich
  shrinks/ellipsizes the session cwd column.
- **< 80:** additionally fold `7d by model` into a second line inside the
  Account cell; residual overflow handled by rich ellipsis.

Rich reads `COLUMNS` when not a TTY, which is also the test hook.

## Degradation & errors

- Non-TTY / piped / `NO_COLOR`: rich degrades automatically — box-drawn
  tables without ANSI codes. `?`/`~` markers keep semantics without color.
- Not initialized: same early exit and message as plain mode, before any rich
  import.
- `--pretty` with an empty/minimal state (no slots, no pools, no sessions):
  sections hide exactly when their plain counterparts hide.

## Testing

New `tests/test_status_pretty.py`, repo conventions (standalone-runnable +
pytest, `CliRunner`, monkeypatched state like `test_token_stale_5h_display`):

1. **Renders clean** on a representative per_session fixture: slots, pools,
   live sessions, one stale account, one DISABLED account, mid-ladder
   next-swap, estimator divergence.
2. **Width behavior:** render at 60 / 100 / 160 cols (via `COLUMNS`); assert
   no output line exceeds the width; bars present only at 160; by-model
   column folds at 60.
3. **Staleness:** `~` and `?` markers present in pretty output for the stale
   fixture; DISABLED flag visible.
4. **Section hiding:** minimal fixture produces no Lanes/Pools/Locks/Swaps
   sections.
5. **Plain fidelity:** existing status tests pass untouched (they exercise
   the `_account_row` extraction through the plain path).

## Out of scope

- `--watch` / live-refresh dashboard.
- Making `--pretty` the TTY default (revisit after it has mileage).
- Pretty variants of other commands (`cus sessions` already has `--json`; a
  pretty pass there is a separate effort).
