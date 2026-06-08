# Runbook — day-to-day `cus` operations

This is the reference for operating `cus` after initial setup. Quick lookup by what you want to do.

## Daily check: are things healthy?

```bash
cus sos           # exit 0 = all clear; non-zero + printed actions if not
cus status        # current account, per-account usage, live sessions, recent swaps
```

If `cus sos` prints anything, follow the printed action. The daemon also writes `~/claude-accounts/SOS.md` and (when `notify-send` is installed) fires a desktop notification on signature change.

The Claude Code statusLine at the bottom of every TUI shows `cus:<account> 5h:NN% 7d:NN% nxt:NN%` normally, or `🚨 cus SOS: <reason>` when action is needed.

## Adding a new account

```bash
cus add <name>            # creates ~/claude-accounts/account-<name>/, prints login command
cus add <name> --exec     # same, but immediately launches `claude` under the new dir
```

After `cus add work --exec`, Claude Code opens with `CLAUDE_CONFIG_DIR=~/claude-accounts/account-work/`. Do the interactive `/login` flow (a URL appears; open it in a browser; paste back the code). Exit when done.

Then:

```bash
cus init --force          # discover + register the new account
cus poll                  # confirm it returns valid usage data
```

The daemon picks it up on its next cycle. No restart needed.

## Re-logging an account whose tokens expired

When `cus sos` reports `OAuth token expired`:

```bash
cus relogin <name>            # prints the exact login command
cus relogin <name> --exec     # immediately launches claude under that dir
```

After login + `/exit`:

```bash
cus poll                      # refreshes state.json
cus sos                       # should report "All clear"
```

## Manual swap (override the daemon's decision)

```bash
cus switch <name>             # atomic swap to <name>
cus switch <name> --dry-run   # preview the file-level changes
```

Swaps are reversible — `cus switch <previous>` rewinds.

## Pinning a session to an account

Some sessions you want kept on a specific account (long-running autonomous work, etc.):

```bash
cus pin %12 default           # pin tmux pane %12 to default; daemon never swaps it
cus pin <session-id> merkos   # pin a specific session-id
cus unpin %12                 # remove the pin
```

Pin config lives in `~/claude-accounts/config.yaml` under `session_locks.pinned`. Edit directly if you prefer.

## Whitelist patterns (never restart these panes)

In `~/claude-accounts/config.yaml`:

```yaml
session_locks:
  never_restart_patterns:
    - "babysitter:.*"           # regex matched against tmux pane's pane_current_command
    - "long-running-.*"
```

