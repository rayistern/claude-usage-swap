# Journal — 2026-05-18 (claude-usage-swap planning)

## Context

Initial planning arc for a new tool that monitors Claude Code account usage and auto-rotates the active OAuth identity by file-copy swap when configurable thresholds are crossed. The user wanted a build plan produced via AVC methodology (and gym integration if available — gym MCP was disconnected so AVC alone).

The arc spans architecture survey (existing tools), mechanism validation (where auth actually lives on Linux), design decisions (lift-not-fork; file-copy vs CLAUDE_CONFIG_DIR; progressive per-account thresholds 50→75→90→force), and phase boundaries (Phase 1+2 for v0.1, hot-swap deferred to Phase 3+). Triggered by conversational request "How can we configure Claude Code to switch accounts as it sees usage approaching the limit?"

## What shipped this session

### Methodology framing

- AVC pipeline overview, plan template, journal template, inbox template, autonomous-collaboration doc read end-to-end before producing artifacts.
- AVC discipline hook verified installed (`/home/rayi/.claude/hooks/avc_methodology_hook.sh` ✓, settings.json reference ✓, Tier 2 LLM-review gated by env var ✓).
- Gym MCP unavailable this session — flagged in inbox, plan written under AVC alone.

### Architecture survey

- Two parallel research agents dispatched: one for cux internals (returned full file:line map of reusable vs replaceable parts), one for broader account-rotation landscape (returned 6 tools; verdict: gap is real, no exact match exists).
- Tools evaluated: cux (Go wrapper, in-place cred swap, single threshold — closest match but overengineered for our case), teamclaude (proxy), AIMUX (manual CLAUDE_CONFIG_DIR switcher), ccflare (proxy with dashboard, 972 stars), claude-code-hub (Postgres+Redis, team-scale), claudeusage-mcp (signal source only).
- Verified: `--dangerously-skip-permissions` does NOT bypass hooks (Claude Code authentication + permission-modes docs). Cross-`CLAUDE_CONFIG_DIR` `--resume` works. `claude --resume` waits for prompt — wake-up message required.

### Mechanism validation

- Direct inspection of `~/.claude/.credentials.json` and `~/.claude-merkos/.credentials.json` confirmed Linux auth is a single 471-byte file with structure `{claudeAiOauth: {accessToken, refreshToken, expiresAt, scopes, subscriptionType, rateLimitTier}}`. No OS-keystore involvement on Linux per official docs.
- Direct inspection of `~/.claude.json` keys (39 top-level) confirmed `oauthAccount` block + `userID` are the account-bound fields; rest is caches/stats.
- Conclusion: minimum swap footprint is two files totaling ~37 KB. Sub-millisecond atomic copy via tempfile+rename.

### Artifacts produced

- `docs/plans/2026-05-18-claude-usage-swap.md` — full build plan, 6 phases with parallelism map, 7 open questions, explicit out-of-scope list.
- `docs/journal/2026-05-18-claude-usage-swap-planning.md` — this file.
- `inbox.md` — 3 entries (1 flag, 2 decisions including 1 ARCH DECISION).
- `~/repos/claude-usage-swap/` — git repo initialized, no commits yet (awaiting user authorization).

## Design calls worth preserving

**File-copy swap, not `CLAUDE_CONFIG_DIR` env-var swap.** AIMUX uses env-var swap; the user's existing `~/.claude-merkos/` follows the same pattern. We deliberately chose file-copy because: (a) existing tmux/shell launches work without wrapper or alias plumbing; (b) one canonical `~/.claude/` location matches user mental model; (c) trade-off is no concurrent multi-account on one machine — which the user said they don't need. Documented in plan §"Decision history" and inbox entry on architecture.

**Progressive per-account thresholds (50→75→90→force).** Novel vs cux/AIMUX. User's framing: "swap to account B at 50% on account A, then again after we swapped back to account A at 50 on account B swap to account B again at 75%." Per-account state machine in `state.json` tracks `next_swap_at_pct` per account; climbs after each return. Yields natural load-balancing across N accounts without needing a separate scheduler.

**Lift cux patterns, do not fork.** Patterns lifted: Stop-hook turn-boundary signaling, PostToolUseFailure 429 substring-match, signature-keyed hook installer, `--resume <id> "Go continue."` wake-up. NOT lifted: cux's keystore-swap and transactional rollback (unnecessary on Linux because `.credentials.json` IS the keystore). Python implementation will reference origin file:line in comments for attribution. Inbox ARCH DECISION entry has full reasoning + walk-back.

**v0.1 = Phase 1+2 only.** Hot-swap deferred. Validate per-account state machine on real workload for several days before adding tmux pane registry, Stop hook tailing, and pause-message injection. 80/20 reasoning: new-session swap delivers most user-visible value; hot-swap adds clustered risk.

**AVC's plan_template adopted for an engineering project.** AVC is primarily research/ML-oriented in practice, but the plan_template (north star + phases with parallelism map + out-of-scope + open questions) maps well to engineering work. The committee/red-team stages were skipped because the prior conversation already surfaced the relevant objections (cux overengineering, cache-bust tax, subagent-skip scenarios, fast-loop sessions).

## Hand-off — Phase 1 ready to start

Next worker (likely same session or next iteration) picks up Phase 1.1 (account-file inventory):

1. Diff `~/.claude.json` against `~/.claude-merkos/.claude.json`. Document differing keys.
2. Confirm the differing keys are exactly `oauthAccount`, `userID`, possibly some cache fields that are safe to swap. If extra non-account state shows up, escalate to plan open-question #2 (surgical key-merge vs whole-file swap).
3. Output: short `docs/ARCHITECTURE.md` noting the canonical account-bound file set.

