# Troubleshooting `cus`

Symptom-organized index. Find the closest match, follow the recipe.

## SOS catalog

`cus sos` enumerates everything that needs human attention. Each condition has a `severity` (urgent / warning) and a printed `action`. The conditions:

### `<name>: OAuth token expired`

**What:** the account's cached OAuth tokens at `~/claude-accounts/account-<name>/.credentials.json` are no longer valid. Anthropic's usage endpoint returned HTTP 401.

**Why it happens:**
- Tokens haven't been used in many days (refresh chain broken)
- Account was logged out elsewhere (e.g. via Claude web UI)
- Anthropic revoked the token for security reasons

**Fix:**
```bash
cus relogin <name>           # prints the exact command
cus relogin <name> --exec    # or just launch claude under that dir
```
Follow the interactive `/login` flow. Then:
```bash
cus poll
cus sos                      # should print "✓ All clear"
```

### `All accounts blocked`

**What:** every configured account is either token-expired, rate-limited, or returning poll errors. Daemon can't swap anywhere.

**Why it happens:**
- All accounts genuinely at their caps (rare unless you only have 1-2 accounts and use heavily)
- You've been poll-spamming and Anthropic's usage endpoint itself is rate-limiting your IP
- Network issue affecting all requests

**Fix:**
1. Identify which accounts are token-expired vs rate-limited: `cus status`
2. Re-login expired ones (see above)
3. For rate-limited accounts, wait for cooldown (5-hour window or 7-day window resets — `cus status` shows reset times when available)
4. If you have only 2 accounts and they alternate hitting caps, consider adding a third: `cus add <name>`

### `<active> at NN% (≥M%) and no other account available`

**What:** active account is over its `next_swap_at_pct` threshold, but no swap target is available (all others blocked).

Same fix as "All accounts blocked" — usually a symptom of the same underlying problem.

### `Stale usage data for: <names> (no fresh poll in >NN min)`

**What:** the daemon hasn't successfully polled these accounts in the last 4 poll-intervals. Daemon may be down, or the polling endpoint is down.

**Fix:**
```bash
systemctl --user status cus.service    # check daemon health
journalctl --user -u cus.service -n 50  # recent log
# If daemon dead, restart:
systemctl --user restart cus.service
# If Anthropic endpoint is down, wait it out — it'll resume polling on its own
```

### `Daemon pid <N> recorded but process is gone`

**What:** the daemon's PID file exists but the process is dead. Crash or unclean shutdown.

**Fix:**
```bash
# Look at last log lines to see if there's a stacktrace
tail -50 ~/claude-accounts/daemon.log
# Restart
systemctl --user restart cus.service
# Then clean up the stale pid file if it persists:
rm ~/claude-accounts/daemon.pid
```

---

## Non-SOS issues

### Daemon is running but not swapping when I think it should

Check, in order:

1. **`cus status`** — is the active account actually over its `next_swap_at_pct`? The threshold is per-account and climbs (50→75→90→force) as we return to each account. After a swap-and-back, the threshold is higher.
2. **Is there a valid swap target?** All other accounts must be (a) not token-expired, (b) not rate-limited, (c) not over their own threshold (depending on strategy). `cus status` shows flags.
3. **`cus daemon --once --no-execute`** in a separate terminal — shows the decision logic with detailed reasoning.
4. **Strategy** — `strict_priority` only swaps to a higher-priority account that has headroom. Try `lowest_usage` if you want it to pick whoever has the most room.
5. **Is `hot_swap.enabled: true`?** — without it, the daemon still swaps the active account, but doesn't touch live sessions. New `claude` invocations get the new account.

### Live session got force-interrupted in the middle of a task

The daemon hit Tier 3 (90% threshold or 429 received). Check `~/claude-accounts/inbox.md` — the daemon logs what was running before the interrupt. To recover:

