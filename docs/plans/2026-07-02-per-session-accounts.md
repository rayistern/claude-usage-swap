# Plan — per-session account pinning (`mode: per_session`) (2026-07-02)

## North star

Each concurrent Claude Code session is pinned to its **own** account via `CLAUDE_CONFIG_DIR`, instead of all sessions sharing one globally-active account. With N sessions spread over N accounts, each account burns ~N× slower, swaps become **per-session** (only the hot account's sessions relaunch), and — the motivating win — a swap no longer busts the prompt cache of every live session on the machine, only the sessions actually moving.

Success looks like: four tmux panes running four sessions on four different accounts; account A crosses its threshold; only A's pane gets hot-swapped to the account with most headroom; the other three panes never notice. `cus status` shows which pane is on which account; `cus launch` picks the best account for every new session.

## Context / background

**Why now.** User request 2026-07-02: "on every usage swap, the cache busts... we can have four concurrent sessions running each on a different claude account, and still swap if necessary, but necessary will be 4x less frequently."

**Where this sits in the decision history.** The original plan (`docs/plans/2026-05-18-claude-usage-swap.md`, decision 3) chose in-place file-copy swap over `CLAUDE_CONFIG_DIR` env-var switching so "existing tmux/shell launches just work without wrapper or alias plumbing," and explicitly listed concurrent multi-account as out of scope ("architecturally precluded by the in-place file-swap design... use AIMUX's `CLAUDE_CONFIG_DIR` pattern instead"). This plan revisits that trade-off *without* discarding the global mode: `per_session` becomes a second mode, and the two are mutually exclusive per machine (`config.yaml: mode: global | per_session`, default `global`).

**Why the foundation is already half-built.** The 2026-05-19 ARCH DECISION "Unified-tree storage" made every `~/claude-accounts/account-<name>/` a valid `CLAUDE_CONFIG_DIR`: live `.credentials.json` + `.claude.json`, plus symlinks (`projects/`, `plugins/`, `agents/`, `skills/`, `commands/`, `hooks/`, `scripts/`, `memory/`) into `~/.claude/` for shared state. `claude --resume` is already verified to work across `CLAUDE_CONFIG_DIR`s (original plan, decision 5), and `poll_account_usage()` (cus.py:1005) already polls every account independently of which is active. What's missing is: a launch wrapper, complete shared-state coverage (see gaps below), per-session account detection, and an orchestrator that swaps *a subset of panes* instead of the world.

**Verified mechanism (2026-07-02 research pass).** `CLAUDE_CONFIG_DIR` is real and per-process in Claude Code v2.1.197 (appears 20× in the binary; empirically isolates credentials + identity per terminal). It is undocumented-but-supported; native multi-account is an open Anthropic feature request ([anthropics/claude-code#44687](https://github.com/anthropics/claude-code/issues/44687)). Off-the-shelf survey found **no tool that combines per-session binding with usage-driven auto-swap**: AIMUX / claude-code-profiles / claude-swap do per-terminal binding with manual or command-invoked switching; cux does auto-swap but global-only; ccflare / teamclaude are proxies that rotate per-request from a shared pool (no session pinning, MITM in the auth path). Extending cus is the smallest path to both halves.

**Known gaps found in the current account dirs (2026-07-02 inventory):**

1. `settings.json` is a **per-dir stub** (`{"theme": "dark"}`) or absent — a session launched under an account dir today gets **no hooks, no statusline, no permission allowlist**. Must become a symlink to `~/.claude/settings.json` (and `settings.local.json` alongside).
2. Symlink coverage drifted per dir (account-default lacks `skills/`; none link `file-history/`, `paste-cache/`, `plans/`, `session-env/`, `sessions/`, `history.jsonl` — some of these are per-account by design, some should be shared; Phase 1.1 inventories which).
3. `.claude.json` inside each dir holds the account-bound keys (`userID`, `oauthAccount`) **plus** ~37 non-account keys (MCP registrations, per-project state, onboarding flags) that will silently diverge across N live copies without a sync mechanism.

## Interlock with global mode

- `mode: global` (default): today's behavior, untouched. All existing tests keep passing unmodified.
- `mode: per_session`: the daemon **never** writes `~/.claude/.credentials.json` / `~/.claude.json`. `~/.claude/` remains the config dir for *bare* `claude` launches (sessions started outside `cus launch`), which continue on whatever account was last globally active — observed and reported, not orchestrated (see Phase 3.4 policy).
- Mode transitions are explicit commands with validation (Phase 4). No automatic flipping.

## Parallelism map

- **Phase 1.1 (shared-state inventory)** is read-only and independent — safe immediately.
- **Phase 1.2 (dir healing)** depends on 1.1. **Phase 1.3 (`.claude.json` sync)** depends on 1.1 only for the key list; parallel with 1.2.
- **Phase 2 (launch wrapper + detection)** depends on Phase 1 (dirs must be safe to launch into).
- **Phase 3 (per-session orchestration)** depends on Phase 2's detection primitives.
- **Phase 4 (mode commands, migration, docs)** depends on 1–3 but its scaffolding (config schema, validation checks) can be written in parallel with Phase 3.

### Phase 1 — Make account dirs fully live

**1.1 — Shared-state inventory.** Enumerate everything Claude Code reads/writes under a config dir (strace/lsof a scratch session under a scratch `CLAUDE_CONFIG_DIR`, plus diff `~/.claude/` contents against account dirs). Classify each entry: **shared** (symlink to `~/.claude/<sub>`: at minimum `settings.json`, `settings.local.json`, `projects/`, `todos`-equivalents, `plugins/`, `agents/`, `skills/`, `commands/`, `hooks/`, `scripts/`, `memory/`, `file-history/`, `plans/`), **per-account** (`.credentials.json`, `.claude.json`, `sessions/`, `cache/`, `backups/`, `history.jsonl`, `session-env/`?), or **don't-care**. Document the canonical table in `docs/ARCHITECTURE.md` (annotate, don't rewrite, per preserve-the-log).

Demo: the table exists; each row has a tested justification (what breaks if it's per-account vs shared).

**1.2 — Dir healing (`cus doctor --fix-dirs`).** Idempotent pass that brings every `account-<name>/` to the canonical layout from 1.1: create missing symlinks, replace stub `settings.json` with a symlink (preserving the stub's contents into the shared file if it has any non-default keys), report anything unexpected. Runs standalone and as a pre-flight inside `cus launch` and `cus mode per-session`.

Demo: `cus doctor --fix-dirs` on the current tree replaces the `{"theme": "dark"}` stub in account-03 with a symlink; a session launched under account-03 gets the statusline and hooks; re-run is a no-op.

**1.3 — `.claude.json` non-account-key sync.** `cus sync-config [--from <source>]`: merges all non-account-bound top-level keys from a designated canonical (`~/.claude.json` by default) into each account dir's `.claude.json`, preserving each dir's `userID` + `oauthAccount`. Reuses the surgical-merge machinery (`claude_json_for_config_dir()`, cus.py:~605). Runs at `cus launch` time (sync-before-exec, so new sessions always see current MCP registrations) — **never** against a dir with a live session (write race with Claude Code's own rewrites; skip + warn instead). Atomic tempfile+rename as everywhere else.

Demo: register a scratch MCP server in `~/.claude.json`, run `cus sync-config`, verify it appears in all five account dirs with each dir's `oauthAccount` untouched.

Effort estimate: 4–6 hours including the inventory.

### Phase 2 — Launch wrapper + per-session account detection

**2.1 — `cus launch [account] [-- <claude args>]`.** Picks an account — explicit name, or `auto`: reuse `pick_swap_target()`'s headroom scoring (cus.py:1358) over the whole pool, honoring the same preference config (GH #69 prefer-default applies here too) — then pre-flights (doctor + sync + creds freshness), sets `CLAUDE_CONFIG_DIR=~/claude-accounts/account-<name>/`, and `os.execvpe`s `claude` with pass-through args. `cus add`/`cus relogin` already print this env pattern; launch makes it the paved road. Optional shell alias (`claude` → `cus launch auto --`) documented but not auto-installed.

**2.2 — Account detection from the session side.** The SessionStart hook and `cus statusline` currently infer "the account" from global state. In per_session mode both must derive it from their own environment: they run as children of the claude process, so `CLAUDE_CONFIG_DIR` is inherited — read it directly, fall back to global-active when unset (bare launch). `sessions.log` gains nothing new schema-wise (it already records `<session-id>,<account>,<cwd>,<pane>,<ts>`) — the account field just becomes trustworthy per-session.

**2.3 — Pane→account ground truth via `/proc`.** For orchestration we need the account of a *running* pane without trusting logs: read `/proc/<pid>/environ` of the pane's claude process and extract `CLAUDE_CONFIG_DIR` (absent → bare launch → global-active). Add to `find_live_panes()` (cus.py:4299) as a first-priority resolver alongside the existing session-id fallback chain; surfaces in `cus status` as a per-pane account column.

Demo: two panes launched via `cus launch rayi1` / `cus launch rayi2`, one bare pane; `cus status` shows three panes with three different accounts; statusline in each pane names its own account and that account's usage (not the global active's).

Effort estimate: 4–6 hours.

### Phase 3 — Per-session orchestration

**3.1 — Per-account decision loop.** In per_session mode, `decide_swap()` evaluates each account **with live sessions** against its own progressive ladder (existing per-account `next_swap_at_pct` state carries over unchanged). Output changes from "swap the world to X" to a list of (pane, from_account, to_account) moves. Target selection per move reuses `pick_swap_target()` excluding the hot account; multiple panes on the hot account may fan out to different targets (best-headroom-first) rather than piling onto one.

**3.2 — Pane-scoped hot-swap.** `_hot_swap_orchestrate_impl()` (cus.py:4823) already iterates per-pane with tiered prep (wait-for-Stop / pause-inject / force-escape), `/exit`, relaunch. Changes: (a) filter to the hot account's panes only, using 2.3's ground truth; (b) **delete the global `execute_swap()` call from the flow** — there is no global swap in this mode; (c) relaunch command becomes `cd <cwd> && CLAUDE_CONFIG_DIR=<target dir> claude [flags] --resume <id>`. Pinning (`session_locks.pinned`) gains its natural semantics: a pinned pane is never moved off its account (today pinning only exempts from global-swap relaunch).

**3.3 — Polling cadence.** Replace the active/inactive split (`polling.active_interval_seconds` / `inactive_interval_seconds`) with has-live-sessions/idle: accounts hosting live panes poll at the fast cadence, idle pool accounts at the slow one. Keeps total poll volume at today's level with 4 accounts live (mind the 429 backoff budget — this is the same trap as the 2026-06-19 burnout; the differential due-gate from GH #59/#86 must compose, not multiply).

**3.4 — Bare-launch policy.** Sessions started outside `cus launch` sit on `~/.claude/` (last global active). Policy: **observe, never orchestrate** — they appear in `cus status` flagged `bare`, SOS raises a note if a bare session is burning a hot account, but the daemon does not `/exit` them (their config dir can't be re-pointed without the wrapper, and swapping `~/.claude/` under them is exactly the cache-bust we're eliminating). The documented remedy is the shell alias from 2.1.

Demo (the north-star scenario): four panes on four accounts; drive one account past its threshold (or lower its threshold in config); daemon relaunches only that pane onto the best target with `--resume`; other three panes' JSONLs show no gap; `sessions.log` shows the moved session's new account.

Effort estimate: 6–8 hours including tests (suite currently 146 passing; target: per-mode parametrization of decision-loop tests + new environ-resolver and fan-out tests).

### Phase 4 — Mode commands, migration, guardrails, docs

**4.1 — `cus mode per-session` / `cus mode global`.** Explicit transitions with validation: per-session requires every pool account to pass doctor + creds freshness; global requires choosing which account lands in `~/.claude/` (default: current global active, unchanged). Both print a summary of what changed. Walk-back is always `cus mode global` — it restores today's exact behavior because global-mode code paths are untouched.

**4.2 — Guardrail interplay audit.** Sweep the global-mode features for per_session semantics: lazy_swap (still useful, now scoped to the moving pane's cache window), burn_before_reset / defer_swap_near_5h_reset (apply per-account), hard_7d_cap_pct (per-account force-move), SOS conditions, GH #2/#3 drift detection (mostly dissolves — there is no global active to drift from; replace with "pane env vs sessions.log mismatch" check), GH #76 swap lock / crash journal (per-pane moves need the same journal entries), GH #77 freshness guard (simplifies: each dir's creds are refreshed by its own live session, no snapshot-vs-live divergence for pool accounts).

**4.3 — Docs + README.** ARCHITECTURE.md (annotated update: the "cannot run two accounts simultaneously" line gets a dated supersession note pointing here), RUNBOOK (launch/mode/doctor flows), TROUBLESHOOTING (bare sessions, sync skips, symlink breakage), README feature matrix row.

Effort estimate: 4–6 hours.

## Risks / open questions

1. **`CLAUDE_CONFIG_DIR` is undocumented.** Anthropic could change semantics in any release. Mitigation: it's load-bearing for a large tool ecosystem (AIMUX, claude-swap, profiles) and an open feature-request thread; doctor validates behavior empirically after Claude Code updates. Global mode remains the fallback.
2. **`.claude.json` divergence between syncs.** MCP servers registered mid-session in one account dir propagate to siblings only at next `cus launch` sync. Accepted for v1; a file-watcher sync is deliberate non-scope (write races with N live writers).
3. **Which sessions the statusline/hook trusts** if a user manually exports a stale `CLAUDE_CONFIG_DIR` in their shell. Doctor + status flag mismatches; not otherwise defended.
4. **Poll volume.** 4–5 accounts at 180s cadence ≈ 100–130 polls/hour against the OAuth usage endpoint. Watch the 429 backoff logs during rollout week; the memory-documented burnout trap says don't shorten intervals to compensate for anything.
5. **Multi-machine.** Unchanged scope: this plan is single-machine, same as global mode.
6. **7-day windows.** `seven_day: false` on the swap trigger is deliberate (memory: fix via `hard_7d_cap_pct`); per_session mode inherits that stance per-account, no change proposed here.

## Out of scope

- Proxy-based architectures (ccflare/teamclaude style) — rejected: per-request pool rotation defeats session-pinned prompt cache, and MITM in the auth path is a new trust surface.
- Auto-installing the `claude`→`cus launch` shell alias (documented opt-in only).
- Windows/macOS (unchanged from v1 scope).
- Cross-account load-balancing of *subagent* traffic within one session — a session is one account, full stop.