Matching panes are skipped during hot-swap (the daemon doesn't `/exit` and relaunch them).

## Hot-swap (Level 4) — opt-in, smoke-test first

Hot-swap of live sessions is **disabled by default** since the 2026-05-19 incident. The orchestrator was rewritten on the same day with 4 fixes; verify on your setup before enabling.

Steps to safely enable:

```bash
# 1. Dry-run: print what the orchestrator WOULD do (no actual changes)
cus check-orchestrate
```

The dry-run lists every tmux pane with live claude, the session-id it would use for `--resume`, and the exact relaunch command. **Verify:**

- One entry per pane (no duplicates)
- The session-id in the "Planned relaunch" line is the CURRENT claude session in that pane (cross-check against the "Resume this session with:" output shown when you `/exit` claude)
- `relaunch_flags` contains `--dangerously-skip-permissions` if you normally launch with it
- No positional wake-up message in the relaunch (deliberately removed after the 2026-05-19 incident)

If dry-run looks correct:

```bash
# 2. Enable in config
cus config --edit       # change hot_swap.enabled to true
systemctl --user restart cus.service

# 3. Monitor first few swaps closely
journalctl --user -u cus.service -f
```

To disable: flip `hot_swap.enabled: false`, restart daemon. Hot-swap-related config knobs:

| Knob | Default | What |
|---|---|---|
| `hot_swap.enabled` | `false` | Master switch |
| `hot_swap.tier_2_at_pct` | `75` | Step at which Tier 2 (pause-message) kicks in |
| `hot_swap.tier_3_at_pct` | `90` | Step at which Tier 3 (force interrupt) kicks in |
| `hot_swap.pause_message` | (long) | Text sent via tmux send-keys to nudge Claude at Tier 2 |
| `hot_swap.wake_up_message` | `""` (empty) | KEPT in config but NOT sent as positional after rewrite. Set non-empty only if you understand the cost. |
| `hot_swap.cache_bust_window_seconds` | `300` | Defer Tier 1 if cache still warm (set EQUAL to Anthropic's 5-min cache TTL or slightly more) |
| `hot_swap.mid_turn_idle_seconds` | `30` | Session counts as "between turns" if no transcript write for N seconds |
| `hot_swap.stop_wait_timeout_seconds` | `300` | Max wait for Stop signal in Tier 1 |
| `hot_swap.pause_response_timeout_seconds` | `120` | Max wait after Tier 2 pause-message |
| `hot_swap.shell_return_timeout_seconds` | `10` | Max poll for pane to return to shell after `/exit` |
| `hot_swap.relaunch_flags` | `""` | Flags passed to relaunched `claude` — set to `"--dangerously-skip-permissions"` if you use it |

## Starting / stopping the daemon

If installed via systemd (`cus init-systemd --enable`):

```bash
systemctl --user status cus.service    # current state
systemctl --user stop cus.service      # pause
systemctl --user start cus.service     # resume
systemctl --user restart cus.service   # reload config.yaml
systemctl --user disable cus.service   # remove from boot
journalctl --user -u cus.service -f    # tail logs
```

Without systemd:

```bash
cus daemon                             # foreground (Ctrl-C to stop)
cus daemon --once                      # single cycle and exit
cus daemon --once --no-execute         # decide but don't actually swap (dry-run)
```

The daemon writes its logs to `~/claude-accounts/daemon.log` either way.

## Tuning thresholds + strategy

Edit `~/claude-accounts/config.yaml`. Common changes:

| What you want | Edit |
|---|---|
| Poll less aggressively | `poll_interval_seconds: 600` (10 min) |
| Don't swap until 75%+ | `thresholds.steps: [75, 90]` |
| Try to "drain" one account before falling back | `strategy: drain` |
| Disable hot-swap (level 3 only — auto-rotate new sessions, leave live ones alone) | `hot_swap.enabled: false` |
| Inject pause-message at 50% instead of 75% | `hot_swap.tier_2_at_pct: 50` |
| Never swap mid-subagent | `subagent_skip.defer_below_tier: 100` (effectively always defer) |
| Swap on every ladder trip (disable cache-aware deferral) | `lazy_swap.enabled: false` |
| Wait longer for a cold cache before swapping | `lazy_swap.cache_window_seconds: 600` |

Restart the daemon after editing: `systemctl --user restart cus.service`. (`cus daemon` re-reads config on every cycle, so no restart needed if running foreground — but systemd-managed processes don't pick up config changes until restart.)

## Auditing swap decisions — who / what / where / when / why

The daemon writes one structured record per cycle to `~/claude-accounts/decisions.jsonl` (size-rotated at ~5MB). Each record captures the active account + usage (who), the action — `swap` / `hold` / `defer` / `would_swap` (what), the affected panes (where), the timestamp (when), and the gate + reason that decided it (why).

```bash
cus decisions                 # last 20 decisions, formatted
cus decisions -n 50           # last 50
cus decisions --swaps-only    # hide routine 'hold' cycles — just swaps/defers
cus decisions --json          # raw JSONL (pipe to jq for analysis)
```

Use this when a swap surprised you ("why did it swap / why didn't it?"). The `[gate]` in brackets names the exact rule: `ladder`, `hard_7d_cap`, `min_improvement_gate`, `usage_growth_gate`, `defer_near_5h_reset`, `burn_before_reset`, `reactive_429`, `lazy_swap` (a cache-aware deferral), or `below_threshold` (nothing to do). The `where` line lists the panes whose prompt cache a background swap would rebuild.

## Checking what's currently configured

```bash
cus config                # effective merged config (defaults + your overrides)
cus list                  # all configured accounts with OAuth identities
cus hooks list            # which Claude Code hooks are installed
```

## Updating to a new version

```bash
cd ~/repos/claude-usage-swap
git pull
systemctl --user restart cus.service
```

If the update changed config schema, `cus config` will show the new fields (with defaults). Edit `config.yaml` if you want to override.

## Uninstalling

```bash
systemctl --user disable --now cus.service
cus hooks uninstall
# Remove statusLine entry from ~/.claude/settings.json (or restore from .bak)
# Optional: rm ~/.config/systemd/user/cus.service
# Optional: rm -rf ~/claude-accounts/  (purely additive; doesn't touch ~/.claude/)
```

The repo itself can be removed by `rm -rf ~/repos/claude-usage-swap`. None of the actual Claude credentials live in the repo — they're all under `~/claude-accounts/` (which is per-machine, not versioned).

## State files reference

Inside `~/claude-accounts/`:

| File | What |
|---|---|
| `config.yaml` | Operator config — edit this |
| `state.json` | Runtime state (active account, per-account thresholds, swap history). Edit only when daemon stopped. |
| `SOS.md` | Latest SOS conditions (daemon updates each cycle; absent when clear) |
| `inbox.md` | Autonomous decisions the daemon made (e.g. force-interrupts at Tier 3) |
| `daemon.log` | Daemon stdout/stderr |
| `decisions.jsonl` | One structured who/what/where/when/why record per cycle — view with `cus decisions` (size-rotated at ~5MB to `.jsonl.1`) |
| `daemon.pid` | Running daemon's PID (used by SOS to detect stale process) |
| `sessions.log` | One line per session start (SessionStart hook) |
| `stops.log` | One line per turn end (Stop hook) |
| `429.log` | Rate-limit detections (PostToolUseFailure hook) |
| `tool_use.log` | Tool-call start/stop signals (PreToolUse + SubagentStop hooks) |
| `account-<name>/` | Per-account `CLAUDE_CONFIG_DIR` — `.credentials.json` + `.claude.json` + `meta.yaml` + symlinks |

## Quick reference: SOS conditions

| Condition | What it means | Action |
|---|---|---|
| `OAuth token expired` | Cached tokens for that account are too old | `cus relogin <name>` → follow login flow → `cus poll` |
| `All accounts blocked` | Every account is rate-limited or expired | Re-login expired ones; wait for rate-limited ones to cool down |
| `Active at NN% and no swap target available` | Current account over threshold, all others blocked | Add another account (`cus add <name>`) or wait |
| `Stale usage data for: <names>` | No fresh poll in >4 cycles | Daemon may be down: `systemctl --user status cus.service` |
| `Daemon pid <N> recorded but process is gone` | Daemon crashed/killed; pid file stale | Restart daemon; then `rm ~/claude-accounts/daemon.pid` if needed |

See `docs/TROUBLESHOOTING.md` for deeper recovery procedures.
