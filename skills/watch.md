Stand up a background watchdog that keeps a chosen set of Claude Code sessions alive, logged-in, and below their usage caps over a stretch of hours or days — using [`cus`](https://github.com/rayistern/claude-usage-swap). Invoke from Claude Code via `/watch` (or copy this file into your own `skills/` and adapt). Pairs with `cus.md` (interactive diagnosis) and `swap.md` (force a swap).

`cus` auto-rotates several Claude OAuth accounts under running sessions so none hits its 5-hour or weekly cap. This skill is the *unattended* companion: you tell it which panes matter, and it runs a fixed-interval health check that (1) confirms those sessions are alive and logged in, (2) reads each backing account's headroom, (3) resolves the common failure modes autonomously where it safely can, and (4) escalates — with exact commands — only the handful of things a human must do (browser logins, hand-edits). It also optionally nudges a stalled session back to work.

This was distilled from a real multi-day weekend watch. The design principle throughout: **do the least intervention that works, prefer letting the daemon self-heal, and never take an irreversible action on a session's behalf.**

> **Posture update 2026-07-07 (operator directive — supersedes "prefer letting the daemon self-heal" above for the attended case):** when the watchdog agent is actively present, **the agent's management takes PRECEDENCE over the daemon — act decisively, do NOT ask permission before a safe at-risk swap, and do NOT defer to the daemon to handle it.** When a protected lane is AT-RISK (within ~5% of the 95% step on ANY of 5h/7d/per-model-Fable), **move it preemptively yourself, now**, rather than waiting for the daemon to swap it at the step. The safety rules below still govern *how* you swap (fresh non-`~` reading — force-poll first; dry-run for clobber-safety; in-place so a live session's context is never reset on an unverified/stale number; never touch locked slots; no `--force`; Escape-only in native prompts) — but *whether* to act on a verified at-risk lane is not a question the operator wants asked. The original "least intervention / let the daemon self-heal" principle still applies to the *unattended* case (headless timer with no agent watching) and to genuinely irreversible actions (browser relogins, hand-edits), which still escalate to a human.

> **LOOK before you report (2026-07-07 — learned from a bad call):** never claim a pane's status ("recovered", "working", "healed") from a single grepped line. A positive-signal line (`● Bash(...)`, `◯ general-purpose ...`, `✻ …`) can be **stale scrollback** left over from *before* a swap, a `/clear`, or a logout — the pane may actually be at an empty `❯` prompt, cleared, or logged out. **Before reporting, full-capture the pane and read its ACTUAL current bottom state**: an empty `❯` prompt (optionally with SessionStart reminders) = idle/cleared, NOT working; a live `◯`/`✻` row with a *ticking* timer at the bottom = working; a `Please run /login`/`/rate-limit-options` menu at the bottom = down. After ANY heal/swap+nudge, verify recovery by reading the pane a few seconds later — do not infer it. Incident: reported tabby-3 "recovered, working (running git)" off a stale `● Bash` line while the pane had actually been `/clear`ed and was empty.

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

---

## Update 2026-07-05 — new failure modes, tooling, and the pool lesson

A full weekday watch surfaced these; fold them into the routine above.

### New recoverable failure: blanked LIVE shared mount (logs out ALL bare sessions at once)

Distinct from `token_stale` and from a lane double-mount: the live shared-mount creds file `~/.claude/.credentials.json` can end up **fully blank** — empty `accessToken`, `expiresAt: 0` — after a shared-mount swap. Every **bare** session on the shared mount then shows "not logged in" simultaneously (arrives as a "the main account got logged out" report). `cus sos` now detects it explicitly ("live shared mount … has no valid token").

Auto-heal each interval:

```bash
# is the live mount blank?
python3 -c "import json,os;o=(lambda d:d.get('claudeAiOauth',d))(json.load(open(os.path.expanduser('~/.claude/.credentials.json'))));print('BLANK' if (not o.get('accessToken') or (o.get('expiresAt') or 0)<=0) else 'OK')"
# if BLANK, restore the ACTIVE account's creds into the live file:
active=$(python3 -c "import json,os;print(json.load(open(os.path.expanduser('~/claude-accounts/state.json')))['active'])")
cus restore-creds "$active" --live
```

Caveats learned the hard way:
- **Restore the currently-active account, not the one you assume.** The shared mount may have swapped under you (seen: merkos→03) — `restore-creds --live` refuses any account that isn't `state.json.active`. Read active first.
- **It's best-effort.** If the newest backup's refresh token was already server-rotated, `cus poll` still shows `TOKEN_EXPIRED` after the restore → that account genuinely needs an interactive `cus relogin <acct>` (escalate). Seen on an account whose 0.6h-old backup was already dead.
- A **daemon-side auto-heal** for this is landing (prevents a swap from blanking the mount *and* auto-restores it each cycle). Once deployed, this manual step is redundant — check whether the daemon already recovered it before intervening.

### The pool lesson: premium crowding is the #1 cause of recurring 429s

`premium`-pool lanes honor the per-model weekly (Fable) gate, which restricts them to the 3–4 Fable-clean accounts. Run several premium lanes and they all crowd those few accounts and burn their **5-hour** windows, while your Fable-capped-but-5h-healthy accounts sit unused — a perpetual 429 loop even with 7 accounts. The new early-warning SOS fires this *before* the 429: **"N premium lane(s) live, 0 valid swap targets."**

Autonomous fix (do it — reversible): unless a lane genuinely runs Fable-model work, make it `standard` so it rotates across ALL accounts by 5h headroom:

```bash
cus pool <slot> standard        # per lane
# and set the launch default so new panes don't re-crowd:  per_session.default_pool: standard
```

Keep `premium` only for the specific lanes pointed at Fable work. If even the standard pool is exhausted, that's genuine capacity shortage → escalate add-accounts, don't churn.

### New tool: in-place lane moves — `cus slot move <slot> <account>`

Moves a live lane onto a target account **in place, session uninterrupted** (claims a distinct login family if the target is already live — including the shared-mount account — or REFUSES rather than clobber). Always `--dry-run` first: verdict `CLAIM` (safe, claims a family) vs `REFUSE` (no free family — don't force) vs `SNAPSHOT` (plain install). Replaces the old `/exit` + `cus launch` dance for "put pane X on account Y"; undo is the reverse move. (Its shared-mount double-book detection was a bug that a dry-run caught — trust the dry-run verdict.)

### Smaller field notes

- **`token_stale` is now auto-refreshed** by the daemon (a `refresh_token` grant, no usage cost). Even more benign than before — do nothing.
- **Stale poll:** an idle account's `5h%` can read a stale `100%` when it has actually reset. `cus force-poll <acct>` for ground truth before acting on a "capped" reading — do not escalate a capped-looking idle account without a fresh poll.
- **72-hour weekly reset:** the `seven_day` cap actually resets ~every 72h (fixed ~04:50–05:00 UTC anchor), *not* every 7 days; `seven_day.resets_at` from the API is misleading. cus now projects the real 72h reset, so "resets in Xh" reflects reality.
- **Autonomy:** for reversible fixes (restore, retag, slot move, config tweak) just do them and log a walk-back — don't escalate a question that a reversible command resolves.
- **New reference:** `docs/DIAGNOSTICS.md` now covers mount topology, the two-dimensional (5h vs per-model-weekly) exhaustion model, the premium/standard split, the blank-mount signature, and the stale-poll gotcha.
