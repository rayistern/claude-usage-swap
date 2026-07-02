# Architecture — claude-usage-swap

## Auth storage on Linux (verified 2026-05-18)

Per [Claude Code authentication docs](https://code.claude.com/docs/en/authentication): "On Linux, credentials are stored in `~/.claude/.credentials.json` with file mode 0600." No OS-keystore (libsecret/gnome-keyring/kwallet) involvement on Linux. macOS uses Keychain (out of scope for v1).

### `~/.claude/.credentials.json` — 471 bytes

The complete OAuth payload. Structure:

```json
{
  "claudeAiOauth": {
    "accessToken": "...",
    "refreshToken": "...",
    "expiresAt": 1234567890,
    "scopes": ["..."],
    "subscriptionType": "...",
    "rateLimitTier": "..."
  }
}
```

This file is account-bound in its entirety. **Whole-file swap** at this path.

### `~/.claude.json` — ~36 KB, 39 top-level keys

Diff results from `~/.claude.json` vs `~/.claude-merkos/.claude.json` (2026-05-18):

- **21 keys differ between the two files.**
- **16 keys identical.**
- **2 keys only in default, 2 keys only in merkos** (sparse account-specific state).

#### Categorization

**Account-bound (truly tied to OAuth identity):**

- `userID` — Anthropic-side user ID hash. Differs per OAuth account.
- `oauthAccount` — block of 15 fields: `accountUuid`, `emailAddress`, `organizationUuid`, `hasExtraUsageEnabled`, `billingType`, `accountCreatedAt`, `subscriptionCreatedAt`, `ccOnboardingFlags`, `claudeCodeTrialEndsAt`, `claudeCodeTrialDurationDays`, `seatTier`, `displayName`, `organizationRole`, `workspaceRole`, `organizationName`.

*Note: on 2026-05-18 the user's `~/.claude/` and `~/.claude-merkos/` have the SAME `oauthAccount` block because billing is currently shared across what would normally be two accounts. The design must still handle the general case of distinct OAuth identities.*

**Machine-bound (DO NOT swap — preserves user state across account swaps):**

- `mcpServers` — registered MCP servers. Differs significantly (3 vs 8 entries) and is per-machine config.
- `projects` — list of project dirs Claude has interacted with from this config dir.
- `cachedGrowthBookFeatures`, `cachedExperimentFeatures` — server-fetched feature flags, refresh on next launch.
- `claudeAiMcpEverConnected` — MCP connection history (machine-level).
- `githubRepoPaths` — repos accessed from this dir.
- `metricsStatusCache`, `feedbackSurveyState`, `tipsHistory`, `skillUsage`.

**Session-/install-state (DO NOT swap — would falsify counters):**

- `numStartups`, `promptQueueUseCount`, `btwUseCount`, `opus47LaunchSeenCount`, `remoteControlUpsellSeenCount`
- `firstStartTime`, `lastOnboardingVersion`, `lastReleaseNotesSeen`, `migrationVersion`
- `routineFiredWatermark`, `changelogLastFetched`, `closedIssuesLastChecked`
- `hasCompletedOnboarding`, `opusProMigrationComplete`, `sonnet1m45MigrationComplete`
- `installMethod`, `seenNotifications`
- `additionalModelOptionsCache`, `additionalModelCostsCache`, `clientDataCache`, `passesEligibilityCache`
- `overageCreditGrantCache`, `hasResetAutoModeOptInForDefaultOffer`
- `claudeCodeFirstTokenDate`, `penguinModeOrgEnabled`
- `officialMarketplaceAutoInstallAttempted`, `officialMarketplaceAutoInstalled`
- `showSpinnerTree`

## Swap strategy — surgical key-merge

**Decision (2026-05-18):** Whole-file `.claude.json` swap would corrupt machine-level state (especially `mcpServers` and `projects`). Implement surgical key-merge of only the truly-account-bound keys.

### Active-side files (Claude reads these directly)

```
~/.claude.json                  ← merge only userID + oauthAccount on swap
~/.claude/.credentials.json     ← wholesale replace on swap
```

### Storage-side files (one set per account)

```
~/claude-accounts/
  config.yaml
  state.json
  account-<name>/
    credentials.json            ← snapshot of .credentials.json
    claude-identity.json        ← {"userID": "...", "oauthAccount": {...}}
    meta.yaml                   ← human-readable: oauth email, priority, locked-sessions, last-swap-ts
```

We only need to persist 2 files + meta per account. The rest of `~/.claude.json` is shared across all accounts and stays at the canonical path.

### Swap algorithm

```
swap(target_account):
  # 1. Save current account's identity (sanity check before destructive ops)
  current = read_state().active
  with open('~/.claude.json') as f:
    live = json.load(f)
  save_identity(account=current, payload={
    'userID': live['userID'],
    'oauthAccount': live['oauthAccount']
  })  # atomic write to ~/claude-accounts/<current>/claude-identity.json
  copy_atomic('~/.claude/.credentials.json',
              f'~/claude-accounts/{current}/credentials.json')

  # 2. Load target's identity
  target_identity = load_identity(target_account)  # {userID, oauthAccount}

  # 3. Merge into live ~/.claude.json (atomic tempfile + rename)
  live['userID'] = target_identity['userID']
  live['oauthAccount'] = target_identity['oauthAccount']
  write_atomic('~/.claude.json', live)

  # 4. Replace credentials.json (atomic tempfile + rename)
  copy_atomic(f'~/claude-accounts/{target_account}/credentials.json',
              '~/.claude/.credentials.json')

  # 5. Update state.json
  set_active(target_account)
```

All file writes use the tempfile-in-same-dir + `os.rename()` pattern for atomicity. POSIX guarantees rename is atomic when source and target are on the same filesystem.

### Edge cases to handle in Phase 1.3

- **`~/.claude.json` being written by Claude during swap.** Mid-write race. Mitigation: detect by trying to JSON-parse before merging; if parse fails, retry once after 100ms.
- **OAuth token refresh during swap.** If `accessToken` is mid-refresh by Claude itself (it rewrites `.credentials.json` periodically), we could lose the refresh. Mitigation: check `expiresAt` after read; if very close to now, defer swap by a few seconds.
- **Active session with `~/.claude.json` already loaded.** Claude doesn't re-read the file on every operation. Existing sessions continue using whatever they loaded at startup. This is fine for our use case (new sessions get the new identity; existing sessions are hot-swapped via Phase 3 explicitly).
- **Symlink target.** If `~/.claude.json` is a symlink, follow it and atomically replace the target. Future-proofing for users who symlink.

## File-set comparison vs AIMUX

[AIMUX](https://github.com/Digital-Threads/aimux) isolates more files per profile (`policy-limits.json`, `mcp-needs-auth-cache.json`, `stats-cache.json`, `statsig/`, `telemetry/`, `settings.local.json`). We deliberately do not isolate these:

| File | AIMUX isolates | We isolate | Reasoning |
|---|---|---|---|
| `.credentials.json` | Yes | Yes | Auth payload — must follow account |
| `.claude.json` (whole) | Yes (per profile) | No (surgical merge of 2 keys) | Most of file is machine-level — see categorization above |
| `policy-limits.json` | Yes | No | Refreshes on next launch from server |
| `stats-cache.json` | Yes | No | Refreshes on next launch from server |
| `statsig/` | Yes | No | Feature-flag cache, refreshes |
| `telemetry/` | Yes | No | Analytics, machine-level |
| `mcp-needs-auth-cache.json` | Yes | No | MCP state, machine-level |
| `settings.local.json` | Yes | No | Local user settings, machine-level |
| `settings.json` | No (symlinked to `~/.claude/`) | No (shared, default location) | User config |
| `projects/` | No (symlinked) | No (shared, default location) | Conversation history |
| `plugins/`, `agents/`, `skills/`, `commands/`, `memory/` | No (symlinked) | No (shared, default location) | User config |

Our list is tighter because we use the canonical `~/.claude/` location for everything except the 2 swapped files — no symlinks, no profile dirs to maintain in lockstep. The trade-off: we cannot run two accounts simultaneously on one machine. AIMUX can.

> **Annotation 2026-07-02 (superseded in per_session / hybrid modes):** the "cannot run two accounts simultaneously" trade-off holds only for `mode: global`. `mode: per_session` and `mode: hybrid` (plan: `docs/plans/2026-07-02-per-session-accounts.md`, GH #99) add slot dirs — one `CLAUDE_CONFIG_DIR` mount per concurrent session, each holding one account's credentials, swapped in-place per slot. `hybrid` additionally keeps swapping the shared `~/.claude/` mount for bare sessions, so a machine with a mix of slotted and bare sessions has both managed. Global mode remains the default and this section stays accurate for it. See "Per-session slot dirs" below for the full config-dir inventory that replaces the "no profile dirs" simplification in those modes.
>
> **No double-booking invariant (GH #104):** with more than one live mount, every mount must hold a *distinct* account — two live mounts on one account both refresh its single-use OAuth token, rotating it out from under the other and logging that session out. The daemon enforces this in its target picker (a slot swap skips accounts the shared mount or another live slot holds; the shared-mount swap skips accounts any live slot holds), and `cus switch` / `cus launch` refuse to land on a live-mount account unless `--force`. `cus launch` auto-pick spreads across free accounts and errors ("all accounts live/expired/saturated") rather than double-book.

## Per-session slot dirs (`mode: per_session`) — config-dir inventory (2026-07-02)

Storage roles in per_session mode:

| Path | Role | Swapped in place? |
|---|---|---|
| `~/claude-accounts/account-<name>/` | Storage: canonical creds snapshot + identity + meta for one account | No — save-back target only |
| `~/claude-accounts/slot-<n>/` | Live mount: `CLAUDE_CONFIG_DIR` for one session | **Yes** — where per-slot swaps happen |
| `~/.claude/` | Live mount for bare launches (the only mount in global mode) | Global mode: yes. per_session mode: observe-only |

### What lives under a config dir — shared vs per-mount classification

Method (2026-07-02): full listing of the production `~/.claude/` (35 entries), diff against all five `account-<name>/` dirs (which have run real `relogin` sessions since 2026-05-19), plus a scratch-`CLAUDE_CONFIG_DIR` `claude --print` run to observe what Claude Code creates from nothing (it created `projects/<cwd>/`, `sessions/`, `backups/.claude.json.backup.<ts>`, `mcp-needs-auth-cache.json`, `.last-cleanup` — confirming slots must be scaffolded with symlinks *before* first launch, or Claude Code plants real dirs where symlinks belong).

**Shared — symlink to `~/.claude/<entry>`** (one canonical copy; what breaks if per-mount: listed per row):

| Entry | Why shared |
|---|---|
| `settings.json`, `settings.local.json` | Hooks, statusline, permission allowlist. A mount without them runs bare — no SessionStart logging, no statusline, no permissions (today's account dirs have a `{"theme": "dark"}` stub: exactly this bug). Symlinked as *files*. |
| `projects/` | Transcripts keyed by cwd. Sharing is what makes `--resume` work regardless of which mount a session started under (2026-05-19 unified-tree decision, verified then). |
| `file-history/` | Checkpoint/file-history blobs referenced from transcripts; must travel with `projects/`. |
| `paste-cache/` | Pasted-content blobs referenced from transcripts. |
| `plans/` | Plan-mode documents, cross-session. |
| `session-env/` | Per-session-id dirs (uuid-keyed — no cross-mount collision). Needed when a session is resumed under a different mount than it started on. |
| `shell-snapshots/` | Shell-env snapshots referenced by session state; same resume argument. |
| `tasks/`, `todos/` | Task/todo lists keyed by session id; travel with transcripts. |
| `commands/`, `hooks/`, `plugins/`, `scripts/`, `skills/`, `agents/`, `memory/` | User config, account-independent (already in `SHARED_SYMLINK_SUBDIRS`). |

**Per-mount** (each mount its own copy):

| Entry | Why per-mount |
|---|---|
| `.credentials.json` | The account credential — the point of the mount. |
| `.claude.json` | Account-bound keys (`userID`, `oauthAccount`) must differ per mount; the ~37 non-account keys are kept aligned by `cus sync-config`. Lives *inside* non-default config dirs (vs `~/.claude.json` at parent level for the default). |
| `sessions/` | Pid-keyed, process-scoped JSON (observed: `1687352.json`). |
| `backups/` | Claude Code writes its own `.claude.json.backup.<ts>` here (observed in scratch run) and cus rotates creds backups here; both are mount-scoped by meaning. |
| `history.jsonl` | Prompt history. Deliberately NOT shared: Claude Code rewrites it, and a tempfile+rename rewrite would silently replace a symlink with a real file and fork the share. Divergent arrow-up history is a trivial cost; a silently-broken share is not. |
| `cache/`, `stats-cache.json`, `statsig/`, `telemetry/` | Ephemeral / account-scoped caches; refresh from server. |
| `mcp-needs-auth-cache.json`, `policy-limits.json`, `remote-settings.json` | Account/org-scoped server state (observed created fresh per config dir). |
| `.last-cleanup`, `.last-update-result.json`, `daemon*`, `jobs/` | Process-scoped markers and Claude Code background-jobs state; don't-care. |

**Global-only, never seen via `CLAUDE_CONFIG_DIR`:** `validator.log`, `loops.md`, `loops-events.log`, `reconciliations.json`, `projects-hook-archive` — written by user hooks/scripts with absolute `~/.claude/...` paths; they never resolve through a mount.

**Known sharp edge:** anything that rewrites a shared *file* symlink via tempfile+rename (e.g. `/config` writing `settings.json`) replaces the symlink with a real file and silently forks that mount off the share. `cus doctor --fix-dirs` detects real-file-where-symlink-expected, folds any non-default keys back into the shared file, and re-links.

## State machine — per-account thresholds

State is `~/claude-accounts/state.json` (atomic JSON, tempfile+rename):

```json
{
  "active": "default",
  "accounts": {
    "default": {
      "current_5h_pct": 12.4,
      "current_7d_pct": 38.1,
      "next_swap_at_pct": 50,
      "last_swap_ts": "2026-05-18T19:00:00Z"
    },
    "merkos": {
      "current_5h_pct": 0.0,
      "current_7d_pct": 0.0,
      "next_swap_at_pct": 50,
      "last_swap_ts": null
    }
  },
  "swap_history": [
    {"ts": "2026-05-18T19:00:00Z", "from": "default", "to": "merkos", "trigger": "default-5h-50"}
  ]
}
```

`next_swap_at_pct` climbs 50 → 75 → 90 → 100 (force) as the daemon swaps out an account and later swaps it back. Reset to 50 when both windows (5h + 7d) drop below 50% (e.g. weekly reset).
