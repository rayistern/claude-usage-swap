Manage [claude-usage-swap (`cus`)](https://github.com/rayistern/claude-usage-swap) — diagnose, configure, add/rename/relogin accounts, install/uninstall. Invoke from Claude Code via `/cus <question or instruction>`.

`cus` is a tool that auto-rotates Claude Code OAuth accounts based on usage thresholds. It runs as a `systemd --user` daemon, polls the Anthropic OAuth usage endpoint per account, and swaps the active credentials when an account approaches its 5-hour or weekly cap.

When the user invokes this skill, follow this routine:

## Step 1 — orient yourself

Always start by running these (read-only, fast):

```bash
cus sos                  # exit 0 = all clear; non-zero = action needed
cus status               # active account, per-account percentages, live sessions
systemctl --user is-active cus.service    # daemon running?
```

Report the headline back to the user in 1-3 sentences: which account is active, what % each is at, whether the daemon is healthy, whether any SOS conditions exist. Do not run additional commands until you've shown this.

If `cus` isn't installed (`command -v cus` returns nothing), tell the user to install it first:

```bash
git clone https://github.com/rayistern/claude-usage-swap ~/repos/claude-usage-swap
python3 ~/repos/claude-usage-swap/cus.py install
```

## Step 2 — interpret the user's intent

Map to one of these common requests:

### "Check my status / how am I doing"
You already showed it in Step 1. If there's an SOS condition, walk the user through the printed action. If everything is healthy, confirm and stop.

### "Add a new account"
Two-step:
1. Run `cus add <name>` (or suggest a name if the user didn't pick one). Print the login command it outputs.
2. Tell the user to run that command **themselves** in a fresh terminal — the `claude /login` flow is interactive (opens a browser; you can't drive it). After they `/exit`, run `cus poll && cus sos` to confirm.

If the user wants you to start the process: run `cus add <name>` for them; explain that they need to do the actual browser-based login.

### "An account's token expired" / re-login
Same as add, but use `cus relogin <name>`. Walk through the same interactive flow.

### "Adjust a setting" / "change the threshold" / "swap less aggressively"
1. Run `cus config --explain` to show the full annotated config.
2. Discuss the change with the user in prose. Common knobs they may want:
   - `thresholds.steps: [50, 75, 90]` — raise the first step if you want it to swap later
   - `strategy` — `lowest_usage` (default) vs `drain` (use one account heavily before switching) vs `strict_priority`
   - `poll_interval_seconds` — 300 is the sane default; lowering makes it more reactive but uses more API calls
   - `hot_swap.enabled` — false = swap only affects new sessions; true = pause + relaunch live ones
   - `hot_swap.tier_2_at_pct` / `tier_3_at_pct` — where pause-message and force-interrupt kick in
   - `subagent_skip.defer_below_tier` — set to 100 to never kill mid-subagent
3. To edit: open `~/claude-accounts/config.yaml` directly (`cus config --edit` opens `$EDITOR`). After editing, restart the daemon: `systemctl --user restart cus.service`.
4. Print the **before** value and offer a **diff** of what you'd change before doing it.

### "Rename an account"
`cus rename <old> <new>` — atomic, preserves state/history/pins. Suggest meaningful names (`work`, `01`, etc.). After rename, daemon needs restart: `systemctl --user restart cus.service`.

### "Pin a session to an account"
`cus pin <pane_or_session_id> <account_name>`. Get the user's tmux pane via `tmux display-message -p "#{pane_id}"` or read it from `cus status` (live sessions are listed).

### "Daemon isn't swapping when I think it should"
Walk the troubleshooting tree from `docs/TROUBLESHOOTING.md`:
1. Is the active account actually over its `next_swap_at_pct`? Show `cus status`.
2. Are valid swap targets available? Check for `TOKEN_EXPIRED`, `RATE_LIMITED`, `POLL_ERROR`, `SUB_ENDED` or `PAST_DUE` flags on other accounts (`SUB_ENDED` = the subscription ended; `PAST_DUE` = its renewal payment failed — both block launches and rotation onto the account, and `cus sos` names the fix).
3. Run `cus daemon --once --no-execute` to see the decision logic + reasoning.
4. Check the daemon log: `journalctl --user -u cus.service -n 50` (systemd) or `tail -50 ~/claude-accounts/daemon.log`.

### "Force a swap now"
`cus switch <name>` — atomic. Reversible by swapping back. Confirm with the user first if the daemon is running (they may want to stop the daemon to avoid an immediate auto-swap-back).

### "Install" / "set up cus"
`cus install` (or `python3 ~/repos/claude-usage-swap/cus.py install` if `cus` isn't on PATH yet). Idempotent; safe to re-run. Use `--skip-statusline` etc. for opt-out.

### "Uninstall" / "remove cus"
`cus uninstall` (preserves `~/claude-accounts/`). Confirm with the user first. To also delete account storage: `cus uninstall --keep-data=false`.

## Step 3 — show what you did

After running any state-changing command, show the user:
- The command output (verbatim if short, summarized if long)
- The relevant `cus status` after
- A walk-back path if the change is non-obvious (e.g. "to undo the swap, `cus switch <previous>`")

## Step 4 — flag anything weird

If a command returns an error you don't understand, **don't guess**. Show the error verbatim, suggest `cus sos` for diagnosis, and point at `docs/TROUBLESHOOTING.md`. Do not invent recovery commands.

## Important constraints

- **Never edit `~/.claude/.credentials.json` or `~/.claude.json` directly.** Always go through `cus switch` / `cus init` / `cus install`.
- **Never drive the interactive `/login` flow yourself.** It needs a browser and a paste-back from the user. Tell them what command to run and let them do it.
- **Never run `cus uninstall --keep-data=false` without explicit user confirmation.** That deletes their account snapshots.
- **The daemon is managed by systemd.** Don't run `cus daemon` in foreground separately; that would create a second daemon. Use `systemctl --user restart cus.service` to pick up config changes.
- **Restart the daemon after any config change** that isn't auto-picked-up (`cus daemon` re-reads config every cycle, but `systemd`-managed processes don't pick up file changes mid-cycle).

## Quick reference for yourself

| Command | What it does |
|---|---|
| `cus status` | Active account, percentages, live sessions, recent swaps |
| `cus sos` | Exit 1 + printed actions if anything needs human attention |
| `cus list` | All configured accounts with OAuth identities |
| `cus config` | Effective merged config |
| `cus config --explain` | Annotated: every setting + description + current value |
| `cus config --edit` | Open `~/claude-accounts/config.yaml` in `$EDITOR` |
| `cus add <name>` | Create a new account dir, print login command |
| `cus relogin <name>` | Print login command for expired account |
| `cus rename <old> <new>` | Atomic account rename |
| `cus switch <name>` | Manual swap |
| `cus pin <pane> <account>` | Pin a session to never be swapped |
| `cus poll` | One-shot usage poll, updates state.json |
| `cus daemon --once --no-execute` | See what the daemon would do, without doing it |
| `cus install` | One-command bootstrap (init + hooks + statusline + systemd + wrapper) |
| `cus uninstall` | Reverse of install (preserves data by default) |
| `systemctl --user status/restart/stop cus.service` | Daemon control |
| `journalctl --user -u cus.service -n 50` | Recent daemon log |

For detailed reference, point the user at:
- `docs/RUNBOOK.md` — day-to-day operations
- `docs/TROUBLESHOOTING.md` — SOS catalog + common issues
- `docs/ARCHITECTURE.md` — file layout, swap algorithm
- `inbox.md` — load-bearing autonomous decisions made by the daemon
