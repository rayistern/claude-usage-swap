Stand up a background watchdog that keeps a chosen set of Claude Code sessions alive, logged-in, and below their usage caps over a stretch of hours or days — using [`cus`](https://github.com/rayistern/claude-usage-swap). Invoke from Claude Code via `/watch` (or copy this file into your own `skills/` and adapt). Pairs with `cus.md` (interactive diagnosis) and `swap.md` (force a swap).

`cus` auto-rotates several Claude OAuth accounts under running sessions so none hits its 5-hour or weekly cap. This skill is the *unattended* companion: you tell it which panes matter, and it runs a fixed-interval health check that (1) confirms those sessions are alive and logged in, (2) reads each backing account's headroom, (3) resolves the common failure modes autonomously where it safely can, and (4) escalates — with exact commands — only the handful of things a human must do (browser logins, hand-edits). It also optionally nudges a stalled session back to work.

This was distilled from a real multi-day weekend watch. The design principle throughout: **do the least intervention that works, prefer letting the daemon self-heal, and never take an irreversible action on a session's behalf.**

> **Posture update 2026-07-07 (operator directive — supersedes "prefer letting the daemon self-heal" above for the attended case):** when the watchdog agent is actively present, **the agent's management takes PRECEDENCE over the daemon — act decisively, do NOT ask permission before a safe at-risk swap, and do NOT defer to the daemon to handle it.** When a protected lane is AT-RISK (within ~5% of the 95% step on ANY of 5h/7d/per-model-Fable), **move it preemptively yourself, now**, rather than waiting for the daemon to swap it at the step. The safety rules below still govern *how* you swap (fresh non-`~` reading — force-poll first; dry-run for clobber-safety; in-place so a live session's context is never reset on an unverified/stale number; never touch locked slots; no `--force`; Escape-only in native prompts) — but *whether* to act on a verified at-risk lane is not a question the operator wants asked. The original "least intervention / let the daemon self-heal" principle still applies to the *unattended* case (headless timer with no agent watching) and to genuinely irreversible actions (browser relogins, hand-edits), which still escalate to a human.

> **LOOK before you report (2026-07-07 — learned from a bad call):** never claim a pane's status ("recovered", "working", "healed") from a single grepped line. A positive-signal line (`● Bash(...)`, `◯ general-purpose ...`, `✻ …`) can be **stale scrollback** left over from *before* a swap, a `/clear`, or a logout — the pane may actually be at an empty `❯` prompt, cleared, or logged out. **Before reporting, full-capture the pane and read its ACTUAL current bottom state**: an empty `❯` prompt (optionally with SessionStart reminders) = idle/cleared, NOT working; a live `◯`/`✻` row with a *ticking* timer at the bottom = working; a `Please run /login`/`/rate-limit-options` menu at the bottom = down. After ANY heal/swap+nudge, verify recovery by reading the pane a few seconds later — do not infer it. Incident: reported tabby-3 "recovered, working (running git)" off a stale `● Bash` line while the pane had actually been `/clear`ed and was empty.