After 1.1 lands, 1.2 (`cus init` migration script) and 1.3 (`cus switch` manual swap CLI) are independent enough that they could be split across two sessions if useful. Phase 2 starts only after Phase 1 is stable on real workload (recommend ≥48 hours of manual swap usage with no corruption before automating).

Concrete first command after this planning arc:

```bash
python3 -c "
import json
a = json.load(open('/home/rayi/.claude.json'))
b = json.load(open('/home/rayi/.claude-merkos/.claude.json'))
diffs = sorted(k for k in set(a)|set(b) if a.get(k) != b.get(k))
print('Keys that differ:', diffs)
"
```

## Non-blocking flags for future work

- **Gym MCP integration revisit when reconnected.** Open plan question #6. Likely candidates: `gym_drill` for stress-testing swap atomicity, `gym_audit_coding` for code review at end of Phase 2.
- **Cross-machine coordination.** Explicitly out of scope for v1. If the user starts running `cus daemon` on more than one machine simultaneously, two daemons could pick the same swap target and double-burn an account. Park as future GH issue.
- **OAuth refresh during swap window.** If the active account's `accessToken` is mid-refresh when we swap (the token file is being rewritten by Claude Code), we could lose the refresh. Mitigation in Phase 1.3: read+verify token file integrity before saving back to account dir; retry if mid-write.
- **Stats-cache + policy-limits not swapped (v0.1).** AIMUX isolates these; we don't. May cause confusing stats display in Claude Code itself after a swap. Watch for during Phase 2 testing; add to swap list if it matters.

## Numbers

- Plan: 1 file, ~220 lines, 6 phases + 7 open questions
- Journal: 1 file (this one)
- Inbox entries: 5 total (1 flag + 4 ARCH/scope decisions)
- Research agents dispatched: 6 (cux internals, cux competitors, AIMUX mechanism, deep alternatives, dangerouslySkipPermissions hook interaction, ccusage JSON format, cux OAuth API source)
- Bash inspections of local config: 4 (~/.claude/ structure, ~/.claude-merkos/ structure, credentials file structures, claude.json key diff)
- Tools used: AVC (`check_hook_status`, `start_journal`, `post_inbox_entry` ×5), TaskCreate ×16, TaskUpdate ×16, Bash, Read, Write, Edit, Agent
- Commits: 4 (planning artifacts, Phase 1 init+switch, Phase 2 daemon+OAuth+hooks, Phases 3-6 hot-swap+controls)
- Lines shipped: cus.py 1936 LOC + hooks 5×~30 LOC bash + docs (plan 202 + journal 87 + architecture 165) + README + inbox

## Phase ship summary (final)

All 6 phases shipped in this single session:

- **Phase 1 — Foundations.** `cus init`, `cus list`, `cus status`, `cus switch`. Two-file surgical swap (`.credentials.json` whole-file + `userID`+`oauthAccount` keys in `~/.claude.json`). Atomic via tempfile+rename.
- **Phase 2 — Auto-rotation daemon.** `cus daemon`, `cus poll`, `cus config`, `cus hooks install/uninstall/list`. Direct Anthropic OAuth usage API call (lifted from cux). Strategy picker: `lowest_usage` / `drain` / `strict_priority` / `round_robin`. Progressive per-account threshold ladder 50→75→90→100. 5 hook scripts shipped, signature-keyed installer.
- **Phase 3 — Hot-swap Tier 1.** `find_live_sessions`, tmux integration, cache-bust window detector (5-min default), `hot_swap_orchestrate`. Live sessions waited-for-Stop, exited + relaunched via `claude --resume <id> "Continue."`.
- **Phase 4 — Tier 2.** Pause-message injection via `tmux send-keys -l` + Enter, with configurable `pause_message` and `pause_response_timeout_seconds`.
- **Phase 5 — Tier 3 + 429 reactive + subagent skip-guard.** `Escape` to interrupt running tools; shell context logged to `inbox.md` with walk-back. `check_rate_limit_reactive()` reads `429.log` against a watermark for sub-poll-interval reactive swaps. `subagent_active()` reads `tool_use.log` PreToolUse/SubagentStop counters.
- **Phase 6 — Operator controls.** `cus pin/unpin` (config-driven session_locks), `cus statusline`, `cus init-systemd --enable`, enriched `cus status` output with TOKEN_EXPIRED/RATE_LIMITED/POLL_ERROR flags.

Notable bug surfaced + fixed during smoke testing: HTTP 429 from the OAuth usage endpoint is *rate-limited account*, not *token expired*. Original code zeroed out `current_*_pct`, which made the strategy picker pick the rate-limited account as swap target. Fixed: 429 → set utilization to 100/100 + flag `rate_limited: true`; strategy picker filters these out. Verified live on rayi's setup (`merkos` is currently rate-limited; daemon correctly refuses to swap to it).

## Hand-off — testing on real workload

Before enabling hot_swap or installing hooks, the user should:

1. Re-login merkos (or whichever stale account) via `CLAUDE_CONFIG_DIR=~/.claude-merkos claude` to refresh creds, then `python3 cus.py init` to re-import the fresh token.
2. Run `python3 cus.py poll` to confirm both accounts return non-error utilization.
3. Run `python3 cus.py daemon --once --no-execute` repeatedly to watch the decision engine react.
4. When confident, `python3 cus.py daemon` in a dedicated tmux pane and let it run.
5. For hot-swap testing: `python3 cus.py hooks install`, edit `~/claude-accounts/config.yaml` `hot_swap.enabled: true`, then run a real Claude Code session and force it past threshold.
6. systemd setup once stable: `python3 cus.py init-systemd --enable`.
