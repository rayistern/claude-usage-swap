Stand up a background watchdog that keeps a chosen set of Claude Code sessions alive, logged-in, and below their usage caps over a stretch of hours or days — using [`cus`](https://github.com/rayistern/claude-usage-swap). Invoke from Claude Code via `/watch` (or copy this file into your own `skills/` and adapt). Pairs with `cus.md` (interactive diagnosis) and `swap.md` (force a swap).

`cus` auto-rotates several Claude OAuth accounts under running sessions so none hits its 5-hour or weekly cap. This skill is the *unattended* companion: you tell it which panes matter, and it runs a fixed-interval health check that (1) confirms those sessions are alive and logged in, (2) reads each backing account's headroom, (3) resolves the common failure modes autonomously where it safely can, and (4) escalates — with exact commands — only the handful of things a human must do (browser logins, hand-edits). It also optionally nudges a stalled session back to work.

This was distilled from a real multi-day weekend watch. The design principle throughout: **do the least intervention that works, prefer letting the daemon self-heal, and never take an irreversible action on a session's behalf.**

---

## When to use

- You're running 2–4 long-lived autonomous sessions and want them protected overnight / over a weekend without babysitting.
- You want one terse status line per interval when all is well, and a spelled-out escalation only when something actually needs you.

Not for: one-off status checks (use `/cus`), or forcing a swap now (use `/swap`).

---

## Setup — one-time, before the loop

1. **Pick the panes to protect and their priority.** Track sessions by tmux **pane id** (stable for the pane's life) or by **tmux session name** (survives a relaunch into a new pane). Decide equal-priority vs. lower-priority — the lower-priority one is the first to shed load if the pool is oversubscribed. Example: `%5 (chats1a)` and `%76 (ratiod2a)` equal; `%70 (tabby-5)` lower.
2. **Confirm the tools exist:** `command -v cus` and `systemctl --user is-active cus.service`. If `cus` is missing, install per `cus.md`.
3. **Schedule the recurring check.** Two options:
   - **`/loop 1h <the check prompt>`** — session-local recurring task; simplest, dies when your Claude session exits. Good for a defined watch window.
   - A `systemd --user` timer or cron calling a headless `claude -p`. Durable across restarts.
   Put the *check routine below* (verbatim, with your pane list substituted) as the recurring prompt. **Make the recurring prompt the bare check — do NOT prefix it with `/loop`,** or each firing re-enters the loop skill and reschedules itself.

---

## The recurring check — run this each interval

### 1. Resolve + health (one command does most of it)

```bash
cus sessions          # per-pane -> slot -> account -> binding, with 5h/7d/per-model % and a plain-words verdict per pane
cus sos; echo "EXIT:$?"
```

`cus sessions` resolves each live pane's TRUE account from the live mount (`/proc` ground truth, not the stale launch-time label), and flags **DRIFT** (state disagrees with reality) and **ORPHAN** slots inline. Use `cus sessions --json` if you want to parse it. This replaces hand-rolled `/proc` loops.

- **A protected pane missing from `cus sessions` (no live pid)** = its Claude process died. This is the #1 alert — `cus` cannot fix it; only a human relaunches. Confirm with `tmux list-panes -a | grep '^%NN '` (shows `bash`, or gone). Report loudly; if you track by session name, re-resolve: `tmux list-panes -t <session> -F '#{pane_id} #{pane_current_command}'` and update your pane list.
- For each **protected** pane, read its account's `5h`, per-model weekly (e.g. `Fable`), and Status.

### 2. Judge GREEN vs. exception

**GREEN** iff: every protected pane is live; `cus sos` exit 0 (or the only SOS items are non-protected / benign — see SOS (d)); each protected pane's account is `5h% < ~90` **and** per-model-weekly `< ~95`; Status `ok` or `TOKEN_STALE`; nothing needlessly paused; and no pane needed a nudge. → Emit **one terse heartbeat line** and stop, nothing more:

```
14:00 ✓ chats1a rayi2 24% · ratiod2a rayi1 55% · tabby5 rayi4 61%
```

Most intervals are green. Keep them one line. Detail only appears when something happened.

**Exception** (anything else) → follow the playbook below, then write a few plain sentences: what was wrong, what you did, and what — if anything — the human must do (spell out exact commands). Write it so it can be read cold hours later.

---

## SOS handling — try to fix autonomously before escalating

**(a) On ANY non-zero SOS, first run the tool's own crash-recovery.** It is SAFE and clears the most common failure — a **slot↔state drift** (a slot whose live identity ≠ its `state.json` account, left by an interrupted swap):

```bash
python3 <cus-repo>/cus.py daemon --once --no-execute
```

This runs the pending-swap recovery: records reality into `state.json` and clears the journal, **without** a real swap. Then re-run `cus sos`. **Caveat learned the hard way:** this only fixes drifts that left a `swap.journal`. A drift with *no* journal will NOT clear this way — the only fix is recording reality in `state.json`, which is a **hand-edit** (see hard rules) → escalate with the exact `slots.<slot>.account = <live account>` change; do not edit it yourself.

**(b) If a real DOUBLE-MOUNT remains** (one account live on 2+ lanes sharing one login family — "families diverged"): identify the doomed mount by token forensics — compare each mount's refresh-token tail + access-token expiry (`<slot>/.credentials.json`) against the account snapshot; the mount whose refresh token does **not** match is the one that dies at its next refresh. **But before escalating: these self-heal.** The daemon rebalances lanes off the crowded account within an interval or two, and a logout of a pane whose work is idle or runs in background processes costs nothing. Only escalate a browser fix if an **equal-priority** pane is *actively losing work* AND it isn't self-healing across two checks.

**(c) No-work-lost fix for a doomed lane** — provision it an independent login. Run the autonomous half yourself:

```bash
python3 <cus-repo>/cus.py login-mount <slot> <account>    # scaffolds the store dir + prints the browser command
```

Then escalate ONLY the interactive part to the human: the `CLAUDE_CONFIG_DIR=<printed path> claude` browser `/login` (log in as the **matching** identity — a wrong-account `--finish` is refused) followed by `login-mount <slot> <account> --finish`. After `--finish`, the live mount adopts the fresh family on its **next swap**.

**(d) Not every SOS is yours.** `TOKEN_STALE` is benign anywhere (the daemon recovers it on next use — do nothing). An SOS whose slots/accounts are **all non-protected** (e.g. a collision between two sessions you don't track, an idle slot, or an account no protected pane is on) → note it as non-protected and move on. `RATE_LIMITED` on an account no protected pane is on is likewise not your problem.

**Root-cause pattern:** if these collisions recur every few hours, the account pool is oversubscribed on **independent login families** relative to concurrent sessions. The durable fix is provisioning more login families (`cus login-mount <account>` × pool_size, then `--finish` each) or running fewer sessions — flag it once, don't re-escalate each recurrence.

---

## Keep-working — optional, nudge a stalled protected pane

If you also want protected sessions to keep *making progress* (not just stay alive), each interval read each pane and judge whether it STALLED:

```bash
tmux capture-pane -t <pane> -p | tail -30
```

A **stall** = alive but idle, having ended its turn when it should keep working (said it'll "pick up tomorrow" / "resume later", asked "what next?", stopped mid-plan, or hit a **transient** API/rate-limit error — not a usage cap — and parked at the prompt). For a stall, send **one** nudge:

```bash
tmux send-keys -t <pane> " Keep going with your task autonomously — don't defer to later, and don't stop to check in unless you're truly blocked." Enter
```

Then re-capture; **if the text is still unsent in the input buffer, send `tmux send-keys -t <pane> Enter` again** — the first Enter sometimes doesn't register while a turn is wrapping up. Confirm it flipped to a live spinner.

**Do NOT nudge if:** actively working (spinner running); **genuinely done** / in a deliberate hold-or-daily-heartbeat mode (leave it, just note it — nudging forces filler work it chose not to do); or showing a **permission / yes-no prompt, tool-approval box, or ambiguous error** — never answer those on the human's behalf; leave it and escalate what it's asking. Max one nudge per pane per interval; if a pane ignores two consecutive nudges, stop and escalate.

To dismiss a session's native rate-limit menu after its window has reset, `tmux send-keys -t <pane> Escape` (cancels back to the prompt — does not select Upgrade/Stop or exit), then nudge.

---

## Hard rules — do NOT violate

- **Never kill/exit a pane or session** (`/exit`, Ctrl-C, closing it). Pausing and continue-nudges are the only keystrokes you send, only to panes you track.
- **Never answer a permission / yes-no / upgrade prompt** on the human's behalf.
- **Never drive an interactive `/login` / `relogin` browser flow** — you can't; your move is to hand the human the exact command.
- **Never hand-edit `state.json` / `.credentials.json` / `.claude.json`** — go through `cus` commands. When only a hand-edit will fix it (no-journal drift), escalate.
- **Never `cus switch --force`** or double-book an account onto a second live mount without a free independent login family (that's the exact clobber this prevents).
- **Never pin a protected pane** — pinning freezes it on its account so it hits the cap instead of being swapped away.

---

## Gotchas learned in the field

- **Stale statuslines.** A pane's own `cus` statusline (account, %) lags until the session has an active turn; an idle pane can show its *previous* account for a while. Trust `cus sessions` (`/proc` ground truth), not the pane's status bar.
- **Double-mounts self-heal.** Resist the urge to escalate every "families diverged" SOS. Watched over a weekend, they consistently cleared on the daemon's next rebalance; the only real casualty was occasional brief logouts of idle panes, which recovered on their own.
- **Background work survives a logout.** Sessions whose actual work runs in background agents / containers keep progressing even while the parent session is logged out — so "logged out" ≠ "work lost." Weigh escalation accordingly.
- **Pane ids drift; session names are steadier.** If a protected pane dies and the human relaunches, it comes back as a *new* pane id. Track by session name when you can, and re-resolve on death.
- **Reference docs:** `docs/RUNBOOK.md`, `docs/DIAGNOSTICS.md`, `docs/TROUBLESHOOTING.md` in this repo cover the swap ladder, the per-session diagnostics view, and the drift/clobber recovery procedures in depth.
