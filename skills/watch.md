Stand up a background watchdog that keeps a chosen set of Claude Code sessions alive, logged-in, and below their usage caps over a stretch of hours or days — using [`cus`](https://github.com/rayistern/claude-usage-swap). Invoke from Claude Code via `/watch` (or copy this file into your own `skills/` and adapt). Pairs with `cus.md` (interactive diagnosis) and `swap.md` (force a swap).

`cus` auto-rotates several Claude OAuth accounts under running sessions so none hits its 5-hour or weekly cap. This skill is the *unattended* companion: you tell it which panes matter, and it runs a fixed-interval health check that (1) confirms those sessions are alive and logged in, (2) reads each backing account's headroom, (3) resolves the common failure modes autonomously where it safely can, and (4) escalates — with exact commands — only the handful of things a human must do (browser logins, hand-edits). It also optionally nudges a stalled session back to work.

This was distilled from a real multi-day weekend watch. The design principle throughout: **do the least intervention that works, prefer letting the daemon self-heal, and never take an irreversible action on a session's behalf.**

> **Identifiers below are generic placeholders** (`acct-A`, `sess-A`, `<session-id>`, `<user>`, `~/repos/<project>`) — substitute your own. Placeholder letters are scoped to **each worked example**, not one fleet-wide legend: the same letter in two different dated examples may be two different real accounts.

> **Posture update 2026-09-16 (operator directive — NARROWS the 2026-07-07 block below): THE DAEMON DOES THE SWAPS. You are a WATCHDOG, not a scheduler.** Operator: *"the daemon should be doing the swaps, no? Unless we're moving so fast or no lanes are available and we have to shuffle or something."* The 2026-07-07 "act decisively, don't ask" directive was about not stopping to ask permission on a genuinely at-risk lane — it was **never** a license to hand-place lanes every tick. `cus.service` already rotates lanes on the ladder steps, and it does so with a full view of the fleet; a watchdog that swaps on top of it fights it, busts prompt caches, and churns token families ([[credential-death-cascade-and-backoff]]).
>
> **Swap yourself ONLY when the daemon demonstrably cannot:** (a) it is dead or stuck (check `systemctl --user show cus.service -p MainPID`), (b) `cus sos` shows **0 valid swap targets** and a park-and-shuffle is the only way to free one, (c) an operator explicitly asks for a specific placement, or (d) a lane is AT the wall NOW and the daemon's next cycle is too late. Otherwise: **read, report, and let the daemon act.** Note what you would have done in the tick report so the operator can see the call you did not make.
>
> **Precedence over the 2026-07-07 block below, stated explicitly (2026-09-18 — blind review found the two blocks giving opposite orders for the most common tick decision):** for a lane that is AT RISK but **not yet walled**, THIS block wins — the daemon swaps it at the ladder step, and you report the call you did not make. The 2026-07-07 "move it preemptively yourself, now" survives only where the daemon provably cannot act: its four exceptions (a)-(d) above, **plus** the case the daemon is structurally blind to — **a LOCKED lane, which `decide_slot_swaps` drops before it plans anything** (`cus.py`: it gates on `session_locks.locked_slots` and prints `skip <slot>: locked`). A locked at-risk lane will NOT be rescued by anyone but you; treat it as case (d) and move it yourself. That includes your own watchdog slot.
>
> **PINNED is NOT the same as locked — do not hand-move a pinned lane (corrected 2026-09-18, verified in `cus.py`):** `session_is_pinned` is consumed only by the hot-swap/global paths; `decide_slot_swaps` never consults it, so in slot mode **the daemon still rotates a pinned pane's slot on the ladder**. Treating "pinned" as daemon-blind produces exactly the double-swap churn this block exists to prevent.
>
> **The legal mechanics for moving a locked lane** (the how-rules below still say "never touch locked slots; no `--force`", and they are narrowed, not waived, by this paragraph): `cus slot move` refuses a locked slot outright unless forced, and `--force` is banned because it also bypasses the #104 double-book guard. So the only sanctioned sequence is **`cus unlock <slot>` → `cus slot move <slot> <acct>` → `cus lock <slot>`**, back-to-back in the same turn so the slot is never left unlocked across ticks. If the lane is not yours and its owner is reachable, prefer telling them over unlocking someone else's deliberate freeze. Incident 2026-09-16: the watchdog performed five hand-placements in ninety minutes and concentrated seven panes onto one account, which then capped — every one of those moves was inside the daemon's own remit.

> **Posture update 2026-07-07 (operator directive — supersedes "prefer letting the daemon self-heal" above for the attended case):** when the watchdog agent is actively present, **the agent's management takes PRECEDENCE over the daemon — act decisively, do NOT ask permission before a safe at-risk swap, and do NOT defer to the daemon to handle it.** When a protected lane is AT-RISK (within ~5% of the 95% step on ANY of 5h/7d/per-model-Fable), **move it preemptively yourself, now**, rather than waiting for the daemon to swap it at the step. The safety rules below still govern *how* you swap (fresh non-`~` reading — force-poll first; dry-run for clobber-safety; in-place so a live session's context is never reset on an unverified/stale number; never touch locked slots; no `--force`; Escape-only in native prompts) — but *whether* to act on a verified at-risk lane is not a question the operator wants asked. The original "least intervention / let the daemon self-heal" principle still applies to the *unattended* case (headless timer with no agent watching) and to genuinely irreversible actions (browser relogins, hand-edits), which still escalate to a human.

> **LOOK before you report (2026-07-07 — learned from a bad call):** never claim a pane's status ("recovered", "working", "healed") from a single grepped line. A positive-signal line (`● Bash(...)`, `◯ general-purpose ...`, `✻ …`) can be **stale scrollback** left over from *before* a swap, a `/clear`, or a logout — the pane may actually be at an empty `❯` prompt, cleared, or logged out. **Before reporting, full-capture the pane and read its ACTUAL current bottom state**: an empty `❯` prompt (optionally with SessionStart reminders) = idle/cleared, NOT working; a live `◯`/`✻` row with a *ticking* timer at the bottom = working; a `Please run /login`/`/rate-limit-options` menu at the bottom = down. After ANY heal/swap+nudge, verify recovery by reading the pane a few seconds later — do not infer it. Incident: reported sess-X "recovered, working (running git)" off a stale `● Bash` line while the pane had actually been `/clear`ed and was empty.

