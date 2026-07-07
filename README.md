# claude-usage-swap (`cus`)

[![CI](https://github.com/rayistern/claude-usage-swap/actions/workflows/ci.yml/badge.svg)](https://github.com/rayistern/claude-usage-swap/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
![Python 3.11+](https://img.shields.io/badge/python-3.11%2B-blue.svg)

Auto-rotate Claude Code OAuth accounts based on usage thresholds. Single-file Python, Linux-only.

> ⚠️ **Read [`SECURITY.md`](SECURITY.md) first.** `cus` reads and moves your Claude OAuth credentials, and it relies on the *undocumented* `CLAUDE_CONFIG_DIR` and OAuth usage endpoint (Anthropic can change these anytime). Whether multi-account rotation fits your subscription's Terms of Service is yours to determine. MIT, no warranty, use at your own risk.

When one of your Claude Pro/Max accounts approaches its 5-hour or weekly cap, `cus` swaps the active credentials to a different account — atomically, with optional hot-swap of in-flight tmux sessions so conversations preserve via `claude --resume`.

> **Status:** v0.1, ready to use. Production-tested on the author's setup. No external maintainers; PRs welcome.

## Is this for you?

`cus` fits a narrow but real profile. It's worth your time if **all** of these are true:

- You have **two or more** Claude Pro/Max accounts (e.g. work + personal, or multiple plans) and legitimately want to spread your own work across them.
- You're on **Linux** (macOS/Windows are out of scope — see [Limitations](#limitations)).
- You're comfortable with a **background daemon** that reads and swaps Claude Code's credential files, plus `systemd --user` and (optionally) `tmux`.

If you just want to *manually* switch between accounts, a lighter tool is a better fit — see [Existing tools](#existing-tools-and-why-this-one). If you're on one account, you don't need this. If any of the above is a "no," `cus` is probably more machinery than you want.

**Before you install, please read [Is this allowed?](#is-this-allowed-anthropics-terms) below.** Rotating accounts to manage caps sits in a gray area of how subscriptions are meant to be used; that section lays out where the line is and why `cus` is designed to stay on the safe side of it.

## What problem this solves

Claude Code's 5-hour and weekly caps are per-account. If you have multiple accounts (work + personal, or multiple plans), the manual workaround is:

1. `/exit` your sessions
2. `export CLAUDE_CONFIG_DIR=~/.claude-account2/`
3. Relaunch with `claude --resume <id>`

Across many concurrent sessions and many machines, this gets old. `cus` automates it:

- Polls each account's usage via the same Anthropic OAuth endpoint Claude Code itself uses for `/usage`
- Per-account progressive thresholds (50% → 75% → 90% → force) yield natural load-balancing across an N-account pool
- Atomic two-file swap (`.credentials.json` + a couple of keys in `~/.claude.json`) — no env-var threading
- Optional hot-swap of live sessions in tmux panes (pause-message injection → `/exit` → `claude --resume <id> "Continue."`)
- SOS subsystem surfaces conditions requiring human action via `cus sos` CLI, `~/claude-accounts/SOS.md`, Claude Code statusLine, and `notify-send`

## Existing tools, and why this one

The multi-account space is not empty, but the tools cluster into buckets that each stop short of what `cus` does. `cus` exists because no single tool does *all* of: **poll-based** per-account progressive thresholds + tmux-aware hot-swap + `CLAUDE_CONFIG_DIR`-style file swap + N-account pool balancing. Where the others land:

**Manual switchers** — you run a command when *you* notice the limit; no daemon, no usage polling:
- [ccrotate](https://github.com/somersby10ml/ccrotate) — JS CLI, `next` / `switch <email>` by hand.
- [claude-swap / `cswap`](https://umesh-malik.com/blog/claude-swap-multi-account-switcher-guide) — fast registered-account switching, no auto-rotation.
- [AIMUX](https://github.com/Digital-Threads/aimux) — manual `CLAUDE_CONFIG_DIR` profile switcher.

**Single-session auto-rotator** — the closest in spirit:
- [cux](https://github.com/inulute/cux) — wraps one session, rotates on an approaching limit with `--resume`, single global threshold. Methodology lifted here and reimplemented in Python; `cus` adds poll-based per-account ladders, an N-account pool, per-session slots, and per-model caps on top.

**Proxy-based** (different architecture — route through a local balancer):
- [teamclaude](https://github.com/KarpelesLab/teamclaude), [ccflare](https://github.com/snipeship/claude-balancer).

**Display-only:**
- [ccusage](https://ccusage.com) — shows usage; doesn't switch.

## Is this allowed? (Anthropic's terms)

Short version: **holding multiple accounts is not itself a Terms of Service violation**, and `cus` is deliberately built to avoid the specific behaviors Anthropic *has* enforced against. But rotating accounts to stay under caps is a gray area, so read this and decide for yourself — this is a personal workflow tool, not something Anthropic endorses.

**What Anthropic has actually enforced against** (January 2026): tools that **extract OAuth tokens from a Claude Code subscription and route them through their own API clients** — OpenCode, Roo Code, Goose and similar had their consumer OAuth tokens blocked. The line Anthropic polices is token exfiltration / reselling / running subscription credentials through third-party clients, plus account *sharing* (multiple people on one plan).

**Why `cus` is designed to stay on the safe side of that line:**
- It **never extracts tokens and never runs its own API client.** It swaps the same `.credentials.json` + `~/.claude.json` keys that Claude Code itself reads, and all traffic goes through the normal `claude` process using Anthropic's own OAuth flow. There is no proxy and no third-party client in the path.
- It uses **your own accounts for your own work** — not shared, not resold. Anthropic's stated position is that holding more than one account is not against the terms; enforcement targets misuse, not ordinary multi-account users.
- Usage is read from the **same OAuth usage endpoint Claude Code uses for `/usage`** — no scraping, no undocumented private API abuse beyond what the client already does.

**Honest caveats:**
- The *purpose* — rotating to avoid hitting a cap — is "limit management" that some would read as "limit evasion." That is the gray part. Use your own judgment.
- Because `cus` depends on Claude Code's on-disk credential format and OAuth endpoints, Anthropic could change either at any release and break it — or decide to discourage multi-account rotation directly. Its usefulness is tied to that posture not changing.
- Don't point two live mounts at *copies* of one login family — that rotates a single-use refresh token and logs one session out. `cus` guards against this automatically (see [No double-booking](#mode-hybrid--manage-slots-and-the-shared-mount-together-2026-07-02)), but it's the mechanical reason account-sharing setups get flagged, so don't defeat the guard.

If any of that is more risk than you want to carry, don't run it. If it's a fit, the rest of this README is the manual.

## Installation

### Requirements

- Linux (macOS / Windows out of scope for v1 — see [Architecture](docs/ARCHITECTURE.md))
- Python 3.11+
- `click` and `pyyaml` (system-wide or via `uv`)
- `tmux` (only needed for hot-swap of live sessions)
- One or more Claude Pro/Max accounts

### One-command install (recommended)

```bash
# 1. Clone
git clone https://github.com/rayistern/claude-usage-swap ~/repos/claude-usage-swap
cd ~/repos/claude-usage-swap

# 2. Run the bootstrap: imports accounts, installs hooks, wires statusline,
#    installs systemd service, symlinks ~/bin/cus or ~/.local/bin/cus.
python3 cus.py install

# 3. From now on, use `cus` (no more `python3 cus.py`):
cus status
cus sos
cus config --explain
```

`cus install` is idempotent. Re-run safely; already-done steps are skipped. Use `--skip-hooks`, `--skip-statusline`, `--skip-systemd`, or `--skip-wrapper` to opt out of individual pieces.

### Manual install (if you want explicit control)

```bash
python3 cus.py init                 # discover + migrate accounts
python3 cus.py hooks install        # install lifecycle hooks
python3 cus.py init-systemd --enable # systemd --user service
# Then manually add statusLine to ~/.claude/settings.json:
# "statusLine": {"type": "command", "command": "python3 /full/path/to/cus.py statusline"}
```

### Uninstall

```bash
cus uninstall                       # stops daemon, removes hooks, statusline, wrapper
cus uninstall --keep-data           # ... but preserves ~/claude-accounts/
```

## Quick reference

```bash
# Setup / teardown (run once per machine)
cus install                   # one-command bootstrap (all-in-one)
cus uninstall                 # reverse install

# Daily inspection
cus whoami                    # which account am I on right now (+ its usage)
cus status                    # active + per-account state + live sessions
cus sos                       # exit 1 + actions if anything needs you
cus list                      # configured accounts with OAuth identities
cus statusline                # one-line (for CC statusLine — usually wired automatically)

# Config — what's configured, how to change
cus config                    # print effective merged config
cus config --explain          # annotated list: every setting + description + current value
cus config --edit             # open ~/claude-accounts/config.yaml in $EDITOR
cus config --path             # print the file path (useful for: `vim $(cus config --path)`)

# Operations
cus poll                      # one-shot usage poll
cus daemon                    # foreground loop (or use systemd)
cus daemon --once             # single cycle and exit
cus daemon --once --no-execute # dry-run (decide, don't swap)

# Manual swap
cus switch <name>             # manual atomic swap
cus switch <name> --dry-run   # preview

# Account lifecycle
cus add <name>                # create a new account dir, print login command
cus add <name> --exec         # ... and launch claude under it
cus relogin <name>            # print login command for an existing account
cus relogin <name> --exec     # ... and launch claude under it
cus rename <old> <new>        # rename an account (preserves config + history)

# Locks
cus pin <pane> <account>      # pin a tmux pane to an account (never swap)
cus unpin <pane>              # remove pin
cus lock <slot>               # freeze a SLOT's account (per_session/hybrid): never swapped or gc'd
cus unlock <slot>             # remove a slot lock

# Rotation-set pools (per_session/hybrid)
cus pool <slot>               # show a slot's pool
cus pool <slot> standard      # retag a slot: premium (honor per-model cap) vs standard (ignore it)

# Same account on multiple sessions (GH #109) — see "Running one account on more than one session"
cus launch <account>          # with per_session.lane_sharing: joins the live lane for that account (no login)
cus login-mount <slot> <acct> # provision an independent login for a 2nd, independently-swappable lane
cus login-mount --list        # show provisioned independent logins

# Low-level (rarely needed after `cus install`)
cus init                      # re-discover + migrate accounts
cus init --force              # refresh stale credential snapshots
cus hooks install/uninstall/list
cus init-systemd --enable     # install + start systemd --user service
```

### Account naming

Folder names are whatever you give at `cus add`. Pick what works for you:
- **Numbered** (`account-01`, `account-02`) — durable; immune to identity changes
- **Word** (`account-work`, `account-personal`, `account-merkos`) — more readable
- **Mixed** — `account-01-work`, etc.

Renaming later is supported: `cus rename <old> <new>`. It updates the folder, `state.json`, `config.yaml`, swap history, and pins atomically.

## How a swap actually happens

Three levels of aggressiveness, controlled by `config.yaml`:

### Level 3: auto-swap for new sessions (default; `hot_swap.enabled: false`)

When an account crosses its threshold step, the daemon:
1. Copies `~/.claude/.credentials.json` and account-bound keys of `~/.claude.json` into `~/claude-accounts/account-<current>/` (preserves any token refresh)
2. Copies the same files *from* `~/claude-accounts/account-<target>/` *into* the live `~/.claude/` location
3. Updates `state.json` with swap history

In-flight Claude sessions keep using whatever creds they loaded at start. New `claude` invocations after the swap use the new account.

### Level 4: hot-swap of live sessions (`hot_swap.enabled: true`)

Adds tier-graded behavior. When threshold is crossed:

- **Tier 1** (first step, default 50%): wait for the session's next `Stop` hook (turn boundary). Defer if cache is still warm (last message < 5 min ago — swap would burn cache rebuild).
- **Tier 2** (default 75%): inject "please pause, we're swapping accounts" via `tmux send-keys` to make Claude wrap up gracefully. Then proceed as Tier 1.
- **Tier 3** (default 90%): send `Escape` to interrupt running tools. Log shell context to `~/claude-accounts/inbox.md` for your review. Then swap.

After the swap, the daemon:
1. Sends `/exit` to the tmux pane (via `tmux send-keys`)
2. Relaunches in the same pane with `cd <cwd> && claude --resume <session-id> "Continue."`
3. The conversation continues from where it left off, now under the new account

### Reactive swap (any level)

The `PostToolUseFailure` hook substring-matches `rate_limit | usage limit | overloaded_error` in tool error bodies. On a hit, the daemon swaps immediately without waiting for the next poll.

### `mode: per_session` — one account per concurrent session (2026-07-02)

Everything above describes `mode: global` (the default): one live mount (`~/.claude/`), every session follows the active account, and a swap moves *all* sessions at once — busting every session's prompt cache. `per_session` mode removes that cost for multi-session workflows:

- Each session launched via `cus launch` gets its own **slot dir** (`~/claude-accounts/slot-<n>/`, used as `CLAUDE_CONFIG_DIR`) holding one account's credentials. Four sessions on four accounts burn each account ~4× slower.
- When an account crosses its ladder step, the daemon runs the **same in-place two-file swap, scoped to that slot** — the session on top keeps running mid-conversation (no `/exit`, no `--resume`), and the other slots' caches are never touched.
- The daemon **never writes `~/.claude/`** in per_session mode: bare `claude` launches keep working on whatever account it holds, observed and SOS-flagged but never swapped. (In `hybrid` mode — below — the daemon *does* manage `~/.claude/` too.)
- Enter/leave with `cus mode per-session` / `cus mode global` (validated, reversible — global-mode code paths are untouched). Optional alias so every launch is slotted: `alias claude='cus launch auto --'`.

**Slot locks & rotation-set pools (per_session / hybrid):**

- `cus lock <slot>` / `cus unlock <slot>` — freeze a slot so the daemon never swaps its account or gc's it (the slot-level counterpart of `cus pin`, which protects a *session* from hot-swap). Shown as `🔒locked` in `cus status`.
- `cus pool <slot> [premium|standard]` (and `cus launch --pool <p>`) — put a slot in a rotation set. **premium** (default) honors the per-model weekly cap (`per_model_weekly.cap_pct`, on when `per_model_weekly.gate_enabled: true`): the slot swaps off an account, and won't swap back, once a tracked model's week hits the cap — e.g. leave a Fable-exhausted account at 97% and don't return until its week resets. **standard** ignores the per-model cap (only aggregate 5h/7d + `hard_7d_cap` apply), so a model-exhausted account keeps serving standard-model work instead of stranding its aggregate headroom. Per-model weekly is a *hard cap*, not a gradual ladder — see `docs/STRATEGIES.md` § "Per-model weekly cap".

### `mode: hybrid` — manage slots AND the shared mount together (2026-07-02)

`global` ignores slots (they freeze on their account); `per_session` ignores bare sessions (observe-only). If a machine has **both** `cus launch` slots and plain `claude` sessions, use `hybrid`: each cycle the daemon moves slots individually (no restart) **and** swaps the shared `~/.claude/` mount for the bare sessions (they follow it together). Reactive 429s are partitioned from a single log read — a slotted session's 429 moves only its slot, a bare session's only the shared mount. Enter with `cus mode hybrid` (same validation as per_session); leave with `cus mode global`.

> **No double-booking (automatic).** Every live mount must stay on a distinct account — two mounts on one account rotate its single-use OAuth refresh token and log one session out. The daemon enforces this: the shared-mount swap never picks an account a live slot holds, and slot swaps never pick the shared mount's (or another slot's) account. `cus switch <acct>` also refuses to move the shared mount onto an account a live slot runs (override with `--force`). So hybrid is safe to run with a mix of slotted and bare sessions (GH #104). When more slots run hot than free accounts exist, the extra slots **hold** on their account rather than double up onto a taken target (2026-07-03 — the old double-up fallback was itself a clobber), and `cus sos` flags any two live mounts caught sharing one login family.

**Passing flags through to `claude` (e.g. `--dangerously-skip-permissions`).** Everything after `--` is forwarded verbatim to the `claude` process the slot execs (`launch` is declared with `ignore_unknown_options`, so unknown flags pass straight through). So a slotted, permission-skipping session is just:

```bash
cus launch auto -- --dangerously-skip-permissions          # auto-pick account
cus launch rayi1 -- --dangerously-skip-permissions --resume X   # explicit account + more flags
alias claude='cus launch auto -- --dangerously-skip-permissions' # make it the default
```

Prefer setting it once in `settings.json` instead of per-launch — see below.

**Skip-permissions via settings (recommended over the flag).** Because slots symlink `settings.json` and `settings.local.json` back to `~/.claude/` (see the storage-roles table in `docs/ARCHITECTURE.md`), there is **one** settings file to edit and every slotted session inherits it — you do *not* configure per-account. The file-based equivalent of `--dangerously-skip-permissions` is, in `~/.claude/settings.local.json`:

```jsonc
{
  "permissions": { "defaultMode": "bypassPermissions" },
  "skipDangerousModePermissionPrompt": true   // skip the one-time acceptance dialog too
}
```

Caveats: `bypassPermissions` is ignored when Claude runs as root, and a managed/policy `permissions.disableBypassPermissionsMode` overrides it. The per-account `~/claude-accounts/account-*/settings.json` stubs (`{"theme":"dark"}`) apply only to *direct* `CLAUDE_CONFIG_DIR=account-<name>` launches (the `cus add` / `cus relogin` flow), not to slotted `cus launch` sessions — those follow the symlinked `~/.claude/` settings.

Details: `docs/plans/2026-07-02-per-session-accounts.md` (design + decision history) and the storage-roles inventory in `docs/ARCHITECTURE.md`.

### Running one account on more than one session (2026-07-02, GH #109)

A live mount's `.credentials.json` is one OAuth login family, and Anthropic rotates the refresh token on every ~hourly refresh. So **two live mounts that hold *copies* of one account clobber** — one session gets silently logged out (the reason the no-double-book guard above exists). There are two safe ways to put 2+ sessions on the same account, and they compose:

**1. Lane sharing (`per_session.lane_sharing: true`) — the no-login default.** A "lane" is just a slot dir that hosts *more than one* session. With lane sharing on, `cus launch <account>` **joins** the live lane already holding that account instead of minting a second mount:

```yaml
per_session:
  lane_sharing: true
```

```bash
cus launch rayi1      # first session → lane on rayi1 (seeded from rayi1's existing login)
cus launch rayi1      # second session → JOINS that same lane (shared login)
cus launch auto       # when EVERY healthy account is already live, joins the lowest-usage
                      # lane instead of refusing (2026-07-03) — incl. the shared ~/.claude
                      # mount (that launch runs as a bare session on the active account)
```

Both sessions share one config dir and one login; a swap of that lane moves them together (one shared cache-bust). **No extra login, ever** — one account lives on one lane, seeded free from the login it already has. This is the right choice when same-account sessions can swap as a group (they usually can — they're together because that account is best, and they'd leave it together when it gets hot).

**2. Independent logins (`independent_logins.use_independent_logins: true`) — the escape hatch.** Only needed when you want one account on **two *independently*-swappable lanes** (one leaves at its cap while the other keeps riding). That genuinely requires a second login family, and only an interactive `/login` can mint one:

```bash
# provision a distinct login for (slot-9, rayi1) — interactive, one time:
cus login-mount slot-9 rayi1          # prints the CLAUDE_CONFIG_DIR=… claude command to run
#   run it, log in AS rayi1, /exit, then:
cus login-mount slot-9 rayi1 --finish # verifies it landed on rayi1 (refuses a wrong-account login) + records it
# now launch a 2nd, independent rayi1 lane (the no-double-book guard lifts because it's provably independent):
cus launch rayi1 --lane slot-9
```

Supporting commands:

- `cus login-mount <slot> <account> --from-existing` — seed a lane's login from the account's on-disk snapshot with **no browser login** (for a single-mount account). Not independent from the snapshot, so only safe as that account's sole lane.
- `cus login-mount --list` — show every provisioned login (identity, fingerprint, assumed expiry).
- The store lives at `~/claude-accounts/logins/<account>/<slot>/`. `cus status` tags each lane (`[indep-login ✓]` / `[copy]`), and `cus sos` flags expired, near-expiry, wrong-account, or **family-collision** (two lanes sharing one login family) conditions.

Config keys (both default **off** — behavior is unchanged until you opt in):

```yaml
per_session:
  lane_sharing: false            # true = `cus launch` joins the live lane for an account (share, swap together)
independent_logins:
  use_independent_logins: false  # true = swaps install/save-back the per-(slot,account) independent login when one is provisioned
  refresh_token_ttl_days: 30     # assumed refresh-token lifetime — drives only the sos near-expiry nudge (unconfirmed; see #109 Phase 0)
  warn_expiry_within_days: 5
```

Design + the experiment that established it: `docs/plans/2026-07-02-seamless-swap-independent-logins.md` and issue #109.

## Progressive thresholds (the novel bit)

Each account has its own `next_swap_at_pct` field. Starts at 50; climbs through `[75, 90, force]` each time we swap *out* of it. Reset to 50 when both windows drop below `reset_below_pct`.

Result: account A swapped out at 50%; we drain B until 50%; back to A but it's still at ~50%, so it doesn't trip again until 75%; etc. Naturally load-balances across the pool instead of hammering A → swap → A → swap.

## SOS — when human action is needed

The daemon can't fix everything autonomously. When tokens expire, all accounts hit cap, or the daemon itself crashes, it surfaces an SOS through four channels:

- **`cus sos` CLI** — exit 1 + printed actions; exit 0 = all clear
- **`~/claude-accounts/SOS.md`** — written every cycle when conditions exist, deleted when clear
- **Claude Code statusLine** — shows `🚨 cus SOS: <reason>` instead of the normal `cus:<account>...`
- **`notify-send` desktop notification** — fires on signature changes (no spam)

Conditions checked:
- OAuth token expired on any account → `cus relogin <name>`
- All accounts blocked → re-login or wait
- Active over threshold with no swap target → add another account
- Stale poll (>4 cycles missed) → daemon may be down
- Daemon pid recorded but process gone → restart

See [docs/TROUBLESHOOTING.md](docs/TROUBLESHOOTING.md) for the full catalog with recipes.

## Storage layout

```
~/claude-accounts/
  config.yaml                     # operator config (edit this)
  state.json                      # runtime state (don't edit while daemon running)
  SOS.md                          # current SOS conditions (or absent if clear)
  inbox.md                        # autonomous decisions (Tier 3 force-interrupts)
  daemon.log                      # daemon stdout/stderr
  account-default/                # a full CLAUDE_CONFIG_DIR
    .credentials.json
    .claude.json
    meta.yaml
    projects/ → ~/.claude/projects/   # symlinks for shared state
    plugins/, agents/, skills/, commands/, memory/, hooks/, scripts/ (symlinks)
  account-merkos/
    ...
  slot-1/, slot-2/ ...            # per_session/hybrid live mounts (one per session/lane)
  logins/<account>/<slot>/        # GH #109 per-(slot,account) independent logins (opt-in)
    .credentials.json             #   an independent OAuth family (distinct from the account snapshot)
    provenance.json               #   mint time, source email, refresh-token fingerprint, bootstrapped flag
```

Each `account-<name>/` directory is a fully-functional `CLAUDE_CONFIG_DIR`. To log into a new account: `CLAUDE_CONFIG_DIR=~/claude-accounts/account-<name>/ claude` (or use `cus add <name>` to create + launch in one step).

The live `~/.claude/` is the "currently active" mount point that Claude Code reads from when no env var is set. Swap = copy from `account-<target>/` into `~/.claude/`.

## Config reference

`~/claude-accounts/config.yaml`:

```yaml
accounts:
  - name: default
    priority: 1
  - name: merkos
    priority: 1

poll_interval_seconds: 300       # daemon polls every N seconds

strategy: lowest_usage           # lowest_usage | drain | strict_priority | round_robin

thresholds:
  steps: [50, 75, 90]            # progressive ladder
  five_hour: true
  seven_day: true
  reset_below_pct: 50

hot_swap:
  enabled: true                  # false = level 3 (new sessions only)
  tier_2_at_pct: 75
  tier_3_at_pct: 90
  pause_message: "please pause your current thought — we're swapping accounts..."
  wake_up_message: "Continue where you left off."
  cache_bust_window_seconds: 300
  mid_turn_idle_seconds: 30
  stop_wait_timeout_seconds: 300
  pause_response_timeout_seconds: 120
  relaunch_mcp_timeout_ms: 120000   # MCP_TIMEOUT (ms) on relaunch; widens the
                                    # per-stdio-server startup budget so slow MCP
                                    # servers register their tools on --resume
                                    # instead of being dropped (GH #25). 0 = use
                                    # Claude Code's 30000ms default.
  relaunch_stagger_seconds: 2.0     # sleep between per-pane relaunches so their
                                    # MCP cold-starts don't all contend for CPU
                                    # (GH #25). 0 = relaunch all at once.

subagent_skip:
  enabled: true
  defer_below_tier: 3            # tier_3 force proceeds regardless

reactive:
  enabled: true                  # detect 429s via PostToolUseFailure hook

session_locks:
  pinned: {}                     # {pane_or_session_id: account_name}
  never_restart_patterns: []     # regex list

hooks:
  install_session_start: true
  install_stop: true
  install_post_tool_use_failure: true
  install_pre_tool_use: true
  install_post_tool_use: true     # GH #27 — pairs with pre_tool_use to close the
                                  # start/stop ledger so subagent_active doesn't
                                  # misfire on every successful tool call.
  install_subagent_stop: true
```

Run `cus config` to see the effective merged config.

## Claude Code skill — `/cus`

Manage `cus` from any Claude Code session via natural language. Install:

```bash
cp ~/repos/claude-usage-swap/skills/cus.md ~/.claude/commands/cus.md
```

Then in any Claude Code session:

```
/cus check my status
/cus how should I make swapping less aggressive
/cus add a new account called work
/cus my merkos account's token expired
/cus what does the strategy setting do
```

The skill orients itself first (runs `cus sos` + `cus status`), interprets your intent, and walks through the right `cus` commands. It won't drive interactive OAuth flows (those need a browser); it'll tell you what to run.

## Documentation

- [docs/RUNBOOK.md](docs/RUNBOOK.md) — day-to-day operations
- [docs/STRATEGIES.md](docs/STRATEGIES.md) — swap strategy reference + future directions (`staggered_straddle`, `cost_aware`, `quota_predictive`, multi-machine coordination, ...)
- [docs/TROUBLESHOOTING.md](docs/TROUBLESHOOTING.md) — SOS catalog + common issues
- [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) — file layout, swap algorithm, state machine
- [docs/AUDIT.md](docs/AUDIT.md) — code audit findings; known issues tracked for future work
- [docs/CONTRIBUTING.md](docs/CONTRIBUTING.md) — PR / development guide
- [docs/plans/](docs/plans/) — original build plan (AVC methodology)
- [docs/journal/](docs/journal/) — session-scoped writeups
- [inbox.md](inbox.md) — load-bearing autonomous decisions with walk-back paths

## Walk-back / uninstall

Everything is reversible:

```bash
systemctl --user disable --now cus.service
cus hooks uninstall                            # removes our hook entries from settings.json
# Manually remove "statusLine" key from ~/.claude/settings.json (or restore from .bak)
mv ~/claude-accounts ~/claude-accounts.removed # purely additive; doesn't touch ~/.claude/
```

Conversation history, plugins, agents, etc. live in `~/.claude/` and are never modified by `cus`.

## Limitations

- **Linux only.** macOS Keychain handling is non-trivial; out of scope for v1.
- **OAuth (Pro/Max) accounts only.** API-key / Bedrock / Vertex auth not supported.
- **One active account at a time per machine.** The file-copy swap model precludes concurrent multi-account on one machine. Use AIMUX if you need that.
- **Cross-machine coordination not implemented.** If you run `cus daemon` on multiple machines, they don't coordinate — two daemons could pick the same swap target.
- **No SessionEnd hook in Claude Code** — session liveness is inferred from JSONL transcript mtime + Stop hook recency. Long-idle sessions may be wrongly marked dead.

## License

MIT (see [LICENSE](LICENSE)).

## Credits

Methodology lifted from [cux](https://github.com/inulute/cux). Anthropic OAuth usage endpoint contract verified live against the same API Claude Code uses for `/usage`.