> **DEAD ≠ idle, and a FROZEN pane ≠ a live one (2026-07-10 — learned from another bad call):** an agent-count scan (grepping `◯`) cannot tell three different states apart — a **live-idle claude** at an empty `❯`, a **claude that exited to a bare shell**, and a **frozen pane still showing stale content** all read as "0 agents." Two checks close the gap: **(1) Is claude even running under the pane?** A bottom line like `rayi in 🌐 … in ~` `❯` is a **login shell prompt, not claude** — the session is DEAD (exited/crashed/killed), not idle; it needs a **relaunch** (`claude --resume <id>` in the session's cwd + `CLAUDE_CONFIG_DIR`), not a nudge. Confirm with `/proc`: `pid=$(tmux list-panes -t <pane> -F '#{pane_pid}'); pgrep -P $pid` — no `claude`/`node` child = dead shell. A freshly-short `etime` on the pane's `-bash` (e.g. `ps -o etime= -p $pid` → `01:57`) tells you *when* it died. **(2) Has the pane's content actually CHANGED since last cycle?** Identical bottom text across two checks (e.g. the same half-typed `❯ why issues?` for an hour) means the session is idle/dead and you're reading a still frame — do NOT report it as "you're actively driving it" or "working." Diff the capture against last cycle before asserting live interaction. Incident: reported catchup1a "merkos idle, you're driving it" for ~4 cycles off frozen user-text while its claude had actually gone idle at 21:34 and later exited to a bare shell; only a `/proc` check (bash `etime` 1:57) revealed it was DEAD. To recover a DEAD protected pane: find its session id by content-matching transcripts under `<config_dir>/projects/<cwd-encoded>/*.jsonl` (grep a distinctive phrase), then `tmux send-keys -t <pane> 'cd <cwd> && CLAUDE_CONFIG_DIR=<dir> claude --resume <id>' Enter` and verify the title/prompt came back.

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

> **TRACK WHAT'S RUNNING — do NOT watch a hardcoded pane list (2026-07-12 — user directive, learned from a bad miss):** the set of live sessions changes constantly; a fixed list (`tabby-*`, `ratio*`, …) silently drops panes and lets them die uncovered. **Each cycle, DISCOVER every pane that has a live `claude` process** and check all of them — the tracked set is "whatever is running now", recomputed every interval, not a list you carry forward. Enumerate with `cus sessions` (preferred — it already walks live pids) OR directly:
> ```bash
> tmux list-panes -a -F '#{session_name}	#{pane_pid}' | while IFS=$'\t' read s pid; do
>   ch=$(pgrep -P "$pid"); for c in $ch; do grep -qE 'claude|node' /proc/$c/comm 2>/dev/null && { echo "$s"; break; }; done
> done   # every session with a live claude child = a pane you must check this cycle
> ```
> For each discovered pane: resolve its slot/account (`CLAUDE_CONFIG_DIR` in the proc env; **`bare` = on `~/.claude`, outside cus rotation** — flag it, it won't auto-rotate), read that account's 5h/7d/Fable, and scan its bottom state for the block/stall/dead classes. Incident: **torahapi1a (slot-11) sat maxed-out at the `/rate-limit-options` menu for ~24h** because it wasn't in the watcher's hardcoded set (which only covered slots 1/2/4/8/9) — the daemon had already rotated its account to a fresh one, but the frozen pane never retried and nobody dismissed the stale menu. Discovering all 16 live panes (vs the 6 tracked) surfaced it immediately.

> **PARK-AND-SHUFFLE — when "0 valid swap targets" but a WORKING lane is capped (2026-07-14 — user directive, learned from a bad "nothing I can do"):** a Fable-saturated + login-pool-full fleet can make `cus slot move` refuse everywhere ("no free login family" / "0 valid swap targets"), and it's tempting to declare the capped working lane unmovable and just watch it 429. **Don't.** An **idle** lane sitting on a Fable-clean account (e.g. merkos Fab29) is *wasted capacity* — it burns nothing, so it does not need the clean account. **Free that clean family by PARKING the idle lane onto a Fable-MAXED account** (`cus slot move <idle-slot> <maxed-acct-with-a-free-family>` — a maxed account is fine for an idle lane, it won't burn its Fable), **then move the WORKING/capped lane into the freed clean family.** Worked 2026-07-14 when slot-2 + slot-13 were both Fable-limited on maxed-rayi1 with 0 valid targets: parked idle slot-14 (merkos→default) and idle slot-5 (rayi2→rayi5), then moved slot-13→merkos (Fab29) and slot-2→rayi2 (Fab89) into the freed families — which also cleared the "0 valid swap targets" SOS. Rules: park only genuinely-IDLE lanes (0 agents, empty `❯`); target a maxed account that has a **free login family** (dry-run to confirm SNAPSHOT/CLAIM, never a pool-exhausted install that blanks the mount — see the merkos/rayi2 blank-hazard); after the shuffle **verify the working lane's creds are valid (not blanked)** and nudge it to retry. Capacity is conserved, just re-allocated from idle → working. If EVERY account is both maxed AND full (no idle lane on any clean account to displace), that's genuine exhaustion — escalate `cus login-mount <clean-acct>` (browser) or ride the weekly reset.

> **A NUDGE ISN'T SENT UNTIL YOU PRESS ENTER — and you MUST verify it submitted (2026-07-14 — user: "your nudge failed because you didn't press enter"):** `tmux send-keys -t <pane> " …message…" Enter` frequently TYPES the message into Claude Code's input box but the trailing `Enter` races the TUI's input debounce and never registers — the text just sits there at `❯ …message…` un-submitted, and the session does nothing. **Send the Enter as a SEPARATE keystroke after a beat, then READ the pane to confirm it fired:** `tmux send-keys -t <pane> " …message…"; sleep 1; tmux send-keys -t <pane> Enter; sleep 3; tmux capture-pane -t <pane> -p | tail -6`. Success = the input box is now empty (`❯ `) AND a `✻ …/◯ …` working row appeared (it re-submitted and is churning). Failure = the message still sits in the `❯` box → press Enter again / re-send. NEVER report a nudge as done off the send-keys return code — that only means keystrokes were delivered to tmux, not that the message was submitted or that the session resumed. (This is the same "verify after, don't infer" rule as swaps — it applies to nudges too.)

> **FIX A LIVE STUCK / LOGGED-OUT / WALLED PANE WITH `cus slot move` + A NUDGE — NEVER `tmux kill-session` (2026-08-07 — user directive: "you're supposed to use cus commands and then nudge the pane. You don't have to kill session"):** when a **LIVE** pane (claude still running under it) shows `Not logged in` / `401` / a Fable wall / blanked mount creds, the fix is two steps and **never a process restart**: **(1) `cus slot move <slot> <clean-Max-acct>`** — self-refuses on clobber; claim-verifies + rotates tokens + installs a fresh #109 login family, rewriting the live mount's creds; a same-account move is a no-op, so move to a *different* clean account. Then **(2) nudge the pane** — `send-keys`, signed `[automated cus watchdog, not Rayi]`, Enter-verified per the NUDGE rule above. The running claude **re-reads the now-fresh credentials file on its next attempt** and clears the stale `Not logged in`/wall. A cached bad token does **NOT** require restarting the process. **Do NOT `tmux kill-session` + `claude --resume` a LIVE pane** — that tears down the whole tmux window (indistinguishable from a crash to the operator — "maybe that's the secret to crashes"), **interrupts the pane's in-progress work, and a fresh relaunch does not resume where the user was holding, silently destroying their state.** Incident 2026-08-07: I killed + fresh-restarted 2zajac1a to "unstick" a logout; the operator saw it "crash the moment you did whatever you did" and lost held work — the correct move was `cus slot move slot-9 03` + a nudge. **This supersedes any earlier "stuck-cached token → restart the process" guidance for LIVE panes.** The `claude --resume` relaunch (line 11 / DEAD-pane recovery) is ONLY for a genuinely DEAD/GONE pane — claude already exited to a bare shell, or `tmux has-session` is false, so there is nothing to nudge; **confirm the pane is actually gone before recreating it.**

> **AFTER A SWAP, IMMEDIATELY TELL THE PANE'S SESSION YOU SWAPPED IT — or it self-swaps and you fight (2026-07-15 — user directive: "you have to tell the other session that you already swapped, otherwise it starts swapping on its own… and you have to tell it right away"):** every live pane runs its OWN session that manages its OWN account (babysitter / self-heal / its own cus logic). When YOU (the watchdog) move that pane's account from outside via `cus slot move`, the pane's session has no idea — it still thinks it's on the old account and its own management ALSO tries to swap/heal, so the two swap against each other (churn + token-rotation divergence). **So the moment you `cus slot move` a LIVE (non-exited) pane, in the SAME turn, send that pane a notice.** This applies to ANY live-pane swap — a preemptive at-risk move too, not just at a limit menu. Do NOT notify EXITED/parked panes (no session to fight you). Right away, same turn as the move.
>
> **BUT — injected messages read as if RAYI typed them, so SIGN them and VERIFY they make sense (2026-07-16 — user correction: "if you're going to send stupid messages to sessions without checking if they make sense, at least sign off that you're an ai not me"):** `tmux send-keys` puts your text into the pane's input box as a **user turn** — the session interprets it as if the human operator typed it. So (1) **ALWAYS sign the message as automated**, e.g. prefix `[automated cus-watchdog message — NOT from Rayi]`, so no session mistakes it for the human; (2) **only send content you've VERIFIED is true for THAT session** — the always-safe factual notice is `"[automated cus-watchdog message — NOT from Rayi] Your account was swapped to <acct> by the watchdog (the old one was near its cap); retry the step if it errored."` Do NOT tell a session to "stop self-swapping / don't run cus slot move" unless you've actually SEEN that session run a swap in its scrollback — an account's 5h climbing can be cached-token drift or daemon re-placement, not the session, so that instruction is often false and confusing; (3) **when unsure, don't inject at all** — a silent cred swap + natural re-auth beats a wrong message. Send text, `sleep 1`, `Enter` as a SEPARATE keystroke, `sleep 3`, then read the pane to confirm it submitted ([[nudge-only-stalled-mid-task]] press-Enter rule).

> **Fable-5 SOFT limit is a distinct failure class the block-scan must catch (2026-07-14):** the hard block you grep for is `/rate-limit-options` / `❯ 1. Stop and wait` / `Upgrade your plan`. But hitting a per-model cap shows a DIFFERENT, softer message — **`You've reached your Fable 5 limit. Run /usage-credits to continue or switch models with /model`** (and the "thinking" verb `Brewed for …`) — rendered as a `⎿` tool-output block ABOVE the input prompt, so a `tail -5` capture at the idle `❯` MISSES it and the pane reads "clean/idle." Add these strings to the scan AND capture the last ~15-18 lines (not 5) so a `⎿` limit block above the prompt is seen. Also: when diagnosing WHICH account a limited pane is really burning, **trust the pane's own live cus statusline (`🔒slot-N acct*`) over `cus sessions` and over disk `oauthAccount`** — the running process caches its token, so it can be authing to (and capping on) a different account than state/disk claim; only the statusline reflects the live token.

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

> **"snapshot" ALWAYS means a FULL snapshot (2026-07-15 — user directive):** when the operator asks for "a snapshot" (or "snapshot please"), that is NOT the terse heartbeat line — it means the **complete fleet + lane picture**, and it must include **per-account 5h %, 7d %, per-model Fable %, AND reset ETAs for BOTH the 5h window and the 7d window** (the 7d/Fable weekly reset time is explicitly required — the user called this out). Render it as: (1) an SOS one-liner (benign flags noted as such); (2) a fleet table — every account with 5h / 7d / Fable + 5h-reset ETA + 7d-reset ETA (show the 72h-projected 7d reset; mark Fable-clean accounts, i.e. Fable < ~90); (3) a live-lane table — each live premium/work pane → its slot, account, 5h %, Fable %, and whether it's working vs exited/parked (a pane showing `Resume this session with: claude --resume` at the bottom = exited). Compute reset ETAs from `state.json` `five_hour_resets_at` and the 72h-projected `seven_day_resets_at` (see the [[fable5-soft-limit-and-statusline-groundtruth]] caveat: the 72h/7d projection does NOT reliably predict the per-model FABLE reset — only an actual force-poll drop confirms Fable freed, so label the 7d ETA as an estimate). Don't abbreviate a snapshot down to the heartbeat line — the operator asked for the full board on purpose. **`python3 <cus-repo>/skills/watch_tables.py` renders the fleet + panes tables (both 7d-reset columns) for you** — the same helper the per-tick report uses (see the REPORT FORMAT directive above); add the SOS one-liner above it for a full snapshot.

> **REPORT FORMAT — every tick emits TWO compact markdown tables, not just the terse line (2026-07-20 — operator directive):** the operator asked that each interval's report show an **accounts table** (fleet headroom + reset ETAs) and an **active-panes table** (protected sessions + the account each rides) at a glance, every tick — not only on an explicit "snapshot". A helper renders both from ground truth so you don't hand-build them:
> ```bash
> python3 <cus-repo>/skills/watch_tables.py                         # default active panes
> python3 <cus-repo>/skills/watch_tables.py torah2a trisso4 ratioef1a   # or name them (session name or %pane id)
> ```
> It reads `cus sessions --json` (live pane→slot→account, pool, 5h/7d/Fable, drift) + `state.json` (reset timestamps) + `config.yaml` (disabled accounts), and prints:
> - **Accounts table** — every account sorted cleanest-Fable-first, with 5h / 7d / Fable %, plus THREE reset ETAs: `5h reset`, **`7d reset (72h)`** (the projected real refresh cus rotates on — the one that matters), and `7d reset (API)` (raw `seven_day_resets_at`, ~7d out, misleading — shown only for comparison). Accounts hosting an active pane are **bold** with a `← pane` marker; disabled accounts show `⛔ DISABLED`.
> - **Active-panes table** — each protected pane → id / slot / pool / account / 5h / 7d / Fable + a status word. A pane `cus sessions` can't resolve (orphan slot) is `/proc`-resolved from its claude child's `CLAUDE_CONFIG_DIR` → `state.json` slots map (shown with a `*` on the slot + "proc-resolved").
> - **Status words** (shared by both tables): `✓ CLEAN` (Fable <10), `✓ headroom`, `⚠ Fable high` (≥90), `⛔ Fable at gate` (≥97, premium daemon swaps here), `⚠ 5h hot` (≥90), `⛔ DISABLED`. A pane row can also read `⛔ GONE (crashed?)` — that's the loud crash alert.
> This does NOT replace the exception playbook: still judge GREEN-vs-exception and act on at-risk lanes; the tables are the *reporting surface*, the terse `HH:MM ✓` line is now the one-line header ABOVE the tables. On a green tick: header line + both tables. On an exception: header + tables + the plain-sentences write-up of what you did.

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

## Update 2026-09-04 — the build-babysitter layer + transcript ground truth

Two additions from the flagship-site retrospective (2026-09-02 → 04), both additive to
this skill:

- **`build-babysitter` skill** (`~/repos/vibeCoding/skills/build-babysitter/`, PR #330):
  the *momentum* companion to this *keep-alive* skill. Where `watch` keeps sessions
  alive, logged in and under cap, the babysitter holds the owner's chair over ONE build
  family — next-item nudges instead of bare "keep going", the D-queue defaults with a
  logged trail, owner-lens QA on a cadence, and the effort scorecard at the end. It
  **uses** this skill's mechanics (dead-pane relaunch, `cus slot move` + nudge, the
  sign-and-verify rules) and does not restate them. Load both when a build is running
  unattended.
- **Session state from transcripts, not panes.** `python3
  ~/repos/context-dashboard/ingest/session_metrics.py <family-slug> --live` (PR #60)
  prints each owner-prompted session with a state judged from its transcript's last
  assistant message — `working` / `parked` / `died_limit` / `died_login` /
  `died_killed` / `cut_off` — plus idle minutes and last words. This is immune to the two
  failure modes above (stale scrollback, 2026-07-07; the Fable soft-limit `⎿` block
  above the prompt that `tail -5` misses, 2026-07-14). Pane capture is still needed to
  confirm a pane is DEAD before relaunching (login shell at the bottom, no claude child)
  and to verify a nudge submitted — the sensor tells you *what* stopped and *why*, the
  pane tells you *whether a process is there to nudge*.