> **DEAD ≠ idle, and a FROZEN pane ≠ a live one (2026-07-10 — learned from another bad call):** an agent-count scan (grepping `◯`) cannot tell three different states apart — a **live-idle claude** at an empty `❯`, a **claude that exited to a bare shell**, and a **frozen pane still showing stale content** all read as "0 agents." Two checks close the gap: **(1) Is claude even running under the pane?** A bottom line like `<user> in 🌐 … in ~` `❯` is a **login shell prompt, not claude** — the session is DEAD (exited/crashed/killed), not idle; it needs a **relaunch** (`claude --resume <id>` in the session's cwd + `CLAUDE_CONFIG_DIR`), not a nudge. Confirm with `/proc`: `pid=$(tmux list-panes -t <pane> -F '#{pane_pid}'); pgrep -P $pid` — no `claude`/`node` child = dead shell. A freshly-short `etime` on the pane's `-bash` (e.g. `ps -o etime= -p $pid` → `01:57`) tells you *when* it died. **(2) Has the pane's content actually CHANGED since last cycle?** Identical bottom text across two checks (e.g. the same half-typed `❯ why issues?` for an hour) means the session is idle/dead and you're reading a still frame — do NOT report it as "you're actively driving it" or "working." Diff the capture against last cycle before asserting live interaction. Incident: reported sess-Y "acct-A idle, you're driving it" for ~4 cycles off frozen user-text while its claude had actually gone idle at 21:34 and later exited to a bare shell; only a `/proc` check (bash `etime` 1:57) revealed it was DEAD. To recover a DEAD protected pane: find its session id by content-matching transcripts under `<config_dir>/projects/<cwd-encoded>/*.jsonl` (grep a distinctive phrase), then `tmux send-keys -t <pane> 'cd <cwd> && CLAUDE_CONFIG_DIR=<dir> claude --resume <id>' Enter` and verify the title/prompt came back.

---

## When to use

- You're running 2–4 long-lived autonomous sessions and want them protected overnight / over a weekend without babysitting.
- You want one terse status line per interval when all is well, and a spelled-out escalation only when something actually needs you.

Not for: one-off status checks (use `/cus`), or forcing a swap now (use `/swap`).

---

## Setup — one-time, before the loop

1. **Pick the panes to protect and their priority.** Track sessions by tmux **pane id** (stable for the pane's life) or by **tmux session name** (survives a relaunch into a new pane). Decide equal-priority vs. lower-priority — the lower-priority one is the first to shed load if the pool is oversubscribed. Example: `%5 (sess-A)` and `%76 (sess-B)` equal; `%70 (sess-Z)` lower.
2. **Confirm the tools exist:** `command -v cus` and `systemctl --user is-active cus.service`. If `cus` is missing, install per `cus.md`.
3. **Schedule the recurring check.** Two options:
   - **`/loop 1h <the check prompt>`** — session-local recurring task; simplest, dies when your Claude session exits. Good for a defined watch window.
   - A `systemd --user` timer or cron calling a headless `claude -p`. Durable across restarts.
   Put the *check routine below* (verbatim, with your pane list substituted) as the recurring prompt. **Make the recurring prompt the bare check — do NOT prefix it with `/loop`,** or each firing re-enters the loop skill and reschedules itself.

---

## The recurring check — run this each interval

> **TRACK WHAT'S RUNNING — do NOT watch a hardcoded pane list (2026-07-12 — user directive, learned from a bad miss):** the set of live sessions changes constantly; a fixed list (`sess-*`, `work-*`, …) silently drops panes and lets them die uncovered. **Each cycle, DISCOVER every pane that has a live `claude` process** and check all of them — the tracked set is "whatever is running now", recomputed every interval, not a list you carry forward. Enumerate with `cus sessions` (preferred — it already walks live pids) OR directly:
> ```bash
> tmux list-panes -a -F '#{session_name}	#{pane_pid}' | while IFS=$'\t' read s pid; do
>   ch=$(pgrep -P "$pid"); for c in $ch; do grep -qE 'claude|node' /proc/$c/comm 2>/dev/null && { echo "$s"; break; }; done
> done   # every session with a live claude child = a pane you must check this cycle
> ```
> For each discovered pane: resolve its slot/account (`CLAUDE_CONFIG_DIR` in the proc env; **`bare` = on `~/.claude`, outside cus rotation** — flag it, it won't auto-rotate), read that account's 5h/7d/Fable, and scan its bottom state for the block/stall/dead classes. Incident: **sess-D (slot-11) sat maxed-out at the `/rate-limit-options` menu for ~24h** because it wasn't in the watcher's hardcoded set (which only covered slots 1/2/4/8/9) — the daemon had already rotated its account to a fresh one, but the frozen pane never retried and nobody dismissed the stale menu. Discovering all 16 live panes (vs the 6 tracked) surfaced it immediately.

> **PARK-AND-SHUFFLE — when "0 valid swap targets" but a WORKING lane is capped (2026-07-14 — user directive, learned from a bad "nothing I can do"):** a Fable-saturated + login-pool-full fleet can make `cus slot move` refuse everywhere ("no free login family" / "0 valid swap targets"), and it's tempting to declare the capped working lane unmovable and just watch it 429. **Don't.** An **idle** lane sitting on a Fable-clean account (e.g. acct-A Fab29) is *wasted capacity* — it burns nothing, so it does not need the clean account. **Free that clean family by PARKING the idle lane onto a Fable-MAXED account** (`cus slot move <idle-slot> <maxed-acct-with-a-free-family>` — a maxed account is fine for an idle lane, it won't burn its Fable), **then move the WORKING/capped lane into the freed clean family.** Worked 2026-07-14 when slot-2 + slot-13 were both Fable-limited on maxed-acct-C with 0 valid targets: parked idle slot-14 (acct-A→acct-B) and idle slot-5 (acct-D→acct-G), then moved slot-13→acct-A (Fab29) and slot-2→acct-D (Fab89) into the freed families — which also cleared the "0 valid swap targets" SOS. Rules: park only genuinely-IDLE lanes (0 agents, empty `❯`); target a maxed account that has a **free login family** (dry-run to confirm SNAPSHOT/CLAIM, never a pool-exhausted install that blanks the mount — see the acct-A/acct-D blank-hazard); after the shuffle **verify the working lane's creds are valid (not blanked)** and nudge it to retry. Capacity is conserved, just re-allocated from idle → working. If EVERY account is both maxed AND full (no idle lane on any clean account to displace), that's genuine exhaustion — escalate `cus login-mount <clean-acct>` (browser) or ride the weekly reset.

> **A NUDGE ISN'T SENT UNTIL YOU PRESS ENTER — and you MUST verify it submitted (2026-07-14 — user: "your nudge failed because you didn't press enter"):** `tmux send-keys -t <pane> " …message…" Enter` frequently TYPES the message into Claude Code's input box but the trailing `Enter` races the TUI's input debounce and never registers — the text just sits there at `❯ …message…` un-submitted, and the session does nothing. **Send the Enter as a SEPARATE keystroke after a beat, then READ the pane to confirm it fired:** `tmux send-keys -t <pane> " …message…"; sleep 1; tmux send-keys -t <pane> Enter; sleep 3; tmux capture-pane -t <pane> -p | tail -6`. Success = the input box is now empty (`❯ `) AND a `✻ …/◯ …` working row appeared (it re-submitted and is churning). Failure = the message still sits in the `❯` box → press Enter again / re-send. NEVER report a nudge as done off the send-keys return code — that only means keystrokes were delivered to tmux, not that the message was submitted or that the session resumed. (This is the same "verify after, don't infer" rule as swaps — it applies to nudges too.)

> **FIX A LIVE STUCK / LOGGED-OUT / WALLED PANE WITH `cus slot move` + A NUDGE — NEVER `tmux kill-session` (2026-08-07 — user directive: "you're supposed to use cus commands and then nudge the pane. You don't have to kill session"):** when a **LIVE** pane (claude still running under it) shows `Not logged in` / `401` / a Fable wall / blanked mount creds, the fix is two steps and **never a process restart**: **(1) `cus slot move <slot> <clean-Max-acct>`** — self-refuses on clobber; claim-verifies + rotates tokens + installs a fresh #109 login family, rewriting the live mount's creds; a same-account move is a no-op, so move to a *different* clean account. Then **(2) nudge the pane** — `send-keys`, signed `[automated cus watchdog, NOT the operator]`, Enter-verified per the NUDGE rule above. The running claude **re-reads the now-fresh credentials file on its next attempt** and clears the stale `Not logged in`/wall. A cached bad token does **NOT** require restarting the process. **Do NOT `tmux kill-session` + `claude --resume` a LIVE pane** — that tears down the whole tmux window (indistinguishable from a crash to the operator — "maybe that's the secret to crashes"), **interrupts the pane's in-progress work, and a fresh relaunch does not resume where the user was holding, silently destroying their state.** Incident 2026-08-07: I killed + fresh-restarted sess-E to "unstick" a logout; the operator saw it "crash the moment you did whatever you did" and lost held work — the correct move was `cus slot move slot-9 acct-I` + a nudge. **This supersedes any earlier "stuck-cached token → restart the process" guidance for LIVE panes.** The `claude --resume` relaunch (line 11 / DEAD-pane recovery) is ONLY for a genuinely DEAD/GONE pane — claude already exited to a bare shell, or `tmux has-session` is false, so there is nothing to nudge; **confirm the pane is actually gone before recreating it.**

> **DEFAULT IS: DO NOT INJECT. READ THE PANE FIRST — a nudge is a last resort, not a probe (2026-09-16 — user directive: "you don't have to nudge unless you have to nudge"):** every `tmux send-keys` into a pane costs that session a **real user turn** — it burns tokens on the very account you are trying to protect, busts its prompt cache, and can **wake an idle lane straight into a wall**. Incident 2026-09-16: the watchdog parked an idle lane onto a 5h-maxed account (correct), then injected the routine post-swap notice below (incorrect) — the notice made the idle session take a turn and 429 immediately, converting a harmless park into a 2-hour outage. **So: never inject to FIND OUT what a pane is doing, and never inject merely because something happened to it.**
>
> **You already have two read-only ways to know a pane's state — use them instead:**
> 1. **`python3 skills/pane_state.py <names>`** — gives `state`, `unchanged_for_s`, `bg_agents`, `input_draft` without touching the pane.
> 2. **Timestamps + the inline banner.** Claude Code stamps a **timestamp after every message**, so the pane and its transcript already tell you *when* it last acted and *why* it stopped. A usage pause announces itself literally — `⚠ Usage limit reached · continuing automatically at 11:20pm` — and names its own resume time. **A usage-paused pane needs NOTHING from you: it resumes itself.** Reading it is free; nudging it is not.
>
> **THE ONE CASE WHERE YOU MUST NUDGE — a wall banner outlives the wall (2026-09-16 — operator: "you may need to nudge in such a situation btw"):** when a pane hits a usage cap it parks itself behind `⚠ Usage limit reached · continuing automatically at <time>` (or the `1. Stop and wait / 2. Wait here... / 3. Upgrade` menu) and **sleeps on a wall-clock timer**. If you then move that lane onto a clean account, **the pane does not know** — it keeps sleeping until its original time, potentially hours later, on an account that has full headroom. Nothing wakes it but you. So: **after swapping a lane that is parked at a wall, cancel the wait and nudge it.** The banner itself says how (`esc or type to cancel`). **Use the SAFE ORDER — message first, Escape only if the banner still blocks submission** (corrected 2026-09-18; the original text here said Escape first, which is exactly the sequence the 2026-09-17 rule below shows converting a self-healing pause into a permanent stall if the follow-up message fails to submit): type the signed message, `sleep 1`, `Enter` as a separate keystroke, `sleep 3`, then READ the pane. Only if it did not submit do you press `Escape` and re-send immediately — never press Escape and then leave the tick without a verified real turn. Worked 2026-09-16: 2connect1a was sleeping until 2:20am; after `cus slot move slot-7 rayi5` + a nudge (that run used Escape first — the order since corrected above) it resumed immediately on a 0%/0% account, ~4h early. **Note the asymmetry:** a pane that merely 429'd and is retrying, or is idle at an empty `❯`, needs nothing — only a pane PARKED BEHIND A TIMER whose premise you just invalidated does. Check first: `tmux capture-pane -t <pane> -p | grep -iE 'Usage limit|continuing automatically|Stop and wait'` — no match means no nudge.
>
> **`pane_state.py` CAN MISREPORT A WALL-PARKED PANE AS `working` — settle it with an unchanged-recheck (2026-09-17):** a parked pane renders `● Usage limit reached · continuing automatically at <time> · esc or type to cancel` as a bullet row, and the reader can score that as a live spinner. Incident 2026-09-17: 8jira2a reported `state=working, waiting_for_agents=2` while it was in fact asleep since 10:18 PM behind a 2:20am timer, on an account that had since been swapped to 0%/0%. **Do not let a `working` verdict veto a live bottom-of-pane banner.** When the two disagree, capture the pane twice ~20s apart: **byte-identical bottom + an empty `❯` + a completed `✻ …· done <time>` row = PARKED**, regardless of what the reader said. A genuinely working pane changes (ticking timer, token counter, new rows) within 20 seconds. The tell that the wall is STALE rather than current: the pane's own cus statusline names an account with headroom (`🔒slot-8 rayi4* 5h:0% 7d:0%`) while the banner cites a limit hit on the previous one. After a nudge (and any Escape), allow ~30s before concluding it did not resume — the first re-read can still read `idle` while the turn spins up.
>
> **ESCAPE CANCELS THE AUTO-CONTINUE — so a half-landed nudge leaves the pane WORSE than you found it (2026-09-17):** the wall banner's own timer (`continuing automatically at 7:30am`) is a real self-recovery mechanism. Pressing `Escape` kills it: the pane prints `● Automatic continue cancelled · /rate-limit-options to re-arm` and will now sit at an empty `❯` **forever**, where before it would have resumed by itself. So if the Escape lands but the follow-up message does NOT submit, you have converted a self-healing pause into a permanent stall. Incident 2026-09-17: 4mainsite1a was Escape+nudged at 04:10Z, the nudge produced only `✻ Worked for 0s · done 4:02 AM` and the pane sat idle for 27 minutes on an account with full headroom — and the tick report wrongly called it "running" because the footer said `2 shells`. **Two rules follow:** (1) **`N shells` / `N monitors` in the footer are BACKGROUND processes, not evidence the session is working** — they keep running through a wall and through an idle prompt; only a live spinner row or a fresh `✻ …` with a ticking timer means working. (2) **After any Escape+nudge, re-read the pane and require a REAL turn**: a `✻ Worked for 0s` line, or `Automatic continue cancelled` sitting above an empty `❯`, means the message never submitted — re-send it immediately, do not leave the tick. Prefer sending the message FIRST and only pressing Escape if the banner is still blocking submission, so you never cancel the timer without replacing it.
>
> **PARSE `pane_state.py` DEFENSIVELY — some lines have NO `session` key, and a crash silently skips the whole fleet (2026-09-17):** the reader emits `{"pane": "<name>", "state": "not_found"}` for a name that matches no pane, and `{"error": …}` on a whole-tmux failure. Neither carries `session`, so the obvious one-liner (`d['session']`) raises `KeyError` on the FIRST such line and every remaining pane goes unreported — the tick looks like it passed while nothing was actually checked. Incident 2026-09-17: a `not_found` line crashed the parser and masked TWO real findings that only surfaced on a defensive re-run — a pane that had been RENAMED (`2jira1a` → `2jira2a`) and another whose claude had exited to a bare shell (`4jira1a`, `state=dead`). **Always `if 'session' not in d: report the raw line and continue`, and treat a `not_found` as "renamed or gone — resolve it against `tmux list-sessions` and `~/.claude/pane-sessions/`", never as "fine".**
>
> **A `dead` pane is not automatically a pane to relaunch — read the scrollback for deliberate human use first (2026-09-17):** `4jira1a` read `dead` (bash, no children, login-shell prompt at the bottom) which is normally the one relaunch state. But its scrollback showed the operator running `chmod 600 ~/.config/atlassian/credentials`, the tmux session was **attached**, and a brand-new sibling Jira session had just been created — i.e. the operator had exited claude on purpose to use that pane as a shell. **Relaunching would have hijacked a terminal someone was typing in.** So: confirm `dead` with `/proc`, then look at WHY — recent human shell commands, an attached session, or a fresh sibling session doing the same job all mean "deliberately retired, report it and hand over the resume id", not "crashed, revive it".

> **Inject ONLY when all of these hold:** (a) you have READ the pane this tick and it is `idle` with `unchanged_for_s` ≥ 60, (b) it stopped **mid-task** (not awaiting a first instruction, not deliberately done, not usage-paused, not at a menu or approval box), and (c) a message is the ONLY thing that can resume it. Everything else — a swap you performed, an account at a step, a usage pause, an idle pane at an empty `❯` — is a **note in your report, not a keystroke into someone's session.**
>
> **The single exception, and it is not optional (reconciled 2026-09-18 — blind review of PR #231 found these two rules six lines apart giving opposite verdicts on the same pane):** clause (b)'s "not usage-paused" means *a pause whose premise still holds* — the pane is asleep on an account that is genuinely still capped, so its own timer will resume it correctly and a keystroke only burns a turn. It does **NOT** cover the pane described in **THE ONE CASE WHERE YOU MUST NUDGE** above: a pane parked behind a wall timer that **you invalidated by moving it to a clean account**. That pane will sleep for hours on full headroom because nothing but you knows the premise changed. Decide with one question: **did I just change the fact this pane is sleeping on?** No → leave it, it resumes itself. Yes → nudge + verify, **message first** — per the SAFE ORDER in that rule, not Escape first. Clause (a)'s `unchanged_for_s ≥ 60` and the mid-task test do not gate this case either — a freshly swapped wall-parked pane is nudged in the same tick as the move.

> **Annotation 2026-09-16 — SUPERSEDED as a default; now opt-in (user directive: "you don't have to nudge unless you have to nudge").** The rule below made a post-swap notice MANDATORY for *any* live-pane swap, and that is what drove a burst of unnecessary injections on 2026-09-16 (six notices in one evening, one of which walled an idle pane — see the DO-NOT-INJECT rule above). **New default: after a swap, say nothing.** `cus slot move` rewrites the live mount in place and the running claude re-reads the fresh credentials on its next attempt, so the swap needs no announcement to take effect. Send the notice ONLY when you have positive evidence of the specific failure this rule was written for — you have actually SEEN that session run its own `cus`/self-heal swap in its scrollback and it is therefore liable to fight you. Absent that evidence, a silent swap is correct and strictly cheaper. The original rule is preserved below for the reasoning and the incident it came from.
>
> **AFTER A SWAP, IMMEDIATELY TELL THE PANE'S SESSION YOU SWAPPED IT — or it self-swaps and you fight (2026-07-15 — user directive: "you have to tell the other session that you already swapped, otherwise it starts swapping on its own… and you have to tell it right away"):** every live pane runs its OWN session that manages its OWN account (babysitter / self-heal / its own cus logic). When YOU (the watchdog) move that pane's account from outside via `cus slot move`, the pane's session has no idea — it still thinks it's on the old account and its own management ALSO tries to swap/heal, so the two swap against each other (churn + token-rotation divergence). **So the moment you `cus slot move` a LIVE (non-exited) pane, in the SAME turn, send that pane a notice.** This applies to ANY live-pane swap — a preemptive at-risk move too, not just at a limit menu. Do NOT notify EXITED/parked panes (no session to fight you). Right away, same turn as the move.
>
> **BUT — injected messages read as if THE OPERATOR typed them, so SIGN them and VERIFY they make sense (2026-07-16 — user correction: "if you're going to send stupid messages to sessions without checking if they make sense, at least sign off that you're an ai not me"):** `tmux send-keys` puts your text into the pane's input box as a **user turn** — the session interprets it as if the human operator typed it. So (1) **ALWAYS sign the message as automated**, e.g. prefix `[automated cus-watchdog message — NOT the operator]`, so no session mistakes it for the human; (2) **only send content you've VERIFIED is true for THAT session** — the always-safe factual notice is `"[automated cus-watchdog message — NOT the operator] Your account was swapped to <acct> by the watchdog (the old one was near its cap); retry the step if it errored."` Do NOT tell a session to "stop self-swapping / don't run cus slot move" unless you've actually SEEN that session run a swap in its scrollback — an account's 5h climbing can be cached-token drift or daemon re-placement, not the session, so that instruction is often false and confusing; (3) **when unsure, don't inject at all** — a silent cred swap + natural re-auth beats a wrong message. Send text, `sleep 1`, `Enter` as a SEPARATE keystroke, `sleep 3`, then read the pane to confirm it submitted ([[nudge-only-stalled-mid-task]] press-Enter rule).

> **Fable-5 SOFT limit is a distinct failure class the block-scan must catch (2026-07-14):** the hard block you grep for is `/rate-limit-options` / `❯ 1. Stop and wait` / `Upgrade your plan`. But hitting a per-model cap shows a DIFFERENT, softer message — **`You've reached your Fable 5 limit. Run /usage-credits to continue or switch models with /model`** (and the "thinking" verb `Brewed for …`) — rendered as a `⎿` tool-output block ABOVE the input prompt, so a `tail -5` capture at the idle `❯` MISSES it and the pane reads "clean/idle." Add these strings to the scan AND capture the last ~15-18 lines (not 5) so a `⎿` limit block above the prompt is seen. Also: when diagnosing WHICH account a limited pane is really burning, **trust the pane's own live cus statusline (`🔒slot-N acct*`) over `cus sessions` and over disk `oauthAccount`** — the running process caches its token, so it can be authing to (and capping on) a different account than state/disk claim; only the statusline reflects the live token.

> **NEVER place a lane on an account whose usage cus CANNOT CURRENTLY READ — check `last_observed_ts`, not just the percentages (2026-09-16 — learned from a self-inflicted outage):** `state.json` **preserves the last-known percentages** when a poll fails, so a 429ing or unreachable account keeps displaying its old numbers indefinitely, and `watch_tables.py` will happily render them as **`✓ headroom`**. They are not a reading; they are a memory. Incident 2026-09-16: rayi3's usage endpoint had been 429ing for 611 consecutive polls and its `last_observed_ts` was **five days old**, but it still showed `0% / 15% / Fable 21%`. The watchdog moved SEVEN panes onto it on the strength of those numbers; ~20 minutes later every one of them hit `Usage limit reached · continuing automatically at 2:20am`. The account had been near its cap the whole time — cus simply could not see it.
>
> **Before ANY placement decision, confirm the target's numbers are FRESH:**
> ```bash
> python3 -c "import json,os;a=json.load(open(os.path.expanduser('~/claude-accounts/state.json')))['accounts']['<acct>'];print(a.get('last_observed_ts'), a.get('rate_limited'), a.get('poll_backoff_consecutive_429s'))"
> ```
> A `last_observed_ts` older than the current 5h window, a truthy `rate_limited`, a non-trivial `poll_backoff_consecutive_429s`, or a pane statusline reading **`5h:? 7d:? (429)`** all mean the same thing: **you are flying blind on that account — treat it as UNKNOWN, never as headroom.** An account that cannot be polled is not a swap target; say so in the report and pick one whose numbers are actually current. Corollary for reporting: never present stale percentages to the operator as the fleet's current state without flagging the staleness.

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
14:00 ✓ sess-A acct-D 24% · sess-B acct-C 55% · sess-Z acct-F 61%
```

Most intervals are green. Keep them one line. Detail only appears when something happened.

**Exception** (anything else) → follow the playbook below, then write a few plain sentences: what was wrong, what you did, and what — if anything — the human must do (spell out exact commands). Write it so it can be read cold hours later.

> **"snapshot" ALWAYS means a FULL snapshot (2026-07-15 — user directive):** when the operator asks for "a snapshot" (or "snapshot please"), that is NOT the terse heartbeat line — it means the **complete fleet + lane picture**, and it must include **per-account 5h %, 7d %, per-model Fable %, AND reset ETAs for BOTH the 5h window and the 7d window** (the 7d/Fable weekly reset time is explicitly required — the user called this out). Render it as: (1) an SOS one-liner (benign flags noted as such); (2) a fleet table — every account with 5h / 7d / Fable + 5h-reset ETA + 7d-reset ETA (show the 72h-projected 7d reset; mark Fable-clean accounts, i.e. Fable < ~90); (3) a live-lane table — each live premium/work pane → its slot, account, 5h %, Fable %, and whether it's working vs exited/parked (a pane showing `Resume this session with: claude --resume` at the bottom = exited). Compute reset ETAs from `state.json` `five_hour_resets_at` and the 72h-projected `seven_day_resets_at` (see the [[fable5-soft-limit-and-statusline-groundtruth]] caveat: the 72h/7d projection does NOT reliably predict the per-model FABLE reset — only an actual force-poll drop confirms Fable freed, so label the 7d ETA as an estimate). Don't abbreviate a snapshot down to the heartbeat line — the operator asked for the full board on purpose. **`python3 <cus-repo>/skills/watch_tables.py` renders the fleet + panes tables (both 7d-reset columns) for you** — the same helper the per-tick report uses (see the REPORT FORMAT directive above); add the SOS one-liner above it for a full snapshot.

> **REPORT FORMAT — every tick emits TWO compact markdown tables, not just the terse line (2026-07-20 — operator directive):** the operator asked that each interval's report show an **accounts table** (fleet headroom + reset ETAs) and an **active-panes table** (protected sessions + the account each rides) at a glance, every tick — not only on an explicit "snapshot". A helper renders both from ground truth so you don't hand-build them:
> ```bash
> python3 <cus-repo>/skills/watch_tables.py                         # default active panes
> python3 <cus-repo>/skills/watch_tables.py sess-F sess-G sess-H   # or name them (session name or %pane id)
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

> **READ THE DO-NOT-INJECT RULES ABOVE FIRST — this section predates them and is narrowed by them (banner added 2026-09-18, blind review of PR #231).** As written below this is a routine per-interval nudge loop; it is not. Three corrections apply to everything in this section: **(1)** a pane parked behind a usage/rate-limit banner is NOT "stalled" — it resumes itself, and the only exception is a wall whose premise you invalidated by moving the lane; **(2)** every sample nudge here must be SIGNED `[automated cus-watchdog message — NOT the operator]`, and `Enter` goes as a SEPARATE `send-keys` call followed by a re-read that proves a real turn happened; **(3)** where this section says "Escape … then nudge", use the corrected SAFE ORDER — message first, Escape only if submission is blocked. Where this section and the rules above disagree, **the rules above win.**

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

- **Never kill/exit a pane or session** (`/exit`, Ctrl-C, closing it). Pausing and continue-nudges are the only keystrokes you send, only to panes you track. (One deliberate exception: retiring the watchdog's OWN prior pane during a migration handoff — step 6 of the "Migrating / re-homing the watchdog" section — and only after its fresh replacement is verified healthy. Never `kill-session` a pane you are protecting.)
- **Never answer a permission / yes-no / upgrade prompt** on the human's behalf.
- **Never drive an interactive `/login` / `relogin` browser flow** — you can't; your move is to hand the human the exact command.
- **Never hand-edit `state.json` / `.credentials.json` / `.claude.json`** — go through `cus` commands. When only a hand-edit will fix it (no-journal drift), escalate.
- **Never `cus switch --force`** or double-book an account onto a second live mount without a free independent login family (that's the exact clobber this prevents).
- **The watchdog's host account is DEDICATED and OUT OF ROTATION — never share it with a work lane.** Owner rule, restated 2026-09-18 after the loop was walled: *"You're supposed to always be locked on a dedicated, isolated slot, with that account excluded from the rotation."* Two conditions, both required, checked EVERY tick:
  1. The watchdog lives in its own **LOCKED** slot (`cus lock <slot>`) — the daemon must not rotate it and no session may lane-share-join it.
  2. Its host account is **`cus disable`d** (`disabled: true` in `config.yaml`), so the daemon can never place another lane there. A locked slot alone is NOT enough: locking freezes the slot, it does not reserve the ACCOUNT. On 2026-09-18 the watchdog sat locked on slot-6/rayi4 while the daemon put work lanes slot-9 and slot-10 on the same rayi4; those panes burned rayi4 to 5h 100% and the watchdog lost its own turns — a dropped tick is a blind fleet.
  Corollary: a dedicated host is spent capacity, so pick the account the fleet wants least — Fable-exhausted, high-7d-but-resetting, or otherwise unusable for premium lanes — never the cleanest account and never `default` (the owner's personal account).
  **Three consequences the blind review of PR #231 surfaced, all of which bite:**
  - **`cus disable` MUTES your host's own alarm.** Disabling an account downgrades its outstanding URGENT/WARNING SOS to one soft INFO line (by design — a parked account should not scream). Your host is now the one account whose trouble will not shout at you. So check it **directly** every tick — its `current_5h_pct` / `current_7d_pct` / `last_observed_ts` in `state.json` (cus itself falls back to `last_poll_ts` when `last_observed_ts` is absent — mirror that, and treat BOTH missing as blind), not `cus sos` — and never read a quiet `cus sos` as evidence your own host is healthy.
  - **You are the only rescue for your own slot.** A locked slot is never rotated by the daemon, so if your host walls, nothing external moves you (the external cron net only relaunches you onto `ANCHOR_ACCT` — if that account is the walled one, it relaunches you into the same wall). Self-rescue is therefore explicitly permitted and is the one placement you always make yourself: pick the next-least-wanted account, `cus disable` it, `cus unlock <your slot>` → `cus slot move <your slot> <new host>` → `cus lock <your slot>`, `cus enable` the old host, repoint the net. Do it BEFORE you are at the wall — at the wall you may not get the turns to do it.
  - **Order of operations, verified live 2026-09-18:** disable the new host FIRST, then move. `cus disable` only removes an account as an *automatic* target (`pick_swap_target`); an explicit `cus slot move <slot> <acct>` at a named account still lands, and needs no `--force` — confirmed at 14:27Z when `cus disable rayi2` followed by `cus slot move slot-14 rayi2` returned `SNAPSHOT … moved slot-14: default -> rayi2 (in place)`. `cus enable` the old host after, or the fleet silently loses an account.
  Re-check both conditions every tick as part of the standing regression checks (the short fixed list you re-verify each tick regardless of what else you find: `default` still disabled; your own slot still locked + right pool + right account + host still disabled; memory/swap trend and oom-kills; `cus.service` MainPID alive). On any rehome, repoint `~/bin/cus-watchdog-heartbeat.sh` in the SAME turn — `SID` (the net gates everything on it; a stale SID watches a dead session), `TRANSCRIPT`, `ANCHOR_ACCT`, `WD_SLOT` and `TMUX_SESSION` — then `bash -n` it.
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
- **Restore the currently-active account, not the one you assume.** The shared mount may have swapped under you (seen: acct-A→acct-I) — `restore-creds --live` refuses any account that isn't `state.json.active`. Read active first.
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
- **Read panes through `skills/pane_state.py`, not by hand.** `python3
  ~/repos/claude-usage-swap/skills/pane_state.py <tmux session names or pane ids>` prints
  one JSON line per named pane: `state` ∈ `working` (a live spinner row like
  `· Zigzagging… (12s · ↓ 1.2k tokens)`, "Waiting for N background agent", a live
  `◯ agent … 21m 55s` row, `⎿ Running…`) / `idle` / `idle_with_draft` (with
  `draft_signed` — press Enter ONLY on your own `[automated …` nudge, and only after it
  has sat `unchanged_for_s` ≥ 30; any other draft is a human mid-sentence) / `approval`
  (a numbered box — never answer it) / `limit_menu` / `login_menu` / `exited` (banner
  while a process is still alive — re-read, never relaunch into it) / `no_claude` (TUI on
  screen, no process — re-read) / `dead` (no process and no TUI — the only relaunch
  state) / `unknown` (a shell under a live node, a redraw) / `tmux_error`. Menus and
  boxes are judged only in the active block above the input rule, so answered boxes and
  dismissed menus higher in scrollback cannot re-trigger. `bg_agents` is the footer's
  `← N agents` count (`← for agents` = 0), not busy-ness. Name your panes: `--all` lists
  live processes only, so a dead pane vanishes from it; a name that matches no pane at
  all prints `not_found` (the pane is gone or renamed — look the session up by uuid
  before assuming death); a pane with no process is `dead`. A whole-tmux failure prints
  one `{"error": …}` line and exits 2 — every protected pane is unknown that tick, not
  dead. Nudge only on `idle` with `unchanged_for_s` ≥ 60; press Enter only on your own
  `[automated …` draft. Pane text still decides liveness and submission; the transcript
  sense below decides WHY it stopped.
  (Since 2026-09-04 that path is a SHIM: the reader's code and tests live in
  `~/repos/vibeCoding/skills/build-babysitter/pane_state.py`, the build-babysitter skill's
  directory, so that skill runs without cus; the shim execs the vibeCoding copy — or, if it
  is not cloned, prints one `{"error": …}` line and exits 3, distinct from the reader's
  exit 2 for "tmux unusable". Remedy for exit 3: `git -C ~/repos/vibeCoding pull` (or clone
  rayistern/vibeCoding there), or link the skill at `~/.claude/skills/build-babysitter`, or
  set `PANE_STATE_PY=<file>`. Rollout order on this box: vibeCoding #334 → #336 → pull →
  a real row from a live pane → only then cus #200; a watchdog that reads exit 3 every tick
  is blind, not broken — escalate, do not scrape by hand.)
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

---

## Update 2026-09-11 — session mail across mounts is fixed (GH #199)

**What was broken.** Claude Code's peer registry — the thing `ListAgents` lists and
`SendMessage` addresses — lives at `<CLAUDE_CONFIG_DIR>/sessions/`. Each live session
publishes a **pair**: `<pid>.json` (metadata, includes `pid`; `procStart` is
present on some shapes and omitted on others — 4 of 5 live shared-registry
`.json` files on 2026-09-14 had none) and
`<pid>.<sha256>.key` (`peerToken` / `pidDomain` / `procStart` — no `pid`). Every cus
slot mount owned a *private* real `sessions/` dir, so a session launched with
`cus launch` and a bare session were mutually invisible: no error at launch, the peer
name simply never resolved. That is why the build-babysitter had to fall back to a file
channel (`docs/babysitter/<date>-builder-reports.md`) for builder → babysitter reports,
and why a watchdog in a slot could see none of the panes it was protecting.

**What changed.** `sessions/` is now symlinked to the shared `~/.claude/sessions/` the
same way `projects/` always was, in every mount-creation path (`scaffold_mount_dir`,
the login-store and login-family scaffolds, the account-dir migration, `cus add`,
`init` import). New slots are born correct. Doctor also visits `logins/<acct>/family-N/`.

**Owner step — run once per machine.** Mounts created before 2026-09-11 still own a
private registry. Heal them with:

```bash
# Dry-run first (default read-only). Exit code 1 when findings exist is EXPECTED —
# it means drift was detected, not that doctor itself failed.
cus doctor --fix-sessions --dry-run

# Then heal. Prefer no slotted `claude` session running: a live session's
# json+key pair is left in place and that mount is DEFERRED (healed=False) rather
# than moving peerToken out from under the process. Re-run after those sessions exit,
# or accept per-mount deferral and relaunch later.
cus doctor --fix-sessions

# --fix-dirs also heals sessions/ but its blast radius is the FULL mount layout
# (settings stubs, other real dirs, etc.), not sessions alone.
# cus doctor --fix-dirs
```

The migration never deletes. Live pairs, and pairs whose liveness cannot be
confirmed (no readable `procStart`, or `/proc` unreadable), are **not moved**
(conversion deferred — a false live is a deferral; a false dead parks a running
session's peerToken). Dead pairs and orphan files are parked as units in
`<mount>/sessions.bak-<date>/`. A mount is only relinked once its dir is empty;
if anything could not be moved, the real dir is left alone and `doctor` exits
non-zero.

**Verifying it worked:** from a bare session run `ListAgents` and confirm a slotted peer
now appears (and vice versa). A slot whose session was live during a deferred heal still
writes to its private dir until relaunch — restart that session after the mount
relinks.

## Update 2026-09-16 — Migrating / re-homing the watchdog (new-pane FRESH-session handoff)

When the watchdog must move to a different account/slot (its host account is
Fable-clean and you want to preserve that capacity, its account died, or it
drifted onto a shared slot after a crash-revive) — do **NOT** relaunch it in
place by resuming the same session id, and do **NOT** open a second pane that
`--resume`s the SAME session id. Two live processes on one session's `.jsonl`
transcript both take turns and append → interleaved / undefined writes (and some
Claude Code builds refuse the second attach outright). Either way the old,
working watchdog dies (or misbehaves) before the new one is proven healthy —
no fallback. This is the trap the 2026-09-16 migration hit.

**The safe procedure — a new pane running a FRESH session (the watchdog is
state-light: its whole contract is THIS file + `MEMORY.md`, so a fresh session
loses nothing operational):**

0. **Reconcile with the DEDICATED-HOST hard rule first (added 2026-09-18 — the
   blind review of PR #231 found that following steps 1-6 literally reproduces
   the very incident the hard rule exists to prevent).** The target account must
   become a **dedicated** host: `cus disable <new-acct>` BEFORE the move, so the
   daemon can never place a work lane beside you, and `cus enable <old-acct>`
   after, so the fleet gets the old one back. A target that currently hosts a
   LIVE work lane is not eligible — disabling it would strand that lane at its
   next step; pick a work-free account instead, or wait for the lane to move.
   The slot you land in must end up LOCKED. See § "Hard rules" for the full rule
   and the self-rescue sequence.
1. **Pick + verify the target is actually launchable.** The park should be a
   **Fable-dead, standard-pool** account (Opus watchdog burns zero Fable, so it
   wastes nothing there and frees Fable-clean accounts for real Fable lanes —
   see memory `opus-watchdog-pin-to-fable-dead-account`). Confirm the target has
   a **free independent login family** and no live mount elsewhere, or the
   locked-slot launch is refused (GH #190/#104). A *canonical* relogin does NOT
   provision an independent family — only `cus login-mount <acct>` (interactive
   browser) does. 2026-09-16 example: `acct-B` was relogged but its family pool
   was exhausted, so `cus launch acct-B --lane slot-1` was refused; the launch
   fell back to `acct-A` (which had a free family). Pre-provisioned locked
   standard slots exist for this (slot-1/acct-B, slot-5/acct-C, slot-6/acct-A).
   *(Annotation 2026-09-18: that slot list is a historical example, not a
   standing roster — slot-6 in particular was released back to the fleet on
   2026-09-18 and the watchdog now lives in slot-14 on a disabled `rayi2`.
   Re-derive the real list from `cus status` each time; never launch at a slot
   because this line named it.)*
2. **Write a handoff briefing file** (see `~/.claude/cus-watchdog-handoff-<date>.md`
   for the 2026-09-16 template): who/where it runs, that it's a fresh session
   REPLACING the prior one (and that the prior pane is now a dead-loop ops chat,
   not a peer watchdog), the loop contract summary, the external cron net, a
   current fleet snapshot, the standing red-lines, and its first actions.
3. **Launch the new pane** in a shell (not a one-shot command that exits — that
   kills the tmux session): `tmux -L default new-session -d -s cus-watchdog -c
   <repo>` then send-keys `cus launch <acct> --lane <slot> --force -- --dangerously-skip-permissions`
   (no `--resume` = fresh session). Name the tmux session `cus-watchdog` to match
   the heartbeat cron's canonical `TMUX_SESSION`.
4. **Verify it came up healthy** (statusline shows the right locked slot+account,
   at a `❯`), then **bootstrap it**: send-keys a prompt pointing it at the handoff
   file → it reads `watch.md`, runs its first tick, and re-arms. Confirm the
   heartbeat file (`~/.claude/cus-watchdog.heartbeat`) mtime goes fresh — that
   proves it ran a real tick, not just booted.
5. **Re-point the external net** (`~/bin/cus-watchdog-heartbeat.sh`): update `SID`,
   `TRANSCRIPT` (to the new slot's config dir), `ANCHOR_ACCT`, `WD_SLOT`, and
   `TMUX_SESSION` (the pane name the relaunch targets — it is wrong as often as
   the others; 2026-09-18 the watchdog was resumed in pane `cus2a` while the net
   still aimed at `cus-watchdog`). Do this
   BEFORE relying on the net so a failed bounce self-recovers to the RIGHT place.
   The heartbeat SID gates the whole net — a stale SID means it watches a dead
   session and never fires for the live one.
6. **Only now retire the old pane** (`tmux kill-session -t <old>`). Because the
   new watchdog is a *different* session id, the old pane can linger harmlessly as
   a fallback until you're satisfied — there is no transcript conflict.

**Net-liveness caveat (2026-09-16):** the heartbeat cron judges liveness by
`max(heartbeat-file, transcript)` mtime. An *interactive* session (a human/agent
chatting with the watchdog session) keeps the transcript fresh, so a **dead loop
can be masked** from the net while the session is being talked to. Mitigation:
touch the heartbeat file EVERY tick (already in the contract) — it's the only
signal that reflects loop ticks specifically, not arbitrary session activity.