1. Re-launch the session: it was relaunched with `claude --resume <id> "Continue."` on the new account.
2. If the interrupted command was a long-running shell or subagent, re-issue it manually.
3. To prevent in the future: lower `tier_3_at_pct` (you'll hit Tier 1/2 earlier and have time to react), or pin critical sessions: `cus pin <pane-or-session-id> <account>`.

### Statusline shows nothing or just `cus: not init`

Either `~/claude-accounts/` doesn't exist yet (run `cus init`) or the statusline command path is wrong. Check `~/.claude/settings.json`:

```json
"statusLine": {
  "type": "command",
  "command": "python3 /home/<you>/repos/claude-usage-swap/cus.py statusline"
}
```

The `python3` path needs to be findable and `click` + `pyyaml` must be importable (`python3 -c "import click, yaml"`).

### "Resource not found" when pushing / `gh` commands fail

You may have multiple GitHub accounts logged in via `gh`. Check:

```bash
gh auth status
gh api user --jq .login   # who is the active account
gh auth switch --user <correct-account>
```

This bites repeatedly when you have e.g. a work and personal GitHub account both authed.

### Hot-swap relaunched two panes into the same chat ("No conversation found")

**Symptom (GH #29):** after a hot-swap, two panes that share a cwd open into the same conversation — one "steals" the chat, the other shows `No conversation found with session ID <uuid>` (sometimes called "no session found"). Recurring. GH #22 covered one source of this; GH #29 covers the remaining ones.

**Cause:** the fallback chain that picks each pane's session-id has three sources (`/proc cmdline --resume` → `sessions.log` latest → newest-mtime `.jsonl` in cwd). Any source can emit a duplicate across two panes — especially step 1: once a prior bad swap relaunched two panes with `claude --resume X`, both processes' `/proc/<pid>/cmdline` reads `--resume X` forever, so the collision **self-replicates through every subsequent swap** even after GH #22 deployed (which only guards step 3).

**Fix (GH #29 default):** a post-pass in `find_live_panes` detects any session-id resolved for ≥2 panes and clears it for all of them, so they relaunch fresh on the next swap. You'll see this in `daemon.log`:
```
COLLISION: session-id abc12345 resolved for 2 panes (%11, %12); clearing all — they will relaunch FRESH (no --resume) to avoid stealing each other's chat
```

**Manual recovery for an already-affected pane:** the next swap will demote both panes to fresh; if you don't want to wait, `/exit` and `claude --resume <correct-id>` (or just `claude` for fresh).

### Hot-swap relaunched session lost conversation context

`claude --resume <id>` reads the JSONL transcript at `~/.claude/projects/<cwd-encoded>/<id>.jsonl`. If your account-* dirs don't have `projects/` symlinked to `~/.claude/projects/`, the new account can't see the transcript and resumption fails.

Verify:
```bash
ls -la ~/claude-accounts/account-<name>/projects
# Should be a symlink pointing at ~/.claude/projects
```

If missing, re-run migration:
```bash
cus init --force
```

### Swap deferred every cycle — daemon log keeps saying "DEFER: subagent active in pane X"

**Symptom (GH #27):** the active account climbs past `next_swap_at_pct` and even past `saturation_pct` (90 %+), but the daemon log shows a `DEFER: subagent active in pane <N> ...; tier=T < <D>` line every cycle and no swap actually executes. Eventually the account hits 100 % and live sessions interrupt.

**Cause:** prior to GH #27, three compounding bugs:
1. **Whole-orchestration abort on one busy pane** — the subagent skip-guard `return`ed before the cred swap and before *any* other pane's relaunch. One stuck pane held up the entire pool.
2. **`defer_below_tier: 100` defeated the tier-3 force valve** — the saturation tier deferred behind a busy pane instead of force-proceeding.
3. **`subagent_active` over-detection** — `tool_use.log` was written with `start` on every tool call but `stop` only on subagent end / failure (missing `PostToolUse` success hook). With ~31× more starts than stops, `subagent_active` effectively reported "any tool ran in the last 60 s," not "in-flight subagent."

**Fix (GH #27 default):** per-pane skip (the cred swap and other panes' relaunches proceed; affected panes are inbox-logged); tier 3 always bypasses the guard regardless of `defer_below_tier`; new `cus_post_tool_use.sh` hook closes the ledger.

**If you see this on older code or with the hook absent:**
```bash
cus hooks install         # picks up the new cus_post_tool_use.sh
cus switch <target>       # manual force-swap to unstick now
```

The subagent guard at tiers 1–2 is still a per-pane skip (correctly), so a genuinely stuck pane will drift onto the old account until you restart it. An inbox entry tagged "deviation" is written for each skipped pane.

### Hot-swap relaunched session is missing some MCP tools (e.g. `mcp__gym__*` gone)

After a hot-swap relaunch, a slow-cold-starting MCP server's tools can be absent for the entire resumed session, even though `claude mcp list` reports the server as "✓ Connected" (GH #25).

Why: `claude --resume` re-spawns every stdio MCP server from scratch and bounds each one's startup connect attempt by `MCP_TIMEOUT` (Claude Code default **30000ms**). Stdio servers get **no startup retry and no mid-session auto-reconnect** — so a server that misses its window is gone until the next manual relaunch. A swap relaunches many panes near-simultaneously, so their MCP cold-starts contend for CPU; a server that takes ~8s standalone (observed: `gym`) balloons past 30s and gets dropped, while faster servers survive. `claude mcp list` is misleading here — it spawns a *separate* health-check process, so "Connected" there says nothing about your running session.

Fix (on by default since GH #25): cus prefixes the relaunch with a higher `MCP_TIMEOUT` and staggers per-pane relaunches. Tune in config if a server is still dropped:
```yaml
hot_swap:
  relaunch_mcp_timeout_ms: 120000   # raise further if a very slow server still drops
  relaunch_stagger_seconds: 2.0     # raise to reduce cold-start CPU contention
```
Preview the exact relaunch command (now includes the `MCP_TIMEOUT=` prefix) with `cus check-orchestrate`. Workaround for an already-affected session: exit and relaunch that one pane by hand (`claude --resume <id>`), ideally with no other sessions cold-starting at the same moment. Secondary: a server taking 8s to answer `initialize` is itself worth fixing upstream — the cus-side timeout is the robust general defense regardless.

### Daemon swaps too aggressively

Two knobs:

1. **Raise the first threshold step**: `thresholds.steps: [75, 90]` (skip 50).
2. **Use `strict_priority`** instead of `lowest_usage` so it stays on your primary account longer.
3. **Increase `cache_bust_window_seconds`** so Tier 1 swaps defer when cache is warm.

### Daemon swaps too slowly

1. **Lower the first threshold step**: `thresholds.steps: [30, 50, 75]`.
2. **Lower `poll_interval_seconds`** — faster reaction time (but more API calls; don't go below ~60).
3. **`reactive.enabled: true`** ensures sub-poll-interval reaction to 429s.

### "Why is `cus poll` returning 429 even though my account isn't capped?"

The Anthropic OAuth usage endpoint itself has a rate limit. If you call `cus poll` rapidly (e.g. during testing), you may trip it. The daemon's 5-min polling cadence stays well under this limit. If you keep hitting it, wait 5-10 min.

### Reverting a bad swap

```bash
cus switch <previous-account>     # rewinds the swap
```

Every swap is in `~/claude-accounts/state.json` under `swap_history`. The active account's `.credentials.json` and account-bound `.claude.json` keys are restored from the storage-side snapshot.

### "I want to look at what the daemon decided last night"

```bash
journalctl --user -u cus.service --since "yesterday"
# or
tail -200 ~/claude-accounts/daemon.log
```

Swap history is also in `~/claude-accounts/state.json` `swap_history` array.

### "I want to be alerted via Slack / email / pushover, not just notify-send"

The simplest approach: wrap `cus sos` in a cron job:

```bash
# crontab -e
*/15 * * * * /usr/bin/python3 /home/<you>/repos/claude-usage-swap/cus.py sos --quiet || /usr/bin/your-alerting-cli "$(/usr/bin/python3 /home/<you>/repos/claude-usage-swap/cus.py sos 2>&1)"
```

`cus sos` exits 0 when clear, 1 when action needed, so the `||` only triggers on real SOS conditions.

---

## Reset to factory state

If nothing makes sense and you want to start over:

```bash
systemctl --user disable --now cus.service
cus hooks uninstall
# Manually remove the "statusLine" key from ~/.claude/settings.json (or restore from .bak)
# Then:
mv ~/claude-accounts ~/claude-accounts.broken      # don't delete — keep as forensic
# Re-run setup:
cus init
cus hooks install
cus init-systemd --enable
```

Your real Claude config at `~/.claude/` is never touched by `cus` reset. Conversations, plugins, settings, etc. are all preserved.

## Filing a bug

Issues: https://github.com/rayistern/claude-usage-swap/issues

Include:
- Output of `cus config`
- Output of `cus status`
- Recent daemon log: `journalctl --user -u cus.service -n 100` or `tail -100 ~/claude-accounts/daemon.log`
- Claude Code version: `claude --version`
- Python version: `python3 --version`
- OS: `uname -a`

Redact credentials. The `.credentials.json` content should never appear in a bug report.
