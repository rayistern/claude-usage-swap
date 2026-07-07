#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = [
#     "click>=8.0",
#     "pyyaml>=6.0",
# ]
# ///
"""claude-usage-swap (cus) — auto-rotate Claude Code OAuth accounts.

Phases:
  1. Foundations: cus init / list / status / switch.
  2. Auto-rotation: cus daemon polls usage, swaps on progressive thresholds.
  3. Hot-swap of live sessions, Tier 1 (wait-for-Stop).
  4. Tier 2 (pause-message injection).
  5. Tier 3 (force interrupt + 429 reactive + subagent skip-guard).
  6. Operator controls (pin, whitelist, statusline, systemd unit).

Storage:
  ~/claude-accounts/
    config.yaml                  Account list + thresholds + poll interval + hot-swap.
    state.json                   Runtime state (active + per-account thresholds + history).
    sessions.log                 SessionStart hook output (one line per new session).
    stops.log                    Stop hook output (one line per turn end).
    429.log                      Rate-limit detections: StopFailure (primary, real
                                 model-level 429s) + PostToolUseFailure (secondary,
                                 best-effort subagent-internal API errors).
    tool_use.log                 PreToolUse + SubagentStop signals.
    daemon.log                   Daemon stdout/stderr when running.
    inbox.md                     Decisions made autonomously by the daemon.
    account-<name>/
      credentials.json           Snapshot of .credentials.json (whole-file).
      claude-identity.json       Surgical extract: {userID, oauthAccount}.
      meta.yaml                  Human-readable: oauth email, priority, locks, ts.

Swap mechanism (verified 2026-05-18 — see docs/ARCHITECTURE.md):
  Two-file swap on Linux:
    - ~/.claude/.credentials.json   wholesale replace (471 bytes — the entire
                                    OAuth payload per Claude Code's auth docs)
    - ~/.claude.json                surgical key-merge of only `userID` and
                                    `oauthAccount` (21 of 39 keys differ between
                                    accounts but most are machine-level state)

  Both file writes use tempfile-in-same-dir + os.replace() for POSIX-atomic
  rename. Source-account state is saved back to its dir before the new account
  state is laid down, so any single swap is reversible by running
  `cus switch <previous-name>` again.

Methodology lifted (not code) from cux (github.com/inulute/cux):
  - OAuth usage API call shape (internal/usage/usage.go:84-135)
  - Strategy picker patterns (drain/balanced) (internal/strategy/strategy.go)
  - Hook-based turn-boundary signaling
  - PostToolUseFailure 429 substring-match (internal/hooks/hooks.go:765-800)
    [A2 2026-06-23: superseded as the PRIMARY 429 source by a StopFailure hook —
    PostToolUseFailure structurally never sees model-level 429s (those fire
    StopFailure with a categorized `error` enum); the old loose substring match
    manufactured false positives from downstream tool output.]
  - --resume <id> "Go continue." wake-up pattern (wrapper.go:180-185)
NOT lifted: cux's keystore-swap, because on Linux .credentials.json IS the
keystore — no libsecret/keychain dance is needed (Claude Code auth docs are
authoritative on this point).
"""

from __future__ import annotations

import contextlib
import fcntl
import hashlib
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import click
import yaml

# --------------------------------------------------------------------------
# Paths
# --------------------------------------------------------------------------

HOME = Path.home()
CLAUDE_JSON = HOME / ".claude.json"
CLAUDE_DIR = HOME / ".claude"
CREDS_JSON = CLAUDE_DIR / ".credentials.json"
ACCOUNTS_DIR = HOME / "claude-accounts"
STATE_JSON = ACCOUNTS_DIR / "state.json"
CONFIG_YAML = ACCOUNTS_DIR / "config.yaml"
SESSIONS_LOG = ACCOUNTS_DIR / "sessions.log"
STOPS_LOG = ACCOUNTS_DIR / "stops.log"
RATE_LIMIT_LOG = ACCOUNTS_DIR / "429.log"
TOOL_USE_LOG = ACCOUNTS_DIR / "tool_use.log"
DAEMON_LOG = ACCOUNTS_DIR / "daemon.log"
DECISIONS_LOG = ACCOUNTS_DIR / "decisions.jsonl"   # GH #56: one structured record per daemon decision
DAEMON_PID = ACCOUNTS_DIR / "daemon.pid"
ORCHESTRATE_LOCK = ACCOUNTS_DIR / "orchestrate.lock"
# GH #76: how long a second swap waits for the global swap lock before
# erroring out loudly. Swaps are sub-second; 30s covers even a pathologically
# slow disk. Module constant (not config) so tests can shrink it without a
# config file. The lock/journal PATHS are functions (below, next to
# _swap_lock) rather than constants: tests repoint ACCOUNTS_DIR at a temp
# tree, and a path captured at import time would silently escape the sandbox
# and touch the live ~/claude-accounts/.
SWAP_LOCK_TIMEOUT_SECONDS = 30.0
INBOX_MD = ACCOUNTS_DIR / "inbox.md"
SOS_MD = ACCOUNTS_DIR / "SOS.md"
LAST_NOTIFY = ACCOUNTS_DIR / ".last_notify.json"

# GH #79: how many timestamped `.credentials.json.bak.<ts>` files to keep per
# directory. Each account's OAuth payload (incl. the ~30-day refresh token)
# lives in exactly ONE authoritative place at a time, so every overwrite of a
# credentials file is potentially the destruction of the only copy — the
# terminal event of every clobber bug (#3, #70, #75, #76, #77). Backups are
# ~500 bytes each; 5 generations costs nothing and converts "account locked
# out until interactive re-login" into "run `cus restore-creds <name>`".
CREDS_BACKUP_KEEP = 5

# Hook scripts ship in <repo>/hooks/. Daemon installs them into ~/.claude/settings.json
# at user request via `cus hooks install`. Source paths discovered relative to cus.py.
HOOKS_SRC_DIR = Path(__file__).resolve().parent / "hooks"
HOOK_SETTINGS_KEY = "cus"  # signature key in settings.json so we don't clobber other tools

# Inside ~/claude-accounts/account-<name>/:
#   - .credentials.json    valid OAuth payload (Claude Code reads this when
#                          CLAUDE_CONFIG_DIR points at this dir)
#   - .claude.json         per-account config — minimally has userID + oauthAccount
#   - meta.yaml            our metadata (priority, locked_sessions, ts)
#   - projects/ → ~/.claude/projects/  (symlink — shared history)
#   - plugins/ → ~/.claude/plugins/    (symlink — shared)
#   - agents/, skills/, commands/, memory/ → likewise symlinked when present
#
# Each account dir is a VALID CLAUDE_CONFIG_DIR. To log in to an account,
# `CLAUDE_CONFIG_DIR=~/claude-accounts/account-<name>/ claude /login`.
#
# 2026-07-02 (per_session plan, Phase 1.1 inventory): extended beyond the
# original 8 with the transcript-adjacent dirs (file-history, paste-cache,
# session-env, shell-snapshots, tasks, todos, plans). Rationale per entry in
# docs/ARCHITECTURE.md "Per-session slot dirs": these are all referenced from
# projects/ transcripts or keyed by session id, so sharing them is what makes
# `--resume` + checkpoints work under any mount. Symlinks are only created
# when the target exists in ~/.claude/ (no dangling links), so installs that
# lack e.g. todos/ are unaffected.
SHARED_SYMLINK_SUBDIRS = [
    "projects", "plugins", "agents", "skills", "commands", "memory", "hooks", "scripts",
    "plans", "file-history", "paste-cache", "session-env", "shell-snapshots", "tasks", "todos",
]

# Files (not dirs) symlinked from every mount to the canonical ~/.claude/ copy.
# settings.json carries hooks + statusline + permission allowlist: a mount
# without it runs BARE (no SessionStart logging, no statusline) — the
# account-dir `{"theme": "dark"}` stub found in the 2026-07-02 inventory is
# exactly this failure. File symlinks have a sharp edge dirs don't: anything
# that rewrites the file via tempfile+rename (e.g. /config) replaces the
# symlink with a real file and silently forks that mount off the share.
# `cus doctor --fix-dirs` detects real-file-where-symlink-expected, folds
# novel keys back into the shared file, and re-links.
SHARED_SYMLINK_FILES = ["settings.json", "settings.local.json"]

# per_session mode (docs/plans/2026-07-02-per-session-accounts.md): slot dirs
# are LIVE mounts — one CLAUDE_CONFIG_DIR per concurrent session, each holding
# one pool account's credentials, swapped in-place per slot exactly like
# ~/.claude/ is swapped in global mode. Account dirs stay storage-only.
SLOT_PREFIX = "slot-"

# How long a slot stays RESERVED after acquire_slot/create_slot claims it, so a
# launch that hasn't exec'd claude yet (no PID on the mount) still looks busy to
# a concurrent launch and to the daemon's gc (review findings 2026-07-02:
# acquire TOCTOU + gc-mid-launch). Must comfortably exceed the acquire → doctor
# → sync → execute_swap → execvpe window; once claude is live, mount_in_use
# takes over and the reservation is irrelevant. A crashed pre-exec launch's
# reservation simply expires and the slot becomes reusable.
SLOT_RESERVATION_SECONDS = 120

# The full list of keys that are tied to OAuth identity — verified empirically
# against the user's two config dirs (2026-05-18). All other keys in
# ~/.claude.json are machine-level state and stay put during a swap.
ACCOUNT_BOUND_KEYS = ["userID", "oauthAccount"]

# Threshold ladder per the design — climbs each time we return to an account
# that we previously swapped out of. 100 sentinel = "force swap, ignore window".
THRESHOLD_STEPS = [50, 75, 90, 100]

# OAuth usage API — same endpoint cux uses, same endpoint Claude Code itself
# uses for /usage. Per cux/internal/usage/usage.go: this beta header was
# verified live 2026-05-01. Both override-able via env for future shifts.
USAGE_API_URL = os.environ.get("CUS_USAGE_ENDPOINT", "https://api.anthropic.com/api/oauth/usage")
USAGE_API_BETA = os.environ.get("CUS_USAGE_BETA", "oauth-2025-04-20")
USAGE_API_TIMEOUT_SECONDS = 10
USAGE_API_RESPONSE_LIMIT_BYTES = 256 * 1024

# Anthropic messages endpoint — the clock_keepalive ping (2026-07-07) POSTs the
# smallest possible model request here to START/keep an account's 5h usage
# window ticking (see DEFAULT_CONFIG["clock_keepalive"] for the full WHY). We
# authenticate with the account's OAuth (subscription) bearer token, exactly as
# `claude -p` does; a subscription OAuth token is only accepted on this endpoint
# when the request carries the Claude Code identity — the `anthropic-beta:
# oauth-2025-04-20` header AND a leading Claude Code system prompt — so both are
# sent below. Env-overridable for future endpoint/version shifts. A wrong
# endpoint/shape degrades gracefully: the ping just fails, is logged
# (CRED-AUDIT decision=failed), and the daemon moves on.
MESSAGES_API_URL = os.environ.get("CUS_MESSAGES_ENDPOINT", "https://api.anthropic.com/v1/messages")
ANTHROPIC_VERSION = os.environ.get("CUS_ANTHROPIC_VERSION", "2023-06-01")
# The system-prompt marker Claude Code sends so a subscription OAuth token is
# accepted on /v1/messages. Kept verbatim — the endpoint checks for it.
KEEPALIVE_SYSTEM_PROMPT = "You are Claude Code, Anthropic's official CLI for Claude."
KEEPALIVE_TIMEOUT_SECONDS = 15

# OAuth token endpoint + Claude Code's public client id. Shared by two callers:
#   - the claim-time refresh-token liveness probe (#127), and
#   - token_self_refresh (#96, 2026-07-02), which mints a fresh access token
#     for an inactive account whose stored token aged out (the token_stale
#     condition, GH #13) before poll_account_usage gives up.
# Same well-known pair the ecosystem (cux, opencode, ...) uses for the refresh
# grant Claude Code itself performs — extracted 2026-07-02 via `strings` on the
# installed @anthropic-ai/claude-code binary's own OAuth client config (the
# `Tys` prod object), not a published API. Verified live 2026-07-03 against a
# known-dead branch: platform.claude.com (and api.anthropic.com) answer 400
# {"error":"invalid_grant"}; the old console.anthropic.com path 404s, and
# urllib's default User-Agent is Cloudflare-blocked (403 code 1010) — hence
# OAUTH_USER_AGENT below, which BOTH callers must send. Env-overridable like the
# usage endpoint above, for future shifts. A wrong endpoint degrades gracefully:
# the #127 probe fails-open (verdict "unknown" → claim proceeds unverified, i.e.
# pre-#127 behavior); the #96 refresh fails-silent (returns False → falls
# through to the pre-existing token_stale flag) — never a hard failure.
OAUTH_TOKEN_URL = os.environ.get("CUS_OAUTH_TOKEN_ENDPOINT", "https://platform.claude.com/v1/oauth/token")
OAUTH_CLIENT_ID = os.environ.get("CUS_OAUTH_CLIENT_ID", "9d1c250a-e61b-44d9-88ed-5944d1962f5e")
OAUTH_USER_AGENT = os.environ.get("CUS_OAUTH_USER_AGENT", "claude-cli/2.0.0 (external)")
OAUTH_TOKEN_TIMEOUT_SECONDS = 15
# token_self_refresh (#96) uses a slightly tighter timeout than the #127 probe —
# it runs inline in the poll path where a slow endpoint delays every stale
# account's poll, whereas the probe runs once at claim time.
OAUTH_REFRESH_TIMEOUT_SECONDS = 10


# --------------------------------------------------------------------------
# Defaults — everything overridable via config.yaml
# --------------------------------------------------------------------------

DEFAULT_CONFIG: dict[str, Any] = {
    # Which live-mount topology this machine runs (mutually exclusive):
    #   global      — today's behavior: one live mount (~/.claude/), the daemon
    #                 swaps it in place, every session follows the active account.
    #   per_session — N slot dirs under ~/claude-accounts/, one per concurrent
    #                 session (launched via `cus launch`); the daemon swaps
    #                 individual slots and NEVER writes ~/.claude/ (bare
    #                 launches are observe-only). Transition via `cus mode`.
    "mode": "global",
    "per_session": {
        # How long a slot may sit idle (no live session, per /proc) before the
        # daemon gc's it — saving its credentials back to the owning account
        # dir and removing the mount. Long default: a free slot is a REUSE
        # candidate for the next `cus launch` (no swap needed when it already
        # holds the right account), so eager reaping costs more than it saves;
        # the risk being bounded is dead mounts accumulating real credentials.
        "slot_gc_idle_hours": 72,
        # Rotation-set (pool) a slot joins when it doesn't declare one at launch
        # (GH #99). Two emergent pools segment slots by the work they do:
        #   - "premium": the per-model weekly gate applies — the slot swaps OFF
        #     an account (and won't swap back onto it) once a tracked model's
        #     week reaches per_model_weekly.cap_pct. For sessions doing premium-
        #     model work (e.g. Fable) where a model-exhausted account is useless.
        #   - "standard": the per-model gate is IGNORED — only aggregate 5h/7d +
        #     hard_7d_cap apply. A model-exhausted account still has aggregate
        #     headroom that serves standard-model work, so standard slots keep
        #     using it instead of stranding that headroom.
        # Pools are NOT static account lists — an account is "in" whichever pool
        # a given slot's rules let it serve, decided live from usage. Only
        # meaningful in per_session mode (global mode has one active account for
        # every session, nothing to segment). Set per slot via
        # `cus launch --pool <p>` or `cus pool <slot> <p>`.
        "default_pool": "premium",
        # GH #109 lanes: when true, `cus launch <account>` JOINS the live mount
        # already holding that account (multiple sessions share one config dir /
        # login and swap together) instead of allocating a fresh slot. This is
        # the no-extra-login path: one account lives on one lane, seeded from the
        # login it already has. The one-account-per-live-mount invariant (#104)
        # still holds — lanes SATISFY it by sharing rather than copying. Default
        # off keeps today's 1-session-per-slot behavior. To run one account on
        # two INDEPENDENTLY-swappable lanes instead of sharing, use the
        # independent-login escape hatch (independent_logins, below).
        "lane_sharing": False,
    },
    # GH #109: per-(slot, account) independent-login store.
    #
    # Today a slot's live .credentials.json is a COPY of one account's canonical
    # snapshot (execute_swap -> atomic_copy(account-<name>/.credentials.json)).
    # Anthropic rotates the OAuth refresh token on every ~hourly refresh, so two
    # live mounts seeded from the same snapshot diverge and one gets its refresh
    # token blanked — the #104/#103/#3 clobber family. The #109 experiment
    # (2026-07-02) proved two INDEPENDENT `/login` flows for the SAME account
    # yield two independent refresh-token families that don't invalidate each
    # other, so one account can safely back multiple live mounts if each mount
    # uses its own independent login.
    #
    # Phase 1 (shipped) is ADDITIVE: the on-disk store, the `cus login-mount`
    # provisioning command, and status/sos surfacing. `use_independent_logins`
    # is the Phase 2 gate — when OFF (default) the swap path is bit-for-bit the
    # copy path we ship today; when ON, execute_swap prefers a stored
    # independent login for the (slot, account) pair and only falls back to the
    # shared-snapshot copy when no login is provisioned. Kept OFF until the
    # store is populated and Phase 2's swap-path change lands (backward-compat
    # per the non-breaking-by-default rule).
    "independent_logins": {
        "use_independent_logins": False,
        # Per-account POOL depth (2026-07-03, login-pool model): how many
        # INDEPENDENT backup login families to provision per account at
        # onboarding (`cus login-mount <account>` run this many times). A lane
        # that must swap onto an account another live mount already holds borrows
        # a FREE family from this pool — a distinct refresh-token family, so no
        # #104 clobber. pool_size does NOT hard-cap families (you may log in
        # more); it only guides onboarding + the pool-exhaustion SOS "provision
        # another" target. N backups ⇒ up to N+1 concurrent live mounts can
        # safely share one account's quota (primary snapshot + N families).
        "pool_size": 3,
        # Assumed OAuth refresh-token lifetime. UNCONFIRMED — the #109 Phase 0
        # to-CONFIRM list still owes a measured idle-expiry number; 30 days is
        # the community-reported figure and only drives the sos near-expiry
        # nudge, never a hard action. Revise once Phase 0 measures it.
        "refresh_token_ttl_days": 30,
        # sos warns this many days before a provisioned login's assumed expiry
        # so the operator can re-login before a swap would fall back to a copy.
        "warn_expiry_within_days": 5,
        # Claim-time liveness (#127, 2026-07-03): before installing a pooled
        # family, prove its refresh token is still ALIVE via a refresh-token
        # grant (the ONLY definitive check — expired access tokens look
        # identical on dead and live branches; rayi1/family-1 vs family-2
        # incident). Dead stores are retired + the next family is tried;
        # network/endpoint trouble FAILS OPEN to today's install-as-is.
        # False ⇒ bit-for-bit pre-#127 claim behavior.
        "verify_family_on_claim": True,
    },
    # PRE-EMPTIVE creds-health early-warning (2026-07-06). Separate from the
    # REACTIVE blank-mount detection/auto-heal (GH #141 + lane follow-up), which
    # fires an URGENT SOS AFTER a live mount's creds have already blanked/logged
    # out. This layer scans every LIVE mount each cycle and emits a WARNING (not
    # urgent) BEFORE failure — when a mount's OAuth access token is near expiry
    # and not refreshing, or its refresh token is nearing its assumed TTL and
    # will soon need a browser re-login. It NEVER makes a network grant to probe
    # (refresh tokens are single-use — a probe would rotate/clobber the live
    # family, GH #104); it judges purely from the stored expiresAt / token
    # presence / provenance age. Backward-compatible: default-on but only ever
    # adds soft WARNINGs, never a hard action, and the reactive path is untouched.
    "creds_warn": {
        # Master gate. False ⇒ the pre-emptive scan is skipped entirely and
        # behavior is bit-for-bit the reactive-only pre-2026-07-06 daemon.
        "enabled": True,
        # Warn when a live mount's OAuth access token expiresAt is within this
        # many MINUTES of now AND the mount isn't observed refreshing. A healthy
        # session refreshes its own token well ahead of expiry (fresh tokens sit
        # hours out), so a token this close to expiry that hasn't been rewritten
        # recently is one that isn't self-maintaining — the state that precedes a
        # stale/blank mount if a refresh later fails. Heads-up only (self-heals
        # on next use); ~45 min gives lead time without noising on normal churn.
        "access_expiry_minutes": 45,
        # A mount whose creds file was (re)written within this many minutes is
        # treated as "recently refreshed" and suppressed from the access-expiry
        # warning — an actively-maintained mount that just rotated its token is
        # not at risk even if we catch it momentarily near a stale expiresAt.
        "recent_refresh_minutes": 10,
        # ── Fix 1 (2026-07-07 blank-mount PREEMPT) ───────────────────────────
        # The soft near-expiry-AND-idle WARNING above (condition 1) fires just
        # BEFORE the classic blank-mount failure: Claude Code's OWN in-place
        # OAuth refresh rewrites the mount's .credentials.json as the token
        # nears expiry, and if that refresh fails mid-write it leaves the file
        # BLANK (accessToken:"" / expiresAt:0) → the live session shows "Not
        # logged in · Run /login" (the 2026-07-07 tabby-5/slot-7 incident). The
        # reactive auto-heal restores the file but Claude Code just re-blanks on
        # its next failed refresh, AND a file-heal can't un-stick a process that
        # already cached the logged-out state. So instead of only WARNING when
        # this condition holds, PROACTIVELY refresh the lane's mount token via
        # cus's OWN refresh grant (_refresh_account_token) and rewrite fresh,
        # valid creds into the mount BEFORE Claude Code's failure-prone in-place
        # refresh can blank it — keeping the mount ahead of expiry so Claude
        # Code never needs its (fragile) refresh. Degrades to the existing
        # warn+heal on any failure; only ever touches a live lane's own mount.
        "preempt": {
            # Master gate. False ⇒ no proactive refresh; behavior is the
            # warn+heal-only pre-2026-07-07 daemon (bit-for-bit).
            "enabled": True,
            # Poll-burnout backoff: at most one proactive-refresh ATTEMPT per
            # lane per this many minutes, whether it succeeds or fails. Without
            # it a persistently-failing grant would re-fire every poll (the
            # 2026-06-19 burnout shape). A SUCCESSFUL refresh is naturally
            # rate-limited far longer than this — a fresh token sits hours out,
            # so the near-expiry-AND-idle trigger can't re-arm until it ages
            # back down; the cooldown only bounds the FAILURE-retry cadence.
            "cooldown_minutes": 15,
        },
        # ── Fix 2 (2026-07-07 STUCK-SESSION escalation) ──────────────────────
        # A file-heal is SILENT success today even when the live session stays
        # wedged in "not logged in" — the running process cached the logged-out
        # state and only a RELAUNCH clears it (hot_swap disabled here, so nothing
        # auto-relaunches). Detect the heal→blank→heal cycle: a lane healed
        # >= heal_threshold times within window_minutes means the file-heal is
        # NOT helping and the operator must relaunch. Emits a distinct URGENT
        # SOS naming the exact relaunch command.
        "stuck": {
            "heal_threshold": 2,     # >= this many heals ...
            "window_minutes": 30,    # ... within this rolling window → escalate
        },
    },
    "poll_interval_seconds": 300,
    "strategy": "smart",  # smart | headroom | lowest_usage | drain | strict_priority | round_robin
    "thresholds": {
        "steps": [50, 75, 90],   # 100 force is implicit
        "five_hour": True,       # apply progressive thresholds to 5h window
        "seven_day": True,       # apply progressive thresholds to 7d window
        "reset_below_pct": 50,   # when both windows drop below this, reset next_swap_at to first step
    },
    # Fix B (user 2026-06-23 "never swap to a 100% account"): a fully-saturated
    # target is a hard wall, not a degraded-but-usable option. Applied as a HARD
    # universal filter in pick_swap_target with NO degraded fallback — if every
    # candidate is at/above this, the picker returns None (stay put) rather than
    # bounce onto an exhausted account and immediately re-trip the same 429s. The
    # sub-100% gray zone is intentionally left to the scoring / would-re-trip
    # logic ("below 100% is a judgment call" — user). 100 = only block truly
    # exhausted accounts; lower it to keep a wider safety margin.
    "never_swap_to_pct": 100,
    # 72-hour seven_day reset detection (field finding 2026-07-05, gist:
    # monperrus/3ac4b303a84946bbeaf2b1123ee99491). The Claude `seven_day`
    # "weekly" cap actually REFRESHES every ~72h at a fixed UTC anchor
    # (~04:50-05:00 UTC), NOT every 7 days — observed 72.0h ±0.6h over three
    # cycles. The API's seven_day.resets_at is misleading: it reports ~7 days
    # out (when the oldest tokens age off), not when you actually get fresh
    # budget. We detect the REAL reset from its observable signature — a sharp
    # DROP in current_7d_pct between two consecutive real observations — and
    # project the next one 72h forward. Both knobs are exposed so they stay
    # tunable if Anthropic changes the cadence.
    "seven_day_reset_hours": 72,       # projected gap between real 7d refreshes
    "seven_day_reset_drop_pct": 15,    # min pct-point drop that counts as a reset (not noise)
    "smart_strategy": {
        "hard_7d_cap_pct": 80,           # force-swap active when 7d crosses this; never SWAP TO an account above
        "burn_window_hours": 2,          # if 5h resets within N hours and clock is ticking, boost score
        "burn_soon_weight": 1.0,         # multiplier on burn-soon bonus
        "five_hour_headroom_weight": 0.6,
        "seven_day_headroom_weight": 0.4,
        # Continuous reset-proximity preference (GH #42). Beyond the within-
        # burn_window bonus above, gently prefer the account whose 5h window
        # resets sooner, across the FULL 5h horizon, so target selection always
        # leans toward the earlier-resetting account ("consider how much time
        # is left on the 5h clock"). Scaled small so it only tips genuine ties;
        # the 0-100 headroom score + burn-soon bonus still dominate. 0 = off.
        "reset_proximity_weight": 8.0,
        "cold_account_penalty": 0,       # 0 = none; >0 deprioritizes accounts with 5h=0% (clock not ticking)
        # When True, the picker may select a rate_limited account if no
        # better option exists. Useful because the `rate_limited` flag
        # conflates two failure modes: (a) Anthropic's polling-endpoint
        # IP throttle (account itself still works) vs (b) account-cap hit
        # (account unusable). We can't reliably distinguish from a 429
        # response alone. Default False = safer (avoid landing on a
        # potentially-capped account). Set True if you observe the flag
        # firing despite the account working elsewhere (other machine, etc.).
        "allow_rate_limited_targets": False,
    },
    "headroom_strategy": {
        "hard_7d_cap_pct": 80,
        "five_hour_weight": 0.7,
        "seven_day_weight": 0.3,
    },
    # Proactive burn-before-reset trigger (GH #42). Strategy-independent: the
    # ladder/cap/reactive triggers only fire when the ACTIVE account climbs
    # toward its own cap — they never move us ONTO an account whose 5h window
    # is about to reset. But a 5h window is "use it or lose it": when it rolls
    # over, accrued usage resets to zero, so the right place to be just before
    # a reset is ON the about-to-reset account (draining its remaining
    # capacity) while leaving the far-from-reset account's window pristine.
    # Fires at TIER 1 only (wait-for-Stop) so it never interrupts a live turn.
    "burn_before_reset": {
        "enabled": True,
        "reset_window_minutes": 30,        # target's 5h must reset within this
        "min_reset_gap_minutes": 60,       # active's 5h must reset >= this much later
        "min_candidate_headroom_pct": 15,  # target's unused 5h (100-5h%) must exceed this
    },
    # Complement of burn_before_reset (GH #51): decline a LADDER swap when the
    # active account's OWN 5h window is about to reset anyway. At ~70% climbing
    # toward the step with the 5h reset minutes away, swapping is pointless
    # churn — the reset drops 5h to ~0 on its own and resets the ladder. "We
    # could have just easily waited" (user 2026-05-28). The reactive-429 path
    # is the backstop if usage spikes to the cap before the reset lands.
    "defer_swap_near_5h_reset": {
        "enabled": True,
        "wait_window_minutes": 15,   # defer the ladder swap only if 5h resets within this
        "max_defer_pct": 90,         # ...unless 5h is already this high (too close to cap to risk waiting)
    },
    "hot_swap": {
        "enabled": False,        # Phase 3+ — opt-in. Smoke-test before enabling (see 2026-05-19 incident notes).
        "tier_2_at_pct": 75,
        "tier_3_at_pct": 90,
        "pause_message": (
            "please pause your current thought — we're swapping Claude "
            "accounts to avoid hitting the usage cap. You'll resume on the "
            "other side; finish what you're saying briefly and stop."
        ),
        # wake_up_message: kept in config for backward-compat but NOT sent
        # as a positional arg after 2026-05-19 rewrite — the positional
        # became a billed first user message + triggered Stop hook fire.
        "wake_up_message": "",
        "cache_bust_window_seconds": 300,   # defer Tier 1 swap if last msg < this old
        "mid_turn_idle_seconds": 30,        # session counted idle if no JSONL line in N s
        "stop_wait_timeout_seconds": 300,   # Tier 1 max wait for Stop signal
        "pause_response_timeout_seconds": 120,  # Tier 2 wait after pause-message
        "shell_return_timeout_seconds": 10, # poll pane_current_command after /exit, up to N seconds
        # Flags passed to relaunched `claude` invocation. Empty by default;
        # set to "--dangerously-skip-permissions" if that's how you normally
        # run claude. Space-separated string; passed as-is to the shell.
        "relaunch_flags": "",
        # When True (default), relaunch is `claude --resume <session-id>` to
        # preserve conversation. Set False to relaunch FRESH (just `claude`,
        # no --resume) — useful for stagnant or loop-driven sessions that
        # don't need conversation continuity, and avoids loading the old
        # JSONL (which may have hook-leaked content from upstream issues).
        "relaunch_with_resume": True,
        # MCP_TIMEOUT (ms) prefixed onto the relaunch command's env (GH #25).
        # Claude Code bounds each stdio MCP server's startup connect attempt by
        # MCP_TIMEOUT (default 30000ms). On `claude --resume`, EVERY MCP server
        # is re-spawned and re-handshaked from scratch — nothing is inherited.
        # A slow-cold-starting server (observed: gym ~8s standalone) that misses
        # this window has its tools dropped for the WHOLE session, with no stdio
        # retry and no mid-session auto-reconnect. An orchestrated swap relaunches
        # many panes near-simultaneously, so each pane's MCP cold-starts contend
        # for CPU and the 8s balloons past 30s — gym's tools silently vanish on
        # resume while faster servers (avc, orchestra) survive. Raising the
        # budget to 120s gives slow servers headroom; the high value only costs
        # wall-time when a server is genuinely hung. Set 0 to not set the env var
        # at all (inherit Claude Code's 30000ms default).
        "relaunch_mcp_timeout_ms": 120000,
        # Seconds to sleep between successive per-pane relaunches (GH #25).
        # Attacks the root cause of the MCP-cold-start contention above: firing
        # N `claude --resume` at once means N×(stdio server cold-starts) fight
        # for the same cores. Staggering lets each pane's MCP servers connect
        # before the next pane piles on. 0 disables staggering (relaunch all at
        # once, the pre-GH#25 behavior).
        "relaunch_stagger_seconds": 2.0,
        # Lookback window (seconds) for the tool_use.log "session is in active
        # multi-step work" check that feeds session_is_idle (GH #33). When the
        # JSONL has been stale longer than mid_turn_idle_seconds but the
        # session has had ANY tool entry within this window, treat it as
        # mid-turn for pause-message / Escape routing. The 2026-05-26 incident:
        # panes running tools every ~30s with one occasional ~3min gap were
        # classified idle by the JSONL-mtime check alone, so the pause-message
        # was skipped and /exit interrupted them mid-work. The default 300s
        # (5 min) catches typical tool-call cadences with text-generation gaps;
        # operators who want more aggressive routing (warn anything that
        # touched a tool in the last 10 min) can bump higher, or lower it
        # toward 30s if they prefer the GH #4 caution.
        "activity_lookback_seconds": 300,
        # Lookback for the post-/exit session-id walkback (GH #32). After
        # /exit + wait_for_shell, if the chosen session_id still has no
        # JSONL it's a ghost — walk backwards through sessions.log entries
        # for the same pane, picking the most recent within this window
        # whose JSONL exists. Default 1 hour balances "find the user's last
        # real session" vs "don't reopen something from yesterday." If
        # nothing in the window has a JSONL, the relaunch is plain `claude`
        # (no --resume) per the GH #20 directive.
        "relaunch_walkback_lookback_seconds": 3600,
    },
    # GH #56: cache-aware lazy background swap. Consulted ONLY in background-
    # swap mode (hot_swap.enabled == False — the credential file is swapped
    # globally and live Claude Code sessions re-read it on their next request,
    # so a swap never interrupts anyone). Its ONLY cost is a one-time prompt-
    # cache rebuild on the live sessions that make their next call on the new
    # account. So DEFER a non-urgent (deferrable) swap while any live session is
    # still cache-warm — wait until the cache is cold (last activity older than
    # cache_window_seconds, Anthropic's ~5min prompt-cache TTL), when the swap
    # is free. Urgent swaps (hard 7d cap / 5h saturation / reactive 429 →
    # SwapDecision.deferrable == False) are NEVER deferred here, so we never
    # strand a session riding into a rate-limit. Implements the user directive
    # (2026-06-08): "swapping frequently just busts cache; we can wait until
    # cache is busted anyway, or until a high percent."
    "lazy_swap": {
        "enabled": True,
        "cache_window_seconds": 300,
    },
    # GH #59: 5h-window reset inference. The statusline reset countdown already
    # ticks live per-render; these make the DAEMON act on it too rather than
    # waiting for the next poll to learn a window reset.
    #   - countdown fallback (`enabled`): when a poll did NOT refresh an account
    #     past its 5h reset (poll failed / in backoff / token stale), trust the
    #     countdown — the window has rolled to ~0 — and write that into state so
    #     decide_swap + SOS use reality instead of the stale pre-reset %. The
    #     next successful poll supersedes it.
    #   - adaptive_repoll: wake just after the soonest known 5h reset (+buffer)
    #     to repoll promptly, instead of flying on the inferred value for a full
    #     poll_interval. Only ever SHORTENS the sleep; floored by min_repoll.
    "reset_inference": {
        "enabled": True,
        "adaptive_repoll": True,
        "repoll_buffer_seconds": 20,
        "min_repoll_seconds": 30,
    },
    # token_self_refresh (2026-07-02, GH #13 follow-up): before flagging an
    # inactive account token_stale, attempt a plain OAuth refresh_token grant
    # against the same undocumented endpoint the `claude` CLI itself uses (see
    # OAUTH_TOKEN_URL / _refresh_account_token). Costs no usage quota (a pure
    # auth call, not an inference call) and creates no session. ANY failure —
    # network, non-200, malformed response — falls straight through to the
    # pre-existing token_stale flag; a successful refresh is a pure bonus,
    # never a new failure mode. Set `enabled: false` to revert to the
    # pre-2026-07-02 behavior (never talk to this endpoint) if Anthropic ever
    # changes its shape and this starts erroring on every stale account.
    "token_self_refresh": {
        "enabled": True,
        # Fix 4 (2026-07-07 un-stale idle accounts). An idle account whose stored
        # access token aged out sits `token_stale` FOREVER even though its refresh
        # token is still valid — nothing exercises it, so the poll-time
        # _refresh_account_token above may never clear it (it's gated by `enabled`
        # and folded into a poll). That causes (a) chronically wrong cus readings
        # and (b) "stale snapshot → blank on install" when a lane swaps onto it.
        # This mints a fresh access token via a DIRECT OAuth refresh grant
        # (`_oauth_refresh_grant`, #127 — no session, no billed tokens), writes it
        # back to the account SNAPSHOT, and clears token_stale.
        #   * `unstale_idle_on_poll: True` — each daemon cycle, sweep accounts
        #     still flagged token_stale (not token_expired) and un-stale them.
        #     False ⇒ only `cus force-poll` un-stales (the operator's manual lever).
        #   * `unstale_cooldown_minutes: 10` — poll-burnout backoff: at most one
        #     grant attempt per account per this many minutes (force-poll bypasses
        #     it — an operator explicitly asked). A DEAD refresh token
        #     (invalid_grant) is left flagged and NOT retried tightly: it genuinely
        #     needs a browser relogin.
        "unstale_idle_on_poll": True,
        "unstale_cooldown_minutes": 10,
    },
    # clock_keepalive (2026-07-07 operator directive) — keep every account's
    # 5-hour usage window continuously TICKING by pinging DORMANT accounts.
    #
    # The mechanic being exploited: Claude's 5-hour usage window is a FIXED
    # window that STARTS on an account's first model request after a dormant
    # period and RESETS ~5h later. An account nothing exercises (no lane making
    # requests) has a DORMANT clock — its 5h window is not running, so it will
    # not reset on any predictable cadence and there is no "fresh capacity in
    # ~5h" coming from it. If instead we keep every account's clock ticking, all
    # accounts cycle through 5h windows continuously and reset on a rolling
    # basis — so the fleet always has some account minutes-from-fresh, which is
    # exactly the predictable rolling capacity the rotation daemon wants to pick
    # from. The operator did this by hand today (CLAUDE_CONFIG_DIR=account-rayi4
    # claude -p "hi" --model claude-haiku-4-5-20251001 started rayi4's clock);
    # this block makes it a first-class, config-gated daemon feature.
    #
    # The ping is a MINIMAL model request (1 user token "hi", max_tokens=1, the
    # cheapest model) sent DIRECTLY to the Anthropic messages endpoint with the
    # account's OAuth bearer token — read-only w.r.t. every live mount/lane (it
    # never writes any lane's .credentials.json), and wrapped so a failed ping
    # only logs and moves on, never crashing the poll. After a successful ping
    # the account is force-re-polled so `five_hour_resets_at` reflects the
    # now-ticking window.
    #
    # Backward-compatible: an ABSENT block uses these defaults (feature ON, per
    # the operator directive). Set `enabled: false` to revert to the
    # pre-2026-07-07 behavior (dormant accounts stay dormant).
    "clock_keepalive": {
        # Master toggle. Operator wants it ON. False ⇒ the keepalive sweep is
        # skipped entirely (bit-for-bit pre-feature behavior).
        "enabled": True,
        # The model id the ping uses — cheapest available so the keepalive costs
        # as close to nothing as possible while still counting toward the 5h
        # window (any model request starts/keeps the window).
        "model": "claude-haiku-4-5-20251001",
        # Cooldown: an account is pinged at most once per this many MINUTES.
        # Default ~290 — just under the 5h / 300-min window — so a dormant
        # account gets re-primed shortly after each reset (keeping the clock
        # essentially always ticking) WITHOUT hammering the endpoint. Recorded
        # per account as `last_keepalive_ping_ts` in state.json.
        "min_ping_interval_minutes": 290,
        # Optional account scoping. `accounts` (allowlist): when non-empty, ONLY
        # these accounts are eligible for keepalive pings; empty/absent ⇒ all
        # enabled, non-disabled, valid-creds accounts. `exclude` (denylist):
        # accounts never pinged even if otherwise eligible. Both default empty.
        "accounts": [],
        "exclude": [],
    },
    "subagent_skip": {
        "enabled": True,
        # If a subagent (or any in-flight tool) is detected, skip that pane's
        # restart at tiers below this number. Tier 3 ALWAYS bypasses this
        # guard (GH #27) — at saturation we cannot afford to defer. Note:
        # since GH #27 the guard skips just the affected pane, not the whole
        # orchestration; the cred swap and other panes' restarts proceed.
        "defer_below_tier": 3,
    },
    # Burn-rate estimator (C, 2026-06-23). Between polls, extrapolate each
    # account's CURRENT usage from its last poll + measured %/min burn rate, so
    # the picker's "is this target too full to swap onto" judgment isn't fooled
    # by a stale-but-climbing number ("estimate where default is based on the
    # last ping and the rate of increase" — user). Only ever moves an estimate
    # UPWARD from the polled value, so it can make target selection MORE
    # conservative but never less. Falls back to raw polled % when no rate has
    # been measured yet (first poll after a swap/reset).
    "estimator": {
        "enabled": True,
        "max_extrapolation_minutes": 10,   # don't trust a measured rate beyond this many min past the last poll
    },
    # Near-threshold responsiveness (2026-07-06 rayi3 incident). rayi3 burned from
    # UNDER its 95% ladder step to 100% 5h BETWEEN two ~180s daemon polls, so a
    # live session hit Claude's native "usage limit" menu before the daemon swapped
    # its lane off — the whole point of the 95% step is to swap BEFORE the cap, but
    # the swap fired late because the daemon acted on a usage snapshot that lagged
    # the real fast burn. Two complementary, config-gated fixes hang off this block:
    #   Fix A — the ladder/cap swap TRIGGER decides on burn-EXTRAPOLATED usage
    #     (last poll + burn_rate x time-to-next-poll), not the stale last-polled
    #     value, so an account at 91%-last-poll but burning fast trips the 95% step
    #     NOW instead of next cycle. Extrapolation only ever pushes the trigger
    #     EARLIER (max() with the raw reading), never masks a real over-cap value.
    #   Fix B — accounts whose extrapolated usage is within `within_pct_of_step` of
    #     their next swap step (or projected to cross it before the next normal
    #     poll) are polled on the fast `fast_interval_seconds` cadence instead of
    #     the normal one, so the daemon SEES the fast burn in time to act on it.
    # `enabled: False` reverts BOTH to prior behavior bit-for-bit. Only accounts
    # actually near a step accelerate (the fleet is NOT blanket-fast-polled), and
    # the 429 poll-backoff gate still wins — a throttled account is never fast-polled.
    "poll_accel": {
        "enabled": True,
        "within_pct_of_step": 5,       # accelerate when extrapolated usage is within this many pts of the next step
        "fast_interval_seconds": 45,   # near-threshold poll cadence (vs the normal active/inactive interval)
        # Clustered-account compounded-burn allowance (2026-07-06 rayi4 incident).
        # An account backing MULTIPLE live lanes climbs toward its cap ~N× faster
        # than its per-account `burn_rate` (measured from single-lane poll-to-poll
        # history) predicts, so the plain `within_pct_of_step` band fires too late:
        # rayi4 sat at 84% 5h on the SLOW inactive cadence and rode past its 95%
        # step before the daemon re-polled. Widen the fast-poll band by this many
        # pts for each EXTRA lane an account backs (band = within + bonus*(lanes-1)),
        # so a clustered near-cap account is fast-polled early enough to swap a lane
        # off in time. 0 = off (prior single-band behavior). Only accounts backing
        # >= 2 live lanes are affected, so under normal one-lane-per-account
        # operation this is inert (no extra polling → no 429-budget cost).
        "cluster_within_bonus_pct": 10,
    },
    # Anti-clustering lane spread (2026-07-06 incident: the daemon piled ALL live
    # lanes onto ONE "best-scored" account — first rayi5, then rayi4 — instead of
    # spreading them). Root cause: the smart/headroom scorers rank an account purely
    # on usage/headroom/burn-proximity with NO awareness of how many live lanes it
    # already backs, so the SAME burn-soon account is the top pick for every lane
    # each cycle; combined with the #109 pool double-book escape hatch, several lanes
    # STACK onto it (4 mounts on 3 families → the shared token diverged → recurring
    # "mount blanked / not logged in" auto-heal loop), and they then all hit that one
    # account's 5h cap simultaneously with no headroom to swap. Two coupled levers:
    #   (1) cluster_penalty — a swap-target SCORE penalty of this many points per
    #       live lane already backing the candidate (existing occupancy + this-cycle
    #       claims), so an account that already holds a lane loses to a distinct empty
    #       one. Prevents cross-cycle re-convergence onto the same magnet account.
    #   (2) max_stack — the daemon will not pile a NON-urgent (deferrable, e.g.
    #       burn-before-reset) move onto an account that already backs >= max_stack
    #       lanes when it can instead HOLD the lane on its current account. Urgent
    #       moves (hard-cap / reactive-429 escapes) may still stack via independent
    #       login families when clean accounts are genuinely scarce — that path is a
    #       last resort the #104/#109 family accounting still guards.
    # `enabled: False` reverts BOTH levers to prior behavior bit-for-bit.
    "spread_lanes": {
        "enabled": True,
        "cluster_penalty": 40.0,       # score pts deducted per live lane already backing a candidate target
        "max_stack": 1,                # deferrable moves won't grow an account past this many lanes when it can hold
    },
    # Per-model WEEKLY tracking (2026-07-02). The usage API exposes per-model
    # usage only weekly (no per-model 5h window). `gate_enabled: False` by
    # default = surface-only (shown in `cus status`, no effect on swaps) so
    # unmodified installs are unchanged. When True, per-model weekly acts as a
    # HARD CAP (not a ladder step): the active account is force-swapped when a
    # tracked model's week reaches `cap_pct`, and no swap TARGET is chosen whose
    # model-week is at/above `cap_pct`. It deliberately does NOT feed the
    # progressive ladder (current_max_pct) — a weekly per-model budget is a hard
    # line, not a gradual climb, so folding it into the ladder made a model trip
    # a swap at steps[0] long before its own cap (fixed 2026-07-02). `models:
    # []` = track every model the API reports; set e.g. ["Fable","Sonnet"] to
    # gate only on the models you actually use.
    "per_model_weekly": {
        "gate_enabled": False,
        "models": [],
        # Separate threshold for the per-model weekly gate. None (default) =
        # inherit the strategy's hard_7d_cap_pct, keeping the original fold-in
        # behavior byte-identical. Set e.g. 90 to force-swap the active account
        # when a tracked model's week hits 90% (and to filter swap targets at
        # that level) while the aggregate-7d cap keeps its own value — lets
        # "swap when Fable hits 90%" coexist with a lower aggregate ceiling.
        "cap_pct": None,
    },
    "reactive": {
        "enabled": True,         # detect 429s via PostToolUseFailure hook
        # Fix A1 (user 2026-06-23): a 429 only justifies an account swap when the
        # ACTIVE account is plausibly near its budget cap. The PostToolUseFailure
        # hook substring-matches "rate limit"/"usage limit"/etc. anywhere in a
        # failed tool body, so a downstream API limit or a concurrency/RPM spike
        # from blasting parallel subagents trips it even though the account has
        # tons of budget left. Below this floor we treat the 429 as noise and do
        # NOT swap (swapping wouldn't help and risks landing on a worse account).
        # Set well under the first ladder step so genuine "poll says 70% but we
        # just hit the wall before the next poll" cases still react. 0 = react to
        # every 429 (pre-2026-06-23 behavior).
        "min_active_pct": 50,
    },
    "session_locks": {
        "pinned": {},            # {pane_id: account_name} — never swap these
        "never_restart_patterns": [],  # list of regex; matched against tmux pane name
        # Slot-level lock (per_session mode). Slot names listed here are frozen:
        # the daemon never moves the slot's account (ladder, hard-cap, and
        # reactive-429 slot moves all skip it) and idle slot-gc won't reap it.
        # Distinct from `pinned` (which protects a SESSION from hot-swap
        # orchestration): a lock protects the SLOT's credential mount itself.
        # Manage via `cus lock <slot>` / `cus unlock <slot>`.
        "locked_slots": [],
    },
    "daemon": {
        "log_path": str(DAEMON_LOG),
        "pid_path": str(DAEMON_PID),
    },
    "hooks": {
        "install_session_start": True,
        "install_stop": True,
        "install_stop_failure": True,     # A2: reliable model-level 429 detector (StopFailure)
        "install_post_tool_use_failure": True,
        "install_pre_tool_use": False,    # only needed if subagent_skip.enabled
        "install_post_tool_use": False,   # only needed if subagent_skip.enabled — pairs with install_pre_tool_use (GH #27)
        "install_subagent_stop": False,   # only needed if subagent_skip.enabled
    },
    "statusline": {
        "verbose": False,                 # cus statusline output verbosity
        "show_other_accounts": True,      # in verbose mode, include other account states
        "show_poll_age": True,            # include "polled Nago"
        "show_reset_times": True,         # show time until 5h / 7d resets
        # ANSI color (GH #38). Claude Code's statusLine renders ANSI, so we
        # force color through even though stdout is a pipe. Honors NO_COLOR.
        "color": True,                    # colorize percentages / active account / flags
    },
    "accounts": [],              # filled in at init time from discovery
}


# --------------------------------------------------------------------------
# Atomic IO
# --------------------------------------------------------------------------

def atomic_write_bytes(path: Path, content: bytes, mode: int = 0o644) -> None:
    """Write `content` to `path` atomically.

    Uses a tempfile in the same directory and os.replace(), which is
    POSIX-atomic. The tempfile prefix mirrors the target filename so a
    leftover tmp (e.g. from a crash mid-write) is obvious.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=path.name + ".tmp.")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(content)
        os.chmod(tmp, mode)
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass
        raise


def atomic_copy(src: Path, dst: Path, mode: int = 0o644) -> None:
    """Atomic file copy: read src into memory, atomic-write dst.

    Avoids the partial-content window of shutil.copy2 (which writes to dst
    in chunks; another process reading dst mid-copy sees truncated bytes).
    Critical for ~/.claude/.credentials.json which Claude reads to decide
    OAuth refresh — a half-written creds file would crash any session.
    """
    with src.open("rb") as f:
        content = f.read()
    atomic_write_bytes(dst, content, mode=mode)


def backup_credentials_file(path: Path, keep: int = CREDS_BACKUP_KEEP) -> Path | None:
    """Preserve an about-to-be-overwritten credentials file as a timestamped
    sibling backup, pruning to the newest `keep` generations (GH #79).

    This is the choke-point last-line defense for the credential-clobber bug
    class (#3, #70, #75, #76, #77): whatever writer bug slips past the other
    guards, the destroyed refresh token is still recoverable for a few
    generations via `cus restore-creds`. Call it immediately before ANY write
    that replaces an existing `.credentials.json` (snapshot or live).

    Design notes:
      - Backup name is `<name>.bak.<UTC-ts>` in the SAME directory, so the
        backup inherits the directory's fate (a deleted account dir takes its
        backups with it) and never crosses account boundaries.
      - Timestamp format %Y%m%dT%H%M%S.%fZ is fixed-width and lexically
        sortable, so pruning can sort filenames without parsing dates.
      - Mode 0600 always — these files hold OAuth tokens.
      - Best-effort by contract: returns None if there is nothing to back up.
        Callers must NOT let a backup failure abort the primary write path —
        but we deliberately do NOT swallow exceptions here; an unwritable
        account dir would break the primary write two lines later anyway,
        and silently skipping backups would defeat the whole feature.

    Returns the backup path, or None if `path` didn't exist.
    """
    if not path.exists():
        return None
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
    bak = path.with_name(f"{path.name}.bak.{ts}")
    atomic_copy(path, bak, mode=0o600)
    # Prune oldest generations beyond `keep`. Lexical sort == chronological
    # sort thanks to the fixed-width timestamp; newest sort last.
    backups = sorted(path.parent.glob(path.name + ".bak.*"))
    for old in backups[:-keep] if keep > 0 else backups:
        try:
            old.unlink()
        except FileNotFoundError:
            pass  # concurrent pruner got it first — fine
    return bak


def list_creds_backups(account_name: str) -> list[Path]:
    """All backup generations for an account's snapshot, newest first (GH #79)."""
    d = ACCOUNTS_DIR / f"account-{account_name}"
    return sorted(d.glob(".credentials.json.bak.*"), reverse=True)


def restore_creds_backup(account_name: str, backup: Path, into_live: bool = False) -> None:
    """Restore a backup generation into an account's snapshot (GH #79).

    The snapshot being replaced is itself backed up first, so a restore is
    always reversible (it just becomes the newest generation). With
    `into_live=True` the backup is also installed as the live
    `~/.claude/.credentials.json` — only meaningful when the account is the
    ACTIVE one, because for the active account the live file (not the
    snapshot) is what Claude actually reads.

    Best-effort by nature: if the backed-up refresh token was rotated
    server-side after the backup was taken, Anthropic may reject it — but in
    the clobber scenarios this feature exists for (#3/#70/#75/#76/#77) the
    destroyed token was never used again after the overwrite, which is
    exactly when the restore works.
    """
    snap = ACCOUNTS_DIR / f"account-{account_name}" / ".credentials.json"
    backup_credentials_file(snap)  # make the restore itself walk-back-able
    atomic_copy(backup, snap, mode=0o600)
    if into_live:
        backup_credentials_file(CREDS_JSON)
        atomic_copy(backup, CREDS_JSON, mode=0o600)


def read_json(path: Path) -> dict:
    with path.open() as f:
        return json.load(f)


def write_json(path: Path, data: Any, mode: int = 0o644) -> None:
    atomic_write_bytes(path, (json.dumps(data, indent=2, sort_keys=True) + "\n").encode(), mode=mode)


def read_yaml(path: Path) -> dict:
    with path.open() as f:
        return yaml.safe_load(f) or {}


def write_yaml(path: Path, data: Any) -> None:
    atomic_write_bytes(path, yaml.safe_dump(data, sort_keys=True, default_flow_style=False).encode())


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


# --------------------------------------------------------------------------
# Account discovery
# --------------------------------------------------------------------------

def discover_config_dirs() -> list[tuple[str, Path]]:
    """Return [(account_name, config_dir_path), ...] for all on-machine accounts.

    Priority order:
      1. `~/claude-accounts/account-*/` — managed accounts (post-migration)
      2. The live `~/.claude/` — always registered as "default" unless an
         entry already exists in our managed dir
      3. Sibling `~/.claude-<name>/` dirs — legacy ad-hoc accounts
         (registered for one-time migration, then user can delete the dir)

    Managed accounts take precedence: if both `~/claude-accounts/account-merkos/`
    and `~/.claude-merkos/` exist, the managed one wins.
    """
    found: list[tuple[str, Path]] = []
    seen: set[str] = set()

    # 1. Managed accounts (post-migration)
    if ACCOUNTS_DIR.exists():
        for p in sorted(ACCOUNTS_DIR.glob("account-*")):
            if not p.is_dir():
                continue
            if not (p / ".credentials.json").exists() and not (p / "credentials.json").exists():
                continue
            name = p.name.removeprefix("account-")
            found.append((name, p))
            seen.add(name)

    # 2. Live ~/.claude/ — register as "default" if not already present
    if "default" not in seen and (CLAUDE_DIR / ".credentials.json").exists():
        found.append(("default", CLAUDE_DIR))
        seen.add("default")

    # 3. Legacy sibling dirs
    for p in sorted(HOME.glob(".claude-*")):
        if not p.is_dir() or not (p / ".credentials.json").exists():
            continue
        name = p.name.removeprefix(".claude-")
        if name in seen:
            continue
        found.append((name, p))
        seen.add(name)

    return found


def claude_json_for_config_dir(config_dir: Path) -> Path | None:
    """Locate the .claude.json file associated with a given config dir.

    Claude Code stores the live config at `~/.claude.json` (parent-level)
    when using the default `~/.claude/` dir. When `CLAUDE_CONFIG_DIR` is
    pointed at a custom dir like `~/.claude-merkos/`, the corresponding
    `.claude.json` lives inside that dir.
    """
    if config_dir == CLAUDE_DIR:
        return CLAUDE_JSON if CLAUDE_JSON.exists() else None
    candidate = config_dir / ".claude.json"
    return candidate if candidate.exists() else None


def extract_identity(claude_json_path: Path) -> dict:
    """Pull the account-bound keys out of a .claude.json file."""
    cj = read_json(claude_json_path)
    return {k: cj[k] for k in ACCOUNT_BOUND_KEYS if k in cj}


# --------------------------------------------------------------------------
# Config + state helpers
# --------------------------------------------------------------------------

def deep_merge(base: dict, override: dict) -> dict:
    """Recursively merge override into a copy of base. Override wins on conflicts."""
    out = {k: (deep_merge(v, override.get(k, {})) if isinstance(v, dict) and isinstance(override.get(k), dict) else override.get(k, v)) for k, v in base.items()}
    for k, v in override.items():
        if k not in out:
            out[k] = v
    return out


def load_config() -> dict:
    """Load config.yaml merged with DEFAULT_CONFIG. Missing file = defaults."""
    user_config = read_yaml(CONFIG_YAML) if CONFIG_YAML.exists() else {}
    return deep_merge(DEFAULT_CONFIG, user_config)


def load_state() -> dict:
    return read_json(STATE_JSON) if STATE_JSON.exists() else {"active": None, "accounts": {}, "swap_history": []}


def save_state(state: dict) -> None:
    write_json(STATE_JSON, state)


def account_meta(name: str) -> dict:
    """Load meta.yaml for a given account; empty dict if missing."""
    meta_path = ACCOUNTS_DIR / f"account-{name}" / "meta.yaml"
    return read_yaml(meta_path) if meta_path.exists() else {}


def append_inbox(entry_type: str, title: str, body: str) -> None:
    """Append a daemon-decision entry to ~/claude-accounts/inbox.md.

    Lightweight version of the AVC inbox format. Used by Tier 3 to log
    shell-kill requests and other autonomous decisions for user review.
    """
    INBOX_MD.parent.mkdir(parents=True, exist_ok=True)
    if not INBOX_MD.exists():
        atomic_write_bytes(INBOX_MD, b"# claude-accounts inbox\n\nAutonomous decisions made by the cus daemon. Newest on top.\n\n")
    ts = now_iso()
    entry = f"\n## {ts} — {entry_type} — {title}\n\n{body}\n"
    # Append-only (not atomic but acceptable for an append-mostly log)
    with INBOX_MD.open("a") as f:
        f.write(entry)


# --------------------------------------------------------------------------
# OAuth usage API client (lifted from cux/internal/usage/usage.go:84-135)
# --------------------------------------------------------------------------

@dataclass
class UsageWindow:
    """One window of usage data from the Anthropic OAuth usage endpoint.

    `utilization` is 0.0-100.0 directly from the API — no math needed.
    `resets_at` may be None if the API returned null (e.g. fresh install).
    """
    utilization: float
    resets_at: str | None


@dataclass
class AccountUsage:
    """All usage windows for a single OAuth account, plus error state.

    Error state model (mutually exclusive; ordered from most-recoverable to
    least-recoverable):
      - token_stale: our stored access token has expired but the refresh token
        is still valid. NOT an SOS condition — claude code will refresh the
        access token transparently on first real use. We just can't poll the
        usage API ourselves until next swap (which writes a fresh access
        token to storage). See GH #13.
      - rate_limited: Anthropic returned 429. Account is throttled; usage
        windows shown are stale (preserved from last successful poll).
      - poll_error: some other transport/parse failure.
      - token_expired: 401 response WITH a non-expired stored access token
        — means refresh-token-level failure, needs interactive re-login.
    """
    five_hour: UsageWindow | None = None
    seven_day: UsageWindow | None = None
    seven_day_sonnet: UsageWindow | None = None
    seven_day_opus: UsageWindow | None = None
    # Per-model WEEKLY utilization keyed by model display_name (e.g. "Fable",
    # "Sonnet", "Opus"). The usage API exposes per-model data only at weekly
    # granularity — there is NO per-model 5-hour window (`five_hour` is
    # aggregate). Populated from the `limits[]` array's model-scoped entries
    # plus the legacy seven_day_sonnet/opus fields. See _parse_per_model_weekly.
    per_model_weekly: dict = field(default_factory=dict)  # {display_name: UsageWindow}
    token_expired: bool = False
    token_stale: bool = False
    polled_at: str = field(default_factory=now_iso)
    raw: dict = field(default_factory=dict)

    @classmethod
    def empty(cls) -> "AccountUsage":
        return cls()


def account_creds_path(account_name: str) -> Path:
    """Canonical path to an account's OAuth credentials file.

    After the unified-tree migration, this is ~/claude-accounts/account-<name>/.credentials.json
    (dot-prefixed; matches Claude Code's CLAUDE_CONFIG_DIR layout). Falls
    back to the pre-migration credentials.json for backward-compat.
    """
    new_path = ACCOUNTS_DIR / f"account-{account_name}" / ".credentials.json"
    old_path = ACCOUNTS_DIR / f"account-{account_name}" / "credentials.json"
    if new_path.exists():
        return new_path
    return old_path


def account_claude_json_path(account_name: str) -> Path:
    """Canonical path to an account's .claude.json.

    Post-migration: ~/claude-accounts/account-<name>/.claude.json. Pre-migration
    fallback: claude-identity.json (which only has account-bound keys).
    """
    new_path = ACCOUNTS_DIR / f"account-{account_name}" / ".claude.json"
    old_path = ACCOUNTS_DIR / f"account-{account_name}" / "claude-identity.json"
    if new_path.exists():
        return new_path
    return old_path


def _read_access_token(account_name: str) -> str | None:
    """Read the OAuth access_token for an account.

    For the ACTIVE account, always reads from the LIVE
    `~/.claude/.credentials.json`. Why: Claude refreshes the access_token
    in that file every ~hour as part of normal operation; our storage-side
    snapshot is only updated during a swap. If we don't swap for several
    hours, the storage snapshot's access_token has expired but the live
    file's is fresh.

    For INACTIVE accounts, reads from the storage-side snapshot in
    `~/claude-accounts/account-<name>/`. The snapshot's access_token may
    be expired but the refresh_token is still valid (30 day lifetime),
    so on next swap+use, Claude will refresh it.

    Bug history: before 2026-05-20 this always read storage. Result:
    polling an active account whose tokens hadn't been swap-saved-back
    in >1hr returned HTTP 401, which we treated as "token expired
    forever." User correctly pointed out that the account WAS working
    (live tokens valid) — we were just reading the wrong file.
    """
    try:
        state = load_state() if STATE_JSON.exists() else {}
    except Exception:
        state = {}
    if state.get("active") == account_name and CREDS_JSON.exists():
        creds_path = CREDS_JSON
    else:
        creds_path = account_creds_path(account_name)
    if not creds_path.exists():
        return None
    try:
        creds = read_json(creds_path)
        return creds.get("claudeAiOauth", {}).get("accessToken")
    except (json.JSONDecodeError, OSError):
        return None


def migrate_account_dir(account_dir: Path) -> dict:
    """Migrate a single account dir from old layout to new (CLAUDE_CONFIG_DIR-compatible).

    Idempotent: if already in new layout, returns {"action": "already_migrated"}.

    Old layout (cus v0.1):
      account-X/credentials.json          (OAuth, 0600)
      account-X/claude-identity.json      ({userID, oauthAccount})
      account-X/meta.yaml

    New layout (CLAUDE_CONFIG_DIR-compatible):
      account-X/.credentials.json          (OAuth, 0600)
      account-X/.claude.json               (account-bound keys)
      account-X/projects/ → ~/.claude/projects/
      account-X/plugins/, agents/, skills/, commands/, memory/  (symlinked)
      account-X/meta.yaml                  (unchanged)

    Returns {"action": "migrated" | "already_migrated" | "no_op", "details": [...]}.
    """
    details: list[str] = []
    new_creds = account_dir / ".credentials.json"
    old_creds = account_dir / "credentials.json"
    new_cj = account_dir / ".claude.json"
    old_identity = account_dir / "claude-identity.json"

    if new_creds.exists() and new_cj.exists():
        # Verify shared symlinks too
        for sub in SHARED_SYMLINK_SUBDIRS:
            link = account_dir / sub
            target = CLAUDE_DIR / sub
            if not link.exists() and target.exists():
                link.symlink_to(target)
                details.append(f"added missing symlink {link.name} → {target}")
        return {"action": "already_migrated" if not details else "patched_symlinks", "details": details}

    # 1. Rename credentials.json → .credentials.json (preserving 0600)
    if old_creds.exists() and not new_creds.exists():
        os.rename(old_creds, new_creds)
        os.chmod(new_creds, 0o600)
        details.append("renamed credentials.json → .credentials.json")

    # 2. Promote claude-identity.json → .claude.json
    identity: dict = {}
    if old_identity.exists():
        try:
            identity = read_json(old_identity)
        except (json.JSONDecodeError, OSError):
            pass

    if not new_cj.exists():
        # If identity is empty (e.g. brand-new dir with no prior data), write
        # an empty {} so Claude Code has something valid to read.
        write_json(new_cj, identity)
        details.append("created .claude.json from claude-identity.json")
    if old_identity.exists():
        old_identity.unlink()
        details.append("removed legacy claude-identity.json")

    # 3. Symlink shared subdirs to ~/.claude/ for history/plugin/skill sharing
    for sub in SHARED_SYMLINK_SUBDIRS:
        link = account_dir / sub
        target = CLAUDE_DIR / sub
        if link.exists():
            continue
        if not target.exists():
            continue  # don't create dangling links
        link.symlink_to(target)
        details.append(f"symlinked {sub} → {target}")

    return {"action": "migrated", "details": details}


# --------------------------------------------------------------------------
# Mounts + slots (per_session mode)
#
# A "mount" is any directory a live Claude Code process uses as its
# CLAUDE_CONFIG_DIR: the legacy global pair (~/.claude/ + ~/.claude.json) or a
# slot dir (~/claude-accounts/slot-<n>/, .claude.json inside). Slots hold ONE
# account's credentials at a time and are swapped in place — the same two-file
# swap as global mode, scoped to one directory, so the session on top never
# restarts. Plan: docs/plans/2026-07-02-per-session-accounts.md.
#
# All paths are computed at call time from module globals (never captured at
# import) for the same reason as _swap_lock_path: tests repoint ACCOUNTS_DIR /
# CLAUDE_DIR at temp trees.
# --------------------------------------------------------------------------

def mount_claude_json_path(mount: Path) -> Path:
    """Where a mount's .claude.json LIVES (whether or not it exists yet).

    Unlike claude_json_for_config_dir (which returns None for a missing file —
    right for readers), writers need the destination path: the default mount
    keeps it at parent level (~/.claude.json), every other mount keeps it
    inside the dir.
    """
    if mount == CLAUDE_DIR:
        return CLAUDE_JSON
    return mount / ".claude.json"


def mount_creds_path(mount: Path) -> Path:
    """A mount's live credentials file (~/.claude/.credentials.json shape)."""
    return mount / ".credentials.json"


def slot_path(slot_name: str) -> Path:
    return ACCOUNTS_DIR / slot_name


def list_slot_dirs() -> list[Path]:
    """Existing slot dirs, sorted by index (slot-2 before slot-10)."""
    if not ACCOUNTS_DIR.exists():
        return []
    def _idx(p: Path) -> int:
        try:
            return int(p.name.removeprefix(SLOT_PREFIX))
        except ValueError:
            return 1 << 30  # non-numeric suffix sorts last, still listed
    return sorted((p for p in ACCOUNTS_DIR.glob(SLOT_PREFIX + "*") if p.is_dir()), key=_idx)


# --------------------------------------------------------------------------
# Per-(slot, account) independent-login store (GH #109, 2026-07-02)
# --------------------------------------------------------------------------
# See DEFAULT_CONFIG["independent_logins"] for the WHY. In short: a slot's live
# creds are today a copy of one account snapshot, and copies of one refresh-token
# family clobber each other on rotation (#104/#103/#3). This store holds an
# INDEPENDENT `/login` per (account, slot) so the same account can back several
# live mounts without a shared token family to clobber.
#
# Layout: ~/claude-accounts/logins/<account>/<slot>/
#     .credentials.json  — the independent OAuth blob (what Phase 2 installs
#                          into the slot instead of the shared-snapshot copy)
#     .claude.json       — identity `/login` wrote here; used to verify the
#                          login landed on the RIGHT account (duplicate-identity
#                          failure mode, 2026-07-01) and to fingerprint expiry
#     provenance.json    — {account, slot, minted_ts, source_email, refresh_fp}
#
# Everything here is READ-ONLY with respect to the swap path in Phase 1 — it is
# populated by `cus login-mount` and consumed only by status/sos. The swap path
# (execute_swap) is unchanged until the Phase 2 gate flips (see the seam comment
# at the target-creds install in _execute_swap_locked).

LOGIN_STORE_DIRNAME = "logins"


def login_store_root() -> Path:
    """Root of the per-(slot, account) independent-login store."""
    return ACCOUNTS_DIR / LOGIN_STORE_DIRNAME


def login_store_dir(account: str, slot: str) -> Path:
    """Dir holding one (account, slot) pair's independent login."""
    return login_store_root() / account / slot


def login_store_creds_path(account: str, slot: str) -> Path:
    return login_store_dir(account, slot) / ".credentials.json"


def login_store_cj_path(account: str, slot: str) -> Path:
    return login_store_dir(account, slot) / ".claude.json"


def login_store_provenance_path(account: str, slot: str) -> Path:
    return login_store_dir(account, slot) / "provenance.json"


def _credential_refresh_token(creds: Any) -> str | None:
    """Refresh token out of a parsed credentials blob, or None.

    Mirrors the defensive extraction in classify_live_creds_owner: any process
    can scribble a list/string/number where a dict is expected, so never assume
    dict shape."""
    oauth = creds.get("claudeAiOauth") if isinstance(creds, dict) else None
    rt = oauth.get("refreshToken") if isinstance(oauth, dict) else None
    return rt if isinstance(rt, str) and rt else None


def _refresh_fingerprint(refresh_token: str) -> str:
    """Short, non-reversible fingerprint of a refresh token for provenance.

    We record a fingerprint rather than the token itself so provenance.json is
    safe to read/diff/log while still letting us tell two independent login
    families apart (the whole point of #109 is that they MUST differ). Twelve
    hex chars of SHA-256 is ample to distinguish a handful of logins without
    being a credential."""
    return "sha256:" + hashlib.sha256(refresh_token.encode()).hexdigest()[:12]


def has_independent_login(account: str, slot: str) -> bool:
    """True iff a usable independent login exists for this (account, slot).

    "Usable" = the creds file parses and carries a refresh token; an
    access-token-only or logout-shaped file is treated as absent (it cannot
    seed a swap — same reasoning as classify_live_creds_owner's "invalid")."""
    path = login_store_creds_path(account, slot)
    if not path.exists():
        return False
    try:
        return _credential_refresh_token(read_json(path)) is not None
    except (json.JSONDecodeError, OSError):
        return False


# ---------------------------------------------------------------------------
# Per-account login POOL (2026-07-03, supersedes per-(slot,account) keying).
#
# A pool is ~/claude-accounts/logins/<account>/family-<N>/ — one INDEPENDENT
# OAuth login family per subdir. The account's canonical snapshot is the implicit
# "primary" (used when the account isn't held anywhere); family-1..N are the
# backups the operator mints once at onboarding. A lane needing to swap onto a
# HELD account borrows a FREE family (one not currently leased to a live slot),
# so the same account can back several live mounts without a shared token family
# to clobber (#104). Lease = state.slots[<slot>].login_family = "<account>/family-<N>".
#
# This reuses the login_store_root() + fingerprint primitives above; the
# per-(slot,account) helpers (login_store_dir, has_independent_login, …) remain
# until the P5 cleanup but are no longer the swap-path source of truth.
# ---------------------------------------------------------------------------

LOGIN_FAMILY_PREFIX = "family-"


def login_pool_dir(account: str) -> Path:
    """Root of one account's independent-login pool: logins/<account>/."""
    return login_store_root() / account


def login_family_dir(account: str, family_id: str) -> Path:
    """Dir holding one pooled family. `family_id` is the subdir name
    (e.g. 'family-2')."""
    return login_pool_dir(account) / family_id


def login_family_creds_path(account: str, family_id: str) -> Path:
    return login_family_dir(account, family_id) / ".credentials.json"


def _family_creds_usable(account: str, family_id: str) -> bool:
    """True iff this family's creds parse and carry a refresh token — the same
    'usable' bar as has_independent_login (an access-token-only or logout-shaped
    file can't seed a swap)."""
    path = login_family_creds_path(account, family_id)
    if not path.exists():
        return False
    try:
        return _credential_refresh_token(read_json(path)) is not None
    except (json.JSONDecodeError, OSError):
        return False


def list_login_families(account: str) -> list[str]:
    """Usable family ids in an account's pool, sorted by numeric index.

    Only families with parseable creds carrying a refresh token count — a
    half-scaffolded dir (mid-`login-mount`, no `/login` yet) is not usable."""
    d = login_pool_dir(account)
    if not d.exists():
        return []
    fams = [p.name for p in d.iterdir()
            if p.is_dir() and p.name.startswith(LOGIN_FAMILY_PREFIX)
            and _family_creds_usable(account, p.name)]
    return sorted(fams, key=_family_index)


def _family_index(family_id: str) -> int:
    """Numeric index of 'family-<N>' for deterministic lowest-free ordering;
    unparseable names sort last."""
    try:
        return int(family_id.removeprefix(LOGIN_FAMILY_PREFIX))
    except ValueError:
        return 1 << 30


def next_family_id(account: str) -> str:
    """The family id `cus login-mount <account>` should scaffold next: one past
    the highest EXISTING family index (usable or not, so a half-finished mint
    isn't reused). Pools start at family-1."""
    d = login_pool_dir(account)
    existing = [p.name for p in d.iterdir()
                if p.is_dir() and p.name.startswith(LOGIN_FAMILY_PREFIX)] if d.exists() else []
    hi = max((_family_index(f) for f in existing), default=0)
    return f"{LOGIN_FAMILY_PREFIX}{hi + 1}"


def slot_leased_family(state: dict, slot: str) -> tuple[str, str] | None:
    """(account, family_id) a slot currently leases, or None. Stored as
    'account/family-N' on state.slots[slot].login_family."""
    entry = (state.get("slots", {}) or {}).get(slot, {}) or {}
    ref = entry.get("login_family")
    if not ref or "/" not in ref:
        return None
    account, family_id = ref.split("/", 1)
    return (account, family_id)


def leased_families(account: str, state: dict) -> set[str]:
    """Family ids of `account` currently leased by a LIVE slot — the in-use set
    a claim must avoid. Idle slots don't count: with no session refreshing the
    token, their lease can't clobber, and the family is reclaimable."""
    live = set(occupied_slot_accounts(state).get(account, []))
    out: set[str] = set()
    for slot in live:
        lease = slot_leased_family(state, slot)
        if lease and lease[0] == account:
            out.add(lease[1])
    return out


def free_login_family(account: str, state: dict) -> str | None:
    """Lowest-index usable family of `account` not leased to a live slot, or None
    if the pool is empty or fully leased (exhausted). This is the family a rescue
    swap claims."""
    leased = leased_families(account, state)
    for fam in list_login_families(account):  # already sorted lowest-first
        if fam not in leased:
            return fam
    return None


def has_free_login_family(account: str, state: dict) -> bool:
    """True iff a rescue swap onto `account` could claim a distinct login family
    (so double-booking it would not clobber). The pool-model replacement for the
    per-slot has_independent_login predicate.

    Deliberately does NOT probe liveness (#127): this is the cheap decision-
    layer predicate, called many times per cycle. The claim itself
    (claim_verified_login_family, execution layer) does the definitive
    refresh-grant probe and skips dead stores; an optimistic True here at
    worst becomes a failed move logged by _execute_slot_moves."""
    return free_login_family(account, state) is not None


def _free_family_count(account: str, state: dict) -> int:
    """How many usable pooled families of `account` are NOT leased to a live slot
    — i.e. how many ADDITIONAL live mounts the pool can back without a clobber,
    beyond the ones already covered. The counting twin of free_login_family
    (which returns just the lowest free one)."""
    leased = leased_families(account, state)
    return sum(1 for fam in list_login_families(account) if fam not in leased)


def _distinct_family_capacity(account: str, state: dict, config: dict | None, slot: str | None) -> int:
    """Max concurrent live mounts `account` can back WITHOUT a GH #104 token-family
    clobber: one plain-snapshot 'primary' mount IF the account isn't already live
    anywhere, PLUS one per FREE pooled login family (plus a legacy per-slot
    independent login for this slot, if present).

    This is the invariant EVERY daemon swap path must respect: never create more
    live mounts of an account than it has distinct login families to cover them,
    or the mounts share one single-use OAuth refresh token and the next rotation
    logs one out. Both the proactive ladder and the reactive-429 path consult this
    before committing a same-cycle move (see decide_slot_swaps /
    check_rate_limit_reactive_per_session) so an over-subscription is HELD at the
    decision layer with a clear log line, not left to the execution layer's
    downstream 'pool exhausted' refusal (which strands the lane just the same,
    only noisier). Incident 2026-07-05: the reactive path piled 6 lanes onto rayi1
    (2 families) because nothing counted families before committing.

    Gate off (default, no pool): capacity is 1 (or 0 if the account is already
    live on a mount) — i.e. 'never double-book', the pre-pool #104 rule, bit-for-
    bit unchanged."""
    cap = 0 if _account_held_by_other_live_mount(state, account, slot, config) else 1
    if independent_logins_enabled(config):
        cap += _free_family_count(account, state)
        if slot is not None and has_independent_login(account, slot):
            cap += 1
    return cap


def _oauth_refresh_grant(refresh_token: str) -> tuple[str, dict | None]:
    """Probe a refresh token's liveness via the OAuth refresh grant (#127).

    Returns ("alive", token_response) — WHICH ROTATES THE TOKEN: the caller
    MUST persist the response's new refresh token immediately or the family
    is lost; ("dead", None) on a definitive invalid_grant (the branch was
    rotated away elsewhere — the poisoned-store signature); ("unknown", None)
    on anything else (network, 5xx, endpoint drift) so callers can FAIL OPEN.

    Why a rotation is the check: access-token expiry cannot distinguish dead
    from live branches — in the 2026-07-03 incident, dead rayi1/family-1 and
    live family-2 both sat at expired access tokens; only the grant told them
    apart. Rotating a FREE family is safe: free ⇒ no live mount holds it, so
    nobody else's chain is invalidated, and the claimant's session would have
    refreshed immediately anyway."""
    body = json.dumps({"grant_type": "refresh_token",
                       "refresh_token": refresh_token,
                       "client_id": OAUTH_CLIENT_ID}).encode()
    req = urllib.request.Request(OAUTH_TOKEN_URL, data=body,
                                 headers={"Content-Type": "application/json",
                                          "Accept": "application/json",
                                          # urllib's default UA is Cloudflare-blocked
                                          # (403 code 1010) at this endpoint.
                                          "User-Agent": OAUTH_USER_AGENT}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=OAUTH_TOKEN_TIMEOUT_SECONDS) as resp:
            return ("alive", json.loads(resp.read(USAGE_API_RESPONSE_LIMIT_BYTES)))
    except urllib.error.HTTPError as e:
        try:
            payload = e.read(USAGE_API_RESPONSE_LIMIT_BYTES).decode(errors="replace")
        except OSError:
            payload = ""
        # Only invalid_grant is a death certificate; any other 4xx/5xx could be
        # throttling, schema drift, or a wrong endpoint — treat as unknown.
        if e.code in (400, 401) and "invalid_grant" in payload:
            return ("dead", None)
        return ("unknown", None)
    except (urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError):
        return ("unknown", None)


def claim_verified_login_family(account: str, state: dict, config: dict | None = None) -> str | None:
    """Pick a free family to claim, PROVEN alive (#127) — the execution-layer
    twin of free_login_family.

    Walks free families lowest-first. Per family: refresh-grant probe.
      - alive  → persist the rotated tokens into the store (the old refresh
                 token is invalid the moment the grant succeeds), return it.
      - dead   → retire the store (rename creds to .dead-<date>, so
                 list_login_families stops offering it and SOS/doctor can
                 count retirements), try the next family. This is how the
                 2026-07-03 poisoned stores (pre-PR#126 save-back bug) get
                 swept out lazily instead of logging out a live session.
      - unknown→ FAIL OPEN: return the family unverified (pre-#127 behavior).
                 A network blip must not turn a working pool into a refused
                 claim; worst case is exactly the old behavior.

    Config gate independent_logins.verify_family_on_claim (default True);
    False short-circuits to plain free_login_family."""
    cfg = config if config is not None else load_config()
    if not cfg.get("independent_logins", {}).get("verify_family_on_claim", True):
        return free_login_family(account, state)
    leased = leased_families(account, state)
    for fam in list_login_families(account):  # lowest-first
        if fam in leased:
            continue
        path = login_family_creds_path(account, fam)
        try:
            creds = read_json(path)
        except (json.JSONDecodeError, OSError):
            continue  # unusable store; next
        rt = _credential_refresh_token(creds)
        if not rt:
            continue
        verdict, tok = _oauth_refresh_grant(rt)
        if verdict == "dead":
            dead_name = f"{path.name}.dead-{datetime.now(timezone.utc):%Y%m%d}"
            path.rename(path.with_name(dead_name))
            click.echo(f"claim-verify: {account}/{fam} refresh token DEAD (invalid_grant) — "
                       f"retired store to {dead_name}; trying next family (#127)")
            continue
        if verdict == "alive":
            # Persist the rotation BEFORE returning — after the grant, the
            # store's old refresh token is a dead branch by definition.
            oauth = dict(creds.get("claudeAiOauth") or {})
            oauth["accessToken"] = tok.get("access_token") or oauth.get("accessToken")
            oauth["refreshToken"] = tok.get("refresh_token") or rt
            if tok.get("expires_in"):
                oauth["expiresAt"] = int((time.time() + float(tok["expires_in"])) * 1000)
            creds = dict(creds)
            creds["claudeAiOauth"] = oauth
            backup_credentials_file(path)
            atomic_write_bytes(path, json.dumps(creds, indent=2).encode(), mode=0o600)
            click.echo(f"claim-verify: {account}/{fam} proven alive (refresh grant OK; "
                       f"store updated with rotated tokens — #127)")
            return fam
        click.echo(f"claim-verify: {account}/{fam} unverifiable (network/endpoint) — "
                   f"claiming unverified, fail-open (#127)")
        return fam
    return None


def login_family_provenance_path(account: str, family_id: str) -> Path:
    return login_family_dir(account, family_id) / "provenance.json"


def scaffold_login_family_dir(account: str, family_id: str) -> Path:
    """Create (idempotently) a minimal CLAUDE_CONFIG_DIR to `/login` into for one
    pooled family, and return it. Mirrors scaffold_login_store_dir (empty
    .claude.json + shared symlinks) but keyed by family, not slot."""
    dst = login_family_dir(account, family_id)
    dst.mkdir(parents=True, exist_ok=True)
    if not (dst / ".claude.json").exists():
        write_json(dst / ".claude.json", {})
    for sub in SHARED_SYMLINK_SUBDIRS:
        link = dst / sub
        target = CLAUDE_DIR / sub
        if target.exists() and not link.exists():
            link.symlink_to(target)
    return dst


def latest_family_id(account: str) -> str | None:
    """Highest-index family subdir for an account (usable or not) — the one a
    just-completed `cus login-mount <account>` scaffolded, for `--finish` to
    verify. None if the pool has no family dirs yet."""
    d = login_pool_dir(account)
    fams = [p.name for p in d.iterdir()
            if p.is_dir() and p.name.startswith(LOGIN_FAMILY_PREFIX)] if d.exists() else []
    return max(fams, key=_family_index) if fams else None


def family_identity(account: str, family_id: str) -> dict:
    """Identity facets `/login` recorded in a family dir's .claude.json."""
    path = login_family_dir(account, family_id) / ".claude.json"
    if not path.exists():
        return {}
    try:
        return _identity_fields(read_json(path))
    except (json.JSONDecodeError, OSError):
        return {}


def _shared_mount_holds(account: str, state: dict, config: dict | None = None) -> bool:
    """True iff the shared ~/.claude mount is a live holder of `account`.

    The shared mount holds state["active"] as a live account in `global` and
    `hybrid` mode — every bare (non-slot) session rides it. This is the second
    live-mount surface a slot swap can clobber (#104): a plain snapshot copy of
    the shared account into a slot puts one OAuth token family on TWO live mounts.

    Why NOT gated on mount_in_use(CLAUDE_DIR): bare sessions launch with the
    DEFAULT config dir and set no CLAUDE_CONFIG_DIR env var, so mount_pids can't
    see them and mount_in_use(CLAUDE_DIR) reads False even with dozens of live
    bare sessions. That false-negative is exactly the 2026-07-05 slot-move
    clobber (issue #141): `cus slot move slot-5 merkos --dry-run` said "not live
    on any other mount / plain snapshot" while merkos held the shared mount with
    ~30 bare sessions. So in global/hybrid mode the shared mount is treated as
    UNCONDITIONALLY holding state["active"] — mirroring the daemon, whose
    _hybrid_cycle excludes state["active"] from every slot target unconditionally
    (exclude_for_slots), never via a mount_in_use probe.

    In `per_session` mode the shared ~/.claude mount is observe-only (the daemon
    never manages it for bare sessions and never excludes state["active"] from
    slot decisions), so we preserve the pre-existing, detectable-only behavior
    there rather than newly blocking per_session slot moves onto state["active"].
    """
    if state.get("active") != account:
        return False
    mode = (config if config is not None else load_config()).get("mode", "global")
    if mode in ("global", "hybrid"):
        return True
    # per_session: honor only a detectable live holder (unchanged behavior).
    return mount_in_use(CLAUDE_DIR)


def _account_held_by_other_live_mount(state: dict, account: str, this_slot: str | None,
                                      config: dict | None = None) -> bool:
    """True iff `account` is currently live on a mount OTHER than `this_slot` — a
    live slot or the shared ~/.claude mount.

    Installing a plain snapshot COPY of an account onto a SECOND live mount is
    the #104 clobber (both refresh the one shared token family). So this gates
    whether a swap must claim a DISTINCT pooled family instead of copying.

    max_age_seconds=0 bypasses the occupancy cache: this is a credential-safety
    guard, and the 5s TTL is exactly one daemon cycle's fan-out window. Incident
    2026-07-03 16:10Z: slot-2 and slot-7 moved onto rayi3 back-to-back in one
    cycle; slot-7's guard read the cached map from before slot-2's install,
    judged rayi3 unheld, and copied the snapshot — two live mounts on one token
    family, and the next refresh logged slot-2 out (empty refreshToken on its
    live creds). Ground truth costs one /proc scan per swap; swaps are rare.

    `config` selects the mode used to judge the shared-mount holder (see
    _shared_mount_holds). Optional/back-compat: None re-loads it. The daemon's
    slot targets never include state["active"] (it's in exclude_for_slots), so
    the shared-mount clause is a no-op there — it only bites the manual
    `cus slot move` path, which has no such exclusion (issue #141)."""
    for s in occupied_slot_accounts(state, max_age_seconds=0.0).get(account, []):
        if s != this_slot:
            return True
    # The shared mount counts too: a slot copying merkos while the shared mount
    # is live on merkos double-books it exactly the same way (issue #141).
    return _shared_mount_holds(account, state, config)


def _live_family_would_collide(account: str, candidate_creds: Path, this_slot: str | None,
                               state: dict, config: dict | None = None) -> bool:
    """True iff the refresh-token FAMILY in `candidate_creds` already runs on
    ANOTHER live mount of `account` — the shared ~/.claude mount or another live
    lane. That is the GH #104 clobber itself: two live mounts on one refresh-token
    family, and the next rotation on either side logs the other out.

    Why compare ACTUAL token bytes (not login_family / has_independent_login
    state): the 2026-07-07 chats1a incident's legacy per-slot store PASSED
    `has_independent_login` (it carried *a* refresh token) yet that token was a
    STALE COPY of the account's shared snapshot family — so trusting the state
    flag judged it "safe" while installing it double-booked the shared family and
    logged the session out. Conversely a GENUINELY independent legacy login
    (distinct token, the supported Track-B case) is clobber-safe and must NOT be
    refused. Only the bytes distinguish the two, so this reads them — the same
    fingerprint primitive `duplicate_live_mount_families` uses.

    Closes the bare-session blind spot too: the existing live-mount detectors
    gate the shared mount on `mount_in_use(CLAUDE_DIR)`, which reads False for
    bare sessions (they set no CLAUDE_CONFIG_DIR — issue #141), so a lane sharing
    the shared-mount account was invisible to them. Here the shared mount is
    counted UNCONDITIONALLY in global/hybrid via `_shared_mount_holds`, exactly
    like the swap-time clobber guard.

    max_age_seconds=0 (ground-truth occupancy) for the same reason as
    `_account_held_by_other_live_mount`: this is a credential-safety check on the
    rare swap path, so it must not read a stale cache mid-fan-out.
    """
    try:
        cand_rt = _credential_refresh_token(read_json(candidate_creds))
    except (json.JSONDecodeError, OSError):
        return False  # unreadable candidate: not a family-collision (the #141
                      # install-point guard handles blank/unreadable sources)
    if not cand_rt:
        return False
    cand_fp = _refresh_fingerprint(cand_rt)
    other_mounts: list[Path] = []
    if _shared_mount_holds(account, state, config):
        other_mounts.append(CREDS_JSON)  # ~/.claude/.credentials.json (bare sessions)
    for s in occupied_slot_accounts(state, max_age_seconds=0.0).get(account, []):
        if s != this_slot:
            other_mounts.append(mount_creds_path(slot_path(s)))
    for p in other_mounts:
        try:
            rt = _credential_refresh_token(read_json(p))
        except (json.JSONDecodeError, OSError):
            continue
        if rt and _refresh_fingerprint(rt) == cand_fp:
            return True
    return False


def read_login_provenance(account: str, slot: str) -> dict | None:
    """Provenance record for a provisioned login, or None if unreadable."""
    path = login_store_provenance_path(account, slot)
    if not path.exists():
        return None
    try:
        prov = read_json(path)
        return prov if isinstance(prov, dict) else None
    except (json.JSONDecodeError, OSError):
        return None


def login_store_identity(account: str, slot: str) -> dict:
    """Identity facets (email/uuid/userID) recorded by `/login` in the store
    dir's .claude.json — used to verify the login landed on the right account."""
    path = login_store_cj_path(account, slot)
    if not path.exists():
        return {}
    try:
        return _identity_fields(read_json(path))
    except (json.JSONDecodeError, OSError):
        return {}


def account_canonical_identity(account: str) -> dict:
    """Identity facets from an account's canonical snapshot .claude.json.

    Reads oauthAccount straight from account-<name>/.claude.json rather than
    meta.yaml's oauth_email: the 2026-07-01 duplicate-identity incident showed
    meta.yaml can lie while .claude.json's oauthAccount is authoritative."""
    path = ACCOUNTS_DIR / f"account-{account}" / ".claude.json"
    if not path.exists():
        return {}
    try:
        return _identity_fields(read_json(path))
    except (json.JSONDecodeError, OSError):
        return {}


def scaffold_login_store_dir(account: str, slot: str) -> Path:
    """Create (idempotently) a minimum-viable CLAUDE_CONFIG_DIR to `/login`
    into for this (account, slot) pair, and return it.

    Mirrors `cus add`'s account-dir scaffold — empty .claude.json (so onboarding
    doesn't fight the login flow) plus the shared symlinks — because that scaffold
    is the proven-working target for an interactive login. Credentials land at
    <dir>/.credentials.json (a real file, never a symlink), which IS the store
    entry, so there is no separate capture/copy step to drift."""
    dst = login_store_dir(account, slot)
    dst.mkdir(parents=True, exist_ok=True)
    if not (dst / ".claude.json").exists():
        write_json(dst / ".claude.json", {})
    for sub in SHARED_SYMLINK_SUBDIRS:
        link = dst / sub
        target = CLAUDE_DIR / sub
        if target.exists() and not link.exists():
            link.symlink_to(target)
    return dst


def write_login_provenance(account: str, slot: str, source_email: str | None,
                           bootstrapped: bool = False) -> dict:
    """Record provenance for a provisioned login and return it.

    Called by `cus login-mount --finish` (after verifying a valid, right-account
    interactive login) and by `--from-existing` (bootstrap from the snapshot).
    Fingerprints the refresh token so later reads can tell families apart
    without re-storing the secret. `bootstrapped=True` marks an entry that is a
    COPY of the account's snapshot family (not an independent login) — only
    clobber-safe as the SOLE live mount for that account; a second mount of the
    same account must be a real `/login`."""
    creds = read_json(login_store_creds_path(account, slot))
    rt = _credential_refresh_token(creds)
    prov = {
        "account": account,
        "slot": slot,
        "minted_ts": now_iso(),
        "source_email": source_email or "unknown",
        "refresh_fp": _refresh_fingerprint(rt) if rt else None,
        "bootstrapped": bootstrapped,
    }
    write_json(login_store_provenance_path(account, slot), prov)
    return prov


def duplicate_login_families() -> list[dict]:
    """Store entries whose refresh-token fingerprint is shared by MORE THAN ONE
    (account, slot) pair — i.e. the SAME token family installed on two mounts.

    This is the precise failure this whole feature exists to prevent: two live
    mounts sharing one refresh-token family clobber on rotation. It happens if
    an account is bootstrapped (`--from-existing`) onto two slots, or a store
    entry is hand-copied. Returns one dict per offending fingerprint:
    {refresh_fp, pairs: ["slot->account", ...]}."""
    by_fp: dict[str, list[str]] = {}
    for rec in list_provisioned_logins():
        prov = rec.get("provenance") or {}
        fp = prov.get("refresh_fp")
        if fp:
            by_fp.setdefault(fp, []).append(f"{rec['slot']}->{rec['account']}")
    return [{"refresh_fp": fp, "pairs": sorted(pairs)}
            for fp, pairs in by_fp.items() if len(pairs) > 1]


def duplicate_live_mount_families(state: dict) -> list[dict]:
    """One refresh-token family live on TWO+ mounts — the #104 clobber itself,
    measured at the LIVE mounts (slot dirs + the global ~/.claude pair) rather
    than the independent-login store (duplicate_login_families only sees
    provisioned logins). Catches double-books however they arose: the daemon's
    pre-2026-07-03 fan-out double-up (slot-3/slot-4 incident — `cus sos` said
    all-clear while one token refresh away from a forced logout), `cus launch
    --force`, or hand-copied credential files. Idle mounts are skipped: with
    no session to refresh the token, a duplicate can't clobber yet.
    Returns [{"refresh_fp", "mounts": ["slot-3 (rayi2)", ...]}]."""
    by_fp: dict[str, list[str]] = {}
    mounts: list[tuple[str, Path, str | None]] = [
        (d.name, d, state.get("slots", {}).get(d.name, {}).get("account"))
        for d in list_slot_dirs()
    ]
    mounts.append(("shared-mount", CLAUDE_DIR, state.get("active")))
    for name, path, acct in mounts:
        if not mount_in_use(path):
            continue
        try:
            rt = read_json(path / ".credentials.json").get("claudeAiOauth", {}).get("refreshToken")
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            continue
        if rt:
            by_fp.setdefault(_refresh_fingerprint(rt), []).append((f"{name} ({acct or '?'})", acct))
    return [{"refresh_fp": fp,
             "mounts": sorted(label for label, _ in ms),
             "accounts": sorted({a for _, a in ms if a})}
            for fp, ms in sorted(by_fp.items()) if len(ms) > 1]


def double_booked_live_accounts(state: dict) -> list[dict]:
    """Accounts live on 2+ mounts without enough independent logins to make
    the extra mounts safe — the #104 condition tracked by ACCOUNT, catching
    both phases of the clobber that duplicate_live_mount_families (same-family)
    only sees before the first rotation:

      phase "will_clobber":   families still identical — the next token
                              refresh on either mount logs the other out;
      phase "already_rotated": families diverged — one mount already refreshed,
                              so the mount(s) still on the old family hold a
                              DEAD refresh token and fail at next refresh (the
                              2026-07-01 duplicate-identity incident shape;
                              seen again on slot-3/slot-4, 2026-07-03).

    Lane sharing is one mount hosting many sessions, so 2+ live MOUNTS on one
    account is never lane sharing; it's safe only when each extra mount has
    its own independent login (distinct family, GH #109). Slots with a
    provisioned login are subtracted before flagging.
    Returns [{"account", "phase", "mounts": ["slot-3", ...]}]."""
    mounts: list[tuple[str, Path, str | None]] = [
        (d.name, d, state.get("slots", {}).get(d.name, {}).get("account"))
        for d in list_slot_dirs()
    ]
    mounts.append(("shared-mount", CLAUDE_DIR, state.get("active")))
    by_account: dict[str, list[tuple[str, str | None]]] = {}
    for name, path, acct in mounts:
        if not acct or not mount_in_use(path):
            continue
        try:
            rt = read_json(path / ".credentials.json").get("claudeAiOauth", {}).get("refreshToken")
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            rt = None
        by_account.setdefault(acct, []).append((name, _refresh_fingerprint(rt) if rt else None))
    out: list[dict] = []
    for acct, ms in sorted(by_account.items()):
        # A mount is SAFE (has a distinct token family) if it holds a pooled
        # LEASE (2026-07-03 pool model) or a legacy per-slot login; the shared
        # mount can have neither, so it always counts as unsafe. Only 2+ UNSAFE
        # mounts on one account are a clobber — a single uncovered mount (the
        # primary) alongside covered families is exactly the intended pool
        # arrangement (primary + N families). Without the lease check this
        # false-flagged a correct pool rescue as an URGENT "diverged" clobber
        # (slot-1 leased 03/family-1 while slot-7 held 03's primary — distinct
        # families, no clobber, but flagged).
        def _covered(n: str) -> bool:
            if n == "shared-mount":
                return False
            if has_independent_login(acct, n):
                return True
            lease = slot_leased_family(state, n)
            return lease is not None and lease[0] == acct
        unsafe = [(n, fp) for n, fp in ms if not _covered(n)]
        if len(unsafe) < 2:
            continue
        fps = {fp for _, fp in unsafe if fp}
        out.append({
            "account": acct,
            "phase": "will_clobber" if len(fps) <= 1 else "already_rotated",
            "mounts": sorted(n for n, _ in unsafe),
        })
    return out


def login_age_days(account: str, slot: str) -> float | None:
    """Days since this login was minted (per provenance), or None if unknown."""
    prov = read_login_provenance(account, slot)
    minted = prov.get("minted_ts") if prov else None
    if not minted:
        return None
    try:
        minted_dt = datetime.fromisoformat(minted.replace("Z", "+00:00"))
    except ValueError:
        return None
    return (datetime.now(timezone.utc) - minted_dt).total_seconds() / 86400.0


def _expiry_state_from_age(age_days: float | None, config: dict) -> tuple[str, float | None]:
    """Classify a refresh token's remaining life from its age in days against the
    ASSUMED refresh-token lifetime. Returns (state, days_left) where state is:
      "ok"      — comfortably within lifetime
      "near"    — within warn_expiry_within_days of assumed expiry
      "expired" — past the assumed lifetime (a swap would fall back to a copy)
      "unknown" — no age to judge by (age_days is None)

    Factored out of login_expiry_state (2026-07-06) so the SAME assumed-TTL math
    can be reused by the pre-emptive live-mount creds-health scan, which needs to
    classify base-account-snapshot / shared-mount / lane refresh tokens — not just
    provisioned login families. login_expiry_state remains the login-family entry
    point (it feeds this the provenance-derived age); the new scan feeds it an age
    resolved per-mount (`_mount_refresh_age_days`). Keeping one classifier means
    the "near"/"expired" thresholds can never drift between the two callers.

    The lifetime is a config assumption (refresh_token_ttl_days), NOT a measured
    fact — #109 Phase 0 still owes the real number. This only drives a soft sos
    nudge, never a hard action, so an over/under-estimate degrades gracefully."""
    cfg = config.get("independent_logins", {}) if isinstance(config, dict) else {}
    ttl = cfg.get("refresh_token_ttl_days", 30)
    warn_within = cfg.get("warn_expiry_within_days", 5)
    if age_days is None:
        return ("unknown", None)
    days_left = ttl - age_days
    if days_left <= 0:
        return ("expired", days_left)
    if days_left <= warn_within:
        return ("near", days_left)
    return ("ok", days_left)


def login_expiry_state(account: str, slot: str, config: dict) -> tuple[str, float | None]:
    """Classify a provisioned login's freshness against the ASSUMED refresh-token
    lifetime. Returns (state, days_left) — see `_expiry_state_from_age` for the
    state vocabulary. Thin wrapper: resolves the login family's provenance age
    (`login_age_days`) and hands it to the shared classifier."""
    return _expiry_state_from_age(login_age_days(account, slot), config)


def list_provisioned_logins() -> list[dict]:
    """Enumerate every (account, slot) pair with a store dir on disk.

    Returns dicts with account, slot, has_login (usable creds present), the
    provenance record (if any), and the identity email `/login` recorded — the
    raw material for status/sos surfacing. Sorted by (account, slot index)."""
    root = login_store_root()
    if not root.exists():
        return []
    out: list[dict] = []
    for account_dir in sorted(p for p in root.iterdir() if p.is_dir()):
        account = account_dir.name
        for slot_d in sorted(p for p in account_dir.iterdir() if p.is_dir()):
            slot = slot_d.name
            ident = login_store_identity(account, slot)
            out.append({
                "account": account,
                "slot": slot,
                "has_login": has_independent_login(account, slot),
                "provenance": read_login_provenance(account, slot),
                "identity_email": ident.get("emailAddress"),
            })
    def _sort_key(rec: dict) -> tuple:
        try:
            idx = int(rec["slot"].removeprefix(SLOT_PREFIX))
        except ValueError:
            idx = 1 << 30
        return (rec["account"], idx)
    return sorted(out, key=_sort_key)


def _slots_missing_logins(state: dict) -> list[tuple[str, str]]:
    """(slot, account) pairs whose account has NO login coverage — neither a
    pooled family (2026-07-03 pool model) nor a legacy per-(slot,account) login.

    Coverage means the account can back a second live mount without a clobber:
    a slot on such an account can borrow a free family at swap time. An account
    with a pool is covered for ALL its slots, so we key the "missing" test on the
    account's pool first, then the legacy per-slot login. Ordered by slot index."""
    slots = state.get("slots", {}) if isinstance(state, dict) else {}
    out: list[tuple[str, str]] = []
    for slot_name, entry in slots.items():
        account = entry.get("account") if isinstance(entry, dict) else None
        if not account:
            continue  # empty slot — nothing to host yet
        if list_login_families(account):
            continue  # account has a pool → this slot can borrow; no per-slot login needed
        if not has_independent_login(account, slot_name):
            out.append((slot_name, account))
    def _idx(pair: tuple[str, str]) -> int:
        try:
            return int(pair[0].removeprefix(SLOT_PREFIX))
        except ValueError:
            return 1 << 30
    return sorted(out, key=_idx)


def independent_logins_enabled(config: dict | None = None) -> bool:
    """Whether the Phase 2 gate is on (independent_logins.use_independent_logins).

    OFF by default → the swap path is bit-for-bit today's shared-snapshot copy."""
    cfg = config if config is not None else load_config()
    return bool(cfg.get("independent_logins", {}).get("use_independent_logins", False))


def swap_install_source(target_name: str, slot: str | None, snapshot_creds: Path,
                        config: dict | None = None) -> tuple[Path, bool]:
    """Which creds file to install into a live mount when swapping to target_name.

    Returns (path, used_independent). Phase 2 (#109): a SLOT swap, with the gate
    on and an independent login provisioned for (target, slot), installs THAT
    login's own token family — so two live mounts on one account never share a
    refresh-token family to clobber (#104/#103/#3). Every other case (gate off,
    no slot, no provisioned login) returns the shared per-account snapshot, i.e.
    today's copy path. Lazy fallback is deliberate: an un-provisioned pair still
    works (as a copy), it just isn't clobber-safe until `cus login-mount` runs."""
    if slot is not None and independent_logins_enabled(config) and has_independent_login(target_name, slot):
        return (login_store_creds_path(target_name, slot), True)
    return (snapshot_creds, False)


def saveback_to_login_store(account: str, slot: str, live_creds: dict, live_bytes: bytes,
                            dest_path: Path | None = None) -> str:
    """Save a slot's live creds back into its independent-login store entry,
    instead of the shared account snapshot (Phase 3a, #109).

    `dest_path` (2026-07-03 pool model): the exact store file to write. When
    given (the pool case) it targets the LEASED family's creds
    (login_family_creds_path); when omitted it falls back to the legacy
    per-(account, slot) path. Either way the freshness guard + backup are the
    same — only the destination differs.

    WHY this must NOT go through classify_live_creds_owner: an independent login
    deliberately carries a refresh token that matches NO account snapshot, so the
    snapshot-matcher would verdict "unknown" and write the independent family
    over the shared snapshot — reconciling the two families and re-introducing
    the exact clobber this design removes (handoff §5.5). The mount has exactly
    one writer session in per_session mode, so the live file provably belongs to
    this (account, slot) family; we save it straight back to the store.

    Carries the GH #77 freshness guard (never overwrite a newer stored login,
    e.g. one just re-provisioned) and the GH #79 backup. Returns a status word."""
    store_path = dest_path if dest_path is not None else login_store_creds_path(account, slot)
    live_exp = _creds_expires_at(live_creds)
    if store_path.exists() and live_exp is not None:
        try:
            store_exp = _creds_expires_at(read_json(store_path))
        except (json.JSONDecodeError, OSError):
            store_exp = None  # unreadable store entry can't claim freshness
        if store_exp is not None and store_exp > live_exp:
            return "skipped-stale"
    backup_credentials_file(store_path)
    atomic_write_bytes(store_path, live_bytes, mode=0o600)
    return "saved"


def _pid_config_dir(pid: int) -> str | None:
    """CLAUDE_CONFIG_DIR from one process's /proc environ, or None.

    Orchestration-independent ground truth for which mount a live process is
    using — readable only for same-user processes, which is exactly the set
    that could be holding our mounts.
    """
    try:
        environ = Path(f"/proc/{pid}/environ").read_bytes()
    except (OSError, PermissionError):
        return None
    for chunk in environ.split(b"\0"):
        if chunk.startswith(b"CLAUDE_CONFIG_DIR="):
            return chunk.split(b"=", 1)[1].decode("utf-8", "replace")
    return None


def mount_pids(mount: Path) -> list[int]:
    """PIDs of live processes whose CLAUDE_CONFIG_DIR is this mount.

    Ground truth from /proc/<pid>/environ — orchestration-independent, so it
    catches sessions cus didn't launch and survives state.json drift.

    Every descendant of a claude process (hook shells, subagent bash) inherits
    the env var and counts as "using the mount" — deliberately so: a slot is
    busy while ANY process holds it, not just the top-level claude.
    """
    want = str(mount).rstrip("/")
    pids: list[int] = []
    proc = Path("/proc")
    try:
        entries = list(proc.iterdir())
    except OSError:
        return []
    for p in entries:
        if not p.name.isdigit():
            continue
        val = _pid_config_dir(int(p.name))
        if val is not None and val.rstrip("/") == want:
            pids.append(int(p.name))
    return pids


def pane_mount_name(pane: str) -> str | None:
    """Which mount ('slot-N' / 'account-X') a tmux pane's claude process uses.

    None = bare/global (~/.claude/) or undeterminable. Walks the pane's
    process tree to the claude pid and reads its /proc environ — same ground
    truth as mount_pids, from the pane side (for `cus status` display).
    """
    if not pane or pane == "no-tmux":
        return None
    try:
        pid = _pane_pid(pane)
        if not pid:
            return None
        cpid = _find_descendant_claude_pid(pid) or pid
    except Exception:
        return None
    cfg = _pid_config_dir(cpid)
    if not cfg:
        return None
    p = Path(cfg).expanduser()
    if p.parent == ACCOUNTS_DIR and (p.name.startswith(SLOT_PREFIX) or p.name.startswith("account-")):
        return p.name
    return None


def mount_account_from_env(state: dict) -> tuple[str | None, str | None]:
    """Resolve (mount_name, account) from this process's own CLAUDE_CONFIG_DIR.

    Hooks and the statusline run as children of the claude process, so the
    env var is inherited — this is how the session side learns which account
    it is on WITHOUT trusting global state (Phase 2.3). Returns:

      - ("slot-N", account-or-None)  when the env points at a slot dir; the
        account is the slot's CURRENT occupant per state.slots — callers must
        re-resolve on every render, because a swap changes it under a live
        session while the env var stays the same.
      - ("account-X", "X")           when it points at an account dir (a
        relogin-style launch — the dir IS the account).
      - (None, None)                 bare launch / env unset / unrecognized.
    """
    cfg_dir = os.environ.get("CLAUDE_CONFIG_DIR")
    if not cfg_dir:
        return (None, None)
    p = Path(cfg_dir).expanduser()
    candidates = [p]
    try:
        candidates.append(p.resolve())
    except OSError:
        pass
    for c in candidates:
        if c.parent != ACCOUNTS_DIR:
            continue
        if c.name.startswith(SLOT_PREFIX):
            return (c.name, state.get("slots", {}).get(c.name, {}).get("account"))
        if c.name.startswith("account-"):
            return (c.name, c.name.removeprefix("account-"))
    return (None, None)


def pick_launch_account(state: dict, config: dict) -> "SwapTarget | None":
    """Pick the best account for a NEW session (Phase 2.2 `cus launch auto`).

    Reuses pick_swap_target's full filter+scoring stack (token-expired /
    saturation / hard-7d / would-re-trip / strategy preferences, GH #69
    included) over the WHOLE pool by shimming a sentinel "active" — a swap
    picker excludes the active account, but for a launch every account is a
    candidate.

    Spreading: accounts on ANOTHER mount are excluded on the first pass so N
    sessions land on N accounts. The fallback narrows to only LIVE-mount
    accounts as the hard floor — an account on a live mount is never returned
    as a NEW mount (two live mounts on one account rotate its single-use token
    and log one out — GH #104), but an IDLE slot's account is fair game to
    reuse (no live mount holds it).

    Lane-share final fallback (2026-07-03, supersedes the pre-lanes "never
    return a live-mount account" hard floor): with per_session.lane_sharing on,
    a live-mounted account IS a legal target — the launch JOINS its existing
    mount (same dir, same login family, no second mount, no clobber) instead
    of minting a new one. So when every healthy account is live (the saturated
    regime that used to refuse), return the lowest-estimated-usage live
    account and let _launch_prepare route it to the join path. Returns None
    only when every account is expired/erroring (or lane_sharing is off).
    """
    # Spread preference: any account a slot entry names, plus the shared active.
    spread_occupied = {e.get("account") for e in state.get("slots", {}).values() if e.get("account")}
    if state.get("active"):
        spread_occupied.add(state["active"])
    # Hard floor: accounts on a LIVE mount — never double-book these.
    live_occupied = _live_slot_accounts(state)
    if state.get("active") and mount_in_use(CLAUDE_DIR):
        live_occupied.add(state["active"])

    def _try(excluded: set) -> SwapTarget | None:
        shim = dict(state)
        shim["accounts"] = {n: a for n, a in state.get("accounts", {}).items() if n not in excluded}
        shim["active"] = "\x00launch-sentinel"  # never a real account name
        if not shim["accounts"]:
            return None
        try:
            return pick_swap_target(shim, config)
        except Exception:
            return None

    # Operator-disabled accounts are out of rotation for launches too — and
    # these two raw fallbacks below BYPASS pick_swap_target's universal
    # filters, so they need the exclusion explicitly (2026-07-03).
    disabled = _disabled_accounts(config)

    # First: spread across all mounts. Fallback: allow reusing idle-slot
    # accounts, but still never a live-mount account.
    target = _try(spread_occupied) or _try(live_occupied)
    if target is None:
        cands = [(n, a) for n, a in state.get("accounts", {}).items()
                 if not a.get("token_expired") and not a.get("poll_error")
                 and n not in live_occupied and n not in disabled]
        if cands:
            n, _ = min(cands, key=lambda p: _account_estimated_effective_pct(p[1], config))
            target = SwapTarget(name=n, reason="launch fallback: lowest estimated usage")
    # Lane-share final fallback (GH #109 lanes, 2026-07-03): every healthy
    # account is on a live mount. Joining a live mount is safe (shared dir,
    # shared login family), so pick the healthiest live account rather than
    # refusing — the refusal predates lanes and left `cus launch auto` dead
    # whenever slots saturated the pool, even with a 4%-used account joinable.
    if target is None and config.get("per_session", {}).get("lane_sharing", False):
        cands = [(n, a) for n, a in state.get("accounts", {}).items()
                 if not a.get("token_expired") and not a.get("poll_error")
                 and n in live_occupied and n not in disabled]
        if cands:
            n, _ = min(cands, key=lambda p: _account_estimated_effective_pct(p[1], config))
            target = SwapTarget(name=n, reason="lane-share fallback: all healthy accounts on live mounts; joining lowest-usage lane")
    return target


def mount_in_use(mount: Path) -> bool:
    return bool(mount_pids(mount))


def scaffold_mount_dir(mount: Path) -> list[str]:
    """Create/heal a mount dir to the canonical layout. Idempotent.

    Creates the dir, the shared-state symlinks (dirs + files, only when the
    target exists in ~/.claude/ — no dangling links), and an empty {}
    .claude.json if none exists (Claude Code needs something valid to read;
    sync_mount_claude_json fills it in properly).

    Returns a list of human-readable actions taken (empty = was already
    canonical). Does NOT touch existing real files/dirs — that judgment
    (fold-and-relink vs leave) belongs to doctor --fix-dirs.
    """
    actions: list[str] = []
    if not mount.exists():
        # exist_ok=True: two racing creators can both reach here for the same
        # index (the create_slot TOCTOU); the loser must not crash on
        # FileExistsError. The under-lock allocation in create_slot makes the
        # race rare, but scaffold is also called directly (doctor pre-flight,
        # launch), so keep it independently safe.
        mount.mkdir(parents=True, exist_ok=True)
        actions.append(f"created {mount}")
    for sub in SHARED_SYMLINK_SUBDIRS + SHARED_SYMLINK_FILES:
        link = mount / sub
        target = CLAUDE_DIR / sub
        if link.is_symlink() or link.exists():
            continue
        if not target.exists():
            continue
        link.symlink_to(target)
        actions.append(f"symlinked {sub} → {target}")
    cj = mount_claude_json_path(mount)
    if not cj.exists():
        write_json(cj, {})
        actions.append("seeded empty .claude.json")
    return actions


def _slot_reserved(entry: dict, now: datetime | None = None) -> bool:
    """True while a slot's reservation (set by acquire/create) is still fresh.

    A reserved slot is treated as busy by both free-slot scans and gc, so an
    in-flight launch that hasn't exec'd claude yet (no PID on the mount) is not
    stolen by a concurrent launch or reaped by the daemon.
    """
    until = entry.get("reserved_until")
    if not until:
        return False
    try:
        until_dt = datetime.fromisoformat(until.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return False
    return (now or datetime.now(timezone.utc)) < until_dt


def _slot_busy(name: str, entry: dict) -> bool:
    """A slot is busy (not allocatable, not gc-able) if a live process holds it
    OR it carries a fresh reservation."""
    return mount_in_use(slot_path(name)) or _slot_reserved(entry)


def _reserve(entry: dict) -> dict:
    entry["reserved_until"] = (datetime.now(timezone.utc) + timedelta(seconds=SLOT_RESERVATION_SECONDS)) \
        .isoformat().replace("+00:00", "Z")
    return entry


def _allocate_slot_unlocked(state: dict) -> tuple[str, Path]:
    """Lowest-free-index allocation + scaffold + reservation, NO lock/save.

    Inner helper so acquire_slot (already holding the swap lock) can allocate
    without re-taking the lock (flock is not reentrant across fds in one
    process — a nested take would deadlock).
    """
    slots = state.setdefault("slots", {})
    n = 1
    while True:
        name = f"{SLOT_PREFIX}{n}"
        # Free index = no state entry AND no dir on disk. A dir with no entry is
        # an orphan (SOS flags it); skip its index rather than colliding.
        if name not in slots and not slot_path(name).exists():
            break
        n += 1
    d = slot_path(name)
    scaffold_mount_dir(d)
    slots[name] = _reserve({"account": None, "created_ts": now_iso()})
    return name, d


def create_slot(state: dict) -> tuple[str, Path]:
    """Allocate the lowest free slot index, scaffold, register + reserve it.

    Runs under the swap lock and persists state itself (then syncs the caller's
    `state["slots"]` to the just-persisted view), so two concurrent creators
    can't allocate the same index — the loser reloads inside the lock and picks
    the next free one. The reservation makes the new slot look busy until its
    launch attaches a PID.
    """
    with _swap_lock():
        fresh = load_state()
        name, d = _allocate_slot_unlocked(fresh)
        save_state(fresh)
    state["slots"] = fresh.get("slots", {})
    return name, d


def acquire_slot(state: dict, prefer_account: str | None = None) -> tuple[str, Path]:
    """Find a free slot for a launch (create one if none), reserving it.

    Runs the free-scan + reservation under the swap lock on a freshly-reloaded
    state, so two concurrent `cus launch`es can't both claim the same slot: the
    reservation is persisted before the lock releases, so the second acquire
    sees it as busy (review finding 2026-07-02: acquire TOCTOU). A slot is free
    only if no live PID holds it AND it carries no fresh reservation.

    Preference order: a free slot ALREADY holding prefer_account (no swap
    needed), then any free slot, then a new slot. Orphan dirs (on disk, no
    state entry) are adopted. Persists state and syncs the caller's view.
    """
    with _swap_lock():
        fresh = load_state()
        slots_state = fresh.setdefault("slots", {})
        free: list[Path] = []
        for d in list_slot_dirs():
            entry = slots_state.setdefault(d.name, {"account": None, "created_ts": now_iso()})
            if _slot_busy(d.name, entry):
                continue
            free.append(d)
        chosen: Path | None = None
        if prefer_account:
            for d in free:
                if slots_state[d.name].get("account") == prefer_account:
                    chosen = d
                    break
        if chosen is None and free:
            chosen = free[0]
        if chosen is not None:
            _reserve(slots_state[chosen.name])
            name, d = chosen.name, chosen
        else:
            name, d = _allocate_slot_unlocked(fresh)  # already locked — use inner
        save_state(fresh)
    state["slots"] = fresh.get("slots", {})
    return name, d


def sync_mount_claude_json(mount: Path, canonical: dict) -> dict:
    """Merge all NON-account-bound top-level keys from `canonical` into a
    mount's .claude.json, preserving the mount's own identity keys (Phase 1.3).

    The inverse of the swap's surgical merge: swaps move ONLY the account-bound
    keys, this moves EVERYTHING ELSE (MCP registrations, per-project state,
    onboarding flags) so N live copies don't drift apart between slot launches.

    MUST NOT run against a mount with a live session — Claude Code rewrites
    .claude.json itself and a concurrent merge is a lost-update race. Callers
    gate on mount_in_use(); this function only does the merge.

    Returns {"changed": bool, "keys_updated": [...]}.
    """
    cj_path = mount_claude_json_path(mount)
    existing = read_json(cj_path) if cj_path.exists() else {}
    merged = dict(existing)
    updated: list[str] = []
    for k, v in canonical.items():
        if k in ACCOUNT_BOUND_KEYS:
            continue
        if k not in merged or merged[k] != v:
            merged[k] = v
            updated.append(k)
    # Identity keys always come from the mount itself, never the canonical —
    # this is what keeps a slot's account pinned through a sync.
    for k in ACCOUNT_BOUND_KEYS:
        if k in existing:
            merged[k] = existing[k]
    if updated:
        write_json(cj_path, merged)
    return {"changed": bool(updated), "keys_updated": updated}


def saveback_mount_credentials(mount: Path, expected_account: str | None, state: dict,
                               only_if_fresher: bool = False) -> dict:
    """Save a mount's live credentials back to the owning account dir, with the
    full clobber-guard stack (GH #3 identity match, GH #77 freshness, GH #79
    backup rotation). Shared by slot gc, per-slot swap-out, and periodic
    save-back — one guarded implementation, not three.

    only_if_fresher=True (the daemon's PERIODIC save-back, Phase 3.5): write
    only when the live tokens are strictly newer (expiresAt) than the
    snapshot's — i.e. the session actually refreshed since the last save.
    Without this, every cycle would rewrite identical bytes and churn the
    GH #79 backup rotation into uselessness.

    Returns {"action": "saved" | "saved_to_owner" | "skipped", "account": ...,
    "detail": ...}. Never raises on a missing/invalid live file — a mount with
    nothing worth saving is a skip, not an error.
    """
    creds_path = mount_creds_path(mount)
    if not creds_path.exists():
        return {"action": "skipped", "account": expected_account, "detail": "no live credentials in mount"}
    try:
        live_creds = json.loads(creds_path.read_bytes())
    except (json.JSONDecodeError, OSError) as e:
        return {"action": "skipped", "account": expected_account, "detail": f"unreadable live creds: {e}"}

    # Pool model (#109): a LEASED mount's live tokens belong to its claimed
    # login family, not the account snapshot — route the save-back to the
    # family dir, mirroring the swap-path save-back in _execute_swap_locked.
    # Regression fixed 2026-07-03: this shared function (periodic save-back +
    # slot gc) lacked the branch, so minutes after slot-2 claimed
    # rayi3/family-1 the PERIODIC save-back stomped account-rayi3's snapshot
    # with the family's tokens — leaving the snapshot family's freshest
    # tokens existing only on slot-7's live mount, and any future
    # snapshot-install double-booking family-1 with slot-2 (#104). Bypasses
    # classify_live_creds_owner by design: a pooled family's refresh token
    # matches no snapshot, so classification can only mis-route it.
    slot_name = mount.name if mount.name.startswith(SLOT_PREFIX) else None
    lease = slot_leased_family(state, slot_name) if slot_name else None
    if (lease is not None and expected_account and lease[0] == expected_account
            and independent_logins_enabled()):
        fam_path = login_family_creds_path(*lease)
        live_exp = _creds_expires_at(live_creds)
        fam_exp = None
        if fam_path.exists():
            try:
                fam_exp = _creds_expires_at(read_json(fam_path))
            except (json.JSONDecodeError, OSError):
                fam_exp = None
        # Same GH #77 freshness guards as the snapshot path below, applied to
        # the family store file instead.
        if fam_exp is not None and live_exp is not None and fam_exp > live_exp:
            return {"action": "skipped", "account": expected_account,
                    "detail": f"family store fresher than live ({lease[0]}/{lease[1]})"}
        if only_if_fresher and not (fam_exp is not None and live_exp is not None and live_exp > fam_exp):
            return {"action": "skipped", "account": expected_account,
                    "detail": "live not fresher than family store (periodic no-op)"}
        backup_credentials_file(fam_path)
        atomic_write_bytes(fam_path, json.dumps(live_creds, indent=2).encode(), mode=0o600)
        _cred_audit("saveback", "wrote", "pooled family store (#109)",
                    slot=slot_name, mount=mount.name, account=expected_account,
                    login_family=f"{lease[0]}/{lease[1]}", token_fp=_audit_token_fp(live_creds))
        return {"action": "saved", "account": expected_account, "family": lease[1],
                "detail": f"pooled login {lease[0]}/{lease[1]} (#109 pool)"}

    verdict, owner, detail = classify_live_creds_owner(live_creds, expected_account or "", state)
    if verdict in ("invalid", "conflict"):
        return {"action": "skipped", "account": expected_account, "detail": f"{verdict}: {detail}"}
    # "foreign" routes to the true owner (GH #3); "expected"/"unknown" go to
    # the expected account — same routing as _execute_swap_locked.
    save_to = owner if verdict == "foreign" and owner else expected_account
    if not save_to:
        return {"action": "skipped", "account": None, "detail": f"no owner resolvable ({verdict}: {detail})"}

    snap = ACCOUNTS_DIR / f"account-{save_to}" / ".credentials.json"
    # ---- 2026-07-06 wrong-account clobber guard (periodic/gc credentials) ----
    # For an "unknown" verdict (no refresh-token lineage — rotation-shaped) cross-
    # check the MOUNT's own .claude.json identity against save_to's trusted anchor
    # (meta.yaml + login families). A drifted mount whose identity disagrees is not
    # a rotation; saving its tokens into save_to's snapshot is the same clobber the
    # swap path guards. "expected"/"foreign" verdicts are lineage-proven and bypass
    # this. Degrades to allow when there is no anchor (unchanged legacy behavior).
    if verdict == "unknown":
        mount_cj = mount_claude_json_path(mount)
        mount_ident = {}
        if mount_cj.exists():
            try:
                mount_ident = _identity_fields(read_json(mount_cj))
            except (json.JSONDecodeError, OSError):
                mount_ident = {}
        ok, why = guard_canonical_identity_write(save_to, mount_ident)
        if not ok:
            sidecar = quarantine_rejected_canonical_write(
                snap, json.dumps(live_creds, indent=2).encode(), why)
            _cred_audit("identity-refuse", "refused-identity", why,
                        slot=slot_name, mount=mount.name, account=save_to,
                        incoming_identity=mount_ident, emit_identity=True,
                        existing_identity=account_identity_anchor(save_to)[0],
                        token_fp=_audit_token_fp(live_creds),
                        extra="quarantined=" + sidecar.name)
            return {"action": "refused_wrong_account", "account": save_to,
                    "detail": f"drifted mount, unknown lineage — {why}; quarantined to {sidecar.name}"}
    # GH #77: never let an older live file clobber a fresher snapshot (e.g.
    # the account was re-logged-in out-of-band while this mount sat idle).
    live_exp = _creds_expires_at(live_creds)
    if snap.exists():
        try:
            snap_exp = _creds_expires_at(read_json(snap))
        except (json.JSONDecodeError, OSError):
            snap_exp = None
        if snap_exp is not None and live_exp is not None and snap_exp > live_exp:
            _cred_audit("freshness-skip", "skipped-freshness", "snapshot fresher than live (GH #77)",
                        slot=slot_name, mount=mount.name, account=save_to,
                        token_fp=_audit_token_fp(live_creds))
            return {"action": "skipped", "account": save_to, "detail": "snapshot fresher than live (GH #77)"}
        if only_if_fresher and not (snap_exp is not None and live_exp is not None and live_exp > snap_exp):
            return {"action": "skipped", "account": save_to, "detail": "live not fresher than snapshot (periodic no-op)"}
    backup_credentials_file(snap)
    atomic_write_bytes(snap, json.dumps(live_creds, indent=2).encode(), mode=0o600)
    action = "saved_to_owner" if verdict == "foreign" else "saved"
    # Audit the canonical-store write: incoming = the mount's own identity, existing
    # = save_to's trusted anchor. In a clobber these disagree — the whole reason the
    # line records both. `verdict` (expected/foreign/unknown) explains the routing.
    _cred_audit("saveback", "wrote", f"snapshot save-back (verdict={verdict})",
                slot=slot_name, mount=mount.name, account=save_to,
                incoming_identity=_dir_identity_of_mount(mount), emit_identity=True,
                existing_identity=account_identity_anchor(save_to)[0],
                token_fp=_audit_token_fp(live_creds))
    return {"action": action, "account": save_to, "detail": detail}


def gc_slot(name: str, state: dict, force: bool = False) -> dict:
    """Reap one slot: refuse if in use, save creds back, remove the dir,
    drop the state entry. Caller is responsible for save_state().

    force=True skips only the in-use refusal (for cleaning up after a
    /proc-invisible holder the operator knows is dead) — the save-back and
    its guards always run.
    """
    d = slot_path(name)
    entry = state.get("slots", {}).get(name, {})
    if not d.exists():
        state.get("slots", {}).pop(name, None)
        return {"action": "dropped_stale_entry", "slot": name}
    if not force and mount_in_use(d):
        return {"action": "refused_in_use", "slot": name, "pids": mount_pids(d)}
    if not force and _slot_reserved(entry):
        # An in-flight launch has claimed this slot but not exec'd claude yet
        # (no PID). rmtree'ing it now would pull the dir out from under the
        # launch that's mid-install (review finding 2026-07-02: gc-mid-launch).
        return {"action": "refused_reserved", "slot": name, "reserved_until": entry.get("reserved_until")}
    expected = state.get("slots", {}).get(name, {}).get("account")
    saveback = saveback_mount_credentials(d, expected, state)
    shutil.rmtree(d)
    state.get("slots", {}).pop(name, None)
    return {"action": "reaped", "slot": name, "saveback": saveback}


def doctor_mount(mount: Path, fix: bool = False) -> list[dict]:
    """Check (and with fix=True, heal) one mount dir against the canonical
    layout. Idempotent: a healed mount reports no findings on re-run.

    Findings are dicts: {"entry", "problem", "action"} where action is what
    was done (fix=True) or what --fix-dirs would do (fix=False). The heal
    rules, per the ARCHITECTURE.md inventory:

      - missing symlink            → create (only if target exists in ~/.claude/)
      - symlink to wrong target    → repoint
      - real DIR where symlink expected → move children into the shared target
        (skip name collisions — uuid-keyed dirs make these rare), then relink.
        If collisions remain, leave the dir and report; merging colliding
        session state is operator judgment, not doctor's.
      - real FILE where symlink expected (settings.json stub) → fold keys the
        shared file doesn't have into it, keep a timestamped .stub-bak of the
        original beside the mount (preserve-the-log), then relink. On key
        CONFLICTS the shared value wins — the stub was never intentionally
        maintained; it's a snapshot of whatever `claude /login` seeded.
      - .credentials.json not 0600 → chmod
    """
    findings: list[dict] = []

    def note(entry: str, problem: str, action: str) -> None:
        findings.append({"entry": entry, "problem": problem, "action": action})

    for sub in SHARED_SYMLINK_SUBDIRS:
        link = mount / sub
        target = CLAUDE_DIR / sub
        if not target.exists():
            continue
        if link.is_symlink():
            if link.resolve() != target.resolve():
                old_target = os.readlink(link)
                if fix:
                    link.unlink()
                    link.symlink_to(target)
                note(sub, f"symlink → {old_target}", f"repoint{'ed' if fix else ''} → {target}")
            continue
        if link.is_dir():
            moved, collisions = 0, 0
            if fix:
                for child in list(link.iterdir()):
                    dst = target / child.name
                    if dst.exists():
                        collisions += 1
                        continue
                    shutil.move(str(child), str(dst))
                    moved += 1
                if not any(link.iterdir()):
                    link.rmdir()
                    link.symlink_to(target)
                    note(sub, "real dir where symlink expected", f"moved {moved} entries into shared, relinked")
                else:
                    note(sub, "real dir where symlink expected", f"moved {moved}, {collisions} collisions remain — left as real dir, resolve manually")
            else:
                note(sub, "real dir where symlink expected", "would merge into shared target and relink")
            continue
        if not link.exists():
            if fix:
                link.symlink_to(target)
            note(sub, "missing symlink", f"link{'ed' if fix else ''} → {target}")

    shared_settings_changed = False
    for fname in SHARED_SYMLINK_FILES:
        link = mount / fname
        target = CLAUDE_DIR / fname
        if not target.exists():
            continue
        if link.is_symlink():
            if link.resolve() != target.resolve():
                if fix:
                    link.unlink()
                    link.symlink_to(target)
                note(fname, "symlink to wrong target", f"repoint{'ed' if fix else ''} → {target}")
            continue
        if link.is_file():
            if fix:
                try:
                    stub = read_json(link)
                    shared = read_json(target)
                except (json.JSONDecodeError, OSError) as e:
                    note(fname, f"real file, unparseable ({e})", "left in place — resolve manually")
                    continue
                folded = [k for k in stub if k not in shared]
                conflicts = [k for k in stub if k in shared and shared[k] != stub[k]]
                if folded:
                    shared.update({k: stub[k] for k in folded})
                    write_json(target, shared)
                    shared_settings_changed = True
                ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
                link.rename(link.with_name(f"{fname}.stub-bak.{ts}"))
                link.symlink_to(target)
                detail = f"folded {folded or 'nothing'} into shared"
                if conflicts:
                    detail += f"; kept shared values for {conflicts}"
                note(fname, "real file where symlink expected", f"{detail}; stub kept as {fname}.stub-bak.{ts}; relinked")
            else:
                note(fname, "real file where symlink expected", "would fold novel keys into shared and relink")
            continue
        if not link.exists():
            if fix:
                link.symlink_to(target)
            note(fname, "missing symlink", f"link{'ed' if fix else ''} → {target}")

    creds = mount_creds_path(mount)
    if creds.exists() and not creds.is_symlink():
        mode = creds.stat().st_mode & 0o777
        if mode != 0o600:
            if fix:
                os.chmod(creds, 0o600)
            note(".credentials.json", f"mode {oct(mode)}", f"chmod{'ed' if fix else ''} 0600")

    cj = mount_claude_json_path(mount)
    if cj.exists():
        try:
            read_json(cj)
        except (json.JSONDecodeError, OSError) as e:
            note(".claude.json", f"unparseable: {e}", "left in place — resolve manually (swap/sync would fail against it)")

    if shared_settings_changed:
        note("(shared settings.json)", "gained folded keys from a stub", "review ~/.claude/settings.json")
    return findings


def account_in_backoff(state: dict, account_name: str) -> tuple[bool, str | None]:
    """Return (in_backoff, until_iso) for an account's poll-throttle backoff.

    Anthropic per-IP-throttles `/api/oauth/usage`. Once an account triggers
    429, we exponentially back off subsequent polls for THAT account to
    avoid hammering the endpoint and prolonging the throttle. Reset on
    next successful poll.
    """
    acct = state.get("accounts", {}).get(account_name, {})
    until = acct.get("poll_backoff_until_ts")
    if not until:
        return False, None
    try:
        until_dt = datetime.fromisoformat(until.replace("Z", "+00:00"))
        if datetime.now(timezone.utc) < until_dt:
            return True, until
    except ValueError:
        pass
    return False, until


def update_backoff(acct: dict, success: bool, config: dict) -> None:
    """Update poll-backoff state. On success: reset. On 429: exponential.

    Backoff doubles each consecutive 429: base * 2^(n-1), capped at max.
    With base=300, max=600 (defaults 2026-05-20, GH #9): 5min → 10min (cap).
    Previously the cap defaulted to 1800s (30min); that wedged accounts for
    hours when another machine kept hitting the same per-IP throttle on
    `/api/oauth/usage`. 10min cap means we recover observability within ~15
    min of the throttle clearing rather than ~hours. Users wanting the old
    behavior can set `polling.max_backoff_seconds: 1800` in config.yaml.
    """
    polling_cfg = config.get("polling", {})
    base = polling_cfg.get("base_backoff_seconds", 300)
    cap = polling_cfg.get("max_backoff_seconds", 600)
    if success:
        acct.pop("poll_backoff_until_ts", None)
        acct.pop("poll_backoff_consecutive_429s", None)
        return
    n = acct.get("poll_backoff_consecutive_429s", 0) + 1
    delay = min(base * (2 ** (n - 1)), cap)
    until = datetime.now(timezone.utc) + timedelta(seconds=delay)
    acct["poll_backoff_consecutive_429s"] = n
    acct["poll_backoff_until_ts"] = until.isoformat().replace("+00:00", "Z")


def _account_poll_interval(state: dict, config: dict, account_name: str) -> float:
    """Resolve the poll cadence (seconds) for one account under the differential
    poll schedule (2026-07-02).

    The ACTIVE account is polled on the fast `polling.active_interval_seconds`
    cadence — it's the only account actually burning usage, so we want to catch
    its ramp toward the swap threshold before it overshoots. INACTIVE accounts
    are polled on the slow `polling.inactive_interval_seconds` cadence — they're
    idle, needed only for swap-target selection, so a stale-ish reading is fine.

    Both fall back to the legacy flat `poll_interval_seconds` when unset, so an
    unmodified config keeps polling every account every cycle (backward-compat).

    Why this exists: flat fast polling of all N accounts sends N requests per
    interval to the per-IP-throttled /api/oauth/usage endpoint. On 2026-07-02,
    flat 60s x 5 accounts dropped every account into 429 poll-backoff (cus went
    blind). Fast-active / slow-inactive cuts the steady-state request rate to
    ~1 per active-interval plus the occasional inactive refresh, keeping us well
    under the throttle while polling the account that matters MORE often, not
    less. See GH #84 for the burn-rate-adaptive evolution of the active cadence.
    """
    polling_cfg = config.get("polling", {})
    flat = config.get("poll_interval_seconds", 300)
    if _account_on_fast_cadence(state, config, account_name):
        return polling_cfg.get("active_interval_seconds") or flat
    return polling_cfg.get("inactive_interval_seconds") or flat


def _account_on_fast_cadence(state: dict, config: dict, account_name: str) -> bool:
    """Is this account burning usage right now (→ fast poll cadence)?

    global mode: exactly the active account — unchanged behavior.
    per_session mode (Phase 3.3): accounts holding LIVE-occupied slots — each
    is genuinely burning. The global active also stays fast while any bare
    session holds ~/.claude/ (observe-only ≠ poll-blind). Mind the 429
    budget: N occupied accounts means ~N× the fast-cadence traffic of global
    mode — same trap class as the 2026-06-19 burnout — so per_session
    rollouts should start with active_interval_seconds relaxed (e.g. 300s)
    and tighten only after a week of clean 429 logs (the differential
    due-gate composes per account; it does not multiply bursts thanks to
    polling.stagger_seconds).
    """
    # per_session AND hybrid both manage live slots, so an account holding a
    # live-occupied slot is burning and polls fast in either. hybrid also swaps
    # the shared mount, so its active stays fast too (the fallback below).
    if config.get("mode", "global") not in ("per_session", "hybrid"):
        return state.get("active") == account_name
    if account_name in occupied_slot_accounts(state):
        return True
    return state.get("active") == account_name and mount_in_use(CLAUDE_DIR)


def _account_poll_due(state: dict, config: dict, account_name: str) -> tuple[bool, str]:
    """Whether `account_name` is due for a poll this cycle under differential
    cadence. Returns (due, human-readable reason).

    Due when the account has never been polled, its stored timestamp is
    unparseable (fail-open — poll rather than starve observability), its 5h
    window has rolled over since the last poll (so the stored % is the stale
    PRE-reset value), or at least its class interval (`_account_poll_interval`)
    has elapsed since last_poll_ts.

    The 5h-rollover override exists to compose with GH #59's adaptive repoll
    (review amendment 2026-07-02): `_adaptive_sleep_seconds` deliberately
    SHORTENS the daemon's inter-cycle sleep so it wakes just after the soonest
    known 5h reset and repolls promptly. Without this override, that early wake
    would find the reset account "not due" (elapsed < its class interval —
    especially an INACTIVE account on the slow cadence) and skip the poll,
    leaving the daemon running on the countdown-INFERRED 0% for up to a full
    inactive interval and defeating the adaptive repoll entirely.
    """
    interval = _account_poll_interval(state, config, account_name)
    # Preserve the un-shortened class cadence: Fix B's "will it cross the step by
    # the next poll" projection horizon must be the NORMAL interval, not one
    # already shrunk by the token_stale fast-track below.
    normal_interval = interval
    acct = state.get("accounts", {}).get(account_name, {})
    last = acct.get("last_poll_ts")
    if not last:
        return True, "no prior poll"
    try:
        last_dt = datetime.fromisoformat(last.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return True, "unparseable last_poll_ts"
    role = "active" if _account_on_fast_cadence(state, config, account_name) else "inactive"
    # token_stale fast-track (2026-07-05 incident): a token_stale account
    # (stored access token aged out, refresh token still valid) must be
    # re-polled PROMPTLY, not left sitting on a stale cached % for hours.
    # poll_account_usage()'s self-refresh preflight (_refresh_account_token)
    # can only mint a fresh token and re-fetch REAL usage when the account is
    # actually polled — but Branch 0 restamps last_poll_ts every stale cycle,
    # so an INACTIVE spare then waits the full slow inactive_interval before
    # the next attempt, leaving its cached value looking authoritative. Force
    # such accounts onto the FAST (active) cadence so a valid refresh token
    # clears token_stale within one active interval instead of a whole slow
    # cycle. Graceful-degrade preserved: a genuinely dead refresh token stays
    # token_stale and simply retries at the fast (NOT every-cycle) cadence — no
    # crash, no busy-loop, and the 429 poll-backoff gate still runs first in
    # the daemon loop. Only ever SHORTENS the interval (min()), so a stale
    # account already on the fast cadence is unaffected.
    if acct.get("token_stale"):
        polling_cfg = config.get("polling", {})
        flat = config.get("poll_interval_seconds", 300)
        fast_interval = polling_cfg.get("active_interval_seconds") or flat
        if fast_interval < interval:
            interval = fast_interval
            role = f"{role}/token_stale-fast"
    # Fix B — near-threshold acceleration (2026-07-06 rayi3 incident). An account
    # whose burn-extrapolated usage is within poll_accel.within_pct_of_step of its
    # next swap step (or projected to cross it before the next NORMAL poll) is
    # polled on the fast `fast_interval_seconds` cadence, so the daemon sees a fast
    # burn toward the cap in time to swap BEFORE it. `normal_interval` (the class
    # cadence computed above, before any token_stale shortening) is the projection
    # horizon for the "will it cross by next poll" test. Bounded by design: only
    # accounts actually near a step accelerate — the fleet is NOT blanket-fast-
    # polled (that is the 2026-06-19 429-burnout trap). The 429 poll-backoff gate
    # still wins: an account in reactive backoff is skipped in the daemon loop
    # BEFORE this function, and we re-check `account_in_backoff` here so the
    # status-preview / any other caller never fast-polls a throttled account.
    # Only ever SHORTENS the interval (min via the `<` guard), never lengthens it.
    if _poll_accel_enabled(config):
        in_backoff, _ = account_in_backoff(state, account_name)
        # Lane count feeds the clustered-account band widening (2026-07-06): an
        # account backing several live lanes must fast-poll EARLIER because its
        # compounded burn outruns the single-lane burn_rate the estimator measured.
        lane_count = len(occupied_slot_accounts(state).get(account_name, []))
        if not in_backoff and _account_near_step(acct, config, normal_interval,
                                                 lane_count=lane_count):
            fast = float(config.get("poll_accel", {}).get("fast_interval_seconds", 45))
            if fast < interval:
                interval = fast
                role = f"{role}/near-step-fast"
    # GH #59 composition: a 5h window that reset AFTER our last poll means the
    # stored usage is pre-reset garbage — poll now regardless of cadence class,
    # so the adaptive early wake actually refreshes the account it woke for.
    if _five_hour_rolled_since_poll(acct):
        return True, f"5h window reset since last poll ({role}; GH #59 adaptive repoll)"
    elapsed = (datetime.now(timezone.utc) - last_dt).total_seconds()
    if elapsed >= interval:
        return True, f"due ({role}: {elapsed:.0f}s >= {interval:.0f}s)"
    return False, f"{role}: {elapsed:.0f}s < {interval:.0f}s"


def _read_access_token_with_expiry(account_name: str) -> tuple[str | None, int | None]:
    """Like _read_access_token but also returns the stored expiresAt timestamp.

    expiresAt is the Unix milliseconds at which the stored access token
    expires (per the OAuth flow). Returns (None, None) if no file can be
    read. The expiresAt is used to detect "token stale, not actually expired"
    (GH #13) — when an inactive account's stored access token has aged out
    but its refresh token is still valid, polling will 401 even though the
    account is fully usable.

    Credential source preference (2026-07-03 token-stale blindness incident,
    GH #130). We poll with the FRESHEST credential we can find for this
    account, in order:
      1. the live shared mount (CREDS_JSON) when this account is the
         shared-active one — Claude Code refreshes it transparently on use;
      2. any SLOT that backs this account whose dir exists — a live LANE's
         own `.credentials.json` is kept fresh by the running Claude Code
         session on top of it, so it is the best token to poll with and it
         exists precisely when the primary account store is most likely to be
         stale (the exact condition that blinded `rayi2` for 3.7h). Live slots
         (a session actually running, `mount_in_use`) are preferred over idle
         ones; among live slots order is arbitrary (any live token is fresh);
      3. the account's primary store (`account_creds_path`) — the historical
         fallback.
    We return the FIRST candidate whose access token is not already past its
    expiresAt (same 30s-grace staleness convention the caller in
    poll_account_usage applies). If EVERY candidate is stale, we return the
    primary store's pair exactly as before so `token_stale` detection still
    fires downstream (and, if the primary is missing, the first stale pair we
    did find, so an active account still reads as token_stale rather than
    "no access_token").

    READ-ONLY by contract: this NEVER writes, refreshes, or rotates a token.
    OAuth refresh tokens are single-use — refreshing the wrong file here would
    log a live session out (GH #104). We only CHOOSE which existing file to
    read; the return contract stays (accessToken, expiresAt).
    """
    try:
        state = load_state() if STATE_JSON.exists() else {}
    except Exception:
        state = {}

    def _read_pair(path: Path) -> tuple[str | None, int | None]:
        """(accessToken, expiresAt) from a creds file, or (None, None).

        Missing/corrupt files are swallowed silently (same try/except style as
        the historical single-path read) so one bad slot creds file never
        blocks the daemon from finding a good one — the whole point of having
        multiple candidates. GH #130 failure mode: a slot whose
        `.credentials.json` is absent or half-written must simply be skipped.
        """
        try:
            if not path.exists():
                return None, None
            creds = read_json(path)
            oauth = creds.get("claudeAiOauth", {})
            return oauth.get("accessToken"), oauth.get("expiresAt")
        except (json.JSONDecodeError, OSError):
            return None, None

    def _is_fresh(expires_at) -> bool:
        """Mirror poll_account_usage's staleness test: fresh iff expiresAt is
        not more than 30s in the past. An unknown/unparseable expiresAt counts
        as fresh — we can't prove it stale, and the live HTTP call will still
        surface a real 401 as token_expired if the token is actually dead."""
        if expires_at is None:
            return True
        try:
            now_ms = int(time.time() * 1000)
            return int(expires_at) >= now_ms - 30_000
        except (TypeError, ValueError):
            return True

    # Assemble candidate creds paths in preference order (see docstring).
    candidates: list[Path] = []
    # (1) live shared mount, only when this account is the shared-active one.
    if state.get("active") == account_name and CREDS_JSON.exists():
        candidates.append(CREDS_JSON)
    # (2) slots backing this account; live slots first, idle slots after. A
    # live slot's creds are refreshed by the session running on it, so they
    # beat both an idle slot and (usually) the primary store for freshness.
    live_slot_creds: list[Path] = []
    idle_slot_creds: list[Path] = []
    for slot_name, entry in state.get("slots", {}).items():
        if (entry or {}).get("account") != account_name:
            continue
        d = slot_path(slot_name)
        if not d.exists():
            continue
        cred = d / ".credentials.json"
        if not cred.exists():
            continue
        try:
            # mount_in_use walks /proc; occupied_slot_accounts caches it for 5s
            # per cycle, but a direct call here is cheap enough (one slot) and
            # a few seconds of staleness only picks idle-vs-live ordering, not
            # correctness — both get tried.
            (live_slot_creds if mount_in_use(d) else idle_slot_creds).append(cred)
        except Exception:
            idle_slot_creds.append(cred)
    candidates.extend(live_slot_creds)
    candidates.extend(idle_slot_creds)
    # (3) primary account store — historical fallback, always last.
    primary = account_creds_path(account_name)
    candidates.append(primary)

    # First non-stale candidate wins. Track the primary's pair (task contract:
    # all-stale returns the primary) and the first present-but-stale pair (so a
    # missing primary still yields token_stale rather than a false "no token").
    primary_pair: tuple[str | None, int | None] = (None, None)
    first_stale_pair: tuple[str | None, int | None] = (None, None)
    for path in candidates:
        token, expires_at = _read_pair(path)
        if path == primary:
            primary_pair = (token, expires_at)
        if token is None:
            continue
        if _is_fresh(expires_at):
            return token, expires_at
        if first_stale_pair[0] is None:
            first_stale_pair = (token, expires_at)
    return primary_pair if primary_pair[0] is not None else first_stale_pair


def _refresh_account_token(account_name: str, creds_path: Path | None = None) -> bool:
    """Best-effort OAuth refresh_token grant for an inactive account whose
    stored access token has aged out (the token_stale condition, GH #13).

    Talks to the SAME undocumented endpoint the `claude` CLI itself uses to
    silently refresh a live account's token every ~hour — extracted
    2026-07-02 via `strings` on the installed @anthropic-ai/claude-code
    binary's own OAuth client config (OAUTH_TOKEN_URL / OAUTH_CLIENT_ID
    above). It is NOT a published API. Costs no usage quota (a pure
    token-mint call, not a `/v1/messages` inference call) and creates no
    session — unlike spawning a real `claude` probe session, which was
    considered and rejected for exactly those two reasons.

    Returns True only on a CONFIRMED successful refresh (new access token
    written back to the account's stored `.credentials.json`). Returns False
    on ANY failure — disabled via `token_self_refresh.enabled`, missing or
    unreadable creds, missing refresh token, network error, non-200, or a
    malformed response. Callers MUST treat False as "fall back to the
    pre-existing token_stale behavior", never as an error to surface —
    Anthropic can change the client_id/URL/response shape without notice,
    so silent, total fallback on any surprise is the only safe posture.

    2026-07-07 (blank-mount preempt, Fix 1): `creds_path` optionally targets a
    SPECIFIC credentials file instead of the account snapshot. The default
    (None → the account's snapshot at `account_creds_path`) is the pre-existing
    behavior every prior caller relies on. The preempt path passes a live LANE
    mount's own `.credentials.json` so the fresh access token lands directly in
    the file the running session reads — exactly what Claude Code's own in-place
    refresh does, but driven reliably by cus ahead of expiry. Using the mount's
    OWN refresh token (read from that file) and writing back only to that file
    keeps it family-safe (#104): no other mount/store is touched, and the daemon's
    existing save-back propagates the fresh token to the canonical store on the
    next cycle, just as it does after a Claude Code refresh.
    """
    if not load_config().get("token_self_refresh", {}).get("enabled", True):
        return False

    if creds_path is None:
        creds_path = account_creds_path(account_name)
    if not creds_path.exists():
        return False
    try:
        creds = read_json(creds_path)
    except (OSError, json.JSONDecodeError):
        return False
    oauth = creds.get("claudeAiOauth")
    if not isinstance(oauth, dict):
        return False
    refresh_token = oauth.get("refreshToken")
    if not refresh_token:
        return False

    body = json.dumps({
        "grant_type": "refresh_token",
        "refresh_token": refresh_token,
        "client_id": OAUTH_CLIENT_ID,
        "scope": " ".join(oauth.get("scopes") or []),
    }).encode()
    req = urllib.request.Request(
        OAUTH_TOKEN_URL,
        data=body,
        headers={
            "Content-Type": "application/json",
            # Must use OAUTH_USER_AGENT, not a project-branded string: #127
            # verified 2026-07-03 that platform.claude.com sits behind Cloudflare,
            # which 403s (code 1010) urllib's default UA. The #127 claim-time
            # probe hits this same endpoint with this same header; reuse it so
            # both callers present the one User-Agent known to pass the WAF.
            "User-Agent": OAUTH_USER_AGENT,
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=OAUTH_REFRESH_TIMEOUT_SECONDS) as resp:
            data = json.loads(resp.read().decode())
    # OSError is the umbrella for socket/SSL/connection failures that aren't
    # always wrapped in URLError (e.g. ssl.SSLError, ConnectionResetError);
    # catching it keeps the fail-silent contract total, per the docstring —
    # any surprise from this undocumented endpoint must degrade to the
    # pre-existing token_stale flag, never crash the poll.
    except (urllib.error.HTTPError, urllib.error.URLError, OSError, TimeoutError,
            json.JSONDecodeError, UnicodeDecodeError):
        return False

    access_token = data.get("access_token")
    expires_in = data.get("expires_in")
    if not isinstance(access_token, str) or not access_token or not isinstance(expires_in, (int, float)):
        return False

    oauth["accessToken"] = access_token
    oauth["refreshToken"] = data.get("refresh_token") or refresh_token
    oauth["expiresAt"] = int(time.time() * 1000) + int(expires_in * 1000)
    scope = data.get("scope")
    if isinstance(scope, str) and scope:
        oauth["scopes"] = scope.split(" ")
    creds["claudeAiOauth"] = oauth

    try:
        backup_credentials_file(creds_path)
        write_json(creds_path, creds, mode=0o600)
    except OSError:
        return False
    return True


def _unstale_account_snapshot(account: str, state: dict, config: dict | None = None,
                              *, force: bool = False) -> bool:
    """Un-stale an idle `token_stale` account by minting a fresh access token via a
    DIRECT OAuth refresh grant and writing it back to the account SNAPSHOT (Fix 4,
    2026-07-07). Clears the `token_stale` flag on `state.accounts[account]` (caller
    persists). Returns True only on a confirmed refresh+writeback.

    The bug it fixes (verified live: `cus force-poll merkos` left token_stale set):
    an idle account whose stored access token expired — nothing exercises it — sits
    `token_stale` forever even though its REFRESH token is still valid. That yields
    (a) chronically wrong cus readings and (b) "stale snapshot → blank on install"
    when a lane swaps onto it (the chats1a-on-merkos blank). The poll-time
    `_refresh_account_token` is gated by `token_self_refresh.enabled` and folded
    into a poll, so it may never clear a stale flag; this is the explicit,
    ungated-by-that-flag un-stale.

    Uses `_oauth_refresh_grant` (#127) — a pure token-mint call: no session, no
    haiku, no billed tokens. Recoverable case only (a valid stored refresh token):
      * grant `alive`   → persist the rotated tokens into the snapshot (the old
                          refresh token is dead the instant the grant succeeds),
                          clear token_stale, audit `decision=refreshed`, return True.
      * grant `dead`    → invalid_grant: the refresh token genuinely needs a
                          browser relogin. Leave token_stale flagged, audit
                          `decision=failed`, return False — and DON'T retry tightly.
      * grant `unknown` → network/endpoint drift: fail-open, leave flagged, retry
                          later (bounded by the cooldown).

    Poll-burnout backoff: at most one attempt per account per
    `token_self_refresh.unstale_cooldown_minutes`, unless `force` (force-poll is
    operator-invoked, so it bypasses the cooldown). Never raises into the caller."""
    cfg = config if config is not None else load_config()
    acct = state.get("accounts", {}).get(account) if isinstance(state, dict) else None
    if not isinstance(acct, dict):
        return False
    # Only the recoverable idle case: stale but NOT hard-expired (a real 401 on a
    # non-expired stored token is token_expired — that needs a browser relogin, not
    # a refresh grant).
    if not acct.get("token_stale") or acct.get("token_expired"):
        return False
    # Poll-burnout backoff (per account, success OR fail), bypassed by force.
    now = time.time()
    cooldown_min = cfg.get("token_self_refresh", {}).get("unstale_cooldown_minutes", 10)
    if not force:
        last = _UNSTALE_ATTEMPT_MS.get(account)
        if last is not None and (now - last) < cooldown_min * 60:
            return False
    _UNSTALE_ATTEMPT_MS[account] = now

    snap = account_creds_path(account)
    if not snap.exists():
        _cred_audit("unstale-refresh", "failed", "no account snapshot on disk",
                    account=account, token_fp="none")
        return False
    try:
        creds = read_json(snap)  # dict — narrowed so the writeback below is type-clean
    except (json.JSONDecodeError, OSError):
        return False
    rt = _credential_refresh_token(creds)
    if not rt:
        # No usable refresh token in the snapshot → genuinely needs a relogin.
        _cred_audit("unstale-refresh", "failed", "no usable refresh token in account snapshot",
                    account=account, token_fp="none")
        return False

    verdict, tok = _oauth_refresh_grant(rt)
    if verdict != "alive" or not isinstance(tok, dict):
        # A definitive invalid_grant is the "dead snapshot" signal (2026-07-07 merkos
        # incident): flag it on the account so the swap-target picker excludes it and
        # the SOS surfaces it (no extra network call — this probe already ran). An
        # "unknown" (network/endpoint) verdict is NOT recorded as dead — that would be
        # a transient masquerading as a death certificate.
        if verdict == "dead":
            acct["snapshot_refresh_dead"] = True
        _cred_audit("unstale-refresh", "failed",
                    f"refresh grant {verdict} (dead ⇒ browser relogin; unknown ⇒ retry later)",
                    account=account, token_fp=_audit_token_fp(creds))
        return False

    # Persist the rotation BEFORE clearing the flag — after a successful grant the
    # snapshot's OLD refresh token is a dead branch (single-use rotation, #104), so
    # the fresh pair must land on disk or we'd lose the family. Whole assembly is
    # guarded so a malformed grant response can never crash the caller.
    try:
        access = tok.get("access_token")
        if not isinstance(access, str) or not access:
            _cred_audit("unstale-refresh", "failed", "grant returned no access_token",
                        account=account, token_fp=_audit_token_fp(creds))
            return False
        oauth = dict(creds.get("claudeAiOauth") or {})
        oauth["accessToken"] = access
        oauth["refreshToken"] = tok.get("refresh_token") or rt
        expires_in = tok.get("expires_in")
        if isinstance(expires_in, (int, float)) and not isinstance(expires_in, bool):
            oauth["expiresAt"] = int((time.time() + float(expires_in)) * 1000)
        new_creds = dict(creds)
        new_creds["claudeAiOauth"] = oauth
        backup_credentials_file(snap)
        write_json(snap, new_creds, mode=0o600)
    except (OSError, TypeError, ValueError):
        return False

    # Clear the stale flag in state (caller saves). token_expired stays cleared too
    # — a working refresh grant proves the account is reachable. Clear any prior
    # dead-snapshot flag too: the grant just proved the snapshot's refresh token is
    # ALIVE (e.g. after a relogin restored it), so it must no longer be excluded.
    acct.pop("token_stale", None)
    acct.pop("snapshot_refresh_dead", None)
    acct["token_expired"] = False
    _cred_audit("unstale-refresh", "refreshed",
                "minted fresh access token via direct OAuth grant; cleared token_stale",
                account=account, token_fp=_audit_token_fp(new_creds),
                extra=f"new_expiry={_expiry_repr(new_creds)}")
    return True


def _sweep_unstale_idle_accounts(state: dict, config: dict) -> list[str]:
    """Each daemon cycle, un-stale every account still flagged token_stale (Fix 4).

    Cheap self-healing pass over the accounts the poll just left token_stale (and
    not token_expired): a direct refresh grant per account, cooldown-bounded so it
    can't burn out, snapshot-writeback + flag-clear on success. Gated by
    `token_self_refresh.unstale_idle_on_poll` (default True). Returns the list of
    un-staled account names. Even if the caller never persists the cleared flag,
    the snapshot writeback is durable — next cycle's poll reads the fresh token and
    clears token_stale via the normal success path — so this can only help."""
    if not config.get("token_self_refresh", {}).get("unstale_idle_on_poll", True):
        return []
    out: list[str] = []
    for name, acct in list(state.get("accounts", {}).items()):
        if not isinstance(acct, dict):
            continue
        if acct.get("token_stale") and not acct.get("token_expired"):
            if _unstale_account_snapshot(name, state, config):
                out.append(name)
    return out


# --------------------------------------------------------------------------
# clock_keepalive (2026-07-07 operator directive) — keep every account's 5h
# usage window ticking. See DEFAULT_CONFIG["clock_keepalive"] for the full WHY.
# --------------------------------------------------------------------------

def _5h_clock_dormant(acct: dict, now_dt: datetime) -> bool:
    """Is this account's 5-hour usage window NOT currently running?

    Claude's 5h window is a FIXED window: it starts on the account's first model
    request after a dormant period and resets ~5h later. While it runs, the poll
    stores its end as `five_hour_resets_at` (an ISO timestamp in the FUTURE). So
    the window is:
      * RUNNING (not dormant) ⇔ `five_hour_resets_at` parses and is in the future
        — something (a live lane, or a recent keepalive ping) started the clock
        and it is still ticking down.
      * DORMANT ⇔ there is no future reset: the field is absent, unparseable, or
        already in the past (the window lapsed and nothing restarted it).

    A dormant clock is the state clock_keepalive exists to break — it will not
    reset on a predictable cadence, so no "fresh in ~5h" capacity is coming from
    this account until something pings it. This is a pure predicate (no I/O) so
    it stays unit-testable apart from the sweep's creds/HTTP side effects."""
    reset_at = acct.get("five_hour_resets_at")
    if reset_at:
        try:
            reset_dt = datetime.fromisoformat(str(reset_at).replace("Z", "+00:00"))
            if reset_dt > now_dt:
                return False  # window still ticking → NOT dormant
        except (TypeError, ValueError):
            pass  # unparseable ⇒ treat as no known running window ⇒ dormant
    return True


def _keepalive_token_for(account: str, acct: dict, state: dict,
                         config: dict) -> tuple[str | None, str]:
    """Resolve a VALID OAuth access token to send the keepalive ping with, or
    (None, reason) to skip. NEVER pings with dead creds.

    Two cases:
      * canonical snapshot is refresh-DEAD (`snapshot_refresh_dead`, from the
        un-stale sweep / seed-from-family PR): the account's own snapshot token
        can't be trusted, so only ping if a VALID login family is available.
        `claim_verified_login_family` proves a free family's refresh token alive
        (rotating+persisting it) and we read that family's fresh access token.
        No free/verifiable family ⇒ skip and leave the account for relogin.
      * normal account: use the freshest snapshot/live token
        (`_read_access_token_with_expiry`); if it has aged out, mint a fresh one
        via `_refresh_account_token` (a direct refresh grant that writes back to
        the SNAPSHOT only — never a live lane's .credentials.json) and re-read."""
    if acct.get("snapshot_refresh_dead"):
        if not has_free_login_family(account, state):
            return None, "snapshot refresh-dead and no free login family — leave for relogin"
        fam = claim_verified_login_family(account, state, config)
        if not fam:
            return None, "snapshot refresh-dead; no verified-alive login family available"
        try:
            fam_creds = read_json(login_family_creds_path(account, fam))
            tok = (fam_creds.get("claudeAiOauth") or {}).get("accessToken")
        except (OSError, json.JSONDecodeError):
            tok = None
        if not tok:
            return None, f"login family {fam} yielded no usable access token"
        return tok, f"using verified login family {fam} (snapshot refresh-dead)"

    token, expires_at = _read_access_token_with_expiry(account)
    now_ms = int(time.time() * 1000)
    fresh = bool(token) and (expires_at is None or _int_or_none(expires_at, now_ms) >= now_ms - 30_000)
    if not fresh:
        # Aged out — mint a fresh access token via a direct refresh grant
        # (writes back to the snapshot only) then re-read. Best-effort: on
        # failure we fall through and skip below rather than ping a stale token.
        if _refresh_account_token(account):
            token, expires_at = _read_access_token_with_expiry(account)
    if not token:
        return None, "no usable access token (and refresh grant did not yield one)"
    return token, "using account snapshot/live token"


def _int_or_none(val, default: int) -> int:
    """Best-effort int() for a stored expiresAt; `default` when unparseable so a
    malformed timestamp is treated as fresh (the live HTTP call still surfaces a
    real auth failure)."""
    try:
        return int(val)
    except (TypeError, ValueError):
        return default


def _keepalive_ping(access_token: str, model: str) -> tuple[bool, str]:
    """Send the MINIMAL keepalive model request that starts/keeps an account's
    5h usage window ticking. Returns (ok, detail); NEVER raises.

    The request is the smallest thing that still counts toward the window: one
    user token "hi", `max_tokens=1`, the cheapest model. It is authenticated
    with the account's OAuth (subscription) bearer token exactly as `claude -p`
    is — hence the Claude Code system-prompt marker + the oauth beta header,
    which the endpoint requires before it accepts a subscription token on
    /v1/messages. This is the one seam tests mock — no real network in tests.

    A 429 is reported as SUCCESS-for-our-purposes: an account Anthropic is
    actively rate-limiting necessarily has a running 5h window, which is exactly
    the ticking clock we wanted — nothing to do but record it and move on."""
    body = json.dumps({
        "model": model,
        "max_tokens": 1,
        "system": KEEPALIVE_SYSTEM_PROMPT,
        "messages": [{"role": "user", "content": "hi"}],
    }).encode()
    req = urllib.request.Request(
        MESSAGES_API_URL, data=body,
        headers={
            "Authorization": f"Bearer {access_token}",
            "anthropic-version": ANTHROPIC_VERSION,
            "anthropic-beta": USAGE_API_BETA,
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": OAUTH_USER_AGENT,
        }, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=KEEPALIVE_TIMEOUT_SECONDS) as resp:
            resp.read(USAGE_API_RESPONSE_LIMIT_BYTES)  # drain; body is irrelevant
            return True, f"HTTP {getattr(resp, 'status', 200)}"
    except urllib.error.HTTPError as e:
        if e.code == 429:
            return True, "HTTP 429 (already rate-limited; window is ticking)"
        detail = ""
        try:
            detail = e.read(512).decode(errors="replace")[:200] if e.fp else ""
        except OSError:
            pass
        return False, f"HTTP {e.code}: {detail}"
    except (urllib.error.URLError, TimeoutError, OSError, ValueError) as e:
        return False, f"{type(e).__name__}: {e}"


def _sweep_clock_keepalive(state: dict, config: dict) -> list[str]:
    """Each daemon cycle, keep every DORMANT account's 5h clock ticking (Fix,
    2026-07-07 operator directive). Returns the names actually pinged this cycle.

    For each ENABLED, non-`disabled`, in-scope account whose 5h window is DORMANT
    (`_5h_clock_dormant`) and that is not being kept alive by a live lane, send a
    single cheap keepalive ping (`_keepalive_ping`) — cooldown-bounded by
    `clock_keepalive.min_ping_interval_minutes` (recorded as `last_keepalive_ping_ts`
    per account) and skipping accounts in poll-backoff or needing relogin. On a
    successful ping, force a usage re-poll of that account so `five_hour_resets_at`
    reflects the now-ticking window. Every step is wrapped so a single account's
    failure only logs (CRED-AUDIT) and moves on — never crashes the poll.

    Routine non-candidates (out of scope, disabled, live lane, still-ticking) are
    skipped SILENTLY to avoid flooding the log every cycle; only dormant accounts
    we deliberately decline to ping (cooldown, backoff, dead creds, relogin) or
    actually ping/fail get a CRED-AUDIT line."""
    kcfg = config.get("clock_keepalive", {})
    if not kcfg.get("enabled", True):
        return []
    interval_min = kcfg.get("min_ping_interval_minutes", 290)
    model = kcfg.get("model", "claude-haiku-4-5-20251001")
    allow = set(kcfg.get("accounts") or [])
    deny = set(kcfg.get("exclude") or [])
    now_dt = datetime.now(timezone.utc)
    pinged: list[str] = []

    for name, acct in list(state.get("accounts", {}).items()):
        if not isinstance(acct, dict):
            continue
        # ── silent, routine non-candidates (would flood the log every cycle) ──
        if allow and name not in allow:
            continue
        if name in deny:
            continue
        if acct.get("disabled"):
            continue  # disabled accounts are out of rotation — never ping
        # A live lane actively making requests already keeps this account's clock
        # ticking; pinging it would be wasteful. `_account_on_fast_cadence` is the
        # existing "is this account burning right now" predicate (live slot, or the
        # shared-active mount in use).
        if _account_on_fast_cadence(state, config, name):
            continue
        if not _5h_clock_dormant(acct, now_dt):
            continue  # window still ticking — nothing to keep alive

        # ── dormant candidate: from here a decline/ping is worth one audit line ──
        in_backoff, until = account_in_backoff(state, name)
        if in_backoff:
            _cred_audit("clock-keepalive-ping", "skipped",
                        f"in poll-backoff until {until}", account=name)
            continue
        if acct.get("token_expired"):
            _cred_audit("clock-keepalive-ping", "skipped",
                        "token_expired — needs browser relogin", account=name)
            continue
        # Cooldown: at most one ping per account per min_ping_interval_minutes.
        last_ping = acct.get("last_keepalive_ping_ts")
        if last_ping:
            try:
                last_dt = datetime.fromisoformat(str(last_ping).replace("Z", "+00:00"))
                if (now_dt - last_dt).total_seconds() < interval_min * 60:
                    _cred_audit("clock-keepalive-ping", "skipped",
                                f"within cooldown ({interval_min}m since last ping)",
                                account=name)
                    continue
            except (TypeError, ValueError):
                pass  # unparseable stamp ⇒ treat as no cooldown, proceed

        token, why = _keepalive_token_for(name, acct, state, config)
        if not token:
            _cred_audit("clock-keepalive-ping", "skipped", why, account=name)
            continue

        try:
            ok, detail = _keepalive_ping(token, model)
        except Exception as e:  # noqa: BLE001 — a ping must never crash the poll
            _cred_audit("clock-keepalive-ping", "failed",
                        f"unexpected {type(e).__name__}: {e}", account=name)
            continue

        if not ok:
            _cred_audit("clock-keepalive-ping", "failed", f"{why}; {detail}", account=name)
            continue

        # Success — stamp the cooldown, audit, then force a usage re-poll so
        # `five_hour_resets_at` reflects the now-ticking window (caller persists).
        acct["last_keepalive_ping_ts"] = now_iso()
        _cred_audit("clock-keepalive-ping", "pinged", f"{why}; {detail}", account=name)
        try:
            update_state_with_usage(state, {name: poll_account_usage(name)})
        except Exception as e:  # noqa: BLE001 — re-poll is a nicety, not required
            _cred_audit("clock-keepalive-ping", "pinged",
                        f"re-poll after ping failed ({type(e).__name__}); "
                        f"next cycle will refresh five_hour_resets_at", account=name)
        pinged.append(name)

    return pinged


def poll_account_usage(account_name: str) -> AccountUsage:
    """Query the Anthropic OAuth usage endpoint for one account.

    Returns parsed AccountUsage with one of these error states (priority
    order — only one is set):
      - token_stale: stored access token's expiresAt is in the past. Don't
        even attempt the HTTP call — we know we'd get 401 and the account
        is recoverable (refresh token still valid). NOT an SOS condition.
      - rate_limited: HTTP 429.
      - token_expired: HTTP 401 despite the stored access token being
        within its expiresAt window — real auth failure needing re-login.
      - poll_error: any other transport/parse failure.

    GH #13: previously ANY 401 was treated as token_expired. That produced
    false positives whenever an inactive account's stored snapshot aged
    out (>1 hour since the last swap-to-this-account), because we wrote
    the access token to storage at swap-time but only the LIVE creds file
    gets the periodic refresh.

    2026-07-02 (token_self_refresh): before falling back to token_stale, try
    a self-refresh via _refresh_account_token. On success this re-reads a
    fresh token/expiry and falls through to the normal HTTP poll below,
    exactly as if the account had never gone stale. On any failure it's
    unchanged from the pre-2026-07-02 behavior.
    """
    token, expires_at = _read_access_token_with_expiry(account_name)
    if not token:
        u = AccountUsage.empty()
        u.raw = {"error": f"no access_token for account {account_name}"}
        return u

    # Pre-flight: is the stored access token already past its expiry?
    # If so, don't bother making the HTTP request — flag token_stale and
    # let the caller treat this as a transient (non-SOS) condition. The
    # refresh token in the same creds file is still valid; the account
    # will be usable the moment we swap to it (claude code refreshes
    # transparently on first request).
    if expires_at is not None:
        try:
            now_ms = int(time.time() * 1000)
            # 30s grace — don't fire on tokens that are about to expire mid-flight
            if int(expires_at) < now_ms - 30_000:
                stale_expires_at = expires_at
                if _refresh_account_token(account_name):
                    token, expires_at = _read_access_token_with_expiry(account_name)
                if not token or expires_at is None or int(expires_at) < now_ms - 30_000:
                    u = AccountUsage.empty()
                    u.token_stale = True
                    u.raw = {
                        "error": "stored access token expired (refresh token still valid)",
                        "expired_minutes_ago": (now_ms - int(stale_expires_at)) // 60_000,
                    }
                    return u
        except (TypeError, ValueError):
            pass

    req = urllib.request.Request(
        USAGE_API_URL,
        headers={
            "Authorization": f"Bearer {token}",
            "anthropic-beta": USAGE_API_BETA,
            "Accept": "application/json",
            "User-Agent": "claude-usage-swap/0.1 (+https://github.com/rayistern/claude-usage-swap)",
        },
        method="GET",
    )

    try:
        with urllib.request.urlopen(req, timeout=USAGE_API_TIMEOUT_SECONDS) as resp:
            body = resp.read(USAGE_API_RESPONSE_LIMIT_BYTES + 1)
            if len(body) > USAGE_API_RESPONSE_LIMIT_BYTES:
                u = AccountUsage.empty()
                u.raw = {"error": "response too large"}
                return u
            data = json.loads(body.decode())
    except urllib.error.HTTPError as e:
        u = AccountUsage.empty()
        body_preview = e.read()[:200].decode(errors="replace") if e.fp else ""
        if e.code == 401:
            # We pre-checked expiresAt above and the token was within its
            # validity window — so a 401 HERE means refresh-token-level
            # failure (or Anthropic key rotation, or account revoked).
            # That's the real token_expired condition.
            u.token_expired = True
            u.raw = {"error": f"HTTP 401 despite non-expired stored token: {body_preview}"}
            return u
        elif e.code == 429:
            # Account is currently rate-limited by Anthropic. Flag it; do NOT
            # set five_hour/seven_day. update_state_with_usage will preserve
            # the prior current_*_pct values. The `rate_limited: True` flag
            # is the load-bearing signal — `pick_swap_target` filters by it.
            #
            # Previously we set both windows to UsageWindow(100.0) as a
            # sentinel "treat as completely full." That worked for the
            # picker but destroyed observability: `cus status` would show
            # 100% / 100% even when the real values were known to be lower
            # from the prior successful poll. See GH issue tracking this.
            u.raw = {"error": f"HTTP 429 (rate_limited): {body_preview}", "rate_limited": True}
        else:
            u.raw = {"error": f"HTTP {e.code}: {body_preview}"}
        return u
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as e:
        u = AccountUsage.empty()
        u.raw = {"error": f"{type(e).__name__}: {e}"}
        return u

    def parse_window(obj: dict | None) -> UsageWindow | None:
        if not isinstance(obj, dict):
            return None
        util = obj.get("utilization")
        if util is None:
            return None
        return UsageWindow(utilization=float(util), resets_at=obj.get("resets_at"))

    return AccountUsage(
        five_hour=parse_window(data.get("five_hour")),
        seven_day=parse_window(data.get("seven_day")),
        seven_day_sonnet=parse_window(data.get("seven_day_sonnet")),
        seven_day_opus=parse_window(data.get("seven_day_opus")),
        per_model_weekly=_parse_per_model_weekly(data),
        polled_at=now_iso(),
        raw=data,
    )


def _parse_per_model_weekly(data: dict) -> dict:
    """Extract per-model WEEKLY utilization from the usage API response.

    The API exposes per-model usage only at weekly granularity — there is NO
    per-model 5-hour window (`five_hour` is aggregate across all models). Two
    sources are merged:

      1. The `limits[]` array — each entry may carry `scope.model.display_name`
         (e.g. "Fable"). The weekly-group, model-scoped entries give that
         model's weekly %. This is the forward-looking structured source: newer
         models (Fable) appear ONLY here, not as a dedicated top-level field.
      2. Legacy dedicated fields `seven_day_sonnet` / `seven_day_opus`, kept for
         models that predate the `limits[]` scoping.

    Returns {display_name: UsageWindow}. The `limits[]` value wins on conflict
    (it's the live structured value); legacy fields fill in models absent there.
    Robust to the API's in-flux shape — any malformed entry is skipped, never
    raised, so a schema tweak can't take polling down.
    """
    out: dict[str, UsageWindow] = {}
    # Legacy named fields first, so limits[] can override them.
    for field_name, model in (("seven_day_sonnet", "Sonnet"), ("seven_day_opus", "Opus")):
        obj = data.get(field_name)
        if isinstance(obj, dict) and obj.get("utilization") is not None:
            out[model] = UsageWindow(utilization=float(obj["utilization"]),
                                     resets_at=obj.get("resets_at"))
    # limits[] model-scoped weekly entries (authoritative).
    limits = data.get("limits")
    if isinstance(limits, list):
        for entry in limits:
            if not isinstance(entry, dict) or entry.get("group") != "weekly":
                continue
            scope = entry.get("scope")
            model = scope.get("model") if isinstance(scope, dict) else None
            if not isinstance(model, dict):
                continue
            name = model.get("display_name")
            pct = entry.get("percent")
            if name and pct is not None:
                out[name] = UsageWindow(utilization=float(pct),
                                        resets_at=entry.get("resets_at"))
    return out


def _per_model_weekly_gate(config: dict) -> tuple[bool, set]:
    """Resolve the per-model weekly gate toggle + optional model allowlist.

    Returns (enabled, allowed_lowercased_names). When `per_model_weekly.models`
    is empty/unset the allowlist is empty, meaning "all models the API reports"
    (no restriction). Default DISABLED so unmodified installs keep the pre-2026-
    07-02 behavior (gate only on aggregate 5h/7d) — this is an opt-in.
    """
    cfg = config.get("per_model_weekly", {})
    allow = {str(m).lower() for m in (cfg.get("models") or [])}
    return bool(cfg.get("gate_enabled", False)), allow


def _max_model_weekly_from_usage(usage: AccountUsage, config: dict) -> float:
    """Highest tracked per-model WEEKLY utilization on an AccountUsage, or 0.0
    when the gate is off / no data. Used to fold per-model weekly caps into the
    active account's threshold-trip decision (current_max_pct)."""
    enabled, allow = _per_model_weekly_gate(config)
    if not enabled or not usage.per_model_weekly:
        return 0.0
    vals = [w.utilization for m, w in usage.per_model_weekly.items()
            if not allow or m.lower() in allow]
    return max(vals) if vals else 0.0


def _max_model_weekly_from_acct(acct: dict, config: dict) -> float:
    """Dict-level twin of _max_model_weekly_from_usage — reads the persisted
    per_model_weekly_pct from state.json. Used to fold per-model weekly caps
    into swap-target selection (_account_effective_pct).

    Stale-guard (2026-07-05 incident): a token_stale (or otherwise un-observable
    — rate_limited / token_expired / poll_error) account's cached
    per_model_weekly_pct is last-known-good, possibly HOURS old or even
    post-rollover — it is NOT a current reading. Treat it as UNKNOWN (return
    0.0), never as a hard cap. Per-model weekly is a 7-day-window concept, so it
    shares current_7d_pct's staleness fate (`_pct_is_unknown`).

    Why this direction is the safe one: returning 0.0 for a stale account means
    a STALE per-model % neither (a) EXCLUDES the account from swap-target
    selection via pick_swap_target's `_max_model_weekly_from_acct(a) < model_cap`
    filter (the "refused a target on a stale reading" error), nor (b) makes the
    hard-cap anti-pingpong guard treat a chosen target as "still capped" on a
    number we couldn't reconfirm. The incident: an operator+agent trusted a
    token_stale account's cached `Fable=100%` as authoritative and moved a live
    Fable session off it — but the account actually had Fable headroom.

    The swap-AWAY force (decide_swap Trigger 1) is already safe WITHOUT this
    guard: it reads FRESH usage via `_max_model_weekly_from_usage`, which is 0.0
    for a token_stale account (its AccountUsage this cycle is empty), so a stale
    per-model can never force a live lane OFF an account. This guard only closes
    the CACHED-dict path used by target selection / the pingpong guard.
    """
    enabled, allow = _per_model_weekly_gate(config)
    pm = acct.get("per_model_weekly_pct") or {}
    if not enabled or not pm:
        return 0.0
    # Per-model shares the 7d window's staleness: if we can't trust current_7d
    # for this account, we can't trust its per-model weekly numbers either.
    if _pct_is_unknown(acct, "current_7d_pct"):
        return 0.0
    vals = [p for m, p in pm.items() if not allow or m.lower() in allow]
    return max(vals) if vals else 0.0


def current_max_pct(usage: AccountUsage, config: dict) -> float:
    """Return the higher of 5h / 7d utilization (whichever the config enables).

    This is the PROGRESSIVE-LADDER signal (Trigger 2 in decide_swap). Per-model
    weekly % is deliberately EXCLUDED here as of 2026-07-02: the ladder steps
    an account up gradually (80 → 90 → …), which suits fast-moving windows but
    NOT a weekly per-model budget. The user's model gate is a hard line ("swap
    when Fable hits 97% AND take the account out of premium rotation"), not a
    gradual climb. Folding per-model into the ladder made Fable trip a swap at
    steps[0] (e.g. 80%) long before its own cap — the wrong behavior.

    Per-model weekly is now enforced ONLY on the hard-cap paths, where it
    belongs: decide_swap Trigger 1 (force-swap at per_model_weekly.cap_pct) and
    pick_swap_target's target filter (exclude candidates at/above that cap).
    Both compare against `_model_weekly_cap_for_config`, independent of the
    ladder. See `_max_model_weekly_from_usage` / `_max_model_weekly_from_acct`.

    Annotation 2026-07-02 (supersedes the prior fold): before this date the
    highest tracked per-model weekly % was max()'d in here so an exhausted
    model tripped the ladder threshold. That path is removed; the hard-cap
    trigger already covered the "exhausted" case at the correct threshold.
    """
    thresholds = config.get("thresholds", {})
    candidates = []
    if thresholds.get("five_hour", True) and usage.five_hour:
        candidates.append(usage.five_hour.utilization)
    if thresholds.get("seven_day", True) and usage.seven_day:
        candidates.append(usage.seven_day.utilization)
    return max(candidates) if candidates else 0.0


# --------------------------------------------------------------------------
# Strategy picker (lifted from cux/internal/strategy/strategy.go)
# --------------------------------------------------------------------------

@dataclass
class SwapTarget:
    name: str
    reason: str


def _hard_7d_cap_for_config(config: dict) -> float:
    """Resolve the active strategy's hard_7d_cap_pct, with smart_strategy's
    value as the cross-strategy fallback. Used as a universal target filter
    so NO strategy picks an account at or above the user-stated weekly
    ceiling. Surfaced 2026-05-24 (GH #17) after the strategy audit found
    lowest_usage/drain/round_robin had no such filter."""
    strategy_cfg = config.get(f"{config.get('strategy', '')}_strategy", {})
    return strategy_cfg.get("hard_7d_cap_pct") or config.get("smart_strategy", {}).get("hard_7d_cap_pct", 80)


def _model_weekly_cap_for_config(config: dict) -> float:
    """Threshold the per-model weekly gate trips at. `per_model_weekly.cap_pct`
    when set; otherwise the strategy's hard_7d_cap_pct — the original fold-in
    design compared per-model % against the same hard cap, so the fallback
    keeps unmodified configs byte-identical."""
    cap = config.get("per_model_weekly", {}).get("cap_pct")
    return float(cap) if cap is not None else _hard_7d_cap_for_config(config)


def _model_weekly_target_cap_for_config(config: dict) -> float:
    """Target-side bar for the per-model weekly gate (2026-07-03, user request).

    The gate's two uses want DIFFERENT thresholds: swapping AWAY should wait
    for cap_pct (drain the week's allotment you already have — evacuating a
    lane at 87% wastes the last 10%), but swapping ONTO an account should stop
    much earlier — arriving work immediately burns the model's remaining
    headroom, so landing a fresh lane on an 87%-Fable account (the `default`
    case: severity already "warning" upstream) just schedules the next
    depletion. `per_model_weekly.target_cap_pct` sets the target-side bar;
    unset falls back to cap_pct — symmetric, byte-identical pre-change
    behavior."""
    cap = config.get("per_model_weekly", {}).get("target_cap_pct")
    return float(cap) if cap is not None else _model_weekly_cap_for_config(config)


def _account_effective_pct(acct: dict, config: dict) -> float:
    """Apply thresholds.{five_hour,seven_day} config when computing an
    account's "effective" usage % — i.e. the value the ladder logic actually
    cares about. Mirrors current_max_pct but operates on the dict-level
    state.json data instead of AccountUsage objects. Used by strategies
    (drain, strict_priority) that previously hardcoded both-windows checks
    and produced inconsistent behavior with the user's config (GH #17)."""
    thr_cfg = config.get("thresholds", {})
    candidates = []
    if thr_cfg.get("five_hour", True):
        candidates.append(acct.get("current_5h_pct", 0.0))
    if thr_cfg.get("seven_day", True):
        candidates.append(acct.get("current_7d_pct", 0.0))
    # Per-model weekly is deliberately NOT folded here (2026-07-02): this is a
    # ladder/scoring signal (would-re-trip, drain ordering, saturation via the
    # estimated twin), and per-model weekly is a hard-cap concept, not a ladder
    # one. The hard-cap TARGET filter in pick_swap_target excludes model-
    # exhausted candidates directly via `_max_model_weekly_from_acct` vs the
    # model cap, so dropping it here does not let an exhausted account get
    # picked — it only stops per-model from tripping the gradual ladder. See
    # current_max_pct's annotation for the full rationale.
    return max(candidates) if candidates else 0.0


def _compute_burn_rate(old_pct: float | None, new_pct: float | None,
                       old_ts: str | None, new_ts: str | None) -> float:
    """%/min usage increase between two polls, or 0.0 if not computable.

    Estimator (C, 2026-06-23). Only positive rates are meaningful: usage climbs
    monotonically WITHIN a window, so a drop means the window reset between polls
    — we return 0 and let the next interval re-measure from the post-reset base.
    """
    if old_pct is None or new_pct is None or not old_ts or not new_ts:
        return 0.0
    try:
        t0 = datetime.fromisoformat(old_ts.replace("Z", "+00:00"))
        t1 = datetime.fromisoformat(new_ts.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return 0.0
    dt_min = (t1 - t0).total_seconds() / 60.0
    if dt_min <= 0 or new_pct < old_pct:
        return 0.0
    return (new_pct - old_pct) / dt_min


def estimate_window_pct(acct: dict, window: str, config: dict, now=None) -> float:
    """Extrapolate an account's CURRENT usage % for one window ("5h"/"7d") from
    its last poll + measured burn rate (C, 2026-06-23). Returns a value >= the
    polled %, clamped to 100.

    Falls back to the raw polled % whenever the estimator is disabled or no rate
    has been measured yet (first poll after a swap/reset) — so in the absence of
    data, every caller behaves exactly as it did pre-estimator. The estimate only
    ever moves UPWARD from the polled value, so wiring it into target-fullness
    checks can only make the picker MORE conservative, never less.
    """
    polled = acct.get(f"current_{window}_pct", 0.0)
    est_cfg = config.get("estimator", {})
    if not est_cfg.get("enabled", True):
        return polled
    rate = acct.get(f"burn_rate_{window}_pct_per_min", 0.0) or 0.0
    # Extrapolate forward from the last real OBSERVATION of `polled`, not the
    # last poll ATTEMPT (2026-07-03 token-stale blindness incident, GH #130).
    # `polled` (current_{window}_pct) is only refreshed on a successful poll;
    # anchoring dt at an error-branch-restamped last_poll_ts would make the
    # value look freshly-observed and UNDER-extrapolate (picker less
    # conservative than reality). `or last_poll_ts` preserves today's behavior
    # for legacy state that predates the field.
    last_observed = acct.get("last_observed_ts") or acct.get("last_poll_ts")
    if rate <= 0 or not last_observed:
        return polled
    if now is None:
        now = datetime.now(timezone.utc)
    try:
        t0 = datetime.fromisoformat(last_observed.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return polled
    # Cap how far we trust a stale rate: a burn rate measured one interval ago
    # says little about usage 30 min later (the session may have gone idle).
    max_min = est_cfg.get("max_extrapolation_minutes", 10)
    dt_min = max(0.0, min((now - t0).total_seconds() / 60.0, max_min))
    return min(100.0, polled + rate * dt_min)


def _account_estimated_effective_pct(acct: dict, config: dict, now=None,
                                     look_ahead_seconds: float = 0.0) -> float:
    """Like _account_effective_pct, but using extrapolated per-window values (C).
    Used by the target-fullness judgments (saturation filter + would-re-trip) so
    a fast-climbing account near the line is treated as already-full.

    `look_ahead_seconds` (Fix A, 2026-07-06 rayi3 incident): project the burn
    FORWARD past `now` by this many seconds — i.e. estimate where usage will be
    by the time the daemon can next observe/act on this account (its poll
    interval), not just where it is right now. The swap TRIGGER passes the
    account's poll interval here so a fast burn that would blow past the ladder
    step / cap in the unobserved gap between polls trips the swap THIS cycle
    (default 0.0 = extrapolate-to-now, the original behavior for all existing
    callers). Implemented by shifting the `now` passed to estimate_window_pct,
    which already clamps to 100 and never returns below the polled value, so a
    look-ahead only ever moves the result UP."""
    if look_ahead_seconds:
        if now is None:
            now = datetime.now(timezone.utc)
        now = now + timedelta(seconds=max(0.0, look_ahead_seconds))
    thr_cfg = config.get("thresholds", {})
    vals = []
    if thr_cfg.get("five_hour", True):
        vals.append(estimate_window_pct(acct, "5h", config, now))
    if thr_cfg.get("seven_day", True):
        vals.append(estimate_window_pct(acct, "7d", config, now))
    # Per-model weekly is NOT folded here (2026-07-02) — same reason as
    # _account_effective_pct: this feeds ladder/saturation judgments, and
    # per-model weekly is enforced on the hard-cap filter instead. A model-
    # exhausted account is still excluded as a target by pick_swap_target's
    # explicit model-cap filter, so it can never be picked.
    return max(vals) if vals else 0.0


# --------------------------------------------------------------------------
# Near-threshold acceleration + extrapolation-aware trigger (2026-07-06 rayi3
# incident). See DEFAULT_CONFIG["poll_accel"] for the full WHY. These are pure
# helpers (usage dict + config + now → number / bool) so the swap-timing logic
# is unit-testable apart from the poll I/O.
# --------------------------------------------------------------------------

def _poll_accel_enabled(config: dict) -> bool:
    """Master gate for both near-threshold fixes. False ⇒ prior behavior
    bit-for-bit (trigger reads raw polled %, no fast cadence)."""
    return bool(config.get("poll_accel", {}).get("enabled", True))


def _next_ladder_step(acct: dict, config: dict) -> float:
    """The usage % this account's ladder will next trip at — its persisted
    `next_swap_at_pct`, resolved against configured steps with the SAME
    end-of-ladder sentinel handling decide_swap's Trigger 2 uses (a value >= 100
    means the ladder ran off the end, so treat it as the last configured step —
    the most aggressive but bounded threshold). Kept as a standalone pure helper
    so Fix A (trigger) and Fix B (fast poll) agree on exactly which step matters.
    """
    steps = config.get("thresholds", {}).get("steps") or [50, 75, 90]
    step = acct.get("next_swap_at_pct", steps[0])
    try:
        step = float(step)
    except (TypeError, ValueError):
        step = float(steps[0])
    if step >= 100:
        step = float(steps[-1])
    return step


def _account_near_step(acct: dict, config: dict, normal_interval_seconds: float,
                       now=None, lane_count: int = 0) -> bool:
    """Is this account close enough to its next ladder step to warrant fast
    polling (Fix B)? True when EITHER:
      (1) its burn-extrapolated usage (to now) is within `within_pct_of_step`
          points of the step — we're in the danger zone and want frequent
          confirmation; OR
      (2) its measured burn rate projects it CROSSING the step before the next
          NORMAL poll (`normal_interval_seconds` out) — a fast climber that is
          still numerically below the band but won't be for long.
    Pure predicate over the account dict + config; `normal_interval_seconds` is
    passed in (not read from state) precisely to keep it I/O-free and testable.
    Returns False when poll_accel is disabled, so the gate reverts cleanly.

    `lane_count` (2026-07-06 rayi4 incident): how many LIVE lanes this account
    backs. An account under N concurrent live sessions climbs ~N× faster than its
    per-account `burn_rate` (measured before those lanes piled on) predicts, so
    the near-step band is widened by `poll_accel.cluster_within_bonus_pct` per
    EXTRA lane. Default 0 ⇒ single-lane behavior (band unchanged). This is why
    clustered rayi4 sat at 84% on the slow cadence and rode past its 95% step —
    the plain 5-pt band never triggered until it was already too late."""
    if not _poll_accel_enabled(config):
        return False
    pa = config.get("poll_accel", {})
    within = float(pa.get("within_pct_of_step", 5))
    if lane_count > 1:
        within += float(pa.get("cluster_within_bonus_pct", 10)) * (lane_count - 1)
    step = _next_ladder_step(acct, config)
    # (1) already inside the band on the extrapolated NOW value.
    if _account_estimated_effective_pct(acct, config, now) >= step - within:
        return True
    # (2) projected to cross the step by the next normal poll.
    projected = _account_estimated_effective_pct(
        acct, config, now, look_ahead_seconds=normal_interval_seconds)
    return projected >= step


def _burn_extrapolation_delta(acct: dict, window: str, config: dict, now=None,
                              look_ahead_seconds: float = 0.0) -> float:
    """Pct-points a window is projected to climb from its last OBSERVATION to
    now+look_ahead at the measured burn rate. 0.0 when the estimator is off, no
    rate has been measured, or the anchor timestamp is missing/unparseable.

    Exists for the per-model weekly projection: per-model weekly rides the 7d
    window and cus tracks no per-model burn rate of its own, so Fix A projects
    the per-model % forward on the 7d rate (a shared-window proxy). Mirrors the
    dt/cap math inside estimate_window_pct but returns just the DELTA so it can
    be added to a per-model base that estimate_window_pct doesn't know about.
    Conservative: rate is only ever positive (see _compute_burn_rate), so the
    delta only pushes the projected value UP."""
    est_cfg = config.get("estimator", {})
    if not est_cfg.get("enabled", True):
        return 0.0
    rate = acct.get(f"burn_rate_{window}_pct_per_min", 0.0) or 0.0
    last_observed = acct.get("last_observed_ts") or acct.get("last_poll_ts")
    if rate <= 0 or not last_observed:
        return 0.0
    if now is None:
        now = datetime.now(timezone.utc)
    try:
        t0 = datetime.fromisoformat(last_observed.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return 0.0
    proj = now + timedelta(seconds=max(0.0, look_ahead_seconds))
    max_min = est_cfg.get("max_extrapolation_minutes", 10)
    dt_min = max(0.0, min((proj - t0).total_seconds() / 60.0, max_min))
    return rate * dt_min


def _is_seven_day_reset_drop(prev_pct, new_pct, config: dict) -> bool:
    """True when a poll-over-poll change in current_7d_pct is a REAL 72h reset.

    Field finding 2026-07-05 (gist: monperrus/3ac4b303a84946bbeaf2b1123ee99491):
    the `seven_day` "weekly" budget actually refreshes every ~72h at a fixed UTC
    anchor, not every 7 days. We can't read that boundary from the API (its
    resets_at points ~7 days out, when the oldest tokens age off), so we infer
    the reset from its only observable signature: utilization dropping SHARPLY
    between two consecutive real observations.

    Pure + side-effect-free so it's trivially testable in isolation from the
    poll I/O. Guards against noise: a small downward fluctuation (measurement
    jitter, or the gradual oldest-tokens-age-off trickle) is NOT a reset — only
    a drop of at least `seven_day_reset_drop_pct` points counts. Cold start
    (either value missing) returns False so the caller falls back to the API
    timestamp until a real drop is observed.
    """
    if prev_pct is None or new_pct is None:
        return False
    drop_threshold = float(config.get("seven_day_reset_drop_pct", 15))
    try:
        return (float(prev_pct) - float(new_pct)) >= drop_threshold
    except (TypeError, ValueError):
        return False


def projected_seven_day_reset(acct: dict, config: dict, now=None) -> str | None:
    """Best estimate (ISO-8601 str) of when this account's 7d budget REALLY
    refreshes — the value display + reset-proximity logic should prefer over the
    raw API `seven_day_resets_at`.

    Field finding 2026-07-05 (gist: monperrus/3ac4b303a84946bbeaf2b1123ee99491):
    the real ~72h refresh lands BEFORE the API's ~7-day boundary, and the
    earlier moment is the one operators care about — that's when budget frees up
    and when a near-cap account becomes usable again.

    Resolution order:
      1. If we've OBSERVED a reset drop (`seven_day_last_reset_ts` is set),
         project forward by `seven_day_reset_hours` (default 72). Because the
         cadence is a FIXED anchor, a stale last-reset spanning several missed
         cycles is rolled forward by whole 72h periods until the projection is
         in the future.
      2. Fall back to the API `seven_day_resets_at` on cold start (no drop seen
         yet) — preserving pre-change behavior until we learn the real anchor.
      3. When BOTH a projection and the API value exist, return the EARLIER of
         the two (the real refresh precedes the oldest-tokens boundary).

    Pure helper (reads state, no writes) so it's testable and safe to call from
    any display/scoring path.
    """
    reset_hours = float(config.get("seven_day_reset_hours", 72))
    api_ts = acct.get("seven_day_resets_at")
    last_reset = acct.get("seven_day_last_reset_ts")

    projected_ts: str | None = None
    if last_reset:
        try:
            t0 = datetime.fromisoformat(str(last_reset).replace("Z", "+00:00"))
            if now is None:
                now = datetime.now(timezone.utc)
            nxt = t0 + timedelta(hours=reset_hours)
            # Fixed-anchor cadence: advance whole periods until future, so a
            # last_reset_ts left behind by several missed polls still yields the
            # NEXT upcoming refresh rather than a stale past timestamp.
            while nxt <= now:
                nxt = nxt + timedelta(hours=reset_hours)
            projected_ts = nxt.isoformat()
        except (ValueError, AttributeError, TypeError):
            projected_ts = None

    candidates = [c for c in (projected_ts, api_ts) if c]
    if not candidates:
        return None
    if len(candidates) == 1:
        return candidates[0]
    # Both present → return the earlier (the real refresh comes first).
    try:
        return min(candidates, key=lambda c: datetime.fromisoformat(str(c).replace("Z", "+00:00")))
    except (ValueError, AttributeError, TypeError):
        return projected_ts or api_ts


def _target_would_immediately_re_trip(acct: dict, config: dict) -> bool:
    """Return True if this candidate is "full" — its effective utilization is
    at/above the FIRST ladder step — and so should not be swapped onto. Used to
    prune "doomed" candidates that would cause a swap-back loop. GH #17.

    2026-06-12: clamp the "full" test to steps[0] instead of the candidate's
    OWN advanced next_swap_at_pct. Why: after an account is swapped away from,
    its step ratchets 90->96 (advance_ladder); maybe_reset_thresholds only
    unwinds that to 90 on a DETECTED 5h rollover or when both windows fall
    below 50. A parked account sitting at 5h=0% / 7d=92% never rolls its 5h
    window, so its step stays stuck at 96 indefinitely — and at threshold=96 a
    7d=92% account passed this filter (92 < 96) and got chosen as a swap target.
    That is the exact hole behind the 2026-06-11T21:04 incident (swapped ONTO a
    weekly-exhausted `default`), which is why hard_7d_cap_pct had been doing the
    job this filter should. "Full" must mean the same 90% line for every account
    regardless of where its ladder happens to sit. Degraded/all-full pools are
    still handled downstream by headroom_fallback + the min-improvement gate, so
    progressive-return semantics survive.
    """
    cfg_steps = config.get("thresholds", {}).get("steps") or [50, 75, 90]
    # Estimator (C): use the extrapolated %, so a target polled just under the
    # step but climbing fast (will be over it by the time the swap lands) is
    # still treated as full. Falls back to polled when no rate is known.
    return _account_estimated_effective_pct(acct, config) >= cfg_steps[0]


def _disabled_accounts(config: dict) -> set:
    """Accounts the operator marked `disabled: true` in config.yaml's accounts
    list — a hard out-of-rotation switch (2026-07-03, user request: `default`
    depleted its Fable weekly while the polled per-model number still read 87%,
    under the 97% gate — the operator knows things the poll doesn't).

    Semantics: NEVER a swap/launch target, no degraded-pool fallback (an
    operator mandate outranks "swap somewhere rather than sit on a hot
    active"). Polling continues (so the operator can see when its windows
    reset) and a lane already ON the account keeps running — the flag only
    stops NEW placements. Re-enable by deleting the key or setting false."""
    return {a.get("name") for a in config.get("accounts", [])
            if isinstance(a, dict) and a.get("disabled")}


def _spread_lanes_enabled(config: dict) -> bool:
    """Master gate for the anti-clustering lane-spread levers (2026-07-06).
    False ⇒ scoring penalty and the deferrable-move HOLD both revert to prior
    behavior bit-for-bit."""
    return bool(config.get("spread_lanes", {}).get("enabled", True))


def _lane_load_penalty(name: str, state: dict, config: dict) -> float:
    """Anti-clustering swap-target score penalty for candidate account `name`
    (2026-07-06 clustering incident).

    Returns `spread_lanes.cluster_penalty` points for EACH live lane already
    backing `name` — read from `state["_lane_load"]`, a caller-supplied
    account→lane-count map (existing /proc occupancy PLUS this-cycle's queued
    claims; see decide_slot_swaps). The picker subtracts this from a candidate's
    strategy score so an account that already holds one or more lanes loses to a
    distinct EMPTY one, breaking the "every lane's best pick is the same magnet
    account" attractor that piled all lanes onto rayi5 then rayi4.

    0.0 when the feature is off, when no load map was supplied (every non-lane
    caller — SOS probes, `cus switch`, global mode — passes no `_lane_load`, so
    their scoring is untouched), or when the candidate backs no lanes. Scales
    linearly with lane count, so a magnet that has already accreted 2-3 lanes is
    penalized 2-3× and the fleet disperses within a cycle or two."""
    if not _spread_lanes_enabled(config):
        return 0.0
    load = (state.get("_lane_load") or {}).get(name, 0)
    if not load:
        return 0.0
    return float(config.get("spread_lanes", {}).get("cluster_penalty", 40.0)) * load


def pick_swap_target(state: dict, config: dict) -> SwapTarget | None:
    """Pick which account to swap to, based on configured strategy.

    Strategies:
      - lowest_usage: pick the (non-current, non-token-expired) account with
        lowest max(5h, 7d) utilization. Cux's "balanced".
      - drain: deplete current; pick a candidate only if current is over cap.
        Within candidates, prefer those farther from their own cap.
      - strict_priority: respect priority order; only fall through if
        higher-priority accounts are over threshold.
      - round_robin: pick next-in-list (wrapping). Ignores usage.
      - headroom / smart: weighted-score over 5h + 7d headroom.

    Universal filters applied across ALL strategies (GH #17 — strategy audit
    2026-05-24):
      0. operator-disabled (`disabled: true` in config accounts) always
         excluded, no fallback — see _disabled_accounts (2026-07-03).
      1. token_expired / poll_error always excluded.
      2. rate_limited excluded unless smart_strategy.allow_rate_limited_targets.
      3. hard_7d_cap: never pick an account at/above the user-stated 80%
         weekly ceiling (was only enforced by headroom/smart before).
      4. would_re_trip: never pick an account whose own ladder would trip
         immediately on swap-arrival (would cause swap-back loop).
    """
    current = state.get("active")
    accounts = state.get("accounts", {})
    if not current or not accounts:
        return None

    # Hard filter: never pick an account with no working tokens / can't poll
    # at all. token_expired and poll_error always exclude, as does an
    # operator `disabled: true` (out-of-rotation mandate — no fallback tier
    # below ever re-admits these, unlike the cap/headroom soft filters).
    disabled = _disabled_accounts(config)
    candidates: list[tuple[str, dict]] = [
        (name, acct) for name, acct in accounts.items()
        if name != current
        and name not in disabled
        and not acct.get("token_expired", False)
        and not acct.get("poll_error")
    ]

    # ---- Poisoned-canonical-store exclusion (2026-07-06, this PR) ----
    # HARD filter, NO fallback: never select an account whose canonical
    # .claude.json identity contradicts its trusted anchor (meta.yaml + login
    # families). Installing such an account's snapshot copies the WRONG account's
    # identity/token onto the live mount — the exact wrong-account-clobber this PR
    # guards, now spreading OUTWARD. Unlike the cap/headroom SOFT filters below
    # there is deliberately no degraded re-admit: a poisoned target is never a
    # lesser-evil option, only a vector for propagating the corruption. If this
    # empties the pool the picker returns None and SOS surfaces the poisoned
    # account (Condition 10.5) pointing the operator at `cus relogin`.
    candidates = [(name, acct) for name, acct in candidates
                  if not account_canonical_store_poisoned(name)[0]]
    if not candidates:
        return None

    # ---- Dead-snapshot exclusion (2026-07-07 merkos incident, this PR) ----
    # HARD filter, NO fallback (mirrors the poisoned-store exclusion above): never
    # select an account whose canonical snapshot is DEAD (its OAuth refresh grant
    # returns invalid_grant — flagged `snapshot_refresh_dead` by the idle-account
    # un-stale sweep) AND that has NO free login family to seed a lane from instead.
    # Installing such an account's snapshot blanks the target mount on Claude Code's
    # first refresh — the merkos blank-on-install that logged chats1a out.
    #
    # Why the `has_free_login_family` carve-out: an account that is dead-snapshot BUT
    # has a claimable family is STILL a valid target — the swap install-point seeds
    # the lane from that family (CRED-AUDIT op=snapshot-dead-family-fallback) instead
    # of the dead snapshot, so the account can host the new lane after all. Only when
    # there's ALSO no family does the account genuinely become un-hostable.
    #
    # We read the state FLAG here (set by the sweep's refresh-grant probe), NOT a
    # fresh probe: the picker runs every cycle and must stay cheap + side-effect-free
    # (the probe rotates a token). The swap install-point does the DEFINITIVE re-probe
    # (`_account_snapshot_dead`) — decision layer optimistic, execution layer
    # authoritative, the same split as has_free_login_family / claim_verified_login_family.
    # If this empties the pool the picker returns None and the dead-snapshot SOS fires.
    candidates = [(name, acct) for name, acct in candidates
                  if not (acct.get("snapshot_refresh_dead") and not has_free_login_family(name, state))]
    if not candidates:
        return None

    # Rate-limited is a SOFT filter by default — try non-rate-limited first,
    # but fall back to rate-limited candidates if nothing else is available.
    # See smart_strategy.allow_rate_limited_targets in DEFAULT_CONFIG for
    # why this conflates two distinct 429s (polling throttle vs account cap).
    strategy_cfg = config.get(f"{config.get('strategy', '')}_strategy", {})
    allow_rl = strategy_cfg.get("allow_rate_limited_targets", config.get("smart_strategy", {}).get("allow_rate_limited_targets", False))
    non_rate_limited = [(n, a) for n, a in candidates if not a.get("rate_limited", False)]
    if non_rate_limited:
        # Prefer non-rate-limited candidates when any exist.
        candidates = non_rate_limited
    elif not allow_rl:
        # All remaining candidates are rate-limited and config says no.
        return None
    # else: candidates list keeps the rate-limited ones; picker proceeds.

    if not candidates:
        return None

    # Fix B (user 2026-06-23 "never swap to a 100% account"): a fully-saturated
    # target is a hard wall, not a degraded-but-usable option. Unlike the 7d-cap
    # and would-re-trip filters below — which deliberately fall back to "swap
    # somewhere rather than sit on a hot active" — there is NO fallback here.
    # Swapping onto a 100% account just churns context and immediately re-trips
    # the same 429s (the 2026-06-23 merkos-bounce incident: reactive 429s landed
    # the active cred on a correctly-polled 100% merkos). If this empties the
    # pool, return None and stay put; SOS surfaces the all-full condition. The
    # sub-100% gray zone is intentionally left to the scoring / would-re-trip
    # logic below — "below 100% is a judgment call" (user).
    full_line = config.get("never_swap_to_pct", 100)
    # Estimator (C): judge saturation on the EXTRAPOLATED %, so an account polled
    # at e.g. 96% but climbing is treated as already-full and excluded (it would
    # be 100% by the time the swap arrives). Falls back to polled when no rate is
    # known, so behavior is unchanged in the absence of burn-rate data.
    not_saturated = [(n, a) for n, a in candidates if _account_estimated_effective_pct(a, config) < full_line]
    if not not_saturated:
        return None
    candidates = not_saturated

    # ---- Per-model weekly (Fable) gate: HARD for premium, NO fallback ----
    # (fix 2026-07-05, this PR; the per-model analog of PR #139's hard-cap
    # anti-pingpong HOLD.) Split OUT of the aggregate-7d SOFT filter below —
    # before 2026-07-05 both conditions lived in one `safe_7d` list — because the
    # two want OPPOSITE degraded-mode behavior:
    #
    #   • aggregate 7d over-cap → SOFT: if EVERY candidate is over the weekly
    #     ceiling the whole system is degraded, and "swap somewhere rather than
    #     sit on a hot active" wins (cap_fallback re-admits them, annotated
    #     DEGRADED). Landing on a 7d-heavy account still lets work through.
    #   • per-model Fable over-cap for a PREMIUM lane → HARD: a premium lane's
    #     whole purpose is Fable-clean placement. Landing it on a Fable-DEAD
    #     account breaks its Fable work outright — strictly WORSE than holding on
    #     the (5h-hot) account it's already on, which can still do Fable and can
    #     move next cycle when a clean target frees (e.g. after a 5h reset). So
    #     if the per-model gate is active and EVERY candidate is at/over the
    #     model cap, we HOLD (return None) instead of degrading onto one.
    #
    # The bug this closes (live 2026-07-05, decisions.jsonl): rayi4 hit its 5h
    # LADDER step with three premium Fable lanes stacked on it; the daemon fanned
    # all three off at once. The first two took the clean Fable targets; the
    # third had none left and fell into the aggregate cap_fallback, which —
    # because per-model was folded INTO that same soft filter — re-admitted a
    # Fable-capped account (rayi2, Fable 100%) and landed the premium lane on it.
    # PR #139's hard-cap HOLD did NOT fire because the FORCING gate here was the
    # 5h ladder, not a hard cap, so its guard never ran. Making the per-model
    # gate its own HARD filter fixes BOTH the primary pick AND the fan-out
    # re-pick (decide_slot_swaps), since both route through this function.
    #
    # Standard pool is UNCHANGED: _config_for_pool forces gate_enabled=False for
    # it (GH #99), so _per_model_weekly_gate is (False, …) and
    # _max_model_weekly_from_acct returns 0.0 for every candidate → model_safe ==
    # candidates, the HOLD never fires, and a standard lane may still land on a
    # Fable-capped account by design.
    #
    # Stale-guard preserved (2026-07-05, GH #161): a token_stale / unobservable
    # account reads per-model 0.0 via _max_model_weekly_from_acct → stays
    # ELIGIBLE here. Only a FRESH at/over-cap reading excludes a candidate, so we
    # never refuse a target on a number we couldn't reconfirm.
    #
    # Cap used is the TARGET-side one (target_cap_pct, falling back to cap_pct,
    # falling back to hard_7d) — identical to the pre-split primary filter:
    # arrivals stop earlier than swap-away drains. See _model_weekly_target_cap_for_config.
    gate_enabled, _ = _per_model_weekly_gate(config)
    model_cap = _model_weekly_target_cap_for_config(config)
    model_safe = [(n, a) for n, a in candidates
                  if _max_model_weekly_from_acct(a, config) < model_cap]
    if gate_enabled and not model_safe:
        # Premium lane, every reachable target Fable-capped → HOLD. Returning
        # None keeps this picker SILENT like every other no-target return (it is
        # also driven from SOS / what-if probes, e.g. _candidate_is_valid_premium_target,
        # where an echo per capped candidate would be pure noise); the swap
        # callers (decide_swap's "no valid swap target" branch, decide_slot_swaps'
        # fan-out HOLD) emit the operator-facing daemon.log line.
        return None
    if model_safe:
        candidates = model_safe

    # Universal filter (GH #17, all strategies): exclude candidates above
    # the user-stated AGGREGATE weekly ceiling. Previously only headroom/smart
    # applied this — lowest_usage/drain/round_robin would happily pick an
    # exhausted account. SOFT: falls back to "any candidate" if ALL are above cap
    # (system is degraded; swap somewhere rather than stay on a hot active). Runs
    # on the per-model-safe survivors from the HARD gate above.
    hard_7d = _hard_7d_cap_for_config(config)
    safe_7d = [(n, a) for n, a in candidates if a.get("current_7d_pct", 0) < hard_7d]
    cap_fallback = not safe_7d
    if safe_7d:
        candidates = safe_7d

    # Universal filter (GH #17, all strategies): prune candidates that
    # would immediately re-trip their own ladder. Otherwise we'd swap
    # A→B and decide_swap on the next cycle would swap B→A → ping-pong.
    # Falls back to "any candidate" if NONE have headroom; the hysteresis
    # gate (GH #16) and usage-growth gate (GH #15) protect against the
    # resulting potential loop.
    with_headroom = [(n, a) for n, a in candidates if not _target_would_immediately_re_trip(a, config)]
    headroom_fallback = not with_headroom
    if with_headroom:
        candidates = with_headroom

    strategy = config.get("strategy", "lowest_usage")

    def _annotate(reason: str) -> str:
        """Tack on degraded-mode notes so the operator sees when we fell
        back to less-safe candidate pools."""
        notes = []
        if cap_fallback:
            notes.append(f"no targets below 7d cap {hard_7d}%")
        if headroom_fallback:
            notes.append("all candidates would re-trip own ladder")
        return reason + (f" [DEGRADED: {'; '.join(notes)}]" if notes else "")

    if strategy == "lowest_usage":
        # cux balanced. Sort by the user's effective threshold metric
        # (honors thresholds.{five_hour,seven_day}), then secondary tiebreak
        # by 5h for stability. Anti-cluster (2026-07-06): a lane-load penalty is
        # ADDED to the effective-util sort key (higher util = sorted later), so a
        # candidate already backing live lanes is deprioritized vs an empty one.
        candidates.sort(key=lambda kv: (_account_effective_pct(kv[1], config)
                                        + _lane_load_penalty(kv[0], state, config),
                                        kv[1].get("current_5h_pct", 0.0)))
        chosen, _ = candidates[0]
        return SwapTarget(name=chosen, reason=_annotate("lowest_usage: lowest effective util"))

    if strategy == "drain":
        # cux drain: pass 1 = candidates with effective_pct under their own
        # next_swap_at (i.e. headroom for an arrival). Pass 2 = any candidate
        # below 100% 5h. Pass 2 still applies the universal cap_fallback /
        # headroom_fallback filters above.
        ordered = [
            (n, a) for n, a in candidates
            if _account_effective_pct(a, config) < a.get("next_swap_at_pct", 50)
        ]
        if ordered:
            ordered.sort(key=lambda kv: (-kv[1].get("current_7d_pct", 0.0),))  # closest-to-cap first
            chosen, _ = ordered[0]
            return SwapTarget(name=chosen, reason=_annotate("drain: under own threshold"))
        # Pass 2: any candidate with some 5h room
        ordered = [(n, a) for n, a in candidates if a.get("current_5h_pct", 0.0) < 100]
        if ordered:
            ordered.sort(key=lambda kv: -kv[1].get("current_7d_pct", 0.0))
            chosen, _ = ordered[0]
            return SwapTarget(name=chosen, reason=_annotate("drain: 5h has room"))
        return None

    if strategy == "strict_priority":
        # Sort by priority asc (1 = highest); pick first with effective
        # headroom. Honors thresholds.{five_hour,seven_day} config (was
        # hardcoded both-windows before — GH #17).
        cfg_accounts = {a["name"]: a for a in config.get("accounts", [])}
        candidates.sort(key=lambda kv: cfg_accounts.get(kv[0], {}).get("priority", 99))
        for name, acct in candidates:
            if _account_effective_pct(acct, config) < acct.get("next_swap_at_pct", 50):
                return SwapTarget(name=name, reason=_annotate("strict_priority: highest priority with headroom"))
        return None

    if strategy == "round_robin":
        # Pick the next account by name AMONG REMAINING candidates (not all
        # accounts blindly). Previously took next-in-list which could include
        # token_expired/over-cap/would-re-trip accounts. Now respects the
        # universal filters; if the "next" account was filtered out, we skip
        # to the next surviving one.
        survived_names = sorted(n for n, _ in candidates)
        if current in survived_names:
            survived_names.remove(current)
        if not survived_names:
            return None
        all_names_sorted = sorted(accounts.keys())
        try:
            cur_idx = all_names_sorted.index(current)
        except ValueError:
            cur_idx = -1
        for offset in range(1, len(all_names_sorted) + 1):
            cand = all_names_sorted[(cur_idx + offset) % len(all_names_sorted)]
            if cand in survived_names:
                return SwapTarget(name=cand, reason=_annotate("round_robin"))
        return None

    if strategy == "headroom":
        # Weighted score of 5h + 7d headroom. Hard 7d cap already applied
        # universally above; this strategy's own hard_7d_cap_pct setting is
        # respected via _hard_7d_cap_for_config which prefers the active
        # strategy's value over smart_strategy's default.
        cfg = config.get("headroom_strategy", {})
        w5 = cfg.get("five_hour_weight", 0.7)
        w7 = cfg.get("seven_day_weight", 0.3)

        def score(acct: dict) -> float:
            return (100 - acct.get("current_5h_pct", 0.0)) * w5 + (100 - acct.get("current_7d_pct", 0.0)) * w7

        # Anti-cluster (2026-07-06): subtract the lane-load penalty so an account
        # already backing live lanes scores below a distinct empty one.
        candidates.sort(key=lambda kv: -(score(kv[1]) - _lane_load_penalty(kv[0], state, config)))
        chosen, chosen_acct = candidates[0]
        s = score(chosen_acct) - _lane_load_penalty(chosen, state, config)
        return SwapTarget(name=chosen, reason=_annotate(f"headroom: 5h={chosen_acct.get('current_5h_pct', 0):.0f}% 7d={chosen_acct.get('current_7d_pct', 0):.0f}% score={s:.1f}"))

    if strategy == "smart":
        # Smart strategy designed for: never go over 80% 7d; route on 5h
        # dynamics; prefer "burn-before-reset" candidates (those whose 5h
        # window will reset soon — using them now means we don't waste
        # remaining capacity). See docs/STRATEGIES.md for the design.
        # Hard 7d cap + would-re-trip filters are now applied universally
        # above (GH #17); this block just handles the scoring.
        cfg = config.get("smart_strategy", {})
        burn_window_hours = cfg.get("burn_window_hours", 2)
        burn_soon_weight = cfg.get("burn_soon_weight", 1.0)
        w5 = cfg.get("five_hour_headroom_weight", 0.6)
        w7 = cfg.get("seven_day_headroom_weight", 0.4)
        cold_penalty = cfg.get("cold_account_penalty", 0)  # 0 = no penalty (default)
        reset_pref_weight = cfg.get("reset_proximity_weight", 8.0)  # GH #42; 0 = off

        def smart_score(acct: dict) -> tuple[float, str]:
            h5 = acct.get("current_5h_pct", 0.0)
            h7 = acct.get("current_7d_pct", 0.0)
            reset_at = acct.get("five_hour_resets_at")

            s = (100 - h5) * w5 + (100 - h7) * w7
            reasons: list[str] = []

            # Burn-soon bonus: if 5h clock is ticking AND reset is within
            # burn_window, prefer this account (use capacity before reset
            # would have "wasted" it). KEY INSIGHT: when 5h is at 0%, the
            # clock isn't ticking — there's no reset deadline.
            if h5 > 0 and reset_at:
                try:
                    reset_dt = datetime.fromisoformat(reset_at.replace("Z", "+00:00"))
                    time_to_reset_s = (reset_dt - datetime.now(timezone.utc)).total_seconds()
                    if 0 < time_to_reset_s < burn_window_hours * 3600:
                        # bonus is bigger when reset is sooner
                        bonus = (burn_window_hours * 3600 - time_to_reset_s) / 60  # in minutes
                        bonus *= burn_soon_weight
                        s += bonus
                        reasons.append(f"burn-soon(+{bonus:.0f}, resets in {time_to_reset_s/60:.0f}min)")
                except (ValueError, AttributeError, TypeError):
                    pass

            # Continuous reset-proximity preference (GH #42, 2026-05-28):
            # beyond the within-burn_window bonus above, still gently prefer
            # the account whose 5h window resets sooner — spanning the FULL
            # 5h horizon so selection always leans toward the earlier-resetting
            # account, breaking near-ties the burn_window cutoff would miss.
            # Scaled small (default weight 8) so it only tips ties; headroom
            # (0-100) and burn-soon still dominate. This is the "consider how
            # much time is left on the 5h clock" half of the request; the
            # burn-before-reset TRIGGER (decide_swap, GH #42) is the other.
            if reset_pref_weight and h5 > 0 and reset_at:
                try:
                    reset_dt = datetime.fromisoformat(reset_at.replace("Z", "+00:00"))
                    ttr_s = (reset_dt - datetime.now(timezone.utc)).total_seconds()
                    horizon_s = 5 * 3600
                    if 0 < ttr_s <= horizon_s:
                        # prox ~1 when about to reset, ~0 when just reset (full 5h ahead)
                        prox = (horizon_s - ttr_s) / horizon_s
                        add = reset_pref_weight * prox
                        s += add
                        reasons.append(f"reset-prox(+{add:.1f})")
                except (ValueError, AttributeError, TypeError):
                    pass

            # Cold-account penalty: deprioritize accounts with 5h=0% if
            # configured (so we don't always pick the "freshest" — useful
            # for the future "straddle" strategy where you want a mix of
            # warm/cold accounts).
            if h5 == 0.0 and cold_penalty:
                s -= cold_penalty
                reasons.append(f"cold-penalty(-{cold_penalty})")

            return s, " ".join(reasons) if reasons else "(headroom only)"

        # Anti-cluster (2026-07-06 rayi5/rayi4 pile-up): after the usage/burn
        # score, subtract cluster_penalty per live lane already backing each
        # candidate. Without this the burn-soon bonus (+~98) made the ONE
        # about-to-reset account the runaway #1 pick for EVERY lane, so they all
        # migrated onto it cycle after cycle. The penalty scales with lane count,
        # so a magnet that has already accreted lanes falls behind a distinct
        # empty account and the fleet disperses. Inert when spread_lanes is off or
        # no _lane_load map was supplied (non-lane callers).
        scored = []
        for n, a in candidates:
            sc, rz = smart_score(a)
            pen = _lane_load_penalty(n, state, config)
            if pen:
                sc -= pen
                rz = (rz + f" cluster-penalty(-{pen:.0f})").strip()
            scored.append((n, a, sc, rz))
        scored.sort(key=lambda x: -x[2])
        chosen, chosen_acct, chosen_score, chosen_reasons = scored[0]
        return SwapTarget(
            name=chosen,
            reason=_annotate(f"smart: 5h={chosen_acct.get('current_5h_pct', 0):.0f}% 7d={chosen_acct.get('current_7d_pct', 0):.0f}% score={chosen_score:.1f} {chosen_reasons}"),
        )

    return None


# --------------------------------------------------------------------------
# Swap primitive (callable from daemon, not just CLI)
# --------------------------------------------------------------------------

# --------------------------------------------------------------------------
# GH #76: swap mutual exclusion + crash journal
# --------------------------------------------------------------------------

def _swap_lock_path() -> Path:
    """Path of the global swap lock (GH #76).

    One flock-style lock serializing execute_swap across ALL entry points
    (daemon reactive + background paths, `cus switch`, `cus auto-swap`,
    hot-swap orchestrator sub-paths). ORCHESTRATE_LOCK only guards the
    pane-typing orchestrator; the 4-file swap sequence itself raced freely
    before this. Lock ordering (fixed, never reversed): ORCHESTRATE_LOCK
    outer, swap lock inner — the orchestrator holds its lock and calls
    execute_swap, which takes this one. Nothing takes them in reverse.

    Computed from ACCOUNTS_DIR at call time (not an import-time constant) so
    tests that repoint ACCOUNTS_DIR at a temp tree can't accidentally lock
    the live ~/claude-accounts/.
    """
    return ACCOUNTS_DIR / "swap.lock"


def _swap_journal_path() -> Path:
    """Path of the write-ahead swap-intent journal (GH #76).

    Present on disk = a swap is in flight (or crashed mid-flight). Written
    before the first mutating step of execute_swap, removed after state.json
    is persisted. Call-time derivation for the same test-sandboxing reason
    as _swap_lock_path.
    """
    return ACCOUNTS_DIR / "swap.journal"


@contextlib.contextmanager
def _swap_lock(timeout_seconds: float | None = None):
    """Global inter-process mutex around the whole swap sequence (GH #76).

    Why: execute_swap is a 5-step, multi-file, non-transactional sequence.
    Each individual write is atomic, but two interleaved swaps (daemon +
    manual `cus auto-swap`, or two daemon paths) can each save the live
    credentials back into the OTHER's outgoing snapshot, destroying a refresh
    token that exists nowhere else. fcntl.flock is used (same pattern as
    ORCHESTRATE_LOCK) because it dies with the process — no stale-lockfile
    cleanup problem after a crash.

    Blocking-with-timeout rather than fail-fast: the common contention case
    is a sub-second swap already in flight, and the right behavior for the
    second caller is to wait its turn, not to error a user-invoked
    `cus switch`. On timeout raises RuntimeError — the exception type every
    execute_swap caller already catches and reports.

    Lock ordering: ORCHESTRATE_LOCK (hot-swap orchestrator) is always taken
    OUTSIDE this lock, never the reverse.
    """
    timeout = SWAP_LOCK_TIMEOUT_SECONDS if timeout_seconds is None else timeout_seconds
    lock_path = _swap_lock_path()
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(lock_path), os.O_CREAT | os.O_RDWR, 0o644)
    try:
        deadline = time.monotonic() + timeout
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    try:
                        os.lseek(fd, 0, 0)
                        holder = os.read(fd, 64).decode(errors="replace").strip() or "(unknown)"
                    except OSError:
                        holder = "(unknown)"
                    raise RuntimeError(
                        f"another swap is in flight (swap lock {lock_path} held by pid {holder}; "
                        f"waited {timeout:.0f}s). Retry in a moment — if the holder pid is dead the "
                        f"lock has already been released by the kernel, so a retry will succeed.")
                time.sleep(0.05)
        # We hold the lock; record our pid for contention diagnostics.
        try:
            os.lseek(fd, 0, 0)
            os.ftruncate(fd, 0)
            os.write(fd, f"{os.getpid()}\n".encode())
        except OSError:
            pass  # pid note is best-effort; the flock itself is what matters
        try:
            yield
        finally:
            try:
                os.lseek(fd, 0, 0)
                os.ftruncate(fd, 0)
            except OSError:
                pass
            try:
                fcntl.flock(fd, fcntl.LOCK_UN)
            except OSError:
                pass
    finally:
        try:
            os.close(fd)
        except OSError:
            pass


def _write_swap_journal(from_name: str | None, to_name: str, trigger: str, slot: str | None = None) -> None:
    """Persist swap intent BEFORE the first mutating step (GH #76).

    If the process dies anywhere inside the swap sequence, this file is what
    lets the next start detect it and reconcile, instead of silently running
    with live files pointing at one account and state.json at another — the
    desync whose next-swap save-back destroys a refresh token (#70/#76).

    per_session: `slot` names the mount the swap targets (None = the global
    ~/.claude/ pair). `from_name` may be None for a swap-into-empty-slot
    (a `cus launch` install — there is no outgoing account).
    """
    payload = {
        "from": from_name, "to": to_name, "trigger": trigger, "ts": now_iso(),
        # For a human reading the file after a crash:
        "note": "swap was in flight; if this file exists after a crash, run any cus command — recovery is automatic on next swap/daemon start",
    }
    if slot is not None:
        payload["slot"] = slot
    write_json(_swap_journal_path(), payload)


def _clear_swap_journal() -> None:
    try:
        _swap_journal_path().unlink()
    except FileNotFoundError:
        pass


def _identity_fields(cj: Any) -> dict:
    """Extract the comparable ACCOUNT-identity facets of a .claude.json payload.

    Used by crash recovery, doctor, SOS, and login-mount verification to decide
    which Anthropic ACCOUNT a set of live files belongs to. Only non-empty
    fields participate, so a sparse file still matches on whatever evidence it
    has.

    Correction 2026-07-03: `userID` (top-level) was REMOVED from the facets.
    It is a per-LOGIN-SESSION / per-config-dir client id, NOT a per-account id —
    two independent `/login`s of the SAME account carry different `userID`s
    (proven live: account 'rayi3' snapshot and its pooled family-1 login shared
    accountUuid `b9c92e39…` + email `rayi3@trisso.com` but differed on `userID`
    `93e1fc34…` vs `5d3df3e8…`). Comparing it made `_identities_match` FALSE-
    reject a correct same-account login (the independent-login-pool onboarding
    error). The authoritative account identity is `oauthAccount.accountUuid`
    (globally unique per account) plus `emailAddress`; those are what we compare.
    """
    if not isinstance(cj, dict):
        return {}
    oa = cj.get("oauthAccount")
    oa = oa if isinstance(oa, dict) else {}
    out: dict = {}
    for key, val in (("accountUuid", oa.get("accountUuid")),
                     ("emailAddress", oa.get("emailAddress"))):
        if val:
            out[key] = val
    return out


def _identities_match(a: dict, b: dict) -> bool | None:
    """True/False when the two identity dicts share comparable fields;
    None when there is no shared evidence (can't say either way)."""
    shared = set(a) & set(b)
    if not shared:
        return None
    return all(a[k] == b[k] for k in shared)


def _dir_identity(account_name: str) -> dict:
    p = ACCOUNTS_DIR / f"account-{account_name}" / ".claude.json"
    if not p.exists():
        return {}
    try:
        return _identity_fields(read_json(p))
    except (json.JSONDecodeError, OSError):
        return {}


def _dir_identity_of_mount(mount: Path) -> dict:
    """Identity facets from a live MOUNT's .claude.json (a slot dir or the shared
    ~/.claude). Used only by the CRED-AUDIT lines to record what identity the
    incoming creds actually carry — a foreign identity here is the clobber signal.
    Best-effort: an unreadable/absent mount file returns {} (rendered `none`)."""
    p = mount_claude_json_path(mount)
    if not p.exists():
        return {}
    try:
        return _identity_fields(read_json(p))
    except (json.JSONDecodeError, OSError):
        return {}


# ---------------------------------------------------------------------------
# Wrong-account canonical-store clobber guard (2026-07-06 incident)
#
# THE INCIDENT (verified live 2026-07-06, recurring over two days): an account's
# CANONICAL credential store — account-<X>/.claude.json (identity) and
# account-<X>/.credentials.json (tokens) — was silently overwritten with a
# DIFFERENT account's identity + token. Concretely, account-rayi1/.claude.json's
# oauthAccount was rayi2's and its .credentials.json polled identically to rayi2,
# even though account-rayi1/meta.yaml AND the independent-login families under
# logins/rayi1/family-* still correctly held rayi1's identity. So the sources the
# swap/save-back path does NOT rewrite (meta.yaml at `cus init` time, the login
# families at browser-`/login` time) stayed CORRECT while the canonical store —
# which the save-back path DOES write — got clobbered.
#
# THE WRITE VECTOR: _execute_swap_locked's OUTGOING-account save-back copies the
# live mount's identity keys into account-<current>/.claude.json with NO identity
# check (the GH #3 guard only covered the CREDENTIALS refresh-token lineage, never
# the paired .claude.json identity write). When a bare/slot session had drifted to
# another account, its live .claude.json carried the wrong oauthAccount, and the
# save-back persisted it over current's canonical identity. The credentials clobber
# rides the "unknown" verdict (a rotation-shaped token that matches no snapshot is
# assumed to be current's and saved back) on the same fully-drifted mount.
#
# THE FIX: meta.yaml + the login families are the TRUSTED ANCHOR precisely because
# the buggy path never writes them. Before any save-back into a canonical store we
# verify the incoming identity against that anchor; a mismatch is REFUSED and the
# rejected bytes are quarantined to a sidecar instead of overwriting the good store.
# ---------------------------------------------------------------------------

def _meta_anchor_identity(account: str) -> dict:
    """Identity facets from an account's meta.yaml — a TRUSTED anchor source.

    meta.yaml records oauth_account_uuid + oauth_email at `cus init` time and is
    NOT rewritten by the swap/save-back path, so in the 2026-07-06 clobber it
    stayed correct (rayi1) while the canonical .claude.json lied (rayi2). That
    write-path asymmetry is the whole reason meta anchors and .claude.json is the
    suspect. 'unknown'/'unknown (run /login)' placeholders (a freshly `cus add`ed
    account that never logged in) count as ABSENT — there is no identity to
    enforce yet, so we must not treat the placeholder as an anchor."""
    meta = account_meta(account)
    out: dict = {}
    uuid = meta.get("oauth_account_uuid")
    if isinstance(uuid, str) and uuid and uuid != "unknown":
        out["accountUuid"] = uuid
    email = meta.get("oauth_email")
    if isinstance(email, str) and email and not email.startswith("unknown"):
        out["emailAddress"] = email
    return out


def _family_anchor_identities(account: str) -> list[dict]:
    """Identity facets recorded by every independent-login family for `account`
    (GH #109) — a second TRUSTED anchor witness. Each family dir's .claude.json
    was written by a real browser `/login` AS that account, a path the buggy
    save-back never touches, so in the incident the families held rayi1's true
    identity while the canonical store was clobbered."""
    out: list[dict] = []
    for fam in list_login_families(account):
        ident = family_identity(account, fam)
        if ident:
            out.append(ident)
    return out


def account_identity_anchor(account: str) -> tuple[dict | None, str]:
    """The AUTHORITATIVE identity for `account`, reconciled ONLY from sources the
    swap/save-back path does not write (so the clobber bug cannot poison the
    anchor itself): meta.yaml (`cus init`-time) and the independent-login families
    (browser-`/login`-time). Deliberately does NOT trust
    account-<X>/.claude.json — that is the exact file 2026-07-06 showed can be
    overwritten with a WRONG account's oauthAccount while meta + families stay
    correct.

    Returns (identity | None, source):
      - (identity, "meta+families")  meta and every family agree — strongest.
      - (identity, "meta")           meta present, no families to cross-check.
      - (identity, "families")       meta absent/placeholder, families agree.
      - (None, "ambiguous: ...")     the witnesses CONTRADICT each other. The
        account is already inconsistent; callers must refuse the write and raise
        SOS rather than pick a side and compound the damage (task's meta-missing/
        ambiguous → refuse rule).
      - (None, "no-anchor: ...")     no trustworthy evidence at all (never logged
        in / feature unused). Callers treat this as "cannot prove a mismatch" and
        fall through to prior behavior — refusing here would break legitimate
        onboarding / no-op writes and every existing test that has no meta.yaml.
    """
    meta = _meta_anchor_identity(account)
    fams = _family_anchor_identities(account)
    # Hard contradiction between meta and any family → the account's own trusted
    # witnesses disagree; don't guess which is right.
    for fam in fams:
        if meta and _identities_match(meta, fam) is False:
            return (None, f"ambiguous: meta.yaml {meta} disagrees with a login family {fam}")
    # Families disagreeing among themselves is the same don't-guess situation.
    for i in range(1, len(fams)):
        if _identities_match(fams[0], fams[i]) is False:
            return (None, f"ambiguous: login families disagree ({fams[0]} vs {fams[i]})")
    if meta and fams:
        # Merge so the anchor carries every facet either witness knows (e.g.
        # meta has the uuid, a family also supplies the email).
        merged = dict(meta)
        for fam in fams:
            for k, v in fam.items():
                merged.setdefault(k, v)
        return (merged, "meta+families")
    if meta:
        return (meta, "meta")
    if fams:
        merged_f: dict = {}
        for fam in fams:
            for k, v in fam.items():
                merged_f.setdefault(k, v)
        return (merged_f, "families")
    return (None, "no-anchor: meta.yaml has no oauth uuid/email and no login families")


def guard_canonical_identity_write(account: str, incoming_identity: dict) -> tuple[bool, str]:
    """Single choke-point: may `incoming_identity` be written into
    account-<account>'s canonical store? Returns (allow, reason).

    Closes the 2026-07-06 wrong-account clobber (see the block comment above).
    Policy — the anchor is account_identity_anchor(account):
      - anchor present + incoming MATCHES anchor  → allow.
      - anchor present + incoming MISMATCH        → REFUSE (the incident).
      - anchor present + no shared fields          → allow: cannot disprove (e.g.
        meta carries only a uuid and the incoming payload only an email).
      - anchor AMBIGUOUS (witnesses disagree)     → REFUSE: the account is
        already poisoned; writing more only compounds it.
      - no anchor at all / empty incoming          → allow: nothing to prove a
        mismatch against, and refusing would break legitimate onboarding /
        no-op writes (and every test with no meta.yaml). Degrade-to-allow keeps
        the guard strictly non-breaking where there is no evidence.
    """
    if not incoming_identity:
        return (True, "no identity in incoming payload — nothing to verify")
    anchor, source = account_identity_anchor(account)
    if anchor is None:
        if source.startswith("ambiguous"):
            return (False, f"'{account}' identity anchor is self-inconsistent ({source})")
        return (True, f"no trustworthy anchor for '{account}' ({source}) — cannot prove a mismatch")
    if _identities_match(incoming_identity, anchor) is False:
        return (False,
                f"incoming identity {incoming_identity} != '{account}' trusted anchor {anchor} ({source})")
    return (True, f"incoming identity matches '{account}' anchor ({source})")


def account_canonical_store_poisoned(account: str) -> tuple[bool, str]:
    """True iff account-<X>'s canonical .claude.json identity CONTRADICTS its
    trusted anchor (meta.yaml + login families) — the 2026-07-06 clobber shape,
    where the canonical store is the WRONG side and meta/families are right.

    Returns (poisoned, detail). Used by (a) the swap-target picker, to HARD-
    exclude a poisoned account (installing its snapshot would spread the wrong
    creds onto the live mount) and (b) SOS, to name the CANONICAL store as the
    clobbered side and point at `cus relogin`. Conservative: an absent anchor or
    absent canonical file is NOT poisoned (nothing to contradict) — only a live,
    hard disagreement counts."""
    anchor, source = account_identity_anchor(account)
    if anchor is None:
        return (False, f"no trustworthy anchor to check against ({source})")
    canonical = account_canonical_identity(account)  # reads account-<X>/.claude.json
    if not canonical:
        return (False, "canonical .claude.json absent or carries no identity")
    if _identities_match(canonical, anchor) is False:
        return (True,
                f"canonical .claude.json {canonical} != trusted anchor {anchor} ({source})")
    return (False, f"canonical store matches anchor ({source})")


def quarantine_rejected_canonical_write(dest_path: Path, incoming_bytes: bytes, why: str) -> Path:
    """Divert a REFUSED canonical-store write to a sidecar next to the target
    file instead of overwriting it: the good canonical store is left untouched
    and the wrong-account bytes are preserved for forensics rather than lost.
    Returns the sidecar path. Best-effort — a failed sidecar write still means
    the overwrite was refused (the load-bearing half)."""
    ts = time.strftime("%Y%m%dT%H%M%S")
    tag = hashlib.sha256((why + ts).encode()).hexdigest()[:8]
    sidecar = dest_path.with_name(dest_path.name + f".rejected-{tag}-{ts}")
    try:
        atomic_write_bytes(sidecar, incoming_bytes, mode=0o600)
    except OSError:
        pass
    return sidecar


# ---------------------------------------------------------------------------
# CRED-AUDIT — one greppable structured line per credential-store write/refuse
#
# WHY THIS EXISTS (the account-rayi1 clobber, verified from daemon.log): two
# lanes shared ONE OAuth login family (divergence), so a lane's live mount
# carried a FOREIGN account's rotated token, and a routine save-back wrote that
# foreign token+identity into the WRONG account's canonical .claude.json /
# .credentials.json. The GH #77 freshness guard passed it (the foreign token was
# genuinely newer); only #172's identity guard now refuses it. daemon.log DID
# capture enough to reconstruct the incident — but only across scattered,
# differently-shaped human lines. The next incident should be ONE grep away.
#
# THE CONTRACT: every place that WRITES or REFUSES a credential/identity store
# emits exactly one `CRED-AUDIT ...` line — same stream as the human-readable
# daemon.log lines (click.echo, captured by systemd), ALONGSIDE (never instead
# of) those lines. Grep `CRED-AUDIT` to see every store mutation the daemon ever
# attempted; grep `CRED-AUDIT decision=refused` / `op=divergence-detected` to
# see the near-misses and the preconditions that set them up.
#
# SCHEMA (fields are space-separated key=value; only non-empty fields appear,
# but op and decision are always present so the line is stable to grep):
#   op=            saveback | swap-install | family-claim | refresh |
#                  freshness-skip | identity-refuse | family-collision-refuse |
#                  divergence-detected
#   slot= / mount= the SOURCE mount the creds came from (a lane slot dir or the
#                  shared ~/.claude mount)
#   account=       the TARGET store being written / refused
#   incoming_identity=  uuid(short)/email of the creds about to be written
#   existing_identity=  uuid(short)/email currently in the target store's anchor
#                  — a mismatch between these two IS the clobber, visible at a glance
#   login_family=  the login family involved (pool lease / claimed family)
#   shared=        true/false — is this family already live on ANOTHER mount
#                  (the divergence signal that precedes a clobber)
#   token_fp=      short non-reversible fingerprint of the refresh token (never
#                  the raw token) so two families can be told apart in the log
#   decision=      wrote | skipped-freshness | refused-identity |
#                  refused-collision | quarantined | detected
#   reason=        short human phrase (quoted) explaining the decision
#
# NEVER log raw access/refresh token bytes — only identity facets (uuid/email)
# and the short refresh-token fingerprint the rest of the module already uses.
# ---------------------------------------------------------------------------

CRED_AUDIT_PREFIX = "CRED-AUDIT"


def _fmt_audit_identity(ident: dict | None) -> str:
    """Render identity facets compactly for a CRED-AUDIT line as `uuid(short)/email`.

    Only the account-identity facets (accountUuid + emailAddress) — never tokens.
    A short uuid prefix keeps the line greppable without dumping the full 36-char
    id; the email disambiguates. Empty/absent identity renders as `none` so the
    field is always present and stable to grep."""
    if not ident:
        return "none"
    uuid = ident.get("accountUuid")
    email = ident.get("emailAddress")
    short = uuid[:8] if isinstance(uuid, str) and uuid else "?"
    return f"{short}/{email or '?'}"


def _audit_token_fp(creds: Any) -> str:
    """Short, non-reversible refresh-token fingerprint for a CRED-AUDIT line.

    Reuses `_refresh_fingerprint` (the same sha256-prefix primitive #109 uses to
    tell login families apart) so the audit log and the divergence detectors
    speak the same fingerprint. Returns `none` when there is no refresh token to
    fingerprint (blank/logout-shaped creds). NEVER emits the raw token."""
    try:
        rt = _credential_refresh_token(creds)
    except (AttributeError, TypeError):
        rt = None
    return _refresh_fingerprint(rt) if rt else "none"


def _cred_audit(op: str, decision: str, reason: str = "", *,
                slot: str | None = None, mount: str | None = None,
                account: str | None = None,
                incoming_identity: dict | None = None,
                existing_identity: dict | None = None,
                emit_identity: bool = False,
                login_family: str | None = None,
                shared: bool | None = None,
                token_fp: str | None = None,
                extra: str | None = None) -> None:
    """Emit ONE structured CRED-AUDIT line describing a credential-store
    write/refuse/detection. See the schema block above.

    Purely additive observability: this NEVER changes control flow and MUST
    NEVER raise into its caller — a swap/save-back must not fail because a log
    line couldn't be formatted, so the whole body is wrapped defensively. Uses
    the same click.echo stream as the neighbouring human-readable daemon.log
    lines (do not invent a new file); the human line stays, this rides alongside.

    emit_identity=True forces the incoming/existing identity fields to appear
    even when empty (rendered `none`) — used at write/refuse points where the
    identity comparison is the load-bearing evidence. Detection lines (which name
    mounts + a shared family instead of a single identity pair) leave it False.
    """
    try:
        parts = [f"{CRED_AUDIT_PREFIX} op={op}"]
        if slot:
            parts.append(f"slot={slot}")
        if mount:
            parts.append(f"mount={mount}")
        if account:
            parts.append(f"account={account}")
        if emit_identity or incoming_identity is not None:
            parts.append(f"incoming_identity={_fmt_audit_identity(incoming_identity)}")
        if emit_identity or existing_identity is not None:
            parts.append(f"existing_identity={_fmt_audit_identity(existing_identity)}")
        if login_family:
            parts.append(f"login_family={login_family}")
        if shared is not None:
            parts.append(f"shared={'true' if shared else 'false'}")
        if token_fp:
            parts.append(f"token_fp={token_fp}")
        if extra:
            parts.append(extra)
        parts.append(f"decision={decision}")
        if reason:
            # Quote + cap the reason so it stays one greppable field even though
            # guard reasons embed dict reprs / '!=' with internal spaces.
            r = reason if len(reason) <= 240 else reason[:237] + "..."
            parts.append(f'reason="{r}"')
        click.echo(" ".join(parts))
    except Exception:  # noqa: BLE001 — observability must never break a swap
        pass


def _recover_pending_swap() -> None:
    """Detect and reconcile a swap that crashed mid-flight (GH #76).

    Caller MUST hold the swap lock (execute_swap and the daemon startup check
    both do). No-op when no journal exists — the overwhelmingly common path.

    Reconciliation logic: the journal says a swap `from` → `to` was in
    flight. Determine how far it got by comparing the live ~/.claude.json
    identity against both accounts' stored identities (with credentials
    refresh-token lineage as a fallback when identity evidence is missing):

      - live == `to`:   the live mutation completed but state.json may not
        have been persisted (crash between install and save_state). Repair:
        finish the install if the live creds are still `from`'s (crash in the
        millisecond window between identity write and creds copy), then set
        state.active = to. This is exactly the #70 "second layer" desync that
        would otherwise poison the NEXT swap's save-back.
      - live == `from`: the crash happened before the live mutation — nothing
        durable changed hands. state.active already says `from`; just clear
        the journal (the interrupted swap simply never happened).
      - indeterminate:  don't guess. Warn loudly with exact manual recovery
        steps, keep the evidence (journal renamed *.stale.<ts>, preserving
        the log per repo discipline), and let the operator reconcile.
    """
    journal = _swap_journal_path()
    if not journal.exists():
        return

    stale_reason: str | None = None
    j: Any = None
    try:
        j = read_json(journal)
    except (json.JSONDecodeError, OSError) as e:
        stale_reason = f"journal unreadable ({e})"
    frm = j.get("from") if isinstance(j, dict) else None
    to = j.get("to") if isinstance(j, dict) else None
    slot = j.get("slot") if isinstance(j, dict) else None
    if stale_reason is None and not to:
        stale_reason = "journal has no 'to' account"

    # per_session: a slot journal means the crashed swap was against a slot
    # mount, so ALL live-file evidence comes from that mount — the global
    # ~/.claude/ pair is a different mount and says nothing about this swap.
    if slot:
        _slot_dir = slot_path(slot)
        live_cj_path = mount_claude_json_path(_slot_dir)
        live_creds_path = mount_creds_path(_slot_dir)
    else:
        live_cj_path = CLAUDE_JSON
        live_creds_path = CREDS_JSON

    landed: str | None = None
    if stale_reason is None:
        try:
            live_ids = _identity_fields(read_json(live_cj_path)) if live_cj_path.exists() else {}
        except (json.JSONDecodeError, OSError):
            live_ids = {}
        m_to = _identities_match(live_ids, _dir_identity(to))
        m_frm = _identities_match(live_ids, _dir_identity(frm)) if frm else None
        if m_to and not m_frm:
            landed = "to"
        elif m_frm and not m_to:
            landed = "from"
        elif m_to and m_frm:
            stale_reason = (f"live identity matches BOTH '{frm}' and '{to}' "
                            f"(duplicate-identity accounts? see GH #70) — refusing to guess")
        else:
            # Identity evidence inconclusive (missing/corrupt .claude.json
            # somewhere). Fall back to credentials refresh-token lineage.
            try:
                live_creds = read_json(live_creds_path) if live_creds_path.exists() else None
            except (json.JSONDecodeError, OSError):
                live_creds = None
            if live_creds is not None:
                verdict, owner, _ = classify_live_creds_owner(live_creds, to, load_state())
                if verdict == "expected":
                    landed = "to"
                elif verdict == "foreign" and owner == frm:
                    landed = "from"
            if landed is None and slot and frm is None:
                # Swap-into-EMPTY-slot crash with no evidence the install
                # landed: the mount holds no (or unidentifiable) credentials
                # and its .claude.json carries no identity. Nothing durable
                # changed hands — same as the global landed=="from" case,
                # except there is no 'from' account to point state at.
                landed = "from"
            if landed is None:
                stale_reason = "live files match neither the 'from' nor the 'to' account"

    if stale_reason is not None:
        ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
        stale_path = journal.with_name(f"{journal.name}.stale.{ts}")
        msg = (
            f"UNRESOLVED crashed swap detected ({frm!r} -> {to!r}): {stale_reason}. "
            f"Evidence preserved at {stale_path}. Manual recovery steps: "
            f"(1) `cus whoami` to see which account the live files actually hold; "
            f"(2) compare with `grep '\"active\"' {STATE_JSON}`; "
            f"(3) if they disagree, edit state.json's \"active\" to the account the live files hold "
            f"(record reality — do NOT run a swap first, its save-back would write one account's "
            f"tokens into another's snapshot); "
            f"(4) `cus poll` to refresh; "
            f"(5) if a snapshot was clobbered, `cus restore-creds <name> --list` (GH #79 backups)."
        )
        click.echo(f"swap-journal: {msg}", err=True)
        try:
            os.replace(journal, stale_path)
        except OSError:
            pass
        try:
            append_inbox("crash-recovery", "unresolved crashed swap — manual reconciliation needed", msg)
        except OSError:
            pass
        return

    if landed == "to":
        # Crash after the live mutation (or mid-way through it): finish the
        # job. If the live creds still carry `from`'s lineage — or, for a
        # slot mount, are missing entirely (empty-slot install crashed
        # between the identity write and the creds copy) — complete the
        # install from `to`'s snapshot.
        target_creds = ACCOUNTS_DIR / f"account-{to}" / ".credentials.json"
        try:
            live_creds = read_json(live_creds_path) if live_creds_path.exists() else None
        except (json.JSONDecodeError, OSError):
            live_creds = None
        state = load_state()
        installed_note = ""
        if live_creds is not None and target_creds.exists():
            verdict, _owner, _ = classify_live_creds_owner(live_creds, to, state)
            if verdict == "foreign":
                backup_credentials_file(live_creds_path)   # GH #79 choke point
                atomic_copy(target_creds, live_creds_path, mode=0o600)
                installed_note = " (live creds still held the outgoing account's tokens; completed the install)"
        elif live_creds is None and slot and target_creds.exists():
            atomic_copy(target_creds, live_creds_path, mode=0o600)
            installed_note = " (slot had no creds yet; completed the install)"
        if slot:
            entry = state.setdefault("slots", {}).setdefault(slot, {"account": None, "created_ts": now_iso()})
            if entry.get("account") != to:
                entry["account"] = to
                state.setdefault("swap_history", []).append({
                    "ts": now_iso(), "from": frm, "to": to, "trigger": "crash-recovery", "slot": slot,
                })
                save_state(state)
            where = f"slots.{slot}.account"
        else:
            if state.get("active") != to:
                state["active"] = to
                state.setdefault("swap_history", []).append({
                    "ts": now_iso(), "from": frm, "to": to, "trigger": "crash-recovery",
                })
                save_state(state)
            where = "state.json active"
        msg = (f"crashed swap {frm!r} -> {to!r} had completed its live mutation; "
               f"reconciled {where} -> {to!r}{installed_note}")
        click.echo(f"swap-journal: {msg}")
        try:
            append_inbox("crash-recovery", "crashed swap reconciled (live had moved)", msg)
        except OSError:
            pass
        _clear_swap_journal()
        return

    # landed == "from": nothing durable changed hands before the crash.
    state = load_state()
    if slot:
        entry = state.setdefault("slots", {}).get(slot)
        if entry is not None and frm is not None and entry.get("account") != frm:
            entry["account"] = frm
            save_state(state)
    elif frm and state.get("active") != frm:
        # Shouldn't happen (save_state is the LAST step), but if state points
        # elsewhere while live provably holds `from`, record reality.
        state["active"] = frm
        save_state(state)
    msg = (f"crashed swap {frm!r} -> {to!r} never reached the live mutation; "
           f"no repair needed (the interrupted swap simply did not happen)")
    click.echo(f"swap-journal: {msg}")
    try:
        append_inbox("crash-recovery", "crashed swap cleared (nothing had moved)", msg)
    except OSError:
        pass
    _clear_swap_journal()


def _creds_expires_at(creds: Any) -> int | float | None:
    """Extract claudeAiOauth.expiresAt (Unix ms) from a credentials payload,
    tolerating any malformed shape. Used as the FRESHNESS discriminator for
    GH #77: identity/lineage checks can't tell fresh tokens from dead ones
    for the SAME account, but a re-login/refresh always advances expiresAt."""
    oauth = creds.get("claudeAiOauth") if isinstance(creds, dict) else None
    exp = oauth.get("expiresAt") if isinstance(oauth, dict) else None
    return exp if isinstance(exp, (int, float)) else None


def _live_mount_creds_invalid(creds: Any) -> bool:
    """True when a credentials payload carries no usable OAuth token.

    This is the detector for the 2026-07-05 blanked-live-mount incident: the
    live ~/.claude/.credentials.json was found with `expiresAt: 0` and an empty
    `accessToken` while the active account's snapshot stayed healthy, so a bare
    session on the shared mount showed "not logged in" and nothing else in
    diagnose() noticed. A file is "invalid" when it is unreadable / not a dict,
    or its OAuth block has an empty/missing accessToken OR a missing / <= 0
    expiresAt (the epoch/blank signature).

    Shape tolerance: the token block is usually nested under `claudeAiOauth`
    (the shape every other reader in this file assumes), but we fall back to a
    flat top-level shape so a differently-serialized file isn't misread as
    valid. `bool` is excluded from the numeric expiresAt check because it is an
    `int` subclass — a stray `expiresAt: true` must not read as a positive
    epoch.
    """
    if not isinstance(creds, dict):
        return True  # missing / corrupt / non-dict → unusable
    oauth = creds.get("claudeAiOauth")
    if not isinstance(oauth, dict):
        oauth = creds  # tolerate a flat top-level OAuth shape
    token = oauth.get("accessToken")
    exp = oauth.get("expiresAt")
    token_ok = isinstance(token, str) and token.strip() != ""
    exp_ok = isinstance(exp, (int, float)) and not isinstance(exp, bool) and exp > 0
    return not (token_ok and exp_ok)


def _snapshot_fresher_than_live(account_name: str) -> bool:
    """True when the account's storage snapshot holds strictly NEWER tokens
    (by claudeAiOauth.expiresAt) than the live ~/.claude/.credentials.json.

    This is the GH #77 signature: the user re-logged in under the storage
    dir (`CLAUDE_CONFIG_DIR=account-X/ claude`) while X was the ACTIVE
    account — fresh tokens landed in the snapshot, but Claude keeps reading
    the (dead) live file. Detection lets poll/SOS point at
    `cus relogin X --finish` instead of the generic re-login advice, and
    lets the swap save-back refuse to destroy the fresh snapshot.
    """
    snap_path = account_creds_path(account_name)
    if not snap_path.exists() or not CREDS_JSON.exists():
        return False
    try:
        snap_exp = _creds_expires_at(read_json(snap_path))
        live_exp = _creds_expires_at(read_json(CREDS_JSON))
    except (json.JSONDecodeError, OSError):
        return False
    return snap_exp is not None and live_exp is not None and snap_exp > live_exp


def classify_live_creds_owner(live_creds: dict, expected: str, state: dict) -> tuple[str, str | None, str]:
    """Identify which account the live credentials file actually belongs to (GH #3).

    Why this exists: a DRIFTED pane (its in-memory tokens are account X's while
    state.json says account Y is active) will eventually refresh its access
    token and write X's tokens over `~/.claude/.credentials.json`. If a swap
    then blindly saves the live file back into Y's storage snapshot, Y's
    snapshot — including its long-lived refresh token — is clobbered with X's
    tokens, and the corruption compounds on every subsequent swap.

    Identification strategy: the credentials file carries no explicit identity
    (no email / account uuid — just tokens), so we match on REFRESH TOKEN
    lineage. Refresh tokens are long-lived (~30 days) and stable across the
    hourly access-token refreshes, so `live.refreshToken == snapshot.refreshToken`
    is a reliable "same account" signal, and matching a *different* account's
    snapshot is definitive proof of drift. Access-token equality alone is NOT
    used as a signal: the live access token legitimately diverges from every
    snapshot within an hour of normal use.

    Returns (verdict, owner, detail):
      - ("expected", expected, ...)  live file provably belongs to `expected` —
        safe to save back into its snapshot.
      - ("foreign", X, ...)          live file provably belongs to account X !=
        expected — the GH #3 drift case. Do NOT save into expected's snapshot.
      - ("conflict", None, ...)      live refresh token matches MULTIPLE
        snapshots (duplicate-identity setup, see GH #70) — ownership is
        ambiguous, don't guess.
      - ("unknown", None, ...)       live refresh token matches no snapshot.
        Usually means the refresh endpoint ROTATED the active account's refresh
        token since the last swap (benign — the save-back is exactly how the
        rotated token gets persisted). Indistinguishable from a drifted pane
        whose refresh also rotated; callers should preserve the historical
        save-back behavior here (skipping would strand legitimately-rotated
        tokens, which is the worse failure).
      - ("invalid", None, ...)       live creds parse as JSON but carry NO
        claudeAiOauth.refreshToken (a `claude logout`-shaped file, an empty
        `{}`, or JSON that isn't even an object). There is nothing durable to
        save back — an access token alone expires within the hour and can't
        be renewed — so writing these bytes over a snapshot can only destroy
        its refresh token. Callers must SKIP the save-back, same as for
        unparseable JSON. (Review amendment 2026-07-01: this case originally
        fell through to "unknown" and clobbered the outgoing snapshot with
        tokenless garbage — the same snapshot-destruction class GH #3 exists
        to prevent, just via valid-JSON garbage instead of invalid.)
    """
    # Defensive extraction: any process can scribble on the live creds file,
    # and json.loads happily returns lists/strings/numbers — so never assume
    # dict shapes. An uncaught AttributeError here would crash the entire
    # swap (callers catch only FileNotFoundError/ValueError/RuntimeError).
    live_oauth = live_creds.get("claudeAiOauth") if isinstance(live_creds, dict) else None
    live_rt = live_oauth.get("refreshToken") if isinstance(live_oauth, dict) else None
    if not live_rt:
        return ("invalid", None,
                "live credentials carry no claudeAiOauth.refreshToken — nothing durable "
                "to save back (logout-shaped or malformed file)")

    matches: list[str] = []
    for name in state.get("accounts", {}):
        snap_path = account_creds_path(name)
        if not snap_path.exists():
            continue
        try:
            snap = read_json(snap_path)
        except (json.JSONDecodeError, OSError):
            # A corrupt/unreadable snapshot can't vote on ownership; skip it
            # rather than failing the whole swap.
            continue
        # Same non-dict paranoia as for the live file: a snapshot holding a
        # JSON list/string must not AttributeError the swap out from under us.
        snap_oauth = snap.get("claudeAiOauth") if isinstance(snap, dict) else None
        snap_rt = snap_oauth.get("refreshToken") if isinstance(snap_oauth, dict) else None
        if snap_rt and snap_rt == live_rt:
            matches.append(name)

    if len(matches) > 1:
        return ("conflict", None,
                f"live refresh token matches multiple account snapshots: {sorted(matches)} "
                f"(duplicate-identity accounts? see GH #70)")
    if len(matches) == 1:
        owner = matches[0]
        if owner == expected:
            return ("expected", owner, f"live refresh token matches '{owner}' snapshot")
        return ("foreign", owner,
                f"live refresh token matches '{owner}' snapshot, not active account "
                f"'{expected}' — a drifted pane's token refresh overwrote the live file (GH #3)")
    return ("unknown", None,
            "live refresh token matches no account snapshot (most likely the active "
            "account's refresh token was rotated since the last swap)")


def execute_swap(target_name: str, trigger: str = "manual", slot: str | None = None,
                 bump_ladder: bool = True) -> dict:
    """Atomically swap to `target_name`. Returns updated state dict.

    Shared between the CLI `cus switch` command and the daemon's auto-swap.
    Caller is responsible for any preflight (hot-swap orchestration, etc.).

    `slot` selects the live mount (per_session mode): None = the legacy
    global pair (~/.claude/ + ~/.claude.json) — today's behavior, bit-for-bit.
    A slot name = that slot dir; the identical two-file in-place swap runs
    against it, the session on top never restarts, and no other mount is
    touched. The outgoing account is the slot's occupant (state.slots), not
    state.active.

    Reads from + writes to the post-migration layout (.credentials.json +
    .claude.json inside each account-* dir). Pre-migration storage is
    auto-migrated on first read via migrate_account_dir().

    GH #76: the whole sequence runs under the global swap lock — every entry
    point (daemon reactive/background, switch, auto-swap, orchestrator
    sub-paths) is serialized here, at the primitive itself, so no caller can
    forget. Slot swaps share the same lock as global swaps: they never
    overlap in time, which keeps the journal/recovery model single-slot and
    is a non-cost (swaps are sub-second, rare). Crash recovery for a
    previously-interrupted swap runs first, under the same lock. Raises
    RuntimeError on lock timeout (the exception type every caller already
    catches).
    """
    with _swap_lock():
        _recover_pending_swap()
        return _execute_swap_locked(target_name, trigger, slot=slot, bump_ladder=bump_ladder)


def _execute_swap_locked(target_name: str, trigger: str, slot: str | None = None,
                         bump_ladder: bool = True) -> dict:
    """Inner swap sequence. Caller (execute_swap) holds the global swap lock."""
    # State is loaded AFTER the lock is acquired: a concurrent swap that just
    # finished has already persisted its state.json, so `current` below is
    # never a stale pre-lock snapshot (the #76 interleaving scenario).
    state = load_state()
    # Loaded once up front (not just for the ladder below): the #109 gate decides
    # both the creds save-back destination and the install source, both earlier
    # than the ladder block that historically owned this load.
    config = load_config()
    if target_name not in state["accounts"]:
        raise ValueError(f"Unknown account '{target_name}'. Known: {sorted(state['accounts'].keys())}")
    if slot is None:
        current = state["active"]
        if current is None:
            # GH #76 problem 2 edge: a hand-built / partially-initialized
            # state.json with active=None used to sail through, create
            # ~/claude-accounts/account-None/, mutate the LIVE files, and only
            # then crash on the threshold-ladder KeyError — after the damage.
            # Refuse up front, before any write.
            raise RuntimeError(
                "state.json has no active account (active=null) — refusing to swap: the save-back "
                "would create a bogus 'account-None' snapshot. Run `cus init` (or set \"active\" in "
                f"{STATE_JSON} to the account the live files currently hold).")
        live_cj_path = CLAUDE_JSON
        live_creds_path = CREDS_JSON
    else:
        mount_dir = slot_path(slot)
        if not mount_dir.exists():
            raise FileNotFoundError(f"slot dir {mount_dir} missing — `cus slot create` (or `cus launch`) first")
        slot_entry = state.setdefault("slots", {}).setdefault(slot, {"account": None, "created_ts": now_iso()})
        # A slot's outgoing account may legitimately be None: a swap into an
        # empty slot is `cus launch`'s install primitive. No save-back, no
        # ladder bump — there is no outgoing account to protect.
        current = slot_entry.get("account")
        live_cj_path = mount_claude_json_path(mount_dir)
        live_creds_path = mount_creds_path(mount_dir)
    if target_name == current:
        return state

    target_dir = ACCOUNTS_DIR / f"account-{target_name}"
    current_dir = ACCOUNTS_DIR / f"account-{current}" if current is not None else None

    # Auto-migrate if dir is in old layout
    migrate_account_dir(target_dir)
    if current_dir is not None:
        migrate_account_dir(current_dir)

    # GH #109: `target_creds` is the shared per-account snapshot. It is the
    # DEFAULT install source below (the `atomic_copy(install_src, ...)`), but
    # when the Phase 2 gate is on and a slot swap has an independent login for
    # (target, slot), swap_install_source substitutes that login's own family so
    # two live mounts on one account don't clobber on rotation (#104/#103/#3).
    # Gate off (default) => install_src == target_creds, i.e. today's copy path.
    target_creds = target_dir / ".credentials.json"
    target_cj = target_dir / ".claude.json"
    if not target_creds.exists():
        raise FileNotFoundError(f"{target_creds} missing — re-run `cus init --force`")
    if not target_cj.exists():
        raise FileNotFoundError(f"{target_cj} missing — re-run `cus init --force`")

    # ---- GH #141 root-cause guard (early / no-partial-state path) ----
    # The 2026-07-05 blanked-live-mount incident (3× in one day) traced to a
    # swap installing the TARGET account's snapshot into the live mount even
    # when that snapshot itself was BLANK — empty accessToken + expiresAt<=0
    # (hypothesis a). A blanked live ~/.claude/.credentials.json logs out every
    # bare session on the shared mount. Refusing HERE — before the swap journal,
    # save-back, or any live write below — leaves the live mount bit-for-bit
    # untouched (its current, valid creds survive), which is exactly the
    # "keep the current valid creds rather than install a blank" contract. The
    # daemon's picker moves to a different target on the next cycle.
    #
    # Scoped to the DEFAULT (non-pool) path: with independent logins off the
    # install source below is always `target_creds` (the snapshot), so checking
    # it here is precise. When the pool gate is on the install source is chosen
    # later (a claimed login family, possibly valid even if the snapshot is not),
    # so that path relies on the definitive guard right before the atomic_copy.
    # Reuses `_live_mount_creds_invalid` — the same predicate the detector/SOS use.
    if not independent_logins_enabled(config):
        try:
            _target_snapshot_creds = read_json(target_creds)
        except (json.JSONDecodeError, OSError) as e:
            raise RuntimeError(
                f"refusing to swap to '{target_name}': its snapshot {target_creds} is unreadable "
                f"({e}) — installing it would blank the live mount and lock out bare sessions "
                f"(GH #141). Re-login (`cus relogin {target_name}`) or restore a backup "
                f"(`cus restore-creds {target_name}`).")
        if _live_mount_creds_invalid(_target_snapshot_creds):
            raise RuntimeError(
                f"refusing to swap to '{target_name}': its snapshot credentials are blank/expired "
                f"(empty accessToken or expiresAt<=0) — installing them would blank the live mount "
                f"and lock out bare sessions (GH #141). Re-login (`cus relogin {target_name}`) or "
                f"restore a backup (`cus restore-creds {target_name}`).")

    if live_cj_path.exists():
        # Read the live .claude.json when present — its non-account keys (MCP
        # registrations, per-project state, sync'd from canonical) must survive
        # the install, so we merge the target identity INTO it rather than
        # replacing it.
        try:
            live_cj = read_json(live_cj_path)
        except json.JSONDecodeError as e:
            raise RuntimeError(f"{live_cj_path} unparseable ({e}) — Claude may be mid-write. Retry in 1s.")
    elif current is None:
        # Swap-into-EMPTY-slot install (cus launch's primitive) with no
        # .claude.json yet (freshly-scaffolded slot, crash between mkdir and
        # seed): start from {}. There is no outgoing account, so nothing is
        # saved back and nothing is lost.
        live_cj = {}
    else:
        # OCCUPIED slot (current set) whose live .claude.json is unexpectedly
        # missing. Do NOT substitute {}: the save-back below would write a
        # bogus empty identity snapshot for the outgoing account (the exact
        # corruption the credentials path guards against a few lines down —
        # review finding 2026-07-02, swap-correctness dimension). Refuse, same
        # discipline as the missing-credentials guard.
        raise FileNotFoundError(
            f"{live_cj_path} missing — cannot save the outgoing account's identity back to "
            f"storage (occupied slot with no live .claude.json; run `cus doctor --fix-dirs`)")

    # GH #76: write the intent journal BEFORE the first mutating step. From
    # here to the post-save_state clear, a crash leaves the journal on disk
    # and _recover_pending_swap reconciles on the next swap / daemon start.
    _write_swap_journal(current, target_name, trigger, slot=slot)

    # Save current identity + creds back to current's storage — skipped
    # entirely for a swap into an EMPTY slot (current is None: nothing to
    # save, nowhere to save it). We update the ENTIRE .claude.json in the
    # source dir (not just account-bound keys) so that any per-account state
    # Claude writes there persists.
    #
    # ---- 2026-07-06 wrong-account clobber guard (identity save-back) ----
    # The live mount's identity is verified against current's TRUSTED anchor
    # (meta.yaml + login families — see guard_canonical_identity_write) BEFORE
    # it is persisted. A drifted mount (a session that logged in as / drifted to
    # another account) carries a wrong oauthAccount in its live .claude.json;
    # writing it here is exactly how account-rayi1/.claude.json ended up holding
    # rayi2's identity. `identity_saveback_ok` is also consumed by the credentials
    # save-back below: a mount whose IDENTITY is foreign is not a place we trust an
    # "unknown"-verdict token from either.
    identity_saveback_ok = True
    if current is not None:
        assert current_dir is not None  # narrowed: current_dir is set iff current is
        current_identity = {k: live_cj[k] for k in ACCOUNT_BOUND_KEYS if k in live_cj}
        identity_saveback_ok, _guard_reason = guard_canonical_identity_write(
            current, _identity_fields(live_cj))
        if not identity_saveback_ok:
            # REFUSE: keep current's existing canonical .claude.json untouched and
            # quarantine the rejected (wrong-account) identity next to it. Losing
            # the outgoing account's own state-key updates for this one swap is far
            # cheaper than clobbering its identity with another account's.
            sidecar = quarantine_rejected_canonical_write(
                current_dir / ".claude.json",
                json.dumps({k: live_cj.get(k) for k in ACCOUNT_BOUND_KEYS}, indent=2).encode(),
                _guard_reason)
            msg = (f"WRONG-ACCOUNT GUARD at swap ({current} -> {target_name}): refused to save the live "
                   f"mount's identity into '{current}' canonical .claude.json — {_guard_reason}. "
                   f"Kept the existing canonical identity; quarantined the rejected one to {sidecar.name}. "
                   f"The live mount has DRIFTED to another account; restart that session. If '{current}'s "
                   f"own canonical store looks wrong, re-login it: `cus relogin {current}`.")
            click.echo(f"creds-save-back: {msg}")
            _cred_audit("identity-refuse", "refused-identity", _guard_reason,
                        slot=slot, mount=(slot or "shared-mount"), account=current,
                        incoming_identity=_identity_fields(live_cj), emit_identity=True,
                        existing_identity=account_identity_anchor(current)[0],
                        extra="quarantined=" + sidecar.name)
            try:
                append_inbox("wrong-account-guard",
                             f"identity save-back refused for '{current}' (drifted live mount)", msg)
            except OSError:
                pass  # inbox is best-effort observability; never fail a swap over it
        else:
            # Read existing per-account .claude.json (if any), overlay current account-bound keys
            if (current_dir / ".claude.json").exists():
                existing = read_json(current_dir / ".claude.json")
            else:
                existing = {}
            existing.update(current_identity)
            write_json(current_dir / ".claude.json", existing)
            # Canonical identity write accepted — record the incoming vs anchor
            # identity so a future clobber (incoming != existing) is one grep away.
            _cred_audit("saveback", "wrote", "identity save-back to canonical .claude.json",
                        slot=slot, mount=(slot or "shared-mount"), account=current,
                        incoming_identity=_identity_fields(live_cj), emit_identity=True,
                        existing_identity=account_identity_anchor(current)[0])

    # ---- GH #3: refresh-time drift detection on the credentials save-back ----
    # The live file is read into memory EXACTLY ONCE and those same bytes are
    # what gets written to storage. This closes the read-check-write race: a
    # pane's token refresh can rewrite the live file at any moment, and a
    # naive "check the file, then atomic_copy the file" would validate one
    # version and copy another. (atomic_write_bytes still makes the *write*
    # atomic; the residual race — a refresh landing between this read and the
    # target-creds install below — existed before this change and only loses
    # at most one access-token refresh, never a snapshot's lineage.)
    if current is not None and not live_creds_path.exists():
        # Preserve pre-GH#3 behavior: atomic_copy(live_creds_path, ...) raised
        # FileNotFoundError here, and `switch`/daemon callers handle exactly
        # that exception type.
        raise FileNotFoundError(f"{live_creds_path} missing — cannot save active account's tokens back to storage")
    live_creds = None
    live_creds_bytes = b""
    if current is not None:
        live_creds_bytes = live_creds_path.read_bytes()
        try:
            live_creds = json.loads(live_creds_bytes)
        except json.JSONDecodeError as e:
            # Pre-GH#3 this copied the unparseable bytes into current's snapshot
            # verbatim, destroying a known-good snapshot with garbage (Claude
            # mid-write, disk corruption, ...). Skipping the save-back is strictly
            # safer: the snapshot keeps its last-known-good tokens, and the swap
            # itself still proceeds (the target install below overwrites the
            # corrupt live file with a good one — which also happens to repair it).
            live_creds = None
            click.echo(f"creds-save-back: SKIPPED — live {live_creds_path} unparseable ({e}); "
                       f"keeping '{current}' snapshot as-is")
    # Phase 3a (#109): if this mount runs a per-(current, slot) INDEPENDENT
    # login family (gate on + a login provisioned for the OUTGOING account on
    # THIS slot), its live refresh token matches no account snapshot by design.
    # Route the save-back to its own store entry so classify_live_creds_owner
    # below never sees it, verdicts "unknown", and clobbers current's shared
    # snapshot with the independent family (handoff §5.5). Gated: with the gate
    # off this branch is never taken and behavior is unchanged.
    _lease = slot_leased_family(state, slot) if slot is not None else None
    if (live_creds is not None and slot is not None and current is not None
            and independent_logins_enabled(config) and _lease is not None and _lease[0] == current):
        # Pool model: this mount runs a LEASED family for the outgoing account.
        # Its live refresh token belongs to that family (not the account
        # snapshot), so save it straight back to the family dir — never through
        # classify_live_creds_owner, which would clobber the shared snapshot.
        _fam = _lease[1]
        status_word = saveback_to_login_store(current, slot, live_creds, live_creds_bytes,
                                              dest_path=login_family_creds_path(current, _fam))
        if status_word == "skipped-stale":
            click.echo(f"creds-save-back: SKIPPED — stored family {current}/{_fam} is newer than "
                       f"the live file; kept the store entry (GH #77-style guard)")
            _cred_audit("freshness-skip", "skipped-freshness", "family store fresher than live (#77)",
                        slot=slot, account=current, login_family=f"{current}/{_fam}",
                        token_fp=_audit_token_fp(live_creds))
        else:
            click.echo(f"creds-save-back: saved {slot} live tokens back to pooled login {current}/{_fam} (#109 pool)")
            _cred_audit("saveback", "wrote", "pooled family store (#109)",
                        slot=slot, account=current, login_family=f"{current}/{_fam}",
                        token_fp=_audit_token_fp(live_creds))
    elif (live_creds is not None and slot is not None and current is not None
            and independent_logins_enabled(config) and has_independent_login(current, slot)):
        # Legacy per-(slot, account) store (superseded by the pool; retained
        # until the P5 cleanup so nothing that still keys per-slot regresses).
        status_word = saveback_to_login_store(current, slot, live_creds, live_creds_bytes)
        if status_word == "skipped-stale":
            click.echo(f"creds-save-back: SKIPPED — stored login for ({slot}, {current}) is newer "
                       f"than the live file; kept the store entry (GH #77-style guard)")
            _cred_audit("freshness-skip", "skipped-freshness", "legacy per-slot store fresher than live (#77)",
                        slot=slot, account=current, token_fp=_audit_token_fp(live_creds))
        else:
            click.echo(f"creds-save-back: saved ({slot}, {current}) independent login back to its store entry (GH #109)")
            _cred_audit("saveback", "wrote", "legacy per-slot independent login store (#109)",
                        slot=slot, account=current, token_fp=_audit_token_fp(live_creds))
    elif live_creds is not None:
        # live_creds is only populated when current is not None (read a few lines
        # up), which in turn guarantees current_dir is a real path — assert it so
        # the snapshot writes below type-check.
        assert current_dir is not None
        verdict, owner, detail = classify_live_creds_owner(live_creds, current, state)
        if verdict in ("expected", "unknown"):
            # "expected": provably current's tokens — the normal save-back.
            # "unknown": no snapshot matched, which is what a legitimate
            # refresh-token rotation on the active account looks like; the
            # save-back is the only way that rotated token gets persisted, so
            # keep the historical behavior (see classify_live_creds_owner).
            #
            # ---- GH #77: freshness guard ----
            # Identity/lineage says these live tokens may be saved back — but
            # if the SNAPSHOT's expiresAt is strictly newer than the live
            # file's, the snapshot was re-logged-in or refreshed out-of-band
            # (the #77 scenario: user ran the storage-dir re-login while this
            # account was ACTIVE, so the fresh tokens landed in the snapshot
            # and the live file still holds the dead ones). Overwriting would
            # silently destroy the freshly-minted refresh token at the exact
            # moment the user is trying to recover from an outage. Note #72's
            # identity guard structurally cannot catch this: same account,
            # same-or-rotated lineage — freshness, not identity, is the
            # discriminator here. The guard compares only when both sides
            # carry a numeric expiresAt; missing/garbled data falls through
            # to the historical save-back.
            snap_exp = None
            live_exp = _creds_expires_at(live_creds)
            snap_path = current_dir / ".credentials.json"
            if live_exp is not None and snap_path.exists():
                try:
                    snap_exp = _creds_expires_at(read_json(snap_path))
                except (json.JSONDecodeError, OSError):
                    snap_exp = None  # unreadable snapshot can't claim freshness
            if snap_exp is not None and live_exp is not None and snap_exp > live_exp:
                msg = (f"FRESHNESS GUARD at swap ({current} -> {target_name}): '{current}' snapshot's tokens "
                       f"are NEWER than the live file's (snapshot expiresAt {int(snap_exp)} > live {int(live_exp)}) "
                       f"— the snapshot was re-logged-in/refreshed out-of-band (GH #77). Skipped the save-back "
                       f"so the fresh tokens survive; the dead live tokens are discarded by the target install. "
                       f"(If '{current}' polls as token_expired, run `cus relogin {current} --finish` "
                       f"before swapping back, or just swap to it — the snapshot is the fresh copy.)")
                click.echo(f"creds-save-back: {msg}")
                _cred_audit("freshness-skip", "skipped-freshness",
                            f"snapshot fresher than live (#77): snap {int(snap_exp)} > live {int(live_exp)}",
                            slot=slot, mount=(slot or "shared-mount"), account=current,
                            token_fp=_audit_token_fp(live_creds))
                try:
                    append_inbox("freshness-guard", f"save-back skipped — '{current}' snapshot fresher than live", msg)
                except OSError:
                    pass  # inbox is best-effort observability; never fail a swap over it
            elif verdict == "unknown" and not identity_saveback_ok:
                # ---- 2026-07-06 wrong-account clobber guard (credentials) ----
                # "unknown" = no refresh-token lineage evidence (rotation-shaped),
                # so the historical behavior assumes the tokens are current's. But
                # when the SAME mount's .claude.json identity was just refused as
                # foreign (identity_saveback_ok False), this is a fully-DRIFTED
                # mount, not a rotation — its tokens belong to another account, and
                # saving them into current's snapshot is the credentials half of
                # the recurring clobber. Refuse + quarantine instead of guessing.
                sidecar = quarantine_rejected_canonical_write(
                    current_dir / ".credentials.json", live_creds_bytes, "drifted mount, unknown lineage")
                msg = (f"WRONG-ACCOUNT GUARD at swap ({current} -> {target_name}): refused to save "
                       f"unattributable live tokens into '{current}' snapshot — the live mount's identity "
                       f"was refused as foreign, so these rotation-shaped tokens are not trustworthy as "
                       f"'{current}'s. Kept the snapshot; quarantined to {sidecar.name}.")
                click.echo(f"creds-save-back: {msg}")
                _cred_audit("identity-refuse", "refused-identity",
                            "drifted mount, unknown lineage — foreign identity refused",
                            slot=slot, mount=(slot or "shared-mount"), account=current,
                            incoming_identity=_identity_fields(live_cj), emit_identity=True,
                            existing_identity=account_identity_anchor(current)[0],
                            token_fp=_audit_token_fp(live_creds),
                            extra="quarantined=" + sidecar.name)
                try:
                    append_inbox("wrong-account-guard",
                                 f"token save-back refused for '{current}' (drifted live mount)", msg)
                except OSError:
                    pass
            else:
                # ---- end GH #77 freshness guard ----
                if verdict == "unknown":
                    click.echo(f"creds-save-back: {detail}; assuming they are '{current}'s and saving back")
                # GH #79: keep a rotated backup of the snapshot we're replacing —
                # if any clobber bug slips past the verdict above, the refresh
                # token is recoverable via `cus restore-creds`.
                backup_credentials_file(current_dir / ".credentials.json")
                atomic_write_bytes(current_dir / ".credentials.json", live_creds_bytes, mode=0o600)
                # Canonical credentials write accepted — incoming = the live mount's
                # identity, existing = current's anchor. In the rayi1 clobber these
                # disagreed while a #77-fresher foreign token slipped through; logging
                # both here makes that exact shape one grep away.
                _cred_audit("saveback", "wrote", f"snapshot credentials save-back (verdict={verdict})",
                            slot=slot, mount=(slot or "shared-mount"), account=current,
                            incoming_identity=_identity_fields(live_cj), emit_identity=True,
                            existing_identity=account_identity_anchor(current)[0],
                            token_fp=_audit_token_fp(live_creds))
        elif verdict == "foreign":
            # THE GH #3 case: a drifted pane's refresh overwrote the live file
            # with a different account's tokens. Two actions:
            #   1. Do NOT write them into current's snapshot (that was the bug —
            #      current's refresh token would be lost).
            #   2. DO write them into the true owner's snapshot: the live file
            #      provably carries the owner's lineage (same refresh token)
            #      with a fresher access token from the drifted pane's refresh.
            #      Discarding it would strand the owner's snapshot on an older
            #      access token for no benefit; same-lineage writes are safe.
            # GH #79: the owner-heal is a same-lineage write, but back up the
            # owner's snapshot anyway — cheap insurance against a wrong verdict.
            backup_credentials_file(ACCOUNTS_DIR / f"account-{owner}" / ".credentials.json")
            atomic_write_bytes(ACCOUNTS_DIR / f"account-{owner}" / ".credentials.json",
                               live_creds_bytes, mode=0o600)
            # Same-lineage owner-heal (GH #3): tokens routed to their TRUE owner,
            # not `current`. account=owner records where they actually landed.
            _cred_audit("saveback", "wrote", "GH#3 drift — routed foreign tokens to true owner",
                        slot=slot, mount=(slot or "shared-mount"), account=owner,
                        existing_identity=(account_identity_anchor(owner)[0] if owner else None),
                        emit_identity=True, token_fp=_audit_token_fp(live_creds))
            msg = (f"DRIFT DETECTED at swap ({current} -> {target_name}): {detail}. "
                   f"Skipped save-back into '{current}' snapshot (kept its last-known-good tokens); "
                   f"routed live tokens to their true owner '{owner}' instead.")
            click.echo(f"creds-save-back: {msg}")
            # Surface it for the operator too — drift means some pane is loaded
            # with the wrong account and will keep rewriting the live file
            # until it's restarted (see GH #3 for the mechanism).
            try:
                append_inbox("drift", f"token-refresh drift detected ({owner} overwrote live creds)", msg)
            except OSError:
                pass  # inbox is best-effort observability; never fail a swap over it
        elif verdict == "invalid":
            # Parseable JSON but no refresh token inside (logout-shaped file,
            # `{}`, or a non-object). Same treatment as unparseable JSON: a
            # save-back could only replace the snapshot's refresh token with
            # nothing, so keep the last-known-good snapshot and move on — the
            # target install below rewrites the live file with good creds.
            # (Review amendment 2026-07-01; previously this fell into the
            # "unknown" save-back and destroyed the outgoing snapshot.)
            click.echo(f"creds-save-back: SKIPPED — {detail}; keeping '{current}' snapshot as-is")
        else:  # "conflict"
            # Multiple snapshots share the live refresh token — duplicate-
            # identity accounts (GH #70). Any write would be a guess; keep
            # every snapshot untouched and let the operator untangle it.
            msg = (f"AMBIGUOUS creds ownership at swap ({current} -> {target_name}): {detail}. "
                   f"Skipped save-back entirely to avoid corrupting a snapshot.")
            click.echo(f"creds-save-back: {msg}")
            try:
                append_inbox("drift", "ambiguous creds ownership — save-back skipped", msg)
            except OSError:
                pass
    # ---- end GH #3 drift detection ----

    # Merge target's account-bound keys into the mount's live .claude.json
    target_account_cj = read_json(target_cj)
    target_identity = {k: target_account_cj[k] for k in ACCOUNT_BOUND_KEYS if k in target_account_cj}
    for k, v in target_identity.items():
        live_cj[k] = v
    write_json(live_cj_path, live_cj)
    # GH #79: the live file's content was (normally) just saved back into the
    # outgoing snapshot — but the clobber bug class exists precisely because
    # the save-back sometimes goes to the wrong place or is skipped. A rotated
    # backup of the live file itself makes the install step independently
    # recoverable.
    backup_credentials_file(live_creds_path)
    # Install source. Pool model (2026-07-03): if the gate is on and the target
    # is ALREADY live on another mount, a plain snapshot copy would clobber its
    # shared token family (#104) — so CLAIM a free pooled family (a distinct
    # family) instead. If the pool is exhausted (no free family AND no legacy
    # per-slot login), REFUSE rather than clobber: raise, and _execute_slot_moves
    # logs it as a failed move (SOS surfaces the exhaustion). When the target is
    # NOT double-booked, fall through to swap_install_source — the shared
    # snapshot (today's copy path) or a legacy per-slot login. Gate off ⇒ the
    # whole claim block is skipped, so behavior is bit-for-bit unchanged.
    claimed_family = None
    install_src = None
    used_independent = False
    if (slot is not None and independent_logins_enabled(config)
            and _account_held_by_other_live_mount(state, target_name, slot, config)):
        # #127: liveness-verified claim — probes each free family's refresh
        # token, retires dead stores (the pre-PR#126 poisoning landmines), and
        # persists the rotation into the store this install copies from.
        claimed_family = claim_verified_login_family(target_name, state, config)
        if claimed_family is not None:
            install_src = login_family_creds_path(target_name, claimed_family)
            used_independent = True
        elif has_independent_login(target_name, slot):
            # Legacy per-slot family (superseded; retained until P5).
            install_src = login_store_creds_path(target_name, slot)
            used_independent = True
        else:
            raise RuntimeError(
                f"pool exhausted for '{target_name}': it is live on another mount and has no free "
                f"independent login family to claim — refusing to install a clobbering copy. "
                f"Provision another: `cus login-mount {target_name}`.")
    if install_src is None:
        install_src, used_independent = swap_install_source(target_name, slot, target_creds, config)
    # ---- 2026-07-07 dead-snapshot family-seed (merkos incident) ----
    # At this point, if neither the claimed-pool-family block above nor
    # swap_install_source picked a distinct login family, install_src is the account
    # SNAPSHOT (target_creds) — the default copy path. That snapshot can be DEAD: its
    # refresh token fails the OAuth refresh grant with invalid_grant even though the
    # account is perfectly usable through a live login family. merkos was exactly this
    # (the un-stale sweep logs `op=unstale-refresh account=merkos decision=failed
    # reason="refresh grant dead"` while lane tabby-2 ran on merkos via a valid
    # family). Installing the dead snapshot blanks the mount on Claude Code's first
    # refresh — the "not logged in" that blanked chats1a when it was swapped onto
    # merkos. The #141 blank-SHAPE guards below do NOT catch it: a dead snapshot can
    # be well-shaped (an expired-but-present access token, a positive-but-past
    # expiresAt) yet still be dead.
    #
    # So: when we're about to install the snapshot and it probes DEAD, seed the lane
    # from a VALID login family instead — claim a FREE family (its own distinct
    # family, #104-safe, recorded as the slot's login_family lease below, exactly the
    # supported rescue path). claim_verified_login_family (#127) proves each family
    # alive via the refresh grant, retires dead stores, and persists the rotation
    # into the store this install copies from. If NO valid family can be claimed
    # (snapshot dead AND pool empty/all-dead), REFUSE rather than blank the mount:
    # raise (a clean no-op — live_creds_path is still untouched until the atomic_copy
    # below), which _execute_slot_moves logs as a failed move; the picker's dead-
    # snapshot exclusion keeps it from being re-picked and the SOS points at relogin.
    # Scoped to the SLOT + gate-on path (claiming/leasing a family is only meaningful
    # for a lane in the pool model); a gate-off dead snapshot still hits the #141
    # blank guards / picker exclusion. Degrade-to-safe: refusing a swap never logs
    # anyone out; installing a dead snapshot does.
    if (claimed_family is None and not used_independent
            and slot is not None and independent_logins_enabled(config)
            and _account_snapshot_dead(target_name, config)):
        _seed_fam = claim_verified_login_family(target_name, state, config)
        if _seed_fam is not None:
            claimed_family = _seed_fam
            install_src = login_family_creds_path(target_name, _seed_fam)
            used_independent = True
            try:
                _seed_fp = _audit_token_fp(read_json(install_src))
            except (json.JSONDecodeError, OSError):
                _seed_fp = "unreadable"
            _cred_audit("snapshot-dead-family-fallback", "seeded-from-family",
                        "account snapshot is DEAD (refresh invalid_grant) — seeded the lane from a "
                        "valid login family instead of blanking the mount (merkos incident)",
                        slot=slot, mount=slot, account=target_name,
                        login_family=f"{target_name}/{_seed_fam}", token_fp=_seed_fp)
            click.echo(f"creds-install: '{target_name}' snapshot is DEAD — seeded {slot} from valid "
                       f"login family {target_name}/{_seed_fam} instead (no dead-snapshot blank)")
        else:
            _cred_audit("snapshot-dead-family-fallback", "refused-no-source",
                        "account snapshot is DEAD and no valid login family could be claimed — "
                        "refusing to install dead creds (would blank the mount)",
                        slot=slot, mount=slot, account=target_name)
            raise RuntimeError(
                f"refusing to install '{target_name}' onto lane {slot}: its canonical snapshot "
                f"credentials are DEAD (the OAuth refresh grant returns invalid_grant) and no valid "
                f"login family remains to seed from — installing the dead snapshot would blank the mount "
                f"and log the session out (the 2026-07-07 merkos dead-snapshot incident, which blanked "
                f"chats1a). Re-login the account first: `cus relogin {target_name}`. Lane left on its "
                f"prior account (no creds written).")
    # ---- 2026-07-07 divergence-logout guard (GH #104 lane invariant) ----
    # INVARIANT: a SLOT swap that lands on an account ALSO held by the shared
    # ~/.claude mount or by another live lane MUST run a DISTINCT login family —
    # a freshly CLAIMED pooled family, recorded as state.slots[slot].login_family
    # (claimed_family is not None). Any other source (the shared account snapshot,
    # OR a legacy per-(account, slot) store) shares ONE OAuth refresh-token family
    # across two live mounts; each mount rotates it independently on refresh, the
    # old refresh token dies, and the loser is logged out with
    # `401 Invalid authentication credentials` (#104).
    #
    # The incident (chats1a, verified live 2026-07-07): `cus slot move slot-2
    # merkos` printed "installed independent login for (slot-2, merkos)" via the
    # LEGACY fallback below — `claim_verified_login_family` returned None (merkos's
    # only pooled family was already leased to slot-1, which was ALSO on merkos),
    # so the code fell to `has_independent_login` / `swap_install_source` and
    # installed a per-slot store WITHOUT recording a family lease. login_family
    # stayed None while merkos was simultaneously the shared-mount account AND on
    # slot-1. ~5 min later the shared mount's token rotation clobbered slot-2's
    # copy → the live session hit `401 · Please run /login` for an hour. The
    # printed "installed independent login" line was a lie: no distinct family
    # persisted, and the legacy store's token was not a genuinely-independent
    # family (a stale snapshot copy).
    #
    # Fix: for a DOUBLE-BOOKED account, refuse to install a source whose
    # refresh-token FAMILY already runs on another live mount of that account —
    # option (b) from the task: fail with a clear error, leaving the lane on its
    # PRIOR good account (nothing is written to live_creds_path until the
    # atomic_copy further down, so raising here is a clean no-op for the mount).
    # The check reads actual token bytes (`_live_family_would_collide`), so it
    # REFUSES the incident's stale-copy legacy store (same family as the shared
    # mount) while still ALLOWING a genuinely-independent legacy login (distinct
    # family — the supported Track-B case, whose own tests must keep passing).
    # A claimed pooled family installs a distinct family by construction, so this
    # is a no-op on the normal rescue path; it is defense-in-depth that also
    # catches a mis-provisioned pool family that happens to share the snapshot's
    # token. Degrade-to-safe: refusing a swap never logs anyone out; clobbering
    # does. The login-free remedy is another pooled family (`cus login-mount`).
    if (slot is not None and independent_logins_enabled(config)
            and _account_held_by_other_live_mount(state, target_name, slot, config)
            and _live_family_would_collide(target_name, install_src, slot, state, config)):
        # The refusal that prevents the divergence-logout: this install source's
        # token family is already live on another mount of `target_name`. shared=true
        # is the whole reason we refuse; token_fp names the colliding family.
        try:
            _collide_fp = _audit_token_fp(read_json(install_src))
        except (json.JSONDecodeError, OSError):
            _collide_fp = "unreadable"
        _cred_audit("family-collision-refuse", "refused-collision",
                    "install source shares a live family on another mount (#104)",
                    slot=slot, account=target_name, login_family=(claimed_family or "legacy/snapshot"),
                    shared=True, token_fp=_collide_fp)
        raise RuntimeError(
            f"refusing to install '{target_name}' onto lane {slot}: the credentials about to be "
            f"installed carry the SAME OAuth refresh-token family already live on the shared mount "
            f"or another lane of '{target_name}'. Two live mounts on one token family log one of "
            f"them out on the next rotation (GH #104 divergence — the 2026-07-07 chats1a logout). "
            f"No distinct login family could be claimed (pool empty, all families leased, or the "
            f"source is a stale snapshot copy). Provision another independent login "
            f"(`cus login-mount {target_name}`) and retry, or move the lane to a different "
            f"account. Lane left on its prior account (no creds written).")
    # ---- GH #141 root-cause guard (definitive install-point gate) ----
    # This is THE line that writes creds to the live mount; every swap path
    # (snapshot copy, claimed pool family, legacy per-slot login) funnels through
    # it. Validate the actual bytes about to be installed and REFUSE to write a
    # blank/expired payload, so no swap can ever leave the live mount logged out
    # (GH #141). The early guard above already covers the default snapshot path
    # with zero partial state; this backstops the pool paths (independent-login
    # families) whose source is resolved only here. `live_creds_path` is still
    # untouched at this point — the atomic_copy below is its first and only write
    # — so raising keeps the mount's current, valid creds intact.
    try:
        _install_src_creds = read_json(install_src)
    except (json.JSONDecodeError, OSError) as e:
        raise RuntimeError(
            f"refusing to install '{target_name}' creds: source {install_src} is unreadable "
            f"({e}) — writing it would blank the live mount (GH #141).")
    if _live_mount_creds_invalid(_install_src_creds):
        raise RuntimeError(
            f"refusing to install '{target_name}' creds: source {install_src.name} is blank/expired "
            f"(empty accessToken or expiresAt<=0) — writing it would blank the live mount and lock "
            f"out bare sessions (GH #141). Re-login (`cus relogin {target_name}`) or restore a "
            f"backup (`cus restore-creds {target_name}`).")
    atomic_copy(install_src, live_creds_path, mode=0o600)
    # THE install-point: the target account's creds are now live on this mount.
    # source = which store we copied from (claimed pool family / legacy / snapshot);
    # existing = target's trusted anchor identity; token_fp names the family that is
    # now live here — the fingerprint a later divergence-detected line would match.
    _cred_audit("swap-install", "wrote",
                ("claimed pooled family" if claimed_family is not None
                 else "independent login (legacy)" if used_independent
                 else "account snapshot"),
                slot=slot, mount=(slot or "shared-mount"), account=target_name,
                existing_identity=_identity_fields(target_account_cj), emit_identity=True,
                login_family=(f"{target_name}/{claimed_family}" if claimed_family else None),
                token_fp=_audit_token_fp(_install_src_creds))
    if claimed_family is not None:
        click.echo(f"creds-install: claimed pooled login {target_name}/{claimed_family} for {slot} "
                   f"(distinct family, no clobber — #109 pool)")
    elif used_independent:
        click.echo(f"creds-install: installed independent login for ({slot}, {target_name}) — GH #109")

    # Update state with progressive-threshold bookkeeping
    ts = now_iso()
    if slot is None:
        state["active"] = target_name
    else:
        slot_entry["account"] = target_name
        slot_entry["last_swap_ts"] = ts
        # Occupancy just changed: drop the cached account→slots map so every
        # same-cycle reader (the NEXT fan-out move's clobber guard, SOS checks)
        # sees this slot on its new account. Complements the guard's own
        # cache bypass — belt and braces after the 2026-07-03 16:10Z clobber.
        _OCCUPIED_SLOTS_CACHE.clear()
        # Record/clear the pool lease (2026-07-03): a claimed family binds this
        # slot to (target, family) so leased_families() excludes it from other
        # slots' free set and save-back routes there. Moving onto a primary /
        # snapshot (no claim) releases any prior lease.
        if claimed_family is not None:
            slot_entry["login_family"] = f"{target_name}/{claimed_family}"
        else:
            slot_entry.pop("login_family", None)
    state["accounts"][target_name]["last_swap_ts"] = ts
    # Ladder hysteresis clock (2026-07-03): only DAEMON-decided swaps arm it.
    # A launch installing an account into a slot isn't churn, but it used to
    # bump last_swap_ts and re-arm the anti-ping-pong cooldown — with a long
    # min_seconds_between_swaps, every launch parked the ladder for all slots
    # on that account while they climbed (2026-07-03: slot-6/slot-7 stuck at
    # 84-88% behind a 50-min lockout that launches kept resetting). The ladder
    # check reads last_auto_swap_ts; last_swap_ts stays for display and the
    # 60s reactive/cap spacings (where counting launches is harmless).
    if trigger != "launch":
        state["accounts"][target_name]["last_auto_swap_ts"] = ts

    # Bump current account's next_swap_at_pct ladder using CONFIG steps.
    # Falls back to the hardcoded default ladder if config is missing.
    # (Skipped for a swap-into-empty-slot: no outgoing account to bump, and
    # skipped when the caller passes bump_ladder=False — the per-slot fan-out
    # moves several slots off ONE hot account in a cycle and must advance that
    # account's shared ladder exactly ONCE, not once per slot, else a single
    # 50% trip could jump straight to 90% (review finding 2026-07-02).)
    if current is not None and bump_ladder:
        # config was already loaded at the top of this function (reused here).
        cfg_steps = config.get("thresholds", {}).get("steps") or [50, 75, 90]
        ladder = list(cfg_steps) + [100]  # 100 = force sentinel
        current_acct = state["accounts"].get(current)
        if current_acct is not None:
            cur_step = current_acct.get("next_swap_at_pct", ladder[0])
            # Find next step strictly greater than cur_step; if none, stay at last (force)
            next_step = next((s for s in ladder if s > cur_step), ladder[-1])
            current_acct["next_swap_at_pct"] = next_step

    history_entry = {"ts": ts, "from": current, "to": target_name, "trigger": trigger}
    if slot is not None:
        history_entry["slot"] = slot
    state.setdefault("swap_history", []).append(history_entry)
    save_state(state)
    # GH #76: swap fully persisted — retire the crash journal.
    _clear_swap_journal()
    return state


# --------------------------------------------------------------------------
# Hook installer (~/.claude/settings.json upsert)
# --------------------------------------------------------------------------

HOOK_EVENTS = {
    "SessionStart": ("cus_session_start.sh", "install_session_start"),
    "Stop": ("cus_stop.sh", "install_stop"),
    # StopFailure (A2, 2026-06-23): the DOCUMENTED, reliable Anthropic-429 signal.
    # A model-level rate limit fires StopFailure (instead of Stop) with a clean
    # categorized `error` enum ("rate_limit"/"overloaded"), NOT PostToolUseFailure
    # — which only sees tool-execution failures. This is the primary 429 detector;
    # the PostToolUseFailure hook below is now a best-effort secondary for
    # subagent-internal API errors only.
    "StopFailure": ("cus_stop_failure.sh", "install_stop_failure"),
    "PostToolUseFailure": ("cus_post_tool_use_failure.sh", "install_post_tool_use_failure"),
    "PreToolUse": ("cus_pre_tool_use.sh", "install_pre_tool_use"),
    # PostToolUse(success) closes the start/stop ledger so subagent_active
    # detects genuinely in-flight tools, not every recent tool call (GH #27).
    "PostToolUse": ("cus_post_tool_use.sh", "install_post_tool_use"),
    "SubagentStop": ("cus_subagent_stop.sh", "install_subagent_stop"),
}


def _settings_json_path() -> Path:
    return CLAUDE_DIR / "settings.json"


def _entry_is_cus(entry: object) -> bool:
    """Identify a settings.json hook entry as one installed by cus (GH #31).

    The modern signal is the `_cus_marker` field. But hooks installed by an
    OLDER cus (or a bootstrap predating the marker code) have NO marker — so
    dedupe-by-marker missed them and `cus hooks install` appended a duplicate
    MARKED copy of every hook. A duplicated PreToolUse hook fires twice per
    tool call, writing two `start` lines to tool_use.log and re-corrupting the
    start/stop ledger that #27/#29 fixed; uninstall had the symmetric bug
    (it orphaned the unmarked legacy entries, leaving them behind).

    Fix: also treat an entry as ours if any of its hook commands points at the
    cus hooks/ directory, or is a bare `cus_*.sh` script name (robust even if
    the repo was moved). This makes install + uninstall idempotent on legacy
    systems instead of duplicating / orphaning their entries.
    """
    if not isinstance(entry, dict):
        return False
    if entry.get("_cus_marker") == HOOK_SETTINGS_KEY:
        return True
    hooks_dir = str(HOOKS_SRC_DIR.resolve())
    for h in entry.get("hooks", []):
        if not isinstance(h, dict):
            continue
        cmd = h.get("command") or ""
        # Command is normally the bare script path, but tolerate env prefixes /
        # args by scanning whitespace-separated tokens.
        for token in cmd.split():
            name = os.path.basename(token)
            if name.startswith("cus_") and name.endswith(".sh"):
                return True
            if token.startswith(hooks_dir):
                return True
    return False


def install_hooks(config: dict) -> dict[str, str]:
    """Install enabled hooks into ~/.claude/settings.json.

    Each hook entry is tagged with a signature in its `_cus_marker` field
    so we can identify our entries and avoid clobbering others'. Returns
    {event_name: "installed" | "already_installed" | "skipped (disabled)"}.

    Idempotent on legacy systems (GH #31): existing cus entries are matched by
    `_entry_is_cus` (marker OR command path), collapsed to a single normalized
    marked entry, so re-running never appends duplicates.
    """
    settings_path = _settings_json_path()
    settings = read_json(settings_path) if settings_path.exists() else {}
    hooks = settings.setdefault("hooks", {})

    result: dict[str, str] = {}
    for event, (script_name, config_key) in HOOK_EVENTS.items():
        if not config.get("hooks", {}).get(config_key, True):
            result[event] = "skipped (disabled in config.yaml)"
            continue

        script_path = HOOKS_SRC_DIR / script_name
        if not script_path.exists():
            result[event] = f"missing hook script at {script_path}"
            continue

        # Hooks for an event are stored as a list-of-matchers. We use
        # an empty matcher (matches everything) for our entries.
        event_entries = hooks.setdefault(event, [])
        new_entry = {
            "_cus_marker": HOOK_SETTINGS_KEY,
            "matcher": "",
            "hooks": [{"type": "command", "command": str(script_path)}],
        }
        # Find ALL existing cus entries (marker OR command path; GH #31).
        # Collapse them into a single normalized, marked entry in the first
        # cus slot and drop any extras — this upgrades unmarked legacy entries
        # AND de-duplicates any duplicates a prior buggy install left behind.
        cus_idxs = {i for i, e in enumerate(event_entries) if _entry_is_cus(e)}
        if cus_idxs:
            already = len(cus_idxs) == 1 and event_entries[next(iter(cus_idxs))] == new_entry
            rebuilt = []
            placed = False
            for i, e in enumerate(event_entries):
                if i in cus_idxs:
                    if not placed:
                        rebuilt.append(new_entry)
                        placed = True
                    # else: drop the duplicate / superseded cus entry
                else:
                    rebuilt.append(e)
            hooks[event] = rebuilt
            result[event] = "already_installed" if already else "updated (normalized; deduped legacy/dupes)"
        else:
            event_entries.append(new_entry)
            result[event] = "installed"

    write_json(settings_path, settings)
    return result


def uninstall_hooks() -> dict[str, str]:
    """Remove all cus entries from ~/.claude/settings.json.

    Matches by `_entry_is_cus` (marker OR command path) so legacy unmarked
    entries are removed too, not orphaned (GH #31).
    """
    settings_path = _settings_json_path()
    if not settings_path.exists():
        return {event: "no settings.json" for event in HOOK_EVENTS}
    settings = read_json(settings_path)
    hooks = settings.get("hooks", {})

    result: dict[str, str] = {}
    for event in HOOK_EVENTS:
        entries = hooks.get(event, [])
        new_entries = [e for e in entries if not _entry_is_cus(e)]
        if len(new_entries) != len(entries):
            hooks[event] = new_entries
            result[event] = "uninstalled"
        else:
            result[event] = "not present"
    write_json(settings_path, settings)
    return result


def list_hooks() -> dict[str, str]:
    """Report current install state for each hook event."""
    settings_path = _settings_json_path()
    if not settings_path.exists():
        return {event: "no settings.json" for event in HOOK_EVENTS}
    settings = read_json(settings_path)
    hooks = settings.get("hooks", {})
    result: dict[str, str] = {}
    for event, (script_name, _) in HOOK_EVENTS.items():
        entries = hooks.get(event, [])
        marked = [e for e in entries if _entry_is_cus(e)]
        if marked:
            cmds = ",".join(h.get("command", "?") for e in marked for h in e.get("hooks", []))
            dup = f" [{len(marked)} entries — DUPLICATED]" if len(marked) > 1 else ""
            result[event] = f"installed -> {cmds}{dup}"
        else:
            result[event] = "not installed"
    return result


# --------------------------------------------------------------------------
# Daemon decision engine
# --------------------------------------------------------------------------

@dataclass
class SwapDecision:
    """Output of decide_swap. Sentinel `None` from decide_swap = no action."""
    target: str
    reason: str
    tier: int   # 1 = wait-for-Stop, 2 = pause-message, 3 = force
    # GH #56: which trigger/rule produced this decision — surfaced in the
    # structured decision log so the operator can see WHY every swap happened.
    gate: str = "ladder"
    # GH #56: may the cache-aware lazy background-swap gate defer this swap?
    # True for non-urgent balancing swaps (ladder below saturation, burn-before-
    # reset). False for urgent ones — hard 7d cap, 5h saturation, reactive 429 —
    # which must proceed regardless of prompt-cache warmth, else we'd strand a
    # session riding into a rate-limit. Keyed explicitly (NOT off `tier`) because
    # a hard-cap swap can land at tier 2 yet must never be deferred.
    deferrable: bool = True


def _build_decision_record(
    state: dict,
    config: dict,
    *,
    action: str,
    gate: str,
    reason: str,
    target: str | None = None,
    tier: int | None = None,
    where: dict | None = None,
) -> dict:
    """Assemble one structured who/what/where/when/why decision record (GH #56).

    Written once per daemon cycle to decisions.jsonl so the operator can audit
    exactly why each swap did or didn't happen over a long testing window.
      who   = the active account + its live usage
      what  = action: swap | hold | defer | would_swap
      where = panes/sessions affected (who pays the prompt-cache rebuild)
      when  = timestamp
      why   = gate (which rule decided) + a human reason sentence
    """
    active = state.get("active")
    accts = {}
    for n, a in (state.get("accounts") or {}).items():
        accts[n] = {
            "5h": a.get("current_5h_pct"),
            "7d": a.get("current_7d_pct"),
            "next_swap_at_pct": a.get("next_swap_at_pct"),
            "five_hour_resets_at": a.get("five_hour_resets_at"),
        }
    who = accts.get(active, {})
    th = config.get("thresholds", {})
    return {
        "when": now_iso(),
        "action": action,
        "gate": gate,
        "reason": reason,
        "who": {
            "active": active,
            "5h": who.get("5h"),
            "7d": who.get("7d"),
            "next_swap_at_pct": who.get("next_swap_at_pct"),
        },
        "target": target,
        "tier": tier,
        "where": where or {},
        "accounts": accts,
        "mode": "hot_swap" if config.get("hot_swap", {}).get("enabled", False) else "background",
        "config": {
            "steps": th.get("steps"),
            "min_improvement_pct": config.get("swap_hysteresis", {}).get("min_improvement_pct"),
            "lazy_swap": config.get("lazy_swap", {}).get("enabled"),
            "burn_before_reset": config.get("burn_before_reset", {}).get("enabled"),
        },
    }


def _log_decision(record: dict) -> None:
    """Append a decision record to decisions.jsonl (size-rotated at ~5MB).

    Best-effort: a logging failure must NEVER break the daemon cycle, so all
    OSErrors are swallowed. GH #56.
    """
    try:
        if DECISIONS_LOG.exists() and DECISIONS_LOG.stat().st_size > 5_000_000:
            DECISIONS_LOG.replace(DECISIONS_LOG.with_name(DECISIONS_LOG.name + ".1"))
        with DECISIONS_LOG.open("a") as f:
            f.write(json.dumps(record, separators=(",", ":")) + "\n")
    except OSError:
        pass


def _migrating_panes() -> dict:
    """The 'where' of a background swap: every live session follows the global
    creds, so all live panes migrate to the new account (and the warm ones pay
    a one-time prompt-cache rebuild). Returns {count, panes} for the decision
    record. Best-effort — only call this on actual swap/defer cycles, not on
    every hold, to avoid scanning sessions.log needlessly. GH #56.
    """
    try:
        panes = sorted({s.pane for s in find_live_sessions()
                        if s.pane and s.pane != "no-tmux"})
    except Exception:
        panes = []
    return {"live_pane_count": len(panes), "panes": panes}


def _lazy_warm_sessions(decision: "SwapDecision", config: dict) -> list:
    """GH #56: live sessions whose prompt cache a background swap would bust now.

    Empty list = the swap is free (or must not be lazy-deferred), so the caller
    should proceed immediately. A non-empty list = defer: those sessions are
    still cache-warm and would each eat a prompt-cache rebuild on the new
    account.

    Returns [] (→ swap now) when:
      - hot-swap mode is on (it has its own per-pane cache-bust deferral),
      - lazy_swap is disabled,
      - the decision is NOT deferrable — urgent swaps (hard 7d cap / 5h
        saturation / reactive 429) must proceed regardless of cache warmth,
        else we'd strand a session riding into a rate-limit.

    Otherwise returns the cache-warm live sessions (last transcript activity
    younger than cache_window_seconds). A background swap migrates ALL live
    sessions onto the new account (single global credential file), so every
    warm session is a payer — pins are advisory swap-policy only and do not
    route creds per-session, so they are NOT excluded.
    """
    if config.get("hot_swap", {}).get("enabled", False):
        return []
    lazy = config.get("lazy_swap", {})
    if not lazy.get("enabled", True):
        return []
    if not getattr(decision, "deferrable", True):
        return []
    window = lazy.get("cache_window_seconds", 300)
    try:
        sessions = find_live_sessions()
    except Exception:
        return []
    return [s for s in sessions if s.transcript_path and cache_warm(s.transcript_path, window)]


def determine_tier(active_acct: dict, config: dict) -> int:
    """Map ladder step + current usage to an urgency tier.

    Tier 1: gentle — wait for Stop, defer if cache warm.
    Tier 2: send pause-message asking session to wrap up.
    Tier 3: force interrupt (double-Escape), log shell state.

    Two inputs are considered (max wins): the ladder step
    (next_swap_at_pct, which represents how many times we've already
    swapped away from this account) AND the CURRENT usage. Previously
    only the ladder step was used — which meant when an account at
    next_swap_at_pct=70 suddenly spiked to 95% real usage, we'd run
    tier 1 (just wait for Stop), the swap could time out and abort,
    and we'd be stuck at 95%+ with no escalation. User-reported bug
    2026-05-25 (GH #20): "ran out of usage, didn't auto swap" — the
    direct cause was tier-1 wait timing out on a stuck pane; the tier
    SHOULD have been 3 since the account was at 100%.
    """
    hot = config.get("hot_swap", {})
    tier_2_at = hot.get("tier_2_at_pct", 75)
    tier_3_at = hot.get("tier_3_at_pct", 90)

    step = active_acct.get("next_swap_at_pct", 50)
    cur_5h = active_acct.get("current_5h_pct", 0)
    cur_7d = active_acct.get("current_7d_pct", 0)
    cur_max = max(cur_5h, cur_7d)

    # Take the max-urgency signal between the ladder step and the actual
    # current usage. The ladder represents accumulated history; current
    # usage represents the live emergency level.
    urgency = max(step, cur_max)

    if urgency >= tier_3_at:
        return 3
    if urgency >= tier_2_at:
        return 2
    return 1


def _five_hour_remaining_seconds(usage: "AccountUsage | None", acct: dict | None) -> float | None:
    """Seconds until an account's 5-hour window resets, or None if undeterminable.

    Prefers the freshly-polled AccountUsage; falls back to the state dict's
    persisted `five_hour_resets_at`. Returns None when the 5h clock isn't
    ticking (utilization <= 0 → no reset deadline, and the stored resets_at is
    stale/meaningless) or no parseable reset timestamp exists. Shared by the
    burn-before-reset trigger (GH #42).
    """
    util = None
    reset_at = None
    if usage is not None and usage.five_hour is not None:
        util = usage.five_hour.utilization
        reset_at = usage.five_hour.resets_at
    if reset_at is None and acct is not None:
        reset_at = acct.get("five_hour_resets_at")
        if util is None:
            util = acct.get("current_5h_pct")
    if not reset_at:
        return None
    # 5h at 0% means the clock hasn't started — resets_at is stale/meaningless.
    if util is not None and util <= 0:
        return None
    try:
        reset_dt = datetime.fromisoformat(reset_at.replace("Z", "+00:00"))
        return (reset_dt - datetime.now(timezone.utc)).total_seconds()
    except (ValueError, AttributeError, TypeError):
        return None


def _maybe_burn_before_reset(
    state: dict,
    config: dict,
    current: str,
    active_acct: dict,
    cur_usage: "AccountUsage",
    usage_by_account: dict[str, AccountUsage],
) -> SwapDecision | None:
    """Proactive 'burn-before-reset' swap trigger (GH #42, requested 2026-05-28).

    The standard ladder/cap/reactive triggers only fire when the ACTIVE
    account climbs toward its own cap. They never move us ONTO an account
    whose 5-hour window is about to reset. But a 5h window is "use it or lose
    it": when account B's window rolls over, whatever B accrued resets to zero
    — so the right place to be just before a reset is ON the account that's
    about to reset, draining its remaining capacity, while leaving the
    far-from-reset account's window pristine for later.

    User intent (2026-05-28): "consider how much more time is left on the 5h
    clock per session when swapping ... we should have always been on the
    closer-to-[reset] session." Earlier verbatim framing: "if one resets in 1
    hour, that one should be burned before the one that resets in 4 hours."

    Guardrails (ALL must hold) so this never churns or interrupts live work:
      - feature enabled (config.burn_before_reset.enabled)
      - a usable swap target exists — reuses pick_swap_target's full filter set
        (not blocked, below hard 7d cap, below its own ladder step), so we
        never land on an exhausted or about-to-re-trip account
      - that target's 5h window resets within reset_window_minutes
      - the target still has unused 5h capacity worth draining
        (>= min_candidate_headroom_pct) — no point landing on a near-capped
        account that would immediately need swapping off again
      - the ACTIVE account's 5h window resets at least min_reset_gap_minutes
        LATER than the target's (or its clock isn't ticking at all) — i.e. we
        genuinely give up nothing usable by leaving it now

    Fires at TIER 1 only (wait-for-Stop, defer if cache warm) so it never
    interrupts an in-flight turn — important given the 2026-05-19 incident
    where a botched mid-turn relaunch burned ~4% of a 5h window.

    Walk-back: set burn_before_reset.enabled: false in config.yaml.
    """
    cfg = config.get("burn_before_reset", {})
    if not cfg.get("enabled", True):
        return None
    reset_window_s = cfg.get("reset_window_minutes", 30) * 60
    min_gap_s = cfg.get("min_reset_gap_minutes", 60) * 60
    min_headroom = cfg.get("min_candidate_headroom_pct", 15)

    target = pick_swap_target(state, config)
    if target is None or target.name == current:
        return None
    tgt_acct = state["accounts"].get(target.name, {})
    tgt_usage = usage_by_account.get(target.name)

    # Target must be about to reset AND still hold capacity worth burning.
    tgt_remaining = _five_hour_remaining_seconds(tgt_usage, tgt_acct)
    if tgt_remaining is None or not (0 < tgt_remaining <= reset_window_s):
        return None
    tgt_5h = (
        tgt_usage.five_hour.utilization
        if tgt_usage and tgt_usage.five_hour
        else tgt_acct.get("current_5h_pct", 0.0)
    )
    if (100 - tgt_5h) < min_headroom:
        return None

    # Active must reset meaningfully LATER (or have no ticking clock at all →
    # treat as infinitely far, since we lose nothing usable by leaving it).
    act_remaining = _five_hour_remaining_seconds(cur_usage, active_acct)
    act_remaining_eff = float("inf") if act_remaining is None else act_remaining
    if act_remaining_eff - tgt_remaining < min_gap_s:
        return None

    act_desc = "never(idle)" if act_remaining is None else f"{act_remaining / 60:.0f}min"
    return SwapDecision(
        target=target.name,
        reason=(
            f"burn-before-reset: {target.name} 5h resets in "
            f"{tgt_remaining / 60:.0f}min (5h={tgt_5h:.0f}%, {100 - tgt_5h:.0f}% headroom) "
            f"vs active {current} resets in {act_desc}; {target.reason}"
        ),
        tier=1,
        gate="burn_before_reset",
        # GH #56: burn-before-reset is a non-urgent throughput optimization, so
        # it IS lazy-deferrable — only worth a cache rebuild when the cache is
        # already cold. If sessions are warm we skip the burn (the missed 5h
        # headroom is free to give up given large weekly headroom).
        deferrable=True,
    )


def decide_swap(
    state: dict,
    config: dict,
    usage_by_account: dict[str, AccountUsage],
    trace: dict | None = None,
) -> SwapDecision | None:
    """Given current state + fresh usage, decide whether to swap.

    Returns None if no action needed. Otherwise a SwapDecision with target
    + tier the caller should respect.

    Two trigger paths:
      1. **Hard 7d cap**: when the active account's 7d utilization exceeds
         the config'd `smart_strategy.hard_7d_cap_pct` (default 80%), trigger
         IMMEDIATELY regardless of the progressive threshold ladder. This
         is the user-stated invariant: "never go over 80% of 7 day usage."
      2. **Progressive threshold ladder**: the standard path. Trigger when
         current max(5h, 7d) crosses the active account's next_swap_at_pct.

    `trace` (GH #56): optional dict the caller passes in to capture WHICH rule
    decided this cycle (gate), the outcome (action: "swap"/"hold"), and a
    human reason — for the structured decision log. Every return path records
    it via `_note`; pass None to skip (the logic is unaffected either way).
    """
    def _note(gate: str, action: str, reason: str) -> None:
        if trace is not None:
            trace["gate"], trace["action"], trace["reason"] = gate, action, reason

    current = state.get("active")
    if not current:
        _note("no_active", "hold", "no active account set")
        return None

    active_acct = state["accounts"][current]
    cur_usage = usage_by_account.get(current)
    if cur_usage is None:
        _note("no_usage", "hold", f"no fresh usage poll for active account {current}")
        return None

    # Fix A — extrapolation-aware trigger (2026-07-06 rayi3 incident). The ladder
    # (Trigger 2) and hard-cap (Trigger 1) trips below compare against the
    # BURN-EXTRAPOLATED usage, projected forward to the moment the daemon could
    # next act on this account (its poll interval), instead of the raw last-polled
    # value. rayi3 read 91% at one poll and 100% at the next ~180s later — a live
    # session hit the native usage-limit menu in that gap before any swap. Anchor
    # the projection horizon at the account's own poll interval; when poll_accel
    # is disabled these stay 0 / raw and every trip is byte-identical to before.
    _trig_now = datetime.now(timezone.utc)
    _trig_look_ahead = (
        _account_poll_interval(state, config, current)
        if _poll_accel_enabled(config) else 0.0
    )

    # NOTE: the previous "Trigger 0: active is rate-limited (poll 429)" has
    # been REMOVED (GH #18). That trigger fired on the POLLING endpoint's
    # 429 — Anthropic's per-IP throttle on /api/oauth/usage, which is NOT
    # a signal that the account itself is exhausted. The account is fully
    # usable for real claude code traffic when polling is throttled.
    # The CORRECT reactive 429 path is `check_rate_limit_reactive` (called
    # at the top of one_cycle), which reads the rate_limit_log populated
    # by the PostToolUseFailure hook from actual user-facing 429s.

    # Trigger 1: hard 7d cap.
    # Active strategy must support this; smart and headroom do via their
    # respective config blocks. Other strategies pick up the default 80%.
    strategy_cfg = config.get(f"{config.get('strategy', '')}_strategy", {})
    hard_7d_cap = strategy_cfg.get("hard_7d_cap_pct") or config.get("smart_strategy", {}).get("hard_7d_cap_pct", 80)
    cur_7d = cur_usage.seven_day.utilization if cur_usage.seven_day else 0.0
    # Per-model weekly gate (opt-in): force a swap OFF the active account when a
    # tracked model's weekly cap is exhausted, even if aggregate 7d has room.
    # 0.0 when the gate is off, so the hard-cap trigger is unchanged for
    # unmodified installs. Per-model is weekly-only, so it belongs on this cap.
    # The model comparison uses its own threshold (per_model_weekly.cap_pct,
    # falling back to hard_7d_cap) so e.g. "Fable at 90" can coexist with an
    # aggregate ceiling of 80.
    cur_model = _max_model_weekly_from_usage(cur_usage, config)
    model_cap = _model_weekly_cap_for_config(config)
    # Fix A: force the hard-cap trips on the burn-EXTRAPOLATED values, not the
    # stale poll. Aggregate 7d extrapolates via estimate_window_pct (same base as
    # cur_usage.seven_day.utilization — they're synced by update_state_with_usage).
    # Per-model weekly has no burn rate of its own; it rides the 7d window, so we
    # project it forward on the 7d rate (a conservative shared-window proxy). Both
    # use max()/+delta so extrapolation only ever raises the value — a real
    # over-cap poll is never masked. Inert when poll_accel is off (look_ahead 0).
    if _poll_accel_enabled(config):
        cur_7d = max(cur_7d, estimate_window_pct(
            active_acct, "7d", config, _trig_now + timedelta(seconds=_trig_look_ahead)))
        if cur_model > 0:
            cur_model = min(100.0, cur_model + _burn_extrapolation_delta(
                active_acct, "7d", config, _trig_now, _trig_look_ahead))
    agg_tripped = cur_7d >= hard_7d_cap
    model_tripped = cur_model > 0 and cur_model >= model_cap
    if agg_tripped or model_tripped:
        # Preserve the FORCING caps before the display reassignment below stomps
        # `hard_7d_cap`. The anti-pingpong guard (further down) compares the chosen
        # target against these swap-AWAY caps — the ones that actually tripped —
        # NOT the target-side caps pick_swap_target filters arrivals with (those
        # can sit higher, which is how a target at/over the forcing cap slips
        # through the picker un-degraded, PR #139 ping-pong).
        _forcing_agg_cap = hard_7d_cap
        _forcing_model_cap = model_cap
        # For the operator-facing messages below: show whichever signal tripped
        # (the model % when only the model cap tripped, else the aggregate).
        cur_7d = cur_7d if agg_tripped else cur_model
        hard_7d_cap = hard_7d_cap if agg_tripped else model_cap
        # Hard 7d cap is normally a bypass path (it's the user's invariant)
        # but apply minimum hysteresis so a previous cap-swap can't be
        # immediately undone — and only allow the swap if the picker has
        # a target with REAL safety (not a DEGRADED fallback). If all
        # accounts are above cap, swapping doesn't help — emit SOS instead.
        hyst_cfg = config.get("swap_hysteresis", {})
        if hyst_cfg.get("enabled", True):
            min_seconds = hyst_cfg.get("min_seconds_between_cap_swaps", 60)
            last_swap = active_acct.get("last_swap_ts")
            if last_swap:
                try:
                    last_swap_dt = datetime.fromisoformat(last_swap.replace("Z", "+00:00"))
                    elapsed = (datetime.now(timezone.utc) - last_swap_dt).total_seconds()
                    if elapsed < min_seconds:
                        msg = (
                            f"hard 7d cap tripped ({current} at {cur_7d:.1f}%) "
                            f"but last swap was {int(elapsed)}s ago (< {min_seconds}s); deferring"
                        )
                        click.echo(f"  {msg}")
                        _note("hard_7d_cap_hysteresis", "hold", msg)
                        return None
                except ValueError:
                    pass

        target = pick_swap_target(state, config)
        if target is None:
            # Visibility (2026-07-03): this hold used to be SILENT — _note()
            # only writes the trace dict, it does NOT echo to daemon.log (unlike
            # every sibling hold gate below). In lanes/hybrid mode a hot lane
            # whose only candidates are all excluded (GH #104 no-double-book) or
            # saturated lands here every cycle, so the operator saw "hard 7d cap
            # tripped" followed by silence and no swap. Echo the reason so the
            # daemon log explains WHY it held; SOS Condition 2b surfaces it too.
            msg = f"hard 7d cap tripped on {current} ({cur_7d:.1f}%) but no valid swap target"
            click.echo(f"  {msg}")
            _note("hard_7d_cap_no_target", "hold", msg)
            return None
        # If the picker had to fall back to a DEGRADED candidate (also above
        # hard_7d_cap), swapping doesn't actually help — both accounts are
        # exhausted. Don't churn; the SOS layer will surface it.
        # Match the annotation WITHOUT the cap number: with a separate
        # per_model_weekly.cap_pct, the cap that tripped here can differ from
        # the aggregate cap pick_swap_target wrote into the annotation.
        if "[DEGRADED:" in target.reason and "no targets below 7d cap" in target.reason:
            msg = (
                f"hard 7d cap tripped on {current} ({cur_7d:.1f}%) but all "
                f"other accounts are ALSO above cap — not swapping (would just churn); SOS will fire"
            )
            click.echo(f"  {msg}")
            _note("hard_7d_cap_degraded", "hold", msg)
            return None
        # ---- Anti-pingpong guard (fix 2026-07-05, PR #139) ----
        # A hard-cap FORCED swap must land on an account that GENUINELY RELIEVES
        # the cap forcing us — i.e. is STRICTLY BELOW that cap on the same
        # dimension. A lateral move onto an equally-or-more-capped account relieves
        # nothing and bounces straight back next cycle. Live trace: `default` at
        # Fable 100% (>= 97 cap) swaps toward its best below-cap target (rayi1),
        # but that move FAILS (pool exhausted, no free login family — see the
        # over-subscription fix in this same PR); with rayi1 unreachable, the
        # picker's headroom fallback returned rayi2 at Fable 97% (== cap → also
        # capped), so default→rayi2→default oscillated forever. min_seconds_between_*
        # only throttles the rate, it doesn't stop the loop.
        #
        # The existing "[DEGRADED: no targets below 7d cap]" check above catches
        # only the AGGREGATE fallback, and only when the picker itself marked the
        # pick degraded. It misses the per-model case because pick_swap_target
        # filters ARRIVALS with the target-side model cap (target_cap_pct), which
        # can sit ABOVE the swap-away cap_pct that tripped here — so rayi2 at 97
        # passes the picker un-degraded even though it clears nothing. This guard
        # compares the chosen target's value on the FORCING dimension directly
        # against the forcing cap, independent of annotation wording, covering both
        # the aggregate hard_7d_cap and the per-model weekly cap. Boundary:
        # target_pct >= cap is NOT relief (rayi2 at exactly 97 is rejected); only a
        # STRICTLY-below-cap target is a genuine escape. When both signals tripped,
        # the target must clear BOTH. If nothing reachable clears the cap, HOLD and
        # let SOS surface the all-capped condition (holding on the current capped
        # account is strictly no worse than a churning lateral move). Backward-
        # compatible: `_max_model_weekly_from_acct` is 0.0 with the per-model gate
        # off, so that branch is inert on unmodified installs.
        target_acct = state["accounts"].get(target.name, {})
        capped_dims: list[str] = []
        if agg_tripped and target_acct.get("current_7d_pct", 0.0) >= _forcing_agg_cap:
            capped_dims.append(f"7d {target_acct.get('current_7d_pct', 0.0):.0f}% >= {_forcing_agg_cap:.0f}%")
        if model_tripped and _max_model_weekly_from_acct(target_acct, config) >= _forcing_model_cap:
            capped_dims.append(f"model {_max_model_weekly_from_acct(target_acct, config):.0f}% >= {_forcing_model_cap:.0f}%")
        if capped_dims:
            gate_word = "hard_7d_cap" if agg_tripped else "per-model weekly"
            shown_cap = _forcing_agg_cap if agg_tripped else _forcing_model_cap
            msg = (f"hold {current}: all reachable targets at/over the {gate_word} cap "
                   f"({shown_cap:.0f}%) — swapping wouldn't relieve it (anti-pingpong); "
                   f"best target '{target.name}' still capped [{'; '.join(capped_dims)}]")
            click.echo(f"  {msg}")
            _note("hard_cap_pingpong", "hold", msg)
            return None
        reason = f"hard 7d cap: {current} at {cur_7d:.1f}% >= {hard_7d_cap}%; target: {target.reason}"
        _note("hard_7d_cap", "swap", reason)
        # Hard cap is the user's invariant — NEVER lazy-deferrable (deferring it
        # would let 7d climb past the cap while sessions are warm). GH #56.
        return SwapDecision(
            target=target.name,
            reason=reason,
            tier=determine_tier(active_acct, config),
            gate="hard_7d_cap",
            deferrable=False,
        )

    # Universal hysteresis: enforce a minimum interval between swaps.
    # Final safety-net against any rapid-swap loop bug in the ladder logic
    # or any picker strategy. Reactive 429 + hard 7d cap above bypass this
    # gate (they're emergencies). Default 300s = 5 minutes between ladder
    # swaps. Config-tunable via swap_hysteresis.min_seconds_between_swaps.
    hyst_cfg = config.get("swap_hysteresis", {})
    if hyst_cfg.get("enabled", True):
        min_seconds = hyst_cfg.get("min_seconds_between_swaps", 300)
        # last_auto_swap_ts, NOT last_swap_ts (2026-07-03): this gate exists to
        # stop ladder ping-pong between DAEMON swaps, so only daemon swaps arm
        # it. Launches used to arm it via last_swap_ts, deferring the ladder
        # for every slot on a freshly-launched account (see _execute_swap_locked).
        # Missing field (pre-upgrade state) = no recent auto swap; the ladder
        # threshold + min_improvement_pct still gate the swap itself.
        last_swap = active_acct.get("last_auto_swap_ts")
        if last_swap:
            try:
                last_swap_dt = datetime.fromisoformat(last_swap.replace("Z", "+00:00"))
                elapsed = (datetime.now(timezone.utc) - last_swap_dt).total_seconds()
                if elapsed < min_seconds:
                    msg = (
                        f"ladder check deferred: last auto swap was {int(elapsed)}s ago "
                        f"(< min_seconds_between_swaps={min_seconds}s)"
                    )
                    click.echo(f"  {msg}")
                    _note("swap_hysteresis", "hold", msg)
                    return None
            except ValueError:
                pass

    # Trigger 2.5: proactive burn-before-reset (GH #42). Placed AFTER the
    # universal hysteresis gate (so it can't churn — the gate above already
    # returned None if we swapped too recently) and only fires when the ladder
    # WON'T (active still below its step), so the two triggers never compete.
    # See _maybe_burn_before_reset for the full rationale + guardrails.
    cfg_steps_bbr = config.get("thresholds", {}).get("steps") or [50, 75, 90]
    ladder_threshold_bbr = active_acct.get("next_swap_at_pct", cfg_steps_bbr[0])
    if ladder_threshold_bbr >= 100:
        ladder_threshold_bbr = cfg_steps_bbr[-1]
    if current_max_pct(cur_usage, config) < ladder_threshold_bbr:
        bbr_decision = _maybe_burn_before_reset(
            state, config, current, active_acct, cur_usage, usage_by_account
        )
        if bbr_decision is not None:
            _note("burn_before_reset", "swap", bbr_decision.reason)
            return bbr_decision

    # Trigger 2: progressive ladder.
    cfg_steps = config.get("thresholds", {}).get("steps") or [50, 75, 90]
    threshold = active_acct.get("next_swap_at_pct", cfg_steps[0])
    if threshold >= 100:
        # Sentinel: the ladder ran off the end (more swap-aways than configured
        # steps). Previously this collapsed to 0% which made ANY usage trip a
        # swap — causing rapid-swap loops when both accounts ended up at the
        # sentinel and the 5h-reset wasn't yet detected. Bug fixed 2026-05-20:
        # at sentinel, treat as if we're still at the LAST configured step
        # (most aggressive but bounded). The hard 7d cap still fires above
        # this, and reactive 429 catches actually-exhausted accounts.
        threshold = cfg_steps[-1]

    # Fix A (2026-07-06 rayi3 incident): trip the ladder on the burn-EXTRAPOLATED
    # max(5h, 7d), projected forward to the next poll — NOT the raw last-polled
    # value. max() with the raw current_max_pct so extrapolation only ever trips
    # EARLIER, never masks a genuine over-step poll; inert when poll_accel is off
    # (_trig_look_ahead == 0 ⇒ extrapolate-to-now on a freshly-polled account ≈
    # raw). This is the core fix: rayi3 sat at 91%-last-poll (< 95% step) while
    # burning fast enough to hit 100% before the next poll; extrapolating its
    # ~180s poll interval forward trips the 95% step THIS cycle so the lane swaps
    # off BEFORE the native cap instead of after.
    cur_pct = current_max_pct(cur_usage, config)
    if _poll_accel_enabled(config):
        cur_pct = max(cur_pct, _account_estimated_effective_pct(
            active_acct, config, _trig_now, look_ahead_seconds=_trig_look_ahead))
    if cur_pct < threshold:
        _note("below_threshold", "hold",
              f"{current} at {cur_pct:.1f}% < ladder threshold {threshold}% — nothing to do")
        return None

    # Defer-if-5h-resetting-soon (GH #51, 2026-05-28): the ladder threshold
    # tripped, but if the pressure is the 5-hour window AND that window is about
    # to reset, swapping is pointless churn — the reset drops 5h back to ~0 on
    # its own and maybe_reset_thresholds resets the ladder. "We could have just
    # easily waited" (user). So WAIT instead of swapping.
    #
    # Guards:
    #   - only when the 5h window is the SOLE window at/over threshold: a reset
    #     zeroes 5h but leaves 7d untouched, so if 7d is also over we'd just
    #     re-trip next cycle — waiting wouldn't help, don't defer;
    #   - only when 5h resets within wait_window_minutes;
    #   - NOT when 5h is already at/above max_defer_pct (too close to the cap to
    #     gamble on waiting — usage could spike to a real 429 first; if we are
    #     wrong, the reactive-429 path is still the backstop).
    # Hard-7d-cap and reactive-429 bypass this (emergencies / 7d doesn't reset
    # soon), same convention as the growth + improvement gates below.
    defer_cfg = config.get("defer_swap_near_5h_reset", {})
    if defer_cfg.get("enabled", True):
        th_cfg = config.get("thresholds", {})
        wait_window_s = defer_cfg.get("wait_window_minutes", 15) * 60
        max_defer_pct = defer_cfg.get("max_defer_pct", 90)
        cur_5h = cur_usage.five_hour.utilization if cur_usage.five_hour else 0.0
        cur_7d_val = cur_usage.seven_day.utilization if cur_usage.seven_day else 0.0
        five_h_over = th_cfg.get("five_hour", True) and cur_5h >= threshold
        seven_d_over = th_cfg.get("seven_day", True) and cur_7d_val >= threshold
        # Fix A (2026-07-06): gate the max_defer_pct "too close to gamble" guard on
        # the burn-EXTRAPOLATED 5h, not the raw poll. Deferring a swap because 5h
        # resets soon is safe ONLY if 5h won't hit the native cap first; a fast
        # burner sitting at raw 88% but projected to 96% before the reset must NOT
        # be deferred (that is exactly the rayi3 too-late failure via this path).
        # Extrapolation only raises cur_5h_eff, so it can only make us defer LESS.
        cur_5h_eff = cur_5h
        if _poll_accel_enabled(config):
            cur_5h_eff = max(cur_5h, estimate_window_pct(
                active_acct, "5h", config, _trig_now + timedelta(seconds=_trig_look_ahead)))
        if five_h_over and not seven_d_over and cur_5h_eff < max_defer_pct:
            time_to_reset_s = _five_hour_remaining_seconds(cur_usage, active_acct)
            if time_to_reset_s is not None and 0 < time_to_reset_s <= wait_window_s:
                msg = (
                    f"ladder threshold tripped ({current} 5h={cur_5h:.0f}% >= {threshold}%) "
                    f"BUT 5h resets in {time_to_reset_s / 60:.0f}min (<= {wait_window_s // 60}min window) "
                    f"and 5h < max_defer_pct({max_defer_pct}%); WAITING for the reset instead of swapping"
                )
                click.echo(f"  {msg}")
                _note("defer_near_5h_reset", "hold", msg)
                return None

    # Usage-growth gate (GH #15): only swap via the ladder if the active
    # account's local usage is ACTUALLY GROWING since the previous poll.
    # User report 2026-05-24: "it literally spent 2 days swapping back and
    # forth with no usage on this machine." Root cause: 7d/5h numbers were
    # high due to OTHER machines' consumption, but locally we weren't
    # spending anything. Every cycle the threshold tripped on stale-high
    # numbers and triggered a swap.
    #
    # Saturation override (GH #19, fixed 2026-05-25): when cur_pct is at
    # or above `saturation_pct` (default 95%), SKIP the growth gate. At
    # full saturation the account literally can't go higher, so delta=0
    # is unavoidable — gating on growth would trap us on the maxed account
    # forever (the exact bug reported: "ran out of usage, didn't auto
    # swap"). Above saturation, the account is effectively exhausted and
    # we MUST swap regardless of whether this machine drove the growth.
    #
    # Reactive-429 and hard-7d-cap paths above intentionally bypass this
    # gate — they're emergencies. Only the ladder is gated.
    growth_cfg = config.get("usage_growth_gate", {})
    saturation_pct = growth_cfg.get("saturation_pct", 95)
    if growth_cfg.get("enabled", True) and cur_pct < saturation_pct:
        min_delta_pct = growth_cfg.get("min_delta_pct", 0.5)
        prev_5h = active_acct.get("prev_current_5h_pct")
        prev_7d = active_acct.get("prev_current_7d_pct")
        cur_5h = cur_usage.five_hour.utilization if cur_usage.five_hour else None
        cur_7d_val = cur_usage.seven_day.utilization if cur_usage.seven_day else None
        # Compute deltas where we have both prev and current readings.
        deltas = []
        if prev_5h is not None and cur_5h is not None:
            deltas.append(cur_5h - prev_5h)
        if prev_7d is not None and cur_7d_val is not None:
            deltas.append(cur_7d_val - prev_7d)
        if deltas and max(deltas) < min_delta_pct:
            # No meaningful growth. Suppress the ladder swap. We still log
            # so the operator can see the gate firing in daemon.log.
            msg = (
                f"ladder threshold tripped ({current} at {cur_pct:.1f}% >= "
                f"{threshold}%) BUT usage isn't growing on this machine "
                f"(max delta {max(deltas):.2f}% < {min_delta_pct}%); skipping swap"
            )
            click.echo(f"  {msg}")
            _note("usage_growth_gate", "hold", msg)
            return None
    elif growth_cfg.get("enabled", True) and cur_pct >= saturation_pct:
        click.echo(
            f"  ladder threshold tripped ({current} at {cur_pct:.1f}%) — at/above "
            f"saturation_pct={saturation_pct}%, bypassing growth gate"
        )

    target = pick_swap_target(state, config)
    if target is None:
        # Visibility (2026-07-03): this hold used to be SILENT — _note() only
        # writes the trace dict, it does NOT echo to daemon.log (unlike the
        # cap_degraded_target / min_improvement gates just below). This is the
        # exact branch that swallowed the "no swaps since lanes" symptom: with
        # 4 live lanes + the shared mount across 5 accounts, GH #104's
        # no-double-book exclusion leaves a hot lane at most one candidate, and
        # when that candidate is itself saturated the picker returns None. The
        # ladder trip was logged, then silence. Echo the reason; SOS Condition
        # 2b raises the operator-actionable flag (add an account / free a lane).
        msg = f"ladder tripped on {current} ({cur_pct:.1f}% >= {threshold}%) but no valid swap target"
        click.echo(f"  {msg}")
        _note("no_target", "hold", msg)
        return None

    # Anti-pingpong / cap-degraded-target gate (GH #53, 2026-05-28): refuse a
    # LADDER swap whose ONLY target is over the hard 7d cap. The hard-cap path
    # already refuses such targets (it's the user's invariant); the ladder path
    # didn't — and that completed a two-leg pingpong. Concretely: active is
    # 7d-capped (e.g. default 7d=80%), we swap to merkos (5h-saturated), merkos's
    # 5h ladder trips, and the only target is the 7d-capped default. The #40
    # improvement gate below compares the configured ladder window(s) only —
    # here 5h — so a 7d-capped account with fresh 5h (default 14%) looks like a
    # huge improvement and the swap-back fires, re-capping 7d → repeat forever.
    # This check runs BEFORE the improvement gate precisely because that gate's
    # ladder-window math is blind to the 7d cap on the target. SOS surfaces the
    # "no usable target" state. (User 2026-05-28: "we shouldn't switch to an
    # account that's going to trigger a switch back.")
    if "[DEGRADED:" in target.reason and f"no targets below 7d cap {int(hard_7d_cap)}%" in target.reason:
        msg = (
            f"ladder swap suppressed: only target {target.name} is over the 7d cap "
            f"({int(hard_7d_cap)}%) — swapping there would pingpong (it'd immediately "
            f"re-trip the hard cap); staying on {current}, SOS will surface"
        )
        click.echo(f"  {msg}")
        _note("cap_degraded_target", "hold", msg)
        return None

    # Anti-pingpong / "must improve" gate (GH #40, 2026-05-27): a LADDER swap
    # should strictly reduce the active account's effective utilization.
    # Otherwise we just churn — and live sessions take a tier-1 hit each cycle.
    #
    # Observed failure mode: both accounts' 5h windows roll over (correctly
    # resetting both ladders to the first step), but absolute usage stays in
    # the same band (e.g. default 77% / merkos 80%, both > step 70). Every
    # cycle the active account trips the ladder; `pick_swap_target`'s
    # `headroom_fallback` returns the only candidate even though it would
    # immediately re-trip its own step; the smart-strategy `burn-soon` bonus
    # can flip the pick onto the higher-usage account when its 5h resets
    # sooner. `min_seconds_between_swaps` blocks the immediate swap-back but
    # then expires — net result is a ~10-minute pingpong, not termination.
    #
    # This gate makes the ladder sequence strictly monotone in active-account
    # effective %, so it provably terminates. Saturated-active rotation still
    # works (e.g. 95% active, 71% candidate over its own step → swaps).
    # The hard-7d-cap (line ~1430) and reactive-429 paths intentionally
    # bypass this gate, same convention as `min_seconds_between_swaps`.
    #
    # `_account_effective_pct` is used on BOTH sides for metric symmetry —
    # `cur_pct` above came from `current_max_pct(cur_usage, …)` (live
    # AccountUsage) while target reads the state dict; mixing them would
    # leak 1-2pp drift through small `min_improvement_pct` margins.
    if hyst_cfg.get("enabled", True):
        min_improvement = hyst_cfg.get("min_improvement_pct", 3)
        active_eff = _account_effective_pct(active_acct, config)
        target_eff = _account_effective_pct(state["accounts"][target.name], config)
        if target_eff > active_eff - min_improvement:
            msg = (
                f"ladder swap suppressed: target {target.name} at "
                f"{target_eff:.1f}% would not improve on active {current} at "
                f"{active_eff:.1f}% by min_improvement_pct={min_improvement}pp "
                f"(picker reason: {target.reason})"
            )
            click.echo(f"  {msg}")
            _note("min_improvement_gate", "hold", msg)
            return None

    reason = f"{target.reason}; current {current} at {cur_pct:.1f}% >= {threshold}%"
    _note("ladder", "swap", reason)
    # GH #56: a ladder swap below saturation is a non-urgent balancing move →
    # lazy-deferrable (only worth a cache rebuild once the cache is cold). At or
    # above saturation_pct the active account is effectively capped, so the swap
    # is urgent (escape the cap before sessions hit a 429) → NOT deferrable.
    return SwapDecision(
        target=target.name,
        reason=reason,
        tier=determine_tier(active_acct, config),
        gate="ladder",
        deferrable=cur_pct < saturation_pct,
    )


# --------------------------------------------------------------------------
# per_session decision loop (Phase 3)
#
# In per_session mode the daemon's unit of action is a SLOT MOVE
# (slot, from_account, to_account), not a global active flip. Each account
# with live-occupied slots is evaluated against its OWN progressive ladder by
# shimming it in as decide_swap's "active" — every existing gate (hard 7d
# cap, hysteresis, growth gate, defer-near-reset, min-improvement) applies
# per account with zero duplicated logic. Executing a move is the Phase 2.1
# in-place slot swap; the session on top is never touched.
# --------------------------------------------------------------------------

# TTL cache for the occupied-slots scan: _account_poll_interval consults
# occupancy once per account per cycle, and each uncached scan walks all of
# /proc. Keyed by ACCOUNTS_DIR so tests with separate temp trees never share
# entries; a few seconds of staleness only delays a cadence/decision tweak to
# the next cycle, which the daemon tolerates by design.
_OCCUPIED_SLOTS_CACHE: dict[str, tuple[float, dict]] = {}


def occupied_slot_accounts(state: dict, max_age_seconds: float = 5.0) -> dict[str, list[str]]:
    """Map account → [slot names] for slots with a LIVE session (per /proc).

    Only live-occupied slots participate in per-slot swap decisions: an idle
    slot holding a hot account burns nothing, so moving it is pure churn (gc
    reaps it eventually). mount_in_use is the same /proc ground truth the
    rest of per_session mode uses.
    """
    key = str(ACCOUNTS_DIR)
    now = time.monotonic()
    hit = _OCCUPIED_SLOTS_CACHE.get(key)
    if hit and now - hit[0] < max_age_seconds:
        return hit[1]
    out: dict[str, list[str]] = {}
    for name, entry in sorted(state.get("slots", {}).items()):
        acct = entry.get("account")
        if not acct:
            continue
        d = slot_path(name)
        if d.exists() and mount_in_use(d):
            out.setdefault(acct, []).append(name)
    _OCCUPIED_SLOTS_CACHE[key] = (now, out)
    return out


def session_current_slot(session_id: str) -> str | None:
    """Resolve which slot a session is running under RIGHT NOW.

    sessions.log's `account` field is a launch-time binding that goes stale
    the moment the daemon moves a slot in place under a live session — but the
    session's `pane` binding is stable for the session's life. So map
    session_id → pane (from sessions.log) → mount (live /proc via
    pane_mount_name) → slot name. Returns None for bare/non-slot sessions.
    This is the trustworthy "where is this session now" primitive that both
    429 attribution and lazy-warm filtering need (review findings 2026-07-02).
    """
    pane = None
    for e in reversed(_parse_sessions_log()):
        if e.get("session_id") == session_id:
            pane = e.get("pane")
            break
    if not pane:
        return None
    mnt = pane_mount_name(pane)
    return mnt if (mnt and mnt.startswith(SLOT_PREFIX)) else None


def live_sessions_on_slot(slot_name: str) -> list:
    """Live sessions currently running under a given slot, by pane→mount (live
    /proc), NOT by sessions.log's stale launch-time account. Used for per-slot
    lazy-swap warmth checks after an in-place move has changed the slot's
    account out from under sessions.log (review finding 2026-07-02)."""
    try:
        sessions = find_live_sessions()
    except Exception:
        return []
    return [s for s in sessions if pane_mount_name(s.pane) == slot_name]


def _locked_slots(config: dict) -> set[str]:
    """Slot names frozen by `session_locks.locked_slots` (cus lock/unlock).

    A locked slot's account is never moved by the daemon — ladder, hard-cap,
    and reactive-429 slot moves all skip it, and idle slot-gc won't reap it.
    The lock is user intent ("this slot stays put no matter what"), so it
    lives in config.yaml next to the session pins, not in daemon state.
    """
    return {str(s) for s in (config.get("session_locks", {}).get("locked_slots") or [])}


# Rotation-set pools (GH #99). A slot's pool decides whether the per-model
# weekly gate applies to that slot's swap decisions. Emergent, not static:
# an account is "in" whichever pool a slot's rules let it serve.
VALID_POOLS = ("premium", "standard")


def _slot_pool(state: dict, slot_name: str, config: dict) -> str:
    """Rotation-set pool a slot belongs to: state.slots[name].pool, falling
    back to per_session.default_pool. Unknown/missing values fall back too, so
    slots created before pools existed read as the default (premium)."""
    default = config.get("per_session", {}).get("default_pool", "premium")
    p = (state.get("slots", {}).get(slot_name, {}) or {}).get("pool") or default
    return p if p in VALID_POOLS else default


def _config_for_pool(config: dict, pool: str) -> dict:
    """Config view used for a pool's swap decision (GH #99).

    Standard-pool slots IGNORE the per-model weekly gate — a model-exhausted
    account still has aggregate headroom that serves standard-model work, so a
    standard slot should keep using it rather than evacuate like a premium slot
    does. We express that by shallow-copying the config with
    per_model_weekly.gate_enabled forced False, which turns the model gate into
    a no-op everywhere it's consulted (hard-cap force-swap AND target filter)
    with zero new branches in decide_swap / pick_swap_target. Premium slots use
    the config unchanged. No-op (returns the original object) when the gate is
    already off, so non-gated installs pay nothing.
    """
    if pool == "standard" and config.get("per_model_weekly", {}).get("gate_enabled"):
        view = dict(config)
        pmw = dict(config.get("per_model_weekly", {}))
        pmw["gate_enabled"] = False
        view["per_model_weekly"] = pmw
        return view
    return config


def _live_slot_accounts(state: dict) -> set[str]:
    """Accounts currently held by a LIVE-occupied slot (per /proc ground truth).

    Used to keep every live mount on a DISTINCT account (GH #104): two live
    mounts on one account both refresh its single-use OAuth token, rotating it
    out from under the other and logging that session out. The shared-mount and
    per-slot swap pickers exclude these so a mount never lands on an account
    another mount is already signed in on.
    """
    return set(occupied_slot_accounts(state).keys())


def _state_excluding_accounts(state: dict, keep: str | None, drop: set) -> dict:
    """Shallow state copy whose `accounts` dict omits `drop` (minus `keep`), so
    a swap-target picker cannot choose an account another live mount already
    holds (GH #104). `keep` is the mount's own current account, always retained
    (it's the one being evaluated / left). Returns the original state unchanged
    when there is nothing to drop."""
    drop = set(drop) - ({keep} if keep else set())
    if not drop:
        return state
    shim = dict(state)
    shim["accounts"] = {n: a for n, a in state.get("accounts", {}).items() if n not in drop}
    return shim


def decide_slot_swaps(state: dict, config: dict, usage_by_account: dict[str, "AccountUsage"],
                      traces: dict | None = None, exclude_accounts: set | None = None) -> list[dict]:
    """Per-slot swap decisions for one cycle (Phase 3.1).

    Returns a list of move dicts: {"slot", "from", "to", "gate", "tier",
    "deferrable", "reason", "pool"}. Multiple slots on the same hot account
    FAN OUT to distinct targets (best-headroom-first — each pick excludes
    targets already taken this round) rather than piling onto one; when the
    account pool runs out of distinct targets, remaining slots HOLD on their
    current account. (Superseded 2026-07-03: the original "doubling up beats
    staying on a tripped account" fallback put one account's token family on
    two live mounts — the exact #104 clobber, hit live on slot-3/slot-4 that
    day. A parked hot slot degrades gracefully; a clobbered one logs a session
    out. Exception: a slot with its own independent login for the target may
    still double-book — distinct family, no clobber, GH #109 Phase 3b.)

    Slots are grouped by (account, pool): slots on the same account but in
    different rotation-set pools get DIFFERENT decisions — a premium slot
    honors the per-model weekly gate (swaps off a model-exhausted account) while
    a standard slot ignores it (keeps using that account's aggregate headroom) —
    so they can't share one decide_swap. See `_config_for_pool` / GH #99.

    `exclude_accounts` (GH #104): accounts NO slot may swap onto — e.g. the
    account the shared mount holds in hybrid mode, so a slot never double-books
    it. A group's OWN account is never excluded from itself (it's the one being
    evaluated/left). Seeded into `taken` so fan-out re-picks avoid them too.

    `traces` (optional): "account:pool" → decide_swap trace dict, for logs.
    """
    exclude = set(exclude_accounts or ())
    moves: list[dict] = []
    # Seed with the cross-mount exclusions so BOTH the primary pick (via the
    # per-group accounts shim below) and the fan-out re-pick avoid them.
    taken: set[str] = set(exclude)
    # Per-cycle family-consumption tally (fix 2026-07-05): how many moves this
    # round have already been aimed at each target, so a same-cycle pool double-
    # book can't exceed the target's distinct login families (see the can_pool
    # gate below). Mirrors the reactive path's round_claims.
    round_claims: dict[str, int] = {}
    locked = _locked_slots(config)
    occupied = occupied_slot_accounts(state)
    # Anti-clustering lane-load base (2026-07-06): account → number of live lanes
    # it ALREADY backs at cycle start. Fed (merged with this-cycle's round_claims)
    # into pick_swap_target's score penalty AND consulted by the deferrable-move
    # HOLD gate below, so the daemon spreads lanes one-per-account instead of
    # piling every lane onto the single burn-soon magnet (the rayi5→rayi4 pile-up).
    base_lane_load: dict[str, int] = {a: len(s) for a, s in occupied.items()}

    def _cur_lane_load() -> dict[str, int]:
        """account → lanes it will back = pre-existing live lanes + moves already
        queued onto it THIS cycle. Recomputed per pick so each successive fan-out
        re-pick sees the accreting load and steers to a fresh account."""
        names = set(base_lane_load) | set(round_claims)
        return {a: base_lane_load.get(a, 0) + round_claims.get(a, 0) for a in names}

    # Group occupied, unlocked slots by (account, pool). Locked slots are frozen
    # — dropped here so a group whose slots are all locked never burns a
    # decide_swap and fan-out never counts them.
    groups: dict[tuple[str, str], list[str]] = {}
    for acct_name, slots in occupied.items():
        for s in slots:
            if s in locked:
                click.echo(f"  skip {s}: locked (session_locks.locked_slots)")
                continue
            groups.setdefault((acct_name, _slot_pool(state, s, config)), []).append(s)
    for (acct_name, pool), slots in sorted(groups.items()):
        cfg_view = _config_for_pool(config, pool)
        shim = dict(state)
        shim["active"] = acct_name
        # Drop cross-mount-excluded accounts from the candidate pool for THIS
        # group's decision (keep the group's own account — it's being left).
        drop = exclude - {acct_name}
        # Independent-login POOL rescue (2026-07-03): an account is only excluded
        # because a COPY onto a second live mount would clobber its shared token
        # family (#104). If that account has a FREE pooled login family, a swap
        # can claim it (distinct family) — so it's a legal target, un-drop it.
        # Without this a hot lane whose only headroom sits behind held accounts
        # had NO target and held forever (the "no swaps since lanes" symptom).
        # Execution (_execute_swap_locked) does the actual claim + lease and
        # REFUSES if the pool turns out exhausted, so this can't cause a clobber.
        # Gated on use_independent_logins — OFF (default) leaves drop unchanged.
        if drop and independent_logins_enabled(config):
            rescuable = {x for x in drop if has_free_login_family(x, state)}
            drop = drop - rescuable
        if drop:
            shim["accounts"] = {n: a for n, a in state.get("accounts", {}).items() if n not in drop}
        # Anti-cluster: expose the current lane-load so the PRIMARY pick (through
        # decide_swap → pick_swap_target) penalizes an account already backing
        # lanes and prefers a distinct empty one.
        shim["_lane_load"] = _cur_lane_load()
        trace: dict = {}
        decision = decide_swap(shim, cfg_view, usage_by_account, trace)
        if traces is not None:
            traces[f"{acct_name}:{pool}"] = trace
        if decision is None:
            continue
        for slot_name in sorted(slots):
            target = decision.target
            reason = decision.reason
            if target in taken:
                # The primary pick is unavailable to THIS slot as a plain
                # snapshot-copy: another live mount holds it (cross-mount
                # exclusion) or an earlier slot took it this round (fan-out).
                # Two escape hatches; healthy fan-out wins, then pool:
                #
                #   1. Fan-out re-pick — a DIFFERENT account with this round's
                #      taken targets excluded (the hot account stays excluded as
                #      the shim's active; same pool-adjusted config so a
                #      standard slot's re-pick also ignores the model gate).
                #      Accepted only when HEALTHY (wouldn't immediately re-trip
                #      its own ladder), or when the pool hatch below can't take
                #      the pick anyway (old fall-back-somewhere behavior).
                #   2. Pool double-book — keep the pick and claim a distinct
                #      login family for this mount (free pooled family, or a
                #      legacy per-slot login — #109 3b). No #104 clobber:
                #      execution does the claim and REFUSES if the pool turns
                #      out exhausted, so an optimistic keep here is safe.
                #
                # Regression guard (2026-07-03, slot-2 → rayi3@99% incident):
                # the pool rescue above un-drops held-but-poolable accounts for
                # the PICK, but `taken` is seeded with the raw exclude set — so
                # the rescued pick always died at this veto, and the re-pick
                # ran on degraded leftovers: pick_swap_target's headroom_fallback
                # happily returned a 99%-5h account ("swap somewhere beats
                # sitting on a hot active"), which beat the 0%-usage pooled
                # pick and rate-limited the live session within a minute.
                # Hence "healthy" gate on the retry: a re-pick that would
                # re-trip on arrival loses to a pooled family on the account
                # the strategy actually scored best.
                # can_pool is capacity-aware (2026-07-05): the target may be
                # double-booked only while it still has a DISTINCT login family
                # free AFTER the moves already queued this cycle. Checking the
                # static has_free_login_family (as before) let several slots on
                # one hot account fan out onto a SINGLE free family in the same
                # cycle — the GH #104 clobber (2026-07-05 rayi1 pile-up). Gate
                # off ⇒ capacity 0 ⇒ can_pool False ⇒ HOLD, exactly as before.
                can_pool = (independent_logins_enabled(cfg_view)
                            and round_claims.get(target, 0) < _distinct_family_capacity(
                                target, state, cfg_view, slot_name))
                shim2 = dict(shim)
                shim2["accounts"] = {n: a for n, a in state.get("accounts", {}).items() if n not in taken}
                # Anti-cluster: the re-pick sees the accreting lane-load too, so it
                # steers to a fresh account rather than the loaded magnet.
                shim2["_lane_load"] = _cur_lane_load()
                retry = pick_swap_target(shim2, cfg_view)
                retry_healthy = retry is not None and not _target_would_immediately_re_trip(
                    state.get("accounts", {}).get(retry.name, {}), cfg_view)
                # Anti-clustering HOLD (2026-07-06 rayi5→rayi4 pile-up). The #109
                # pool double-book below is what lets several lanes STACK onto one
                # burn-soon account in a single cycle (4 mounts on 3 families → the
                # shared token diverged → the "mount blanked" auto-heal loop, and
                # they then all hit that account's 5h cap together). The whole
                # incident was `burn_before_reset` moves — a pure OPTIMIZATION that
                # fires only while the lane's current account is BELOW its own step
                # (decide_swap gates bbr on current_max < ladder_threshold), i.e.
                # the lane does NOT need to move at all. When such a move would grow
                # a target past `max_stack` live lanes (existing occupancy + this
                # cycle's claims), HOLD: the lane keeps its perfectly-usable current
                # account and can move to a DISTINCT account next cycle. Scoped to
                # the bbr gate on purpose — a LADDER move means the source IS hot
                # (at/over its step) and genuinely needs off, so when clean accounts
                # are scarce it still pool-double-books (the task's "allow stacking
                # when clean accounts are scarce"); likewise hard-cap / reactive-429
                # escapes (deferrable=False) must go somewhere. The #104/#109 family
                # accounting guards those last-resort stacks. Gated on
                # spread_lanes.enabled (off ⇒ pre-fix path, bit-for-bit).
                sl_cfg = config.get("spread_lanes", {})
                spread_on = _spread_lanes_enabled(config)
                max_stack = int(sl_cfg.get("max_stack", 1))
                target_lane_load = base_lane_load.get(target, 0) + round_claims.get(target, 0)
                cluster_hold = (spread_on and decision.gate == "burn_before_reset"
                                and target_lane_load >= max_stack)
                if retry is not None and (retry_healthy or not can_pool):
                    # Re-pick reasons were silently dropped pre-2026-07-03 —
                    # decisions.jsonl showed the ORIGINAL pick's stats next to
                    # the re-picked target (log said "5h=0%" for a 99% move).
                    target, reason = retry.name, retry.reason
                elif cluster_hold:
                    # Would pile a non-urgent move onto an already-loaded account —
                    # keep the lane put instead (anti-clustering).
                    click.echo(f"  {slot_name}: holding on '{acct_name}' — not stacking a "
                               f"non-urgent {decision.gate} move onto '{target}', which already "
                               f"backs {target_lane_load} live lane(s) (spread_lanes.max_stack="
                               f"{max_stack}); the lane stays put and can move to a distinct "
                               "account next cycle (2026-07-06 lane-clustering incident)")
                    continue
                elif can_pool:
                    reason = (f"{reason} (pool double-book: '{target}' backs another "
                              f"live mount; claiming a distinct login family — #109)")
                else:
                    # No valid DISTINCT target for this slot, and the original
                    # pick can't be pool-double-booked (no free login family).
                    # This branch == (retry is None and not can_pool); the
                    # re-pick returned None for one of two reasons:
                    #   1. Every other account is saturated / token_expired /
                    #      would immediately re-trip its own ladder. Pre-2026-07-03
                    #      the fallback DOUBLED UP on the original target here —
                    #      installing one token family on two live mounts, the
                    #      exact clobber #104 forbids (hit live on slot-3/slot-4,
                    #      2026-07-03: one refresh logs the other out).
                    #   2. (2026-07-05, this PR) a PREMIUM lane whose every
                    #      remaining candidate is at/over the per-model Fable
                    #      weekly cap — pick_swap_target now HOLDS rather than
                    #      landing a premium lane on a Fable-dead account (the
                    #      per-model analog of #139's hard-cap HOLD). This is the
                    #      exact rayi4 fan-out incident: the first two lanes took
                    #      the clean Fable targets, the third had only Fable-capped
                    #      accounts left.
                    # Either way HOLD: a parked hot slot degrades gracefully and
                    # can move next cycle when a clean target frees (e.g. after a
                    # 5h reset); staying on the hot account is strictly better
                    # than a Fable-dead landing or a #104 clobber.
                    click.echo(f"  {slot_name}: holding on '{acct_name}' — no valid distinct swap "
                               f"target for this {pool} lane (remaining candidates excluded: "
                               f"saturated / would re-trip, or over the per-model Fable weekly cap "
                               f"for a premium lane; won't double-book '{target}' onto a second "
                               f"live mount — GH #104 / #139)")
                    continue
            taken.add(target)
            round_claims[target] = round_claims.get(target, 0) + 1
            moves.append({
                "slot": slot_name, "from": acct_name, "to": target,
                "gate": decision.gate, "tier": decision.tier,
                "deferrable": decision.deferrable, "reason": reason,
                "pool": pool,
            })
    return moves


def check_rate_limit_reactive_per_session(state: dict, config: dict, entries: list | None = None,
                                          exclude_accounts: set | None = None) -> list[dict]:
    """per_session counterpart of check_rate_limit_reactive (Phase 3.1/4.2).

    Attributes fresh 429s to the SLOT the offending session runs on right now
    (session → pane → live mount → slot → current occupant), NOT to the
    launch-time account in sessions.log — that field goes stale after the
    daemon moves a slot in place, which would misattribute the 429 to the
    wrong account and move the wrong (or no) slot (review finding 2026-07-02).
    Returns urgent (non-deferrable, tier-3) moves for the exact slots that hit
    429s. A 429 from a bare session (no slot) yields no move — bare mounts are
    observe-only in this mode; SOS surfaces it.

    Mutates state["last_429_check_ts"] exactly like the global variant;
    caller persists state.

    `entries` (GH #99 hybrid): when None (default) reads the 429 log and
    advances the watermark itself. Hybrid mode passes the SLOT-attributable
    partition of a single read and owns the watermark, so we don't re-read or
    re-advance here (see check_rate_limit_reactive for the partition rationale).
    """
    if not config.get("reactive", {}).get("enabled", True):
        return []
    if entries is None:
        entries = _read_rate_limit_log_since(state.get("last_429_check_ts"))
        state["last_429_check_ts"] = now_iso()
    if not entries:
        return []
    # Resolve each 429'd session to the slot it is CURRENTLY on, then that
    # slot's current occupant. Dedupe: one move per hit slot.
    slot_to_account: dict[str, str] = {}
    locked = _locked_slots(config)
    for e in entries:
        slot_name = session_current_slot(e["session_id"])
        if not slot_name:
            continue  # bare session or resolvable-to-no-slot → observe-only
        if slot_name in locked:
            # The user froze this slot; even a real 429 doesn't move it.
            # SOS surfaces the exhausted-account condition instead.
            click.echo(f"  reactive-429 on {slot_name}: locked — not moving (session_locks.locked_slots)")
            continue
        acct = state.get("slots", {}).get(slot_name, {}).get("account")
        if acct:
            slot_to_account[slot_name] = acct
    if not slot_to_account:
        return []
    hyst = config.get("swap_hysteresis", {})
    min_gap = hyst.get("min_seconds_between_reactive_swaps", 60) if hyst.get("enabled", True) else 0
    moves: list[dict] = []
    # Seed with cross-mount exclusions (GH #104): even an urgent 429 escape must
    # not land on an account another live mount holds.
    taken: set[str] = set(exclude_accounts or ())
    # Per-cycle family-consumption tally (fix 2026-07-05): how many moves this
    # round have already been aimed at each target. A target may be picked only
    # while it still has a DISTINCT login family free AFTER the moves already
    # queued this cycle (each will claim one). Without this, two 429s in one cycle
    # both saw the same single free family in the static snapshot and both
    # committed onto it — the exact GH #104 clobber the pool exists to prevent
    # (execution refuses the 2nd, but noisily, and the lane is stranded anyway).
    round_claims: dict[str, int] = {}
    for slot_name, acct in sorted(slot_to_account.items()):
        last_swap = state.get("accounts", {}).get(acct, {}).get("last_swap_ts")
        if min_gap and last_swap:
            try:
                elapsed = (datetime.now(timezone.utc) - datetime.fromisoformat(last_swap.replace("Z", "+00:00"))).total_seconds()
                if elapsed < min_gap:
                    continue  # reactive hysteresis: this account just moved
            except ValueError:
                pass
        shim = dict(state)
        shim["active"] = acct
        # Keep the slot's own account present (pick excludes the active anyway);
        # drop this-round targets + cross-mount exclusions.
        drop = {n for n in taken if n != acct}
        # Independent-login POOL rescue (2026-07-03), now with same-round capacity
        # accounting (2026-07-05): a 429'd slot may escape onto a held account only
        # while that account still has a DISTINCT login family free AFTER the moves
        # already queued this cycle — each queued move onto it will claim one. The
        # swap installs that distinct family (no clobber). A held account whose
        # remaining capacity has reached zero stays EXCLUDED, so the picker never
        # returns it and the lane holds instead of over-subscribing. Same relaxation
        # as the proactive ladder path; execution does the claim + refuses if the
        # pool turns out exhausted. Gated on use_independent_logins: off ⇒ no
        # families ⇒ held accounts have capacity 0 ⇒ drop is unchanged (never
        # double-book — the pre-pool #104 rule), so gate-off behavior is identical.
        if drop and independent_logins_enabled(config):
            drop = {x for x in drop
                    if _distinct_family_capacity(x, state, config, slot_name)
                       - round_claims.get(x, 0) <= 0}
        shim["accounts"] = {n: a for n, a in state.get("accounts", {}).items() if n == acct or n not in drop}
        # Anti-cluster (2026-07-06): even an urgent 429 escape prefers a distinct,
        # unloaded account over one already backing lanes. Load = live occupancy +
        # this-cycle's reactive claims (round_claims). No HOLD here — a 429'd lane
        # MUST leave — but the penalty biases WHICH account it lands on.
        occ_now = occupied_slot_accounts(state)
        shim["_lane_load"] = {a: len(occ_now.get(a, [])) + round_claims.get(a, 0)
                              for a in set(occ_now) | set(round_claims)}
        # Pick the escape target under the offending slot's pool rules (GH #99):
        # a standard slot's 429 escape may land on a model-exhausted account,
        # a premium slot's may not.
        pool = _slot_pool(state, slot_name, config)
        target = pick_swap_target(shim, _config_for_pool(config, pool))
        if target is None:
            continue  # nowhere safe to go; SOS handles the all-full case
        # Authoritative capacity HOLD (fix 2026-07-05, GH #104 reactive over-
        # subscribe): never commit a move that would put more concurrent live
        # mounts on `target` than it has distinct login families to cover them.
        # The un-drop above already keeps exhausted HELD accounts out of the
        # candidate pool, so pick rarely returns one — but this also catches an
        # unheld account picked twice in one cycle, and makes the reactive path
        # degrade with a clear HOLD line rather than relying on the execution
        # layer's downstream 'pool exhausted' refusal (which strands the 429'd
        # lane on its hot account just the same, only noisier). Incident 2026-07-05:
        # 6 lanes landed on rayi1's 2 families and the shared token diverged.
        if round_claims.get(target.name, 0) >= _distinct_family_capacity(target.name, state, config, slot_name):
            click.echo(f"  reactive-429 on {slot_name}: HOLDING on '{acct}' — no distinct login "
                       f"family free for '{target.name}' (a 2nd live mount would clobber its shared "
                       f"token family — GH #104). Provision one: `cus login-mount {target.name}`")
            continue
        round_claims[target.name] = round_claims.get(target.name, 0) + 1
        taken.add(target.name)
        moves.append({
            "slot": slot_name, "from": acct, "to": target.name,
            "gate": "reactive_429", "tier": 3, "deferrable": False,
            "reason": f"user-facing 429 on the '{acct}' session in {slot_name}; {target.reason}",
            "pool": pool,
        })
    return moves


def _lazy_warm_slot_sessions(move: dict, config: dict) -> list:
    """Per-move lazy_swap gate (Phase 3.2): the sessions a slot move would
    cache-bust, when the move is deferrable and any of them is still warm.

    Scoped to the move's SLOT via live pane→mount resolution, NOT to the
    outgoing account via sessions.log: after a slot's first in-place move,
    sessions.log still records the launch-time account, so an account_filter
    would match nothing and silently skip the deferral it exists to provide
    (review finding 2026-07-02). Urgent moves (hard cap / saturation /
    reactive 429) return [] — swap regardless.
    """
    if config.get("hot_swap", {}).get("enabled", False):
        return []
    lazy = config.get("lazy_swap", {})
    if not lazy.get("enabled", True):
        return []
    if not move.get("deferrable", True):
        return []
    window = lazy.get("cache_window_seconds", 300)
    sessions = live_sessions_on_slot(move["slot"])
    return [s for s in sessions if s.transcript_path and cache_warm(s.transcript_path, window)]


def _slot_saveback_and_gc(state: dict, config: dict, no_execute: bool) -> None:
    """Periodic per-slot credential save-back + long-idle slot gc. Shared by
    the per_session and hybrid cycles (extracted 2026-07-02, GH #99)."""
    # 3.5 — periodic save-back: a live session refreshes OAuth tokens into its
    # slot's .credentials.json; without this, a machine crash loses the newest
    # refresh token for every slotted account. only_if_fresher keeps it a
    # no-op (no writes, no backup churn) until a session actually refreshed.
    for name, entry in sorted(state.get("slots", {}).items()):
        acct = entry.get("account")
        d = slot_path(name)
        if acct and d.exists():
            r = saveback_mount_credentials(d, acct, state, only_if_fresher=True)
            if r["action"] != "skipped":
                # Leased mounts save to their pooled family dir, not the
                # account snapshot — say which, so the log doesn't read as a
                # snapshot stomp (the pre-fix 2026-07-03 symptom line).
                dest = (f"logins/{r['account']}/{r['family']}" if r.get("family")
                        else f"account-{r['account']}")
                click.echo(f"  save-back {name}: {r['action']} → {dest}")

    # Slot gc (risk #5 in the plan: dead mounts holding real credentials).
    # Only long-idle slots — a slot free for minutes is a REUSE candidate for
    # the next `cus launch` (free launch, no swap), so reaping it eagerly
    # would cost more than it saves. Unparseable/missing timestamps fail SAFE
    # (never delete on bad data; doctor flags orphans instead).
    gc_hours = config.get("per_session", {}).get("slot_gc_idle_hours", 72)
    for name in list(state.get("slots", {})):
        d = slot_path(name)
        entry = state.get("slots", {}).get(name, {})
        # Busy = live PID OR fresh reservation (an in-flight launch mid-install
        # that hasn't exec'd claude yet — don't reap the dir out from under it).
        if d.exists() and _slot_busy(name, entry):
            continue
        ref_ts = entry.get("last_launch_ts") or entry.get("created_ts")
        if not ref_ts:
            continue
        try:
            idle_s = (datetime.now(timezone.utc) - datetime.fromisoformat(ref_ts.replace("Z", "+00:00"))).total_seconds()
        except ValueError:
            continue
        if name in _locked_slots(config):
            continue  # locked slot: standing reservation; never idle-reaped
        if idle_s >= gc_hours * 3600 and not no_execute:
            result = gc_slot(name, state)
            if result["action"] == "reaped":
                sb = result["saveback"]
                click.echo(f"  lane-gc: reaped {name} (idle {idle_s/3600:.0f}h; save-back {sb['action']})")


def _execute_slot_moves(moves: list[dict], state: dict, config: dict, no_execute: bool) -> None:
    """Execute (or dry-run/lazy-defer) a list of slot move dicts. Shared by the
    per_session and hybrid cycles (extracted 2026-07-02, GH #99).

    Fan-out ladder dedup (review finding 2026-07-02): several slots on ONE hot
    account move in the same cycle, but that account's ladder step is shared and
    must advance exactly once — the first executed move bumps it, the rest pass
    bump_ladder=False.
    """
    ladder_bumped: set[str] = set()
    for move in moves:
        label = f"{move['slot']}: {move['from']} -> {move['to']}"
        warm = _lazy_warm_slot_sessions(move, config)
        if warm:
            msg = (f"lazy-swap DEFER {label}: {len(warm)} cache-warm session(s) on "
                   f"'{move['from']}' — non-urgent {move['gate']} move would burn cache (GH #56)")
            click.echo(f"  {msg}")
            _log_decision(_build_decision_record(
                state, config, action="defer", gate="lazy_swap", reason=msg,
                target=move["to"], tier=move["tier"], where={"slot": move["slot"]},
            ))
            continue
        click.echo(f"  lane move: {label} ({move['gate']})")
        click.echo(f"    reason: {move['reason']}")
        if no_execute:
            click.echo("    (--no-execute) skipping lane move")
            _log_decision(_build_decision_record(
                state, config, action="would_swap", gate=move["gate"], reason=move["reason"],
                target=move["to"], tier=move["tier"], where={"slot": move["slot"], "from": move["from"]},
            ))
            continue
        try:
            bump = move["from"] not in ladder_bumped
            execute_swap(move["to"], trigger=f"auto-{move['gate']}", slot=move["slot"], bump_ladder=bump)
            ladder_bumped.add(move["from"])
            click.echo(f"    moved in place — session on {move['slot']} continues uninterrupted")
            _log_decision(_build_decision_record(
                state, config, action="swap", gate=move["gate"], reason=move["reason"],
                target=move["to"], tier=move["tier"], where={"slot": move["slot"], "from": move["from"]},
            ))
        except (RuntimeError, FileNotFoundError, ValueError) as e:
            click.echo(f"    lane move FAILED: {type(e).__name__}: {e}", err=True)
            _log_decision(_build_decision_record(
                state, config, action="error", gate=move["gate"],
                reason=f"{move['reason']} — FAILED: {e}",
                target=move["to"], tier=move["tier"], where={"slot": move["slot"], "from": move["from"]},
            ))


def _per_session_cycle(state: dict, config: dict, usage_by_account: dict, no_execute: bool) -> None:
    """The per_session daemon cycle after polling (Phase 3): periodic
    save-back, long-idle slot gc, reactive-429 moves, ladder moves — all
    per slot, never touching ~/.claude/ (bare mounts are observe-only, 3.4).

    Caller (one_cycle) has already merged fresh usage into state; this
    function persists state (usage + 429 watermark) before executing moves,
    mirroring the global path's crash ordering.
    """
    _slot_saveback_and_gc(state, config, no_execute)

    # Cross-mount exclusions (GH #104), fix 2026-07-05. In per_session mode every
    # live slot holds its own account, and no lane may move onto an account ANOTHER
    # live slot already holds — two mounts on one account rotate its single-use
    # OAuth refresh token and log one out. Hybrid mode has always computed and
    # passed this exclusion (see _hybrid_cycle's exclude_for_slots); per_session —
    # the PRIMARY live mode — never did, so decide_slot_swaps and
    # check_rate_limit_reactive_per_session ran with an EMPTY exclusion set. Their
    # #104 clobber guards only fire for an account already in `taken`, so with an
    # empty seed they never engaged for accounts held by OTHER live slots: the
    # first lane to best-target a low-usage account already live elsewhere sailed
    # straight past the HOLD/pool-rescue logic into a plain double-book. That let
    # the reactive-429 and ladder paths pile several lanes onto one account past
    # its login families — the 2026-07-05 rayi1 pile-up (6 lanes, 2 families →
    # the shared refresh token diverged and clobbered the losers, GH #104). Seed
    # the exclusion exactly as hybrid does so both paths' pool-rescue/HOLD logic
    # engages. Unconditional (not gated on the pool feature), matching hybrid: with
    # the pool off it simply excludes every already-live account (never double-book).
    exclude_for_slots = _live_slot_accounts(state)

    # Urgent reactive-429 moves preempt ladder moves (same precedence as the
    # global cycle's step 0).
    moves = check_rate_limit_reactive_per_session(state, config, exclude_accounts=exclude_for_slots)
    traces: dict = {}
    if not moves:
        moves = decide_slot_swaps(state, config, usage_by_account, traces,
                                  exclude_accounts=exclude_for_slots)

    # Persist usage + 429 watermark + gc'd slots BEFORE acting, so a crash
    # mid-move leaves valid state (same ordering as the global path).
    save_state(state)

    if not moves:
        occupied = occupied_slot_accounts(state)
        _log_decision(_build_decision_record(
            state, config, action="hold", gate="no_slot_moves",
            reason=f"no per-slot moves; occupied: " + (", ".join(f"{a}({'+'.join(s)})" for a, s in sorted(occupied.items())) or "none"),
        ))
        for name, acct in state.get("accounts", {}).items():
            occ = " [" + "+".join(occupied[name]) + "]" if name in occupied else ""
            te = " (TOKEN_EXPIRED)" if acct.get("token_expired") else ""
            click.echo(f"   {name}{occ}: 5h={acct.get('current_5h_pct', 0):.1f}%, 7d={acct.get('current_7d_pct', 0):.1f}%, next={acct.get('next_swap_at_pct', 50)}%{te}")
        return

    _execute_slot_moves(moves, state, config, no_execute)


def _execute_global_mount_swap(decision: "SwapDecision", state: dict, config: dict, no_execute: bool) -> None:
    """Execute (or dry-run/lazy-defer) a swap of the shared ~/.claude/ mount —
    the global path's decide→defer→execute logic, minus SOS (caller emits it).

    Used by hybrid mode to manage bare sessions alongside slots (GH #99). Global
    mode keeps its own inline copy in one_cycle so its tested flow is untouched;
    this deliberately mirrors it. Both are candidates to unify once hybrid is
    proven in the field.
    """
    if decision is None:
        return
    click.echo(f"  global (shared mount) swap: {state['active']} -> {decision.target} (tier {decision.tier})")
    click.echo(f"    reason: {decision.reason}")
    lazy_warm = _lazy_warm_sessions(decision, config)
    if lazy_warm:
        warm_panes = sorted({s.pane for s in lazy_warm if s.pane and s.pane != "no-tmux"})
        msg = (f"lazy-swap DEFER (shared mount): {len(lazy_warm)} cache-warm bare session(s) "
               f"across {len(warm_panes)} pane(s) — non-urgent {decision.gate} swap to "
               f"{decision.target} would burn cache (GH #56)")
        click.echo(f"  {msg}")
        _log_decision(_build_decision_record(
            state, config, action="defer", gate="lazy_swap", reason=msg,
            target=decision.target, tier=decision.tier,
            where={"warm_pane_count": len(warm_panes), "panes": warm_panes},
        ))
        return
    if no_execute:
        click.echo("    (--no-execute) skipping shared-mount swap")
        _log_decision(_build_decision_record(
            state, config, action="would_swap", gate=decision.gate,
            reason=decision.reason, target=decision.target, tier=decision.tier,
            where=_migrating_panes(),
        ))
        return
    if config.get("hot_swap", {}).get("enabled", False):
        try:
            hot_swap_orchestrate(decision, state, config)
        except NameError:
            execute_swap(decision.target, trigger=f"auto-tier{decision.tier}")
    else:
        execute_swap(decision.target, trigger=f"auto-tier{decision.tier}")
        click.echo(f"    swapped shared mount (bare sessions follow; lanes unaffected)")
    _log_decision(_build_decision_record(
        state, config, action="swap", gate=decision.gate,
        reason=decision.reason, target=decision.target, tier=decision.tier,
        where=_migrating_panes(),
    ))


def _hybrid_cycle(state: dict, config: dict, usage_by_account: dict, no_execute: bool) -> None:
    """Hybrid daemon cycle (GH #99): manage per-session SLOTS individually AND
    the shared ~/.claude/ mount for bare sessions, in ONE cycle.

    Slots move in place (no session restart); the shared mount swaps like global
    mode (every bare session follows it on its next request). Reactive 429s are
    partitioned from a SINGLE log read: a slot-attributable 429 moves only that
    slot, a bare-session 429 moves only the shared mount — never both, and the
    watermark advances exactly once.

    No double-booking (GH #104): every live mount must end on a DISTINCT
    account — two mounts on one account rotate its single-use refresh token and
    log one out. So slot swaps exclude the shared mount's account, and the
    shared-mount swap excludes every account a slot holds (or is moving onto)
    this cycle.
    """
    _slot_saveback_and_gc(state, config, no_execute)

    # Cross-mount exclusions (GH #104). The shared mount holds state["active"];
    # each live slot holds its own account. A slot must never move onto the
    # shared account or another slot's account; the shared mount must never move
    # onto any slot's account.
    shared_active = state.get("active")
    slot_accts = _live_slot_accounts(state)
    exclude_for_slots = slot_accts | ({shared_active} if shared_active else set())

    # Reactive 429 — partition one read so slots and the shared mount don't
    # both act on the same entry (see the reactive fns' `entries` param).
    slot_moves: list[dict] = []
    bare_decision = None
    if config.get("reactive", {}).get("enabled", True):
        all_entries = _read_rate_limit_log_since(state.get("last_429_check_ts"))
        state["last_429_check_ts"] = now_iso()
        slot_entries, bare_entries = [], []
        for e in all_entries:
            (slot_entries if session_current_slot(e["session_id"]) else bare_entries).append(e)
        slot_moves = check_rate_limit_reactive_per_session(
            state, config, entries=slot_entries, exclude_accounts=exclude_for_slots)
        # Bare reactive escape must avoid accounts slots hold or are moving onto.
        bare_excl = slot_accts | {m["to"] for m in slot_moves}
        bare_decision = check_rate_limit_reactive(
            _state_excluding_accounts(state, shared_active, bare_excl), config, entries=bare_entries)

    # Proactive slot moves only when no urgent slot 429 preempts them.
    traces: dict = {}
    if not slot_moves:
        slot_moves = decide_slot_swaps(state, config, usage_by_account, traces,
                                       exclude_accounts=exclude_for_slots)

    # Proactive shared-mount swap (for bare sessions) only when no urgent bare
    # 429 preempts it. decide_swap operates on state["active"] = the shared
    # mount's account; its candidate pool excludes every account a slot holds or
    # is moving onto this cycle, so the shared mount can't double-book one.
    if bare_decision is not None:
        global_decision = bare_decision
    else:
        shared_excl = slot_accts | {m["to"] for m in slot_moves}
        global_decision = decide_swap(
            _state_excluding_accounts(state, shared_active, shared_excl), config, usage_by_account, {})

    # Persist usage + 429 watermark BEFORE acting (crash-safe ordering).
    save_state(state)

    if slot_moves:
        _execute_slot_moves(slot_moves, state, config, no_execute)
    _execute_global_mount_swap(global_decision, state, config, no_execute)

    if not slot_moves and global_decision is None:
        occupied = occupied_slot_accounts(state)
        _log_decision(_build_decision_record(
            state, config, action="hold", gate="no_moves",
            reason="hybrid: no slot moves and no shared-mount swap; occupied: "
                   + (", ".join(f"{a}({'+'.join(s)})" for a, s in sorted(occupied.items())) or "none"),
        ))


def update_state_with_usage(state: dict, usage_by_account: dict[str, AccountUsage]) -> dict:
    """Mutate state.json's per-account current_*_pct from a poll cycle.

    Each per-account update follows one of five exclusive branches:
      0. token_stale (stored access token aged out — refresh token still
         valid) — flag and preserve prior percentages, NO SOS escalation
      1. token_expired (HTTP 401 despite non-expired stored token) — real
         auth failure, flag and preserve prior percentages
      2. rate_limited (HTTP 429) — flag and preserve prior percentages
      3. poll_error (other failures) — flag and preserve prior percentages
      4. success — clear all flags, store new percentages + reset times

    The branches are exclusive on a per-cycle basis. GH #13 added the
    token_stale branch to distinguish "we can't poll this inactive account
    because our snapshot's access token aged out" (transient, recoverable
    on next swap) from "the refresh token itself has failed and we need
    interactive re-login" (real SOS condition).
    """
    config = load_config()
    for name, acct in state["accounts"].items():
        u = usage_by_account.get(name)
        if u is None:
            continue

        # Branch 0: token stale (stored access token expired but refresh
        # token still valid — recoverable on next swap, NOT an SOS condition).
        # Clear any prior false-positive token_expired flag if present.
        if u.token_stale:
            acct["token_stale"] = True
            acct["token_expired"] = False
            acct.pop("rate_limited", None)
            acct.pop("poll_error", None)
            acct["last_poll_ts"] = u.polled_at
            continue

        # Branch 1: token expired (401 despite non-expired stored token)
        if u.token_expired:
            acct["token_expired"] = True
            acct.pop("token_stale", None)
            acct.pop("rate_limited", None)
            acct.pop("poll_error", None)
            acct["last_poll_ts"] = u.polled_at
            # Token expiry isn't a polling-throttle event; don't backoff.
            continue

        # Branch 2: rate-limited (429). Preserve last known current_*_pct +
        # apply exponential backoff so we stop hammering the endpoint.
        if u.raw.get("rate_limited"):
            acct["rate_limited"] = True
            acct["token_expired"] = False
            acct.pop("poll_error", None)
            acct["last_poll_ts"] = u.polled_at
            update_backoff(acct, success=False, config=config)
            continue

        # Branch 3: other poll error (network, timeout, JSON parse). Preserve prior values.
        has_error = bool(u.raw.get("error"))
        has_any_window = u.five_hour is not None or u.seven_day is not None
        if has_error and not has_any_window:
            acct["poll_error"] = u.raw["error"]
            acct["token_expired"] = False
            acct.pop("rate_limited", None)
            acct["last_poll_ts"] = u.polled_at
            continue

        # Branch 4: success. Clear all flags + store new values + reset backoff.
        acct.pop("poll_error", None)
        acct.pop("rate_limited", None)
        acct.pop("token_stale", None)
        acct["token_expired"] = False
        # A SUCCESSFUL poll of an INACTIVE account read its SNAPSHOT's access token
        # (poll_account_usage → _read_access_token reads storage for inactive accts),
        # so the snapshot authenticates and is NOT refresh-dead — clear a stale flag
        # (e.g. after a relogin restored the snapshot). The ACTIVE account is polled
        # from the LIVE creds, whose success says nothing about the snapshot (merkos:
        # live family fine, snapshot dead), so we must NOT clear it for the active one.
        if name != state.get("active"):
            acct.pop("snapshot_refresh_dead", None)
        update_backoff(acct, success=True, config=config)
        # Estimator (C, 2026-06-23): measure per-window burn rate (%/min) from the
        # gap between the PRIOR poll and this one, before the old values are
        # overwritten below. Only valid WITHIN a window: if the 5h/7d window rolled
        # over (resets_at changed), the prior % is from a different window, so the
        # rate is meaningless — store 0 and let the next interval re-measure from
        # the post-reset base. Stored on the account for estimate_window_pct().
        old_5h = acct.get("current_5h_pct")
        old_7d = acct.get("current_7d_pct")
        # Anchor the burn-rate window at the PRIOR real OBSERVATION, not the
        # prior poll ATTEMPT (2026-07-03 token-stale blindness incident, GH
        # #130). old_5h/old_7d are the last OBSERVED percentages — they survive
        # the error branches above (which `continue` without touching
        # current_*_pct) but `last_poll_ts` gets restamped by those same error
        # branches. Using last_poll_ts here would understate dt across a stale
        # gap and overstate %/min. `last_observed_ts or last_poll_ts` falls
        # back to today's behavior for legacy state that predates the field.
        old_poll_ts = acct.get("last_observed_ts") or acct.get("last_poll_ts")
        old_5h_resets = acct.get("five_hour_resets_at")
        old_7d_resets = acct.get("seven_day_resets_at")
        new_5h_val = u.five_hour.utilization if u.five_hour else old_5h
        new_7d_val = u.seven_day.utilization if u.seven_day else old_7d
        new_5h_resets = u.five_hour.resets_at if (u.five_hour and u.five_hour.resets_at) else old_5h_resets
        new_7d_resets = u.seven_day.resets_at if (u.seven_day and u.seven_day.resets_at) else old_7d_resets
        acct["burn_rate_5h_pct_per_min"] = (
            _compute_burn_rate(old_5h, new_5h_val, old_poll_ts, u.polled_at)
            if old_5h_resets == new_5h_resets else 0.0
        )
        acct["burn_rate_7d_pct_per_min"] = (
            _compute_burn_rate(old_7d, new_7d_val, old_poll_ts, u.polled_at)
            if old_7d_resets == new_7d_resets else 0.0
        )
        # Snapshot the PREVIOUS current_*_pct before overwriting — used by
        # the usage-growth gate in decide_swap (GH #15) to suppress ladder
        # swaps when local usage isn't actually increasing.
        if "current_5h_pct" in acct:
            acct["prev_current_5h_pct"] = acct["current_5h_pct"]
        if "current_7d_pct" in acct:
            acct["prev_current_7d_pct"] = acct["current_7d_pct"]
        acct["current_5h_pct"] = u.five_hour.utilization if u.five_hour else acct.get("current_5h_pct", 0.0)
        acct["current_7d_pct"] = u.seven_day.utilization if u.seven_day else acct.get("current_7d_pct", 0.0)
        acct["last_poll_ts"] = u.polled_at
        # last_observed_ts = "last time we actually OBSERVED real usage data",
        # written ONLY on this success branch (2026-07-03 token-stale blindness
        # incident, GH #130). Distinct from last_poll_ts = "last time we
        # attempted/settled a poll", which the error branches above keep
        # restamping without observing anything. `_five_hour_rolled_since_poll`
        # and the burn-rate estimator read last_observed_ts so a token_stale
        # account (Branch 0) can no longer masquerade as freshly-observed and
        # defeat the GH #59 countdown reset inference.
        acct["last_observed_ts"] = u.polled_at
        if u.five_hour and u.five_hour.resets_at:
            acct["five_hour_resets_at"] = u.five_hour.resets_at
        if u.seven_day and u.seven_day.resets_at:
            acct["seven_day_resets_at"] = u.seven_day.resets_at
        # 72h seven_day reset detection (2026-07-05, gist: monperrus). The
        # `seven_day` budget really refreshes every ~72h at a fixed UTC anchor,
        # NOT every 7 days, and the API resets_at doesn't mark it. Infer the
        # real reset from its observable signature — a sharp DROP in 7d
        # utilization between two consecutive OBSERVATIONS — and stamp when it
        # happened so projected_seven_day_reset() can project the next one 72h
        # out. old_7d is the last observed % (captured above before overwrite);
        # new_7d_val is this poll's %. Kept as a call to the pure helper so the
        # detection logic stays unit-testable apart from the poll I/O.
        if _is_seven_day_reset_drop(old_7d, new_7d_val, config):
            acct["seven_day_last_reset_ts"] = u.polled_at
        # Per-model WEEKLY utilization (GH: fable/sonnet tracking). Persisted as
        # a flat {model: pct} dict so `cus status` and the weekly-cap gate can
        # read it from state.json without a live poll. Only written on a
        # SUCCESSFUL poll (this branch); the error branches above `continue`
        # early, preserving the prior dict — same preserve-on-error semantics as
        # current_*_pct. An empty result clears it (no model-scoped limits =
        # none active).
        acct["per_model_weekly_pct"] = {
            model: win.utilization for model, win in u.per_model_weekly.items()
        }
    return state


# --------------------------------------------------------------------------
# SOS diagnostics — surfaces conditions requiring human action
# --------------------------------------------------------------------------

@dataclass
class SOSCondition:
    """One thing that needs human action. Severity drives statusline color."""
    severity: str  # "urgent" (red) | "warning" (yellow) | "info" (soft, sos-only)
    summary: str   # one-line headline
    action: str    # concrete next step
    affected: str  # account name or "daemon" or "system"


def _relogin_command_for(account_name: str, source_dir: str | None = None) -> str:
    """Return the exact shell command to re-login a given account.

    GH #77: the command must target the file Claude will actually READ for
    this account, which depends on whether the account is ACTIVE:

      - ACTIVE account (whatever its name): the live `~/.claude/` +
        `~/.claude.json` are its authoritative slot, so a bare
        `claude /login` (no CLAUDE_CONFIG_DIR) writes the fresh tokens
        exactly where they're needed. The old behavior — always pointing at
        the storage dir — put the fresh tokens in the SNAPSHOT while the
        live file kept the dead ones: polls stayed 401 (poll reads live for
        the active account), and the next swap-away's save-back overwrote
        the fresh snapshot with the dead live tokens. (If the user already
        did the storage-dir login, `cus relogin <name> --finish` syncs
        snapshot → live.)
      - INACTIVE account: log in under its managed storage dir
        (`CLAUDE_CONFIG_DIR=~/claude-accounts/account-<name>/`) — the
        snapshot is the authoritative copy for inactive accounts. This now
        includes an inactive "default": the old unconditional
        `claude /login` for default clobbered the ACTIVE account's live
        tokens whenever default wasn't the one active (the mirror-image bug
        noted in GH #77).

    If the managed dir doesn't exist yet, the returned command still works —
    running it creates the dir.
    """
    try:
        state = load_state() if STATE_JSON.exists() else {}
    except (json.JSONDecodeError, OSError):
        state = {}
    if account_name == state.get("active"):
        # Live files are the active account's authoritative slot.
        return "claude /login"
    managed = ACCOUNTS_DIR / f"account-{account_name}"
    return f"CLAUDE_CONFIG_DIR={managed}/ claude"


def finish_active_relogin(account_name: str) -> None:
    """Sync a re-logged-in snapshot into the LIVE files (GH #77).

    For when the user re-logged in the ACTIVE account under its storage dir
    (old instructions, or muscle memory): the fresh tokens sit in
    `account-<name>/.credentials.json` while Claude keeps reading the dead
    live `~/.claude/.credentials.json`. This installs snapshot → live
    (credentials + the account-bound .claude.json keys), completing the
    recovery.

    Raises RuntimeError (the standard caller-caught type) when the sync
    would be wrong:
      - account isn't active: for inactive accounts the snapshot is already
        authoritative; there is nothing to finish — and installing an
        inactive account's tokens live would clobber the actual active
        account.
      - snapshot is strictly OLDER than live: installing would replace
        working tokens with deader ones. Equal/incomparable timestamps
        proceed (a same-second sync is harmless; missing metadata shouldn't
        block an explicit user request).
    """
    try:
        state = load_state() if STATE_JSON.exists() else {}
    except (json.JSONDecodeError, OSError):
        state = {}
    active = state.get("active")
    if account_name != active:
        raise RuntimeError(
            f"'{account_name}' is not the active account (active: {active}). --finish installs a "
            f"snapshot into the LIVE files, which belong to the active account only; for an inactive "
            f"account the snapshot is already authoritative — nothing to finish.")
    acct_dir = ACCOUNTS_DIR / f"account-{account_name}"
    snap_creds = acct_dir / ".credentials.json"
    if not snap_creds.exists():
        raise RuntimeError(
            f"{snap_creds} does not exist — run the re-login first: "
            f"CLAUDE_CONFIG_DIR={acct_dir}/ claude   (then rerun --finish)")
    try:
        snap_exp = _creds_expires_at(read_json(snap_creds))
    except (json.JSONDecodeError, OSError) as e:
        raise RuntimeError(f"{snap_creds} unreadable ({e}) — not installing it live")
    live_exp = None
    if CREDS_JSON.exists():
        try:
            live_exp = _creds_expires_at(read_json(CREDS_JSON))
        except (json.JSONDecodeError, OSError):
            live_exp = None  # corrupt live file is no reason to refuse a repair
    if snap_exp is not None and live_exp is not None and snap_exp < live_exp:
        raise RuntimeError(
            f"'{account_name}' snapshot's tokens are OLDER than the live file's (snapshot expiresAt "
            f"{int(snap_exp)} < live {int(live_exp)}) — installing them would replace working tokens "
            f"with deader ones. If you just re-logged in under {acct_dir}, the login didn't write "
            f"there; rerun it. If the LIVE tokens are the good ones, there is nothing to finish.")

    backup_credentials_file(CREDS_JSON)  # GH #79 choke point
    atomic_copy(snap_creds, CREDS_JSON, mode=0o600)

    # Also carry the account-bound identity keys over, in case the re-login
    # refreshed them (e.g. a changed oauthAccount payload). Best-effort: a
    # missing snapshot .claude.json just means there's nothing to merge.
    snap_cj_path = acct_dir / ".claude.json"
    if snap_cj_path.exists() and CLAUDE_JSON.exists():
        try:
            snap_cj = read_json(snap_cj_path)
            live_cj = read_json(CLAUDE_JSON)
        except (json.JSONDecodeError, OSError):
            return  # creds are synced (the part that matters); skip identity merge
        merged = False
        for k in ACCOUNT_BOUND_KEYS:
            if k in snap_cj and live_cj.get(k) != snap_cj[k]:
                live_cj[k] = snap_cj[k]
                merged = True
        if merged:
            write_json(CLAUDE_JSON, live_cj)


def _diagnose_live_mount_creds(
    creds: Any,
    mode: str,
    active_account: str | None,
    live_identity: dict | None = None,
    active_identity: dict | None = None,
) -> SOSCondition | None:
    """Pure predicate for the blanked/invalid live shared-mount check (2026-07-05).

    In `global` and `hybrid` mode the live ~/.claude/.credentials.json is what
    BARE sessions on the shared mount read. When it gets blanked (the observed
    signature: `expiresAt: 0` + empty `accessToken`) those sessions show "not
    logged in" while the active account's own snapshot stays healthy — so
    nothing else in diagnose() notices and `cus sos` reported all-clear during
    the incident (GH #141). In `per_session` mode the shared mount is
    observe-only / authless by design (bare launches are observed, not swapped),
    so a blank there is expected and MUST NOT fire.

    Factored out of diagnose() as a pure function — takes an already-parsed
    creds dict (None / non-dict = unreadable → invalid), the mode, the active
    account name, and optionally the live-mount vs active-account identity dicts
    — so it can be unit-tested without touching real files.

    Primary signal: token blank/invalid (`_live_mount_creds_invalid`). Secondary
    signal (only when both identity dicts are supplied): the live mount's
    identity disagrees with the active account's expected identity — a live file
    that is technically token-valid but belongs to the WRONG account, the
    2026-07-01 duplicate-identity shape. Returns one SOSCondition or None.
    """
    # Only the shared-mount-serving modes care about the live file's validity.
    if mode not in ("global", "hybrid"):
        return None

    # Name the concrete remedy account; fall back to a placeholder if state has
    # no active account recorded (can't happen in a served mode in practice, but
    # keeps the message well-formed rather than emitting "None").
    account = active_account or "<active-account>"
    remedy = (
        f"      cus restore-creds {account} --live\n"
        f"    (restores the newest good credential backup into the live file; the "
        f"account must be active, which it is). If there is no usable backup, "
        f"re-login instead:\n"
        f"      cus relogin {account}   # then: cus poll")

    if _live_mount_creds_invalid(creds):
        return SOSCondition(
            severity="urgent",
            summary=("live shared mount ~/.claude/.credentials.json has no valid token — "
                     "bare sessions on the shared mount will show 'not logged in'"),
            action=(
                "The live shared-mount credentials file has been blanked/invalidated "
                "(empty accessToken or expiresAt <= 0), the 2026-07-05 signature (GH #141). "
                f"Any BARE session on the shared mount is locked out even though '{account}'s "
                "own snapshot may be healthy. Reinstall a good token into the live file:\n"
                + remedy),
            affected=active_account or "system",
        )

    # Secondary: token is present+valid but points at the wrong account. Only
    # evaluated when both identities are known — _identities_match returns None
    # on no shared evidence, and we treat only an explicit False as a mismatch.
    if (live_identity and active_identity
            and _identities_match(live_identity, active_identity) is False):
        live_email = live_identity.get("emailAddress") or "?"
        want_email = active_identity.get("emailAddress") or "?"
        return SOSCondition(
            severity="urgent",
            summary=("live shared mount ~/.claude/.credentials.json belongs to the wrong "
                     "account — bare sessions on the shared mount are on the wrong identity"),
            action=(
                f"The live mount identity ('{live_email}') disagrees with the active account "
                f"'{account}' ('{want_email}') — the 2026-07-01 duplicate-identity shape. "
                "Reinstall the active account's own credentials into the live file:\n"
                + remedy),
            affected=active_account or "system",
        )
    return None


def _newest_usable_creds_source(account_name: str) -> Path | None:
    """Newest credential FILE for `account_name` whose payload is a usable OAuth
    token, or None if nothing usable exists (GH #141 auto-heal source picker).

    Searches, newest-first: the account's live snapshot `.credentials.json`, then
    its rotated `.credentials.json.bak.*` generations (which are, by construction,
    always OLDER than the snapshot — a backup is the pre-overwrite copy). The
    snapshot is therefore the freshest candidate and is tried first. "Usable" is
    the negation of `_live_mount_creds_invalid` — the exact predicate the swap
    guard and the SOS detector use — so a source this returns is safe to install
    into the live mount without re-blanking it. Unreadable / unparseable files
    are skipped, not fatal.

    This is the backup-selection half of `cus restore-creds <account> --live`,
    factored so the daemon can pick a heal source without shelling out. It also
    considers the snapshot itself (which `restore-creds` does not) because in the
    observed incident the snapshot stayed HEALTHY while only the live file was
    blanked — copying the snapshot back is then the direct, freshest heal.
    """
    candidates: list[Path] = []
    snap = account_creds_path(account_name)
    if snap.exists():
        candidates.append(snap)
    # list_creds_backups returns newest-first already; snapshot stays ahead of
    # every backup since backups predate it.
    candidates.extend(list_creds_backups(account_name))
    for path in candidates:
        try:
            creds = read_json(path)
        except (json.JSONDecodeError, OSError):
            continue  # a corrupt candidate can't heal anything; try the next
        if not _live_mount_creds_invalid(creds):
            return path
    return None


def _auto_heal_live_mount(state: dict, config: dict, no_execute: bool = False) -> bool:
    """Self-heal a blanked/invalid live shared mount in-daemon (GH #141).

    In `global`/`hybrid` mode the live ~/.claude/.credentials.json is what BARE
    sessions on the shared mount read. When it gets blanked (empty accessToken /
    expiresAt<=0) every bare session locks out. Detection already exists
    (`_diagnose_live_mount_creds`); this is the automatic REMEDY that used to
    require a human running `cus restore-creds <active> --live` — the ask in
    GH #141 ("make it never need a human").

    Behavior — the in-daemon equivalent of `cus restore-creds <active> --live`:
      * No-op unless the mount is actually blanked/invalid (idempotent; safe to
        call every cycle — a healthy mount is never touched).
      * Only runs in shared-mount-serving modes (global/hybrid). In per_session
        the shared mount is observe-only/authless by design, so a blank there is
        expected and must NOT be "healed" — matches `_diagnose_live_mount_creds`.
      * Restores the ACTIVE account's newest USABLE credential source
        (`_newest_usable_creds_source`: snapshot, else newest valid backup) into
        the live file, with the SAME atomic-write + rotated-backup discipline as
        `restore_creds_backup`'s --live install: back up the (blank) live file
        first so the heal is itself reversible, then atomically copy.
      * NEVER installs a blank (the source is validated by the picker) and never
        clobbers a healthy mount (the invalid-check gates the whole thing).
      * When NO usable source exists, does nothing and returns False, so the
        URGENT re-login SOS in diagnose() still fires (genuine human needed).

    Returns True iff it healed. `no_execute` makes it log the intended heal
    without writing, for `cus daemon --once --no-execute` dry-runs.
    """
    if config.get("mode", "global") not in ("global", "hybrid"):
        return False
    active = state.get("active")
    if not active:
        return False  # no active account recorded → nothing authoritative to install
    try:
        live_creds = read_json(CREDS_JSON) if CREDS_JSON.exists() else None
    except (json.JSONDecodeError, OSError):
        live_creds = None  # unreadable == unusable, treat as blanked
    if not _live_mount_creds_invalid(live_creds):
        return False  # mount is healthy — no-op
    source = _newest_usable_creds_source(active)
    if source is None:
        # No usable snapshot or backup anywhere: cannot self-heal without a
        # browser re-login. Leave the blank in place so diagnose() surfaces the
        # URGENT re-login SOS — the one case that still needs a human.
        click.echo(f"  auto-heal: live shared mount is blanked but '{active}' has NO usable "
                   f"credential backup — leaving it for the URGENT re-login SOS (GH #141)")
        return False
    if no_execute:
        click.echo(f"  auto-heal (--no-execute): WOULD restore '{active}' creds from {source.name} "
                   f"into the live shared mount (GH #141)")
        return False
    # Same discipline as restore_creds_backup(into_live=True): back up the live
    # file (even blanked, it is one more recoverable generation) then atomic-copy
    # the validated source over it. Only the live file is touched — the snapshot
    # is a heal SOURCE here, never a write target, so a healthy snapshot is
    # preserved untouched.
    backup_credentials_file(CREDS_JSON)
    atomic_copy(source, CREDS_JSON, mode=0o600)
    msg = (f"live shared mount ~/.claude/.credentials.json was blanked/invalid (GH #141) — "
           f"auto-restored '{active}' credentials from {source.name} into the live file; "
           f"bare sessions on the shared mount are usable again without a human.")
    click.echo(f"  AUTO-HEAL: {msg}")
    try:
        append_inbox("auto-heal", "blanked live shared mount self-healed (GH #141)", msg)
    except OSError:
        pass  # inbox is best-effort observability; never fail the heal over it
    return True


# ---------------------------------------------------------------------------
# LANE-mount blank detection + auto-heal (2026-07-05, GH #141 follow-up).
#
# The GH #141 fix above heals only the SHARED mount (~/.claude/.credentials.json).
# But a per-slot LANE has its OWN mount creds (slot-N/.credentials.json) — the
# file the running Claude Code session on that lane actually reads — and it can be
# blanked the exact same way by an EXTERNAL writer (Claude's own logout, a crash
# mid-write). That is precisely what happened to slot-2 on 2026-07-05: its mount
# creds went blank and had to be fixed by hand, because neither `cus sos` nor the
# daemon's auto-heal looked at anything but the shared mount. These helpers are the
# lane analogue of Condition 0 + `_auto_heal_live_mount`: same predicate
# (`_live_mount_creds_invalid`), same atomic-write + rotated-backup + never-write-
# a-blank discipline, but scoped to each LIVE lane's own mount.
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Blank-mount PREEMPT + STUCK-SESSION + FORENSICS shared state (2026-07-07)
# ---------------------------------------------------------------------------
# In-process tracking for the three 2026-07-07 durable fixes. All three are
# process-local (the daemon is a long-lived process) and intentionally NOT
# persisted to disk:
#   * A daemon restart clearing them is safe-by-design — the worst case is one
#     extra proactive-refresh attempt (Fix 1) or a reset heal-count (Fix 2), both
#     harmless; there is nothing durable to corrupt.
#   * Keeping them off-disk avoids adding a new on-disk format (backward-compat).
# Tests reset them explicitly (they persist for the module's lifetime).
#
# _PREEMPT_ATTEMPT_MS: (slot, account) -> monotonic-ish wall-clock seconds of the
#   last proactive-refresh ATTEMPT, the poll-burnout backoff clock for Fix 1.
# _LANE_HEAL_HISTORY: (slot, account) -> list of wall-clock heal timestamps, the
#   heal→blank→heal detector for Fix 2 (pruned to the stuck window on read).
_PREEMPT_ATTEMPT_MS: dict[tuple[str, str], float] = {}
_LANE_HEAL_HISTORY: dict[tuple[str, str], list[float]] = {}
# _UNSTALE_ATTEMPT_MS: account -> last un-stale refresh-grant attempt (Fix 4,
#   2026-07-07), the poll-burnout backoff clock for the idle-account un-stale sweep.
_UNSTALE_ATTEMPT_MS: dict[str, float] = {}
# _SNAPSHOT_DEAD_PROBE: account -> (wall-clock seconds of the last probe, is-dead bool),
#   the cooldown cache for `_account_snapshot_dead` (2026-07-07 merkos dead-snapshot
#   incident). That probe is a network round-trip that ROTATES the snapshot's refresh
#   token on success, so — like the un-stale sweep — it must run at most once per
#   `unstale_cooldown_minutes` per account (poll-burnout backoff). Cleared below.
_SNAPSHOT_DEAD_PROBE: dict[str, tuple[float, bool]] = {}


def _reset_blank_tracking() -> None:
    """Clear all in-process blank-mount / un-stale tracking (test hook; also
    usable to force a fresh preempt/un-stale attempt or heal-count after a config
    change). Never needed in normal daemon operation — the dicts self-prune."""
    _PREEMPT_ATTEMPT_MS.clear()
    _LANE_HEAL_HISTORY.clear()
    _UNSTALE_ATTEMPT_MS.clear()
    _SNAPSHOT_DEAD_PROBE.clear()


def _account_snapshot_dead(account: str, config: dict | None = None, *, force: bool = False) -> bool:
    """True iff account-<X>'s CANONICAL snapshot (.credentials.json) cannot
    currently authenticate — the "dead snapshot" the 2026-07-07 merkos incident
    exposed.

    THE INCIDENT: merkos's canonical snapshot held a DEAD refresh token (the
    OAuth refresh grant returns invalid_grant — see the un-stale sweep's
    `CRED-AUDIT op=unstale-refresh account=merkos decision=failed
    reason="refresh grant dead"`), yet merkos was perfectly usable through a live
    login FAMILY (lane tabby-2 ran on it fine). The danger is the SWAP install-
    point: installing that dead snapshot into a NEW lane mount blanks the mount
    ("not logged in") the moment Claude Code attempts its first token refresh —
    exactly what blanked chats1a when it was swapped onto merkos.

    WHY `_live_mount_creds_invalid` DOESN'T CATCH THIS: that predicate only flags
    the blank-SHAPED signature (empty accessToken / expiresAt<=0). A dead snapshot
    can be perfectly well-SHAPED — a real-looking-but-expired access token, a
    positive-but-past expiresAt — and still be dead because its refresh branch was
    rotated away elsewhere. Shape alone can't tell a dead branch from a live one;
    only the refresh grant can (the same lesson as #127 / classify_live_creds_owner).

    Verdict:
      * access token currently VALID (non-blank, within expiry) → NOT dead: the
        snapshot authenticates right now, so no (token-rotating) probe is needed.
        This cheap path keeps the common healthy-account case network-free.
      * no refresh token AND no valid access token → dead (nothing to mint from).
      * else probe the refresh token via `_oauth_refresh_grant`:
          - "alive"   → PERSIST the rotated tokens into the snapshot (the grant is
                        single-use — #104 — so the fresh pair MUST land on disk or
                        the family is lost) and return NOT dead. Mirrors the persist
                        block in `_unstale_account_snapshot`.
          - "dead"    → invalid_grant → return dead.
          - "unknown" → network/endpoint drift → FAIL OPEN (return NOT dead): a
                        transient must never turn a working account into a refused
                        swap. The #141 blank-shape guards still backstop an actually
                        blank install, so worst case is exactly today's behavior.

    Cooldown-cached in `_SNAPSHOT_DEAD_PROBE` (poll-burnout backoff, bypassed by
    `force`). Never raises into the caller."""
    snap = account_creds_path(account)
    if not snap.exists():
        # Missing snapshot is owned by other guards (the `cus init` check in
        # _execute_swap_locked; the #141 blank-shape guards). Not a dead-refresh case.
        return False
    try:
        creds = read_json(snap)
    except (json.JSONDecodeError, OSError):
        # Unreadable snapshot: the #141 install-point guard already refuses it as
        # blank. Not our (dead-refresh) call to make.
        return False
    # Cheap path: a currently-valid access token means the snapshot authenticates
    # right now — definitively not dead, and no token-rotating probe needed. The 30s
    # grace mirrors poll_account_usage so we don't probe a token about to expire.
    oauth = creds.get("claudeAiOauth") if isinstance(creds, dict) else None
    access0 = oauth.get("accessToken") if isinstance(oauth, dict) else None
    exp = _creds_expires_at(creds)
    now_ms = int(time.time() * 1000)
    if isinstance(access0, str) and access0.strip() and exp is not None and int(exp) > now_ms - 30_000:
        return False
    rt = _credential_refresh_token(creds)
    if not rt:
        # No refresh token to mint from AND no valid access token above → unusable.
        return True
    cfg = config if config is not None else load_config()
    cooldown_min = cfg.get("token_self_refresh", {}).get("unstale_cooldown_minutes", 10)
    now = time.time()
    if not force:
        cached = _SNAPSHOT_DEAD_PROBE.get(account)
        if cached is not None and (now - cached[0]) < cooldown_min * 60:
            return cached[1]
    verdict, tok = _oauth_refresh_grant(rt)
    if verdict == "alive" and isinstance(tok, dict):
        # Persist the rotation BEFORE returning (mirrors _unstale_account_snapshot):
        # after a successful grant the snapshot's OLD refresh token is a dead branch
        # (single-use rotation, #104), so the fresh pair must land on disk or we'd
        # lose the family. Whole assembly is guarded so a malformed grant response
        # can never crash a swap.
        try:
            access = tok.get("access_token")
            if isinstance(access, str) and access:
                new_oauth = dict(creds.get("claudeAiOauth") or {})
                new_oauth["accessToken"] = access
                new_oauth["refreshToken"] = tok.get("refresh_token") or rt
                expires_in = tok.get("expires_in")
                if isinstance(expires_in, (int, float)) and not isinstance(expires_in, bool):
                    new_oauth["expiresAt"] = int((time.time() + float(expires_in)) * 1000)
                new_creds = dict(creds)
                new_creds["claudeAiOauth"] = new_oauth
                backup_credentials_file(snap)
                write_json(snap, new_creds, mode=0o600)
                _cred_audit("snapshot-dead-probe", "refreshed",
                            "snapshot access token was expired but its refresh grant is ALIVE — "
                            "minted+persisted fresh tokens; snapshot is usable",
                            account=account, token_fp=_audit_token_fp(new_creds),
                            extra=f"new_expiry={_expiry_repr(new_creds)}")
        except (OSError, TypeError, ValueError):
            pass  # persist failed; fall through — the grant WAS alive, so not dead
        _SNAPSHOT_DEAD_PROBE[account] = (now, False)
        return False
    if verdict == "dead":
        _SNAPSHOT_DEAD_PROBE[account] = (now, True)
        _cred_audit("snapshot-dead-probe", "detected",
                    "snapshot refresh grant returned invalid_grant — canonical creds are DEAD "
                    "(installing them would blank the mount)",
                    account=account, token_fp=_audit_token_fp(creds))
        return True
    # "unknown" → fail open. Cache the not-dead verdict for the cooldown so a flapping
    # endpoint isn't hammered, but never let a transient refuse a swap.
    _SNAPSHOT_DEAD_PROBE[account] = (now, False)
    return False


def _lane_lastvalid_path(mount_creds: Path) -> Path:
    """Path of a lane mount's last-known-valid SHADOW copy (Fix 3, 2026-07-07).

    A sibling of the mount's `.credentials.json` named `.credentials.json.lastvalid`.
    We APPEND the suffix (not `with_suffix`, which would replace `.json`) so the
    shadow sits next to the live file and is trivially greppable. Refreshed
    whenever the mount is observed valid, it serves two ends: (a) an INSTANT,
    same-family heal source (see `_lane_heal_source`), and (b) a stable baseline
    to diff a freshly-blanked mount against for forensics (prior-valid token
    fingerprint / expiry vs the now-blank state)."""
    return Path(str(mount_creds) + ".lastvalid")


def _update_lane_lastvalid(mount_creds: Path, creds: Any) -> None:
    """Refresh a lane's last-known-valid shadow copy IFF the mount is valid
    (Fix 3). Best-effort observability: never raises into the caller — a failed
    shadow write must not disturb a poll/preempt/heal. No-op on a blank/invalid
    mount (we never overwrite a good shadow with a bad snapshot — the whole point
    is to retain the last GOOD generation to heal/diff against)."""
    if _live_mount_creds_invalid(creds):
        return
    try:
        atomic_copy(mount_creds, _lane_lastvalid_path(mount_creds), mode=0o600)
    except OSError:
        pass


def _lastvalid_age_seconds(mount_creds: Path) -> float | None:
    """Seconds since the lane's last-valid shadow was last refreshed (its mtime),
    i.e. roughly how long since the mount was last seen healthy — the "how long
    since it was last valid" forensic field. None when no shadow exists yet."""
    shadow = _lane_lastvalid_path(mount_creds)
    try:
        return (time.time() - shadow.stat().st_mtime) if shadow.exists() else None
    except OSError:
        return None


def _mtime_iso(path: Path) -> str:
    """UTC ISO mtime of a file for a CRED-AUDIT line, or `none` if absent/unreadable."""
    try:
        if not path.exists():
            return "none"
        return datetime.fromtimestamp(path.stat().st_mtime, timezone.utc).isoformat(timespec="seconds")
    except OSError:
        return "none"


def _expiry_repr(creds: Any) -> str:
    """Compact expiresAt for a CRED-AUDIT line: the raw epoch-ms plus its ISO
    rendering, or `none`. For a blanked mount this is typically `0` — the epoch
    signature — which is itself the evidence, so we log it verbatim."""
    exp = _creds_expires_at(creds)
    if exp is None:
        return "none"
    try:
        iso = datetime.fromtimestamp(exp / 1000, timezone.utc).isoformat(timespec="minutes")
    except (OverflowError, OSError, ValueError):
        iso = "?"
    return f"{int(exp)}({iso})"


def _record_lane_heal(slot: str, account: str) -> None:
    """Log one successful lane heal for the Fix 2 heal→blank→heal detector.
    Appends now() to the lane's rolling history; `_diagnose_stuck_lanes` prunes
    to the stuck window and escalates once the count crosses the threshold."""
    _LANE_HEAL_HISTORY.setdefault((slot, account), []).append(time.time())


def _blanked_live_lanes(state: dict, config: dict) -> list[tuple[str, str]]:
    """(slot, account) for every LIVE lane whose OWN mount creds are blanked/invalid.

    A "live lane" is a slot with a registered account (`state.slots[slot].account`)
    AND a live session on its mount (`occupied_slot_accounts` → `mount_in_use` /proc
    ground truth). The file a lane's session reads is the slot mount's own
    `.credentials.json` (`mount_creds_path`) — NOT the leased pooled-family store,
    which is only the swap SOURCE / save-back target. When THAT mount file is
    blanked (empty accessToken / expiresAt<=0, the 2026-07-05 slot-2 signature) the
    session shows "not logged in".

    Deliberately excludes the cases the task calls out as NOT-a-fault:
      * Only lane-serving modes (per_session/hybrid) have first-class lanes — the
        complement of the shared-mount check, which is global/hybrid. In global the
        daemon does not manage lanes, so it does not heal them here.
      * IDLE / observe-only slots (no live session on the mount) are skipped:
        occupied_slot_accounts only returns slots with a live PID, so a blank in an
        idle slot — expected, it holds no session — never surfaces.
      * Slots with no registered account are skipped (nothing authoritative to
        reinstall; also covers a slot mid-swap that hasn't committed an account).
    """
    if config.get("mode", "global") not in ("per_session", "hybrid"):
        return []
    out: list[tuple[str, str]] = []
    # account -> [live slot names]; already /proc-gated and account-gated.
    for account, slots in occupied_slot_accounts(state).items():
        for slot in slots:
            cred = mount_creds_path(slot_path(slot))
            try:
                creds = read_json(cred) if cred.exists() else None
            except (json.JSONDecodeError, OSError):
                creds = None  # unreadable == unusable from the session's point of view
            if _live_mount_creds_invalid(creds):
                out.append((slot, account))
    return out


def _lane_heal_source(slot: str, account: str, state: dict, config: dict) -> Path | None:
    """Newest USABLE creds file to reinstall into a blanked live lane mount, or
    None when nothing usable exists (needs a real relogin → escalate to SOS).

    Honors the GH #104/#109 login-family discipline, which is the ONE thing that
    differs from the shared-mount heal's source pick:
      * If the lane LEASES a pooled family (`state.slots[slot].login_family`, and
        the independent-logins gate is on), the only safe source is THAT family's
        own creds. Installing the account's shared snapshot instead would put a
        DIFFERENT refresh-token family on the live lane and clobber every other
        holder of that snapshot the next time either side refreshes (#104). So a
        pooled lane whose family store is itself blanked/missing heals from
        NOTHING here (returns None) rather than cross-contaminating — it escalates
        to the relogin SOS, same as the shared-mount "no usable backup" case.
      * A plain (non-pooled) lane is a shared-snapshot copy, so it heals from its
        own last-valid SHADOW first (Fix 3, 2026-07-07 — the freshest, guaranteed
        same-family copy of exactly what this mount last held) and then, failing
        that, the account's newest usable snapshot/backup — the same
        `_newest_usable_creds_source` the shared-mount heal uses.

    "Usable" is `not _live_mount_creds_invalid(...)` throughout — the same bar the
    swap install-point guard and the shared-mount heal apply — so whatever this
    returns is safe to atomic-copy into the mount without re-blanking it.
    """
    lease = slot_leased_family(state, slot)
    if lease is not None and lease[0] == account and independent_logins_enabled(config):
        fam_path = login_family_creds_path(*lease)
        if fam_path.exists():
            try:
                if not _live_mount_creds_invalid(read_json(fam_path)):
                    return fam_path
            except (json.JSONDecodeError, OSError):
                pass
        # Leased family store blanked/missing → NO cross-family fallback (#104).
        # The last-valid shadow is deliberately NOT consulted for a pooled lane:
        # a stale shadow could predate a re-lease to a different family, and
        # re-installing it would cross-contaminate; the family store is the only
        # authoritative source for a pooled lane.
        return None
    # Plain lane. The last-valid shadow is a copy of THIS lane's own mount taken
    # while it was valid — same slot, same refresh-token lineage by construction
    # — so it is the freshest #104-safe heal source and cannot cross-contaminate.
    # Prefer it; fall through to the snapshot/backup when there's no usable shadow
    # (e.g. right after a daemon restart, before any valid scan has taken one).
    shadow = _lane_lastvalid_path(mount_creds_path(slot_path(slot)))
    if shadow.exists():
        try:
            if not _live_mount_creds_invalid(read_json(shadow)):
                return shadow
        except (json.JSONDecodeError, OSError):
            pass
    # Fall through to the account's newest usable snapshot/backup — UNLESS the
    # snapshot is DEAD (its refresh grant returns invalid_grant). 2026-07-07 merkos
    # incident: `_newest_usable_creds_source` only rejects blank-SHAPED creds
    # (`_live_mount_creds_invalid`), so a well-shaped-but-dead snapshot would be
    # returned here and re-blank the mount on Claude Code's first refresh — a
    # heal→blank→heal loop (which the Fix 2 stuck-session detector would then flag).
    # Dropping the dead snapshot from the plain-lane heal chain means such a lane
    # escalates straight to the URGENT relogin SOS (a human is genuinely needed)
    # instead of looping. The pooled-lane branch above already never touches the
    # snapshot (#176), so this closes the one remaining dead-snapshot heal path.
    source = _newest_usable_creds_source(account)
    if source is not None and source == account_creds_path(account) and _account_snapshot_dead(account, config):
        return None
    return source


def _lane_mount_sos(slot: str, account: str) -> SOSCondition:
    """SOS for a blanked LIVE lane mount (GH #141 follow-up, 2026-07-05 slot-2).

    Only reached when the daemon's in-cycle heal (`_auto_heal_live_lanes`) could
    NOT fix the lane — i.e. no usable credential source exists for `account`
    (snapshot + backups, and any leased login family, are all blanked), which is
    the one case that genuinely needs a human `cus relogin`.
    """
    return SOSCondition(
        severity="urgent",
        summary=(f"lane {slot} ({account}) mount creds are blanked — "
                 f"session will show 'not logged in'"),
        action=(
            f"The live lane mount {slot}/.credentials.json has been blanked/invalidated "
            f"(empty accessToken or expiresAt <= 0), the 2026-07-05 slot-2 signature "
            f"(GH #141 lane follow-up). The Claude Code session running on lane {slot} is "
            f"locked out even though other accounts may be fine, and the daemon could NOT "
            f"self-heal it — '{account}' has no usable credential source (its snapshot and "
            f"backups, and any leased login family, are all blanked). Re-login the account:\n"
            f"      cus relogin {account}   # then: cus poll\n"
            f"    or restore a good backup if one exists:\n"
            f"      cus restore-creds {account} --list"),
        affected=account,
    )


def _divergence_risk_lanes(state: dict, config: dict) -> list[tuple[str, str, bool]]:
    """(slot, account, mount_token_expired) for every LIVE lane whose OWN mount
    runs the SAME OAuth refresh-token family as another live mount of its account
    — the shared ~/.claude mount or another live lane. This is the GH #104
    divergence-logout SETUP (2026-07-07 chats1a): two live mounts on one family,
    logged out on the next rotation with `401 Invalid authentication credentials`.

    This is the DETECTOR twin of the swap-time `_live_family_would_collide` guard:
    the guard stops a NEW swap from creating the setup; this surfaces a setup that
    already exists (a lane provisioned before the guard shipped, a manual mount
    edit, a state where login_family was never recorded) so an operator sees it
    BEFORE the rotation bites.

    Why it reads token BYTES rather than keying on login_family being None:
      * It must CATCH the incident — whose lane had a legacy store that PASSED
        `has_independent_login` but carried a stale COPY of the shared family. A
        state-only check that trusted has_independent_login (like
        `double_booked_live_accounts._covered`) would have judged it safe and
        stayed silent — exactly why `cus sos` reported all-clear during the
        incident.
      * It must NOT false-positive on a GENUINELY independent legacy login
        (distinct token, login_family unset but clobber-safe — the supported
        Track-B case with its own passing tests). Comparing actual families keeps
        that lane quiet (its family matches no other mount).
    So the discriminator is the true invariant — a shared LIVE family — not a
    proxy for it. A lane holding a distinct pooled lease installs a distinct
    family, so it never matches and is never flagged.

    Closes the bare-session blind spot the existing live-mount detectors have:
    `duplicate_live_mount_families` / `double_booked_live_accounts` gate the
    shared mount on `mount_in_use(CLAUDE_DIR)` (False for bare sessions — #141),
    so a lane sharing the shared-mount account was invisible to them.
    `_live_family_would_collide` counts the shared mount unconditionally in
    global/hybrid.

    Scoping (mirrors `_blanked_live_lanes`, deliberately quiet elsewhere): only
    when the independent-logins gate is ON, only lane-serving modes
    (per_session/hybrid), and only LIVE lanes (`occupied_slot_accounts` is
    /proc-gated, so idle/observe-only slots — which hold no session to refresh a
    token — are excluded).

    The bool is whether the lane's OWN mount is already blank/expired
    (`_live_mount_creds_invalid`) — the caller tiers severity on it (already
    clobbered ⇒ urgent, still valid ⇒ warning early nudge).
    """
    if not independent_logins_enabled(config):
        return []
    if config.get("mode", "global") not in ("per_session", "hybrid"):
        return []
    out: list[tuple[str, str, bool]] = []
    for account, slots in occupied_slot_accounts(state).items():
        for slot in slots:
            mount_path = mount_creds_path(slot_path(slot))
            # Does THIS lane's live family already run on another mount of `account`?
            if not _live_family_would_collide(account, mount_path, slot, state, config):
                continue
            try:
                mount_creds = read_json(mount_path)
            except (json.JSONDecodeError, OSError):
                mount_creds = None
            out.append((slot, account, _live_mount_creds_invalid(mount_creds)))
    return out


def _lane_divergence_sos(slot: str, account: str, mount_expired: bool) -> SOSCondition:
    """SOS for a lane whose live mount shares one OAuth token family with the
    shared-mount account (or another lane's account) — the GH #104
    divergence-logout setup (2026-07-07 chats1a). See `_divergence_risk_lanes`.

    Severity: urgent if the lane's mount token is ALREADY blank/expired (the
    clobber has likely already fired and the session is/soon logged out);
    warning otherwise (the setup exists but the rotation hasn't bitten yet — an
    early nudge to re-provision before it does).
    """
    return SOSCondition(
        severity="urgent" if mount_expired else "warning",
        summary=(f"lane {slot} shares one OAuth token family for account '{account}' with another "
                 f"live mount and has NO distinct login family — divergence/logout risk (GH #104)"
                 + ("; its mount token is already blank/expired" if mount_expired else "")),
        action=(
            f"Lane {slot} is live on '{account}' and its mount runs the SAME refresh-token family "
            f"as the shared ~/.claude mount or another live lane of '{account}' (its "
            f"state.slots.{slot}.login_family is unset / it never claimed a distinct family). Two "
            f"live mounts on one token family is the #104 clobber: the next refresh on either "
            f"side rotates the shared refresh token and logs the other out with "
            f"'401 Invalid authentication credentials' (the 2026-07-07 chats1a logout, which "
            f"cost a live session an hour). Fix it by re-running the move so it CLAIMS a distinct "
            f"pooled family:\n"
            f"      cus login-mount {account}     # add a pooled family if none is free\n"
            f"      cus slot move {slot} {account}   # re-claims a distinct family lease\n"
            f"    or move the lane to a different account:\n"
            f"      cus slot move {slot} <other-account>"),
        affected=account,
    )


def _auto_heal_live_lanes(state: dict, config: dict, no_execute: bool = False) -> list[str]:
    """Self-heal blanked live LANE mounts in-daemon (GH #141 follow-up, 2026-07-05).

    The lane analogue of `_auto_heal_live_mount`: for every LIVE lane whose own
    mount creds are blanked (`_blanked_live_lanes`), reinstall its owning account's
    newest USABLE credential source (`_lane_heal_source` — the leased family store
    if pooled, else the account snapshot/backup) into the lane mount, with the SAME
    atomic-write + rotated-backup + never-write-a-blank discipline the shared-mount
    heal uses. Runs in `_emit_sos_after` BEFORE `diagnose()`, so a healable blank
    never reaches the operator as an SOS — the heal fixes the mount and diagnose
    then sees it valid. Only a lane with NO usable source falls through to the
    URGENT relogin SOS (`_lane_mount_sos`).

    Idempotent (a healthy lane is byte-for-byte untouched — the invalid-check gates
    the whole thing), never clobbers a healthy lane, and honors login-family
    discipline (a pooled lane heals only from its own family — see
    `_lane_heal_source`). Returns the list of healed slot names. `no_execute` logs
    the intended heal without writing, for `cus daemon --once --no-execute`.

    Forensics (Fix 3, 2026-07-07): every blank detection emits a `op=blank-detected`
    CRED-AUDIT line and every successful restore a `op=blank-heal` line, capturing
    the mount mtime, how long since the mount was last valid (shadow age), the
    prior-valid token fingerprint (from the last-valid shadow) vs the now-blank
    state, and the expiry that triggered — so the NEXT recurrence can be
    characterized (which family, how stale, was Claude Code's refresh the writer).
    No raw tokens ever (fingerprint only). Each heal is also recorded for the Fix 2
    heal→blank→heal stuck-session detector (`_diagnose_stuck_lanes`)."""
    if config.get("mode", "global") not in ("per_session", "hybrid"):
        return []
    healed: list[str] = []
    for slot, account in _blanked_live_lanes(state, config):
        source = _lane_heal_source(slot, account, state, config)
        dest = mount_creds_path(slot_path(slot))
        # ── Forensics: characterize the blank BEFORE we heal it (Fix 3). ──────
        # Read the now-blank mount + the last-valid shadow so we can log what the
        # mount held when it was healthy vs the blank it is now — the evidence
        # that pins the cause (family, staleness, epoch signature) next time.
        try:
            _blank_creds = read_json(dest) if dest.exists() else None
        except (json.JSONDecodeError, OSError):
            _blank_creds = None
        try:
            _shadow_p = _lane_lastvalid_path(dest)
            _shadow_creds = read_json(_shadow_p) if _shadow_p.exists() else None
        except (json.JSONDecodeError, OSError):
            _shadow_creds = None
        _lv_age = _lastvalid_age_seconds(dest)
        _cred_audit(
            "blank-detected", "detected",
            "live lane mount creds blanked/invalid (2026-07-07 blank-mount signature)",
            slot=slot, account=account, token_fp=_audit_token_fp(_blank_creds),
            extra=(f"mount_mtime={_mtime_iso(dest)} "
                   f"last_valid_age={'unknown' if _lv_age is None else f'{_lv_age:.0f}s'} "
                   f"blank_expiry={_expiry_repr(_blank_creds)} "
                   f"prev_valid_fp={_audit_token_fp(_shadow_creds)} "
                   f"prev_valid_expiry={_expiry_repr(_shadow_creds)}"))
        if source is None:
            # No usable snapshot/backup/family: cannot self-heal without a browser
            # relogin. Leave the blank so diagnose() surfaces the URGENT SOS.
            click.echo(f"  auto-heal: live lane {slot} ({account}) mount is blanked but there is NO "
                       f"usable credential source — leaving it for the URGENT relogin SOS "
                       f"(GH #141 lane follow-up)")
            continue
        if no_execute:
            click.echo(f"  auto-heal (--no-execute): WOULD restore '{account}' creds from {source} "
                       f"into live lane {slot} (GH #141 lane follow-up)")
            continue
        # Install-point guard mirror (#141): never write a blank. The source was
        # already validated by _lane_heal_source, but re-check at the install point
        # so a source that went bad between pick and copy can't re-blank the mount.
        try:
            if _live_mount_creds_invalid(read_json(source)):
                continue
        except (json.JSONDecodeError, OSError):
            continue
        # Same discipline as _auto_heal_live_mount / restore_creds_backup: back up
        # the (blank) lane mount first so the heal is itself reversible, then
        # atomic-copy the validated source over it. Only the lane mount is touched
        # — the family store / snapshot is a heal SOURCE here, never a write target.
        backup_credentials_file(dest)
        atomic_copy(source, dest, mode=0o600)
        # Forensics: record the heal, its source, and the restored token's fp so a
        # heal→blank→heal cycle is visible in the audit stream (Fix 3).
        try:
            _healed_creds = read_json(dest)
        except (json.JSONDecodeError, OSError):
            _healed_creds = None
        _cred_audit(
            "blank-heal", "wrote",
            f"auto-restored blanked live lane mount from {source.name}",
            slot=slot, account=account, token_fp=_audit_token_fp(_healed_creds),
            extra=f"source={source} restored_expiry={_expiry_repr(_healed_creds)}")
        # Record the heal for the Fix 2 stuck-session (heal→blank→heal) detector.
        _record_lane_heal(slot, account)
        # The restored mount is a fresh last-known-good — refresh the shadow so a
        # subsequent blank diffs against THIS generation, and heal has it instantly.
        _update_lane_lastvalid(dest, _healed_creds)
        msg = (f"live lane {slot} ({account}) mount creds were blanked/invalid (GH #141 lane "
               f"follow-up) — auto-restored '{account}' credentials from {source} into {dest}; "
               f"the lane's session is usable again without a human (2026-07-05 slot-2 incident).")
        click.echo(f"  AUTO-HEAL: {msg}")
        try:
            append_inbox("auto-heal", f"blanked live lane {slot} self-healed (GH #141 follow-up)", msg)
        except OSError:
            pass  # inbox is best-effort observability; never fail the heal over it
        healed.append(slot)
    return healed


# ---------------------------------------------------------------------------
# PRE-EMPTIVE creds-health early-warning (2026-07-06)
# ---------------------------------------------------------------------------
# The blank-mount detection + auto-heal above (GH #141 + lane follow-up) is the
# REACTIVE backstop: it only notices AFTER a live mount's creds have already
# blanked/logged out, and fires an URGENT SOS (or silently heals). Incident
# 2026-07-05: a live LOCKED lane's mount creds blanked and the operator only
# learned via an after-the-fact SOS. This layer adds a PROACTIVE scan each cycle
# over every LIVE mount (lanes + shared mount) that emits a soft WARNING BEFORE
# a mount fails — while its OAuth access token is merely near-expiry-and-idle, or
# its refresh token is nearing its assumed TTL and will soon need a browser
# re-login. It NEVER makes a network grant to probe liveness (OAuth refresh
# tokens are single-use — a probe grant would rotate the stored token and clobber
# the live family, GH #104); it judges purely from the STORED expiresAt / token
# presence / provenance age. The reactive path is untouched — this only ADDS an
# earlier, softer signal. Gated by config `creds_warn.enabled` (default True).


def _account_snapshot_age_days(account: str) -> float | None:
    """Best-available age (days) of an account's base SNAPSHOT refresh token, or
    None if there's nothing to judge by.

    Login FAMILIES carry a `minted_ts` provenance record (see login_age_days), but
    a base account snapshot / the shared mount has no such record — so we fall back
    to the account's meta.yaml timestamps as a mint proxy: `refreshed_ts` (written
    on the last `cus init --force` snapshot refresh) preferred, else `imported_ts`
    (first import). This is an imperfect proxy — a refresh token can rotate on the
    live mount without a snapshot refresh — but it only feeds the SOFT refresh-TTL
    WARNING, never a hard action, and degrades to "unknown" (no warning) when the
    timestamp is missing, so an over/under-estimate is harmless. NEVER a network
    probe — refresh tokens are single-use (GH #104)."""
    meta = account_meta(account)
    stamp = meta.get("refreshed_ts") or meta.get("imported_ts")
    if not stamp or not isinstance(stamp, str):
        return None
    try:
        dt = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - dt).total_seconds() / 86400.0


def _mount_refresh_age_days(account: str, slot: str | None, state: dict, config: dict) -> float | None:
    """Age (days) of the refresh token a given LIVE mount actually reads, or None
    when no age source exists (→ the refresh-TTL warning is skipped for it).

    Resolves the right provenance the same way `_lane_heal_source` resolves the
    right heal source, so the age we age-out on matches the token in play:
      * a lane LEASING a pooled family → that family's `minted_ts` provenance;
      * a lane with a legacy per-(slot,account) independent login → its provenance;
      * everything else (a plain shared-snapshot-copy lane, or the shared mount,
        slot=None) → the account snapshot mint proxy (`_account_snapshot_age_days`).
    """
    if slot is not None:
        lease = slot_leased_family(state, slot)
        if lease is not None and lease[0] == account and independent_logins_enabled(config):
            return login_age_days(*lease)  # login_age_days keys on (account, family_id)
        if has_independent_login(account, slot):
            return login_age_days(account, slot)
    return _account_snapshot_age_days(account)


def _diagnose_mount_creds_health(
    mount_label: str,
    account: str,
    creds: Any,
    now_ms: int,
    config: dict,
    *,
    refresh_age_days: float | None = None,
    recently_refreshed: bool = False,
) -> SOSCondition | None:
    """PURE predicate for the pre-emptive creds-health scan: given ONE live mount's
    already-parsed creds dict + the current time + config (+ its resolved refresh-
    token age and whether it was recently rewritten), return a soft WARNING
    SOSCondition or None. No file / process / network I/O — the impure inputs
    (creds dict, age, recently_refreshed flag) are resolved by the caller
    (`_diagnose_live_mounts_creds_health`), mirroring `_diagnose_premium_headroom`,
    so this is unit-testable with plain values.

    Deliberately returns None (no WARNING) for an ALREADY-BLANK/invalid mount: that
    is the REACTIVE layer's job (`_diagnose_live_mount_creds` / the lane heal fire
    an URGENT for it). The two layers must not collide — a blanked mount is one
    URGENT, not also a WARNING. This layer only speaks to mounts that are still
    valid but AGING toward failure.

    Precedence (one WARNING per mount, so SOS.md stays readable and de-dups
    cleanly): the refresh-token-TTL warning wins over the access-token warnings —
    it is the only one that needs a human action (a browser `cus relogin` with a
    deadline), and it subsumes the access-token heads-up. Among the access-token
    signals, an ALREADY-stale access token (condition 3) outranks a merely
    near-expiry one (condition 1).

    Summaries are kept STABLE across cycles (no live-decrementing minutes/days in
    the summary — those go in the action) so the existing SOS notify de-dup
    (`maybe_write_sos` hashes severity:summary) fires the desktop notification once
    per condition, not every poll."""
    if not config.get("creds_warn", {}).get("enabled", True):
        return None
    # A blank/invalid mount belongs to the reactive URGENT path — never double it
    # up as a WARNING here (keeps the two layers from colliding; test (d)).
    if _live_mount_creds_invalid(creds):
        return None

    il = config.get("independent_logins", {}) if isinstance(config, dict) else {}
    ttl = il.get("refresh_token_ttl_days", 30)
    warn_within = il.get("warn_expiry_within_days", 5)

    # (2) Refresh token approaching its assumed TTL → a browser re-login will soon
    # be required. Highest human-action value, so it wins.
    state, days_left = _expiry_state_from_age(refresh_age_days, config)
    if state in ("near", "expired"):
        # Relogin-by date, derived from now_ms so tests are deterministic.
        by_dt = datetime.fromtimestamp(now_ms / 1000, timezone.utc) + timedelta(days=max(0.0, days_left or 0.0))
        by_date = by_dt.date().isoformat()
        age_txt = f"~{refresh_age_days:.0f} days old" if refresh_age_days is not None else "of unknown age"
        if state == "near":
            return SOSCondition(
                severity="warning",
                summary=(f"refresh token on {mount_label} ({account}) nears its assumed "
                         f"{ttl}-day expiry — plan a browser re-login soon"),
                action=(
                    f"The {mount_label} mount's login for '{account}' is {age_txt}; its OAuth "
                    f"refresh token is assumed to expire around {by_date} (within "
                    f"{warn_within}d — config independent_logins.warn_expiry_within_days). Once it "
                    f"expires a live session can no longer self-refresh and the mount blanks / logs "
                    f"out (the 2026-07-05 failure mode). No action needed yet — it keeps refreshing "
                    f"until then — but plan a browser re-login before {by_date}:\n"
                    f"      cus relogin {account}"),
                affected=account,
            )
        return SOSCondition(
            severity="warning",
            summary=(f"refresh token on {mount_label} ({account}) is past its assumed "
                     f"{ttl}-day TTL — re-login before it blanks"),
            action=(
                f"The {mount_label} mount's login for '{account}' is {age_txt}, past the assumed "
                f"{ttl}-day refresh-token TTL. It MAY still work (the TTL is an assumption, not a "
                f"measured fact — #109 Phase 0), but it is the most likely mount to blank/log out "
                f"next. Re-login it soon to be safe:\n"
                f"      cus relogin {account}"),
            affected=account,
        )

    # Access-token signals need a numeric expiresAt to judge; without one we can't
    # say anything (a valid-but-timestamp-less token isn't provably at risk).
    exp = _creds_expires_at(creds)
    if exp is None:
        return None
    minutes_left = (exp - now_ms) / 60_000.0
    exp_iso = datetime.fromtimestamp(exp / 1000, timezone.utc).isoformat(timespec="minutes")

    # (3) Degrading-but-not-blank: access token already past expiresAt (token_stale)
    # while the login is still present. This is the state that PRECEDES a blank if
    # the self-refresh later fails.
    if minutes_left <= 0:
        return SOSCondition(
            severity="warning",
            summary=(f"access token on {mount_label} ({account}) is already stale — "
                     f"mount will try to self-refresh; at risk if the refresh token is old"),
            action=(
                f"The live {mount_label} mount for '{account}' still has a login, but its OAuth "
                f"access token expired at {exp_iso} (already past). A running session normally "
                f"self-refreshes it on next use — heads-up only. But if that refresh ALSO fails the "
                f"mount blanks (the 2026-07-05 signature). If it stays stale across cycles, plan a "
                f"re-login:\n"
                f"      cus relogin {account}"),
            affected=account,
        )

    # (1) Access token near expiry AND the mount isn't observed refreshing. A
    # healthy session refreshes well ahead of expiry (fresh tokens sit hours out),
    # so a token this close to expiry whose creds file hasn't been rewritten
    # recently is one that isn't self-maintaining.
    window = config.get("creds_warn", {}).get("access_expiry_minutes", 45)
    if minutes_left <= window and not recently_refreshed:
        return SOSCondition(
            severity="warning",
            summary=(f"access token on {mount_label} ({account}) expires soon and the "
                     f"mount isn't refreshing — may age into a stale/blank state"),
            action=(
                f"The live {mount_label} mount for '{account}' has an OAuth access token expiring in "
                f"~{minutes_left:.0f} min ({exp_iso}) and its creds file hasn't been rewritten "
                f"recently, so it isn't self-refreshing right now. A healthy session refreshes well "
                f"ahead of expiry; near-expiry AND idle is the state that ages into a stale/blank "
                f"mount if a later refresh can't reach Anthropic. Heads-up only — it should self-heal "
                f"on next use; watch for a blank-mount SOS on {mount_label}."),
            affected=account,
        )
    return None


def _diagnose_live_mounts_creds_health(state: dict, config: dict) -> list[SOSCondition]:
    """Impure driver for the pre-emptive creds-health scan: enumerate every LIVE
    mount, read its creds + resolve its refresh-token age + whether it was recently
    rewritten, and collect the WARNINGs `_diagnose_mount_creds_health` returns.

    Live mounts, by mode (mirrors the reactive layer's scoping):
      * LANES — per_session/hybrid: every slot with a live session on its own mount
        (`occupied_slot_accounts`, /proc ground truth). The file that lane's session
        reads is `mount_creds_path(slot_path(slot))`.
      * SHARED mount — global/hybrid: `~/.claude/.credentials.json`, read by bare
        sessions; its account is the active one.

    Read-only: reads creds files and stats their mtime; never writes, refreshes, or
    makes a network call. Returns a (possibly empty) list of WARNING SOSConditions;
    the caller merges them into `diagnose()`'s output so they ride the existing
    SOS.md write + notify de-dup. Off entirely when `creds_warn.enabled` is False."""
    if not config.get("creds_warn", {}).get("enabled", True):
        return []
    mode = config.get("mode", "global")
    now_ms = int(time.time() * 1000)
    recent_min = config.get("creds_warn", {}).get("recent_refresh_minutes", 10)
    out: list[SOSCondition] = []
    seen: set[tuple[str, str]] = set()  # (mount_label, account) — one warning per mount/cycle

    def _recently_written(path: Path) -> bool:
        """True iff the creds file was (re)written within recent_refresh_minutes —
        the read-only 'is this mount actively maintaining its token' signal (no
        network probe). A just-rotated mount is not at risk even if momentarily
        caught near a stale expiresAt."""
        try:
            return path.exists() and (time.time() - path.stat().st_mtime) < recent_min * 60
        except OSError:
            return False

    def _scan(mount_label: str, account: str, cred_path: Path, slot: str | None) -> None:
        if (mount_label, account) in seen:
            return
        seen.add((mount_label, account))
        try:
            creds = read_json(cred_path) if cred_path.exists() else None
        except (json.JSONDecodeError, OSError):
            creds = None  # unreadable → reactive layer owns it; predicate returns None
        cond = _diagnose_mount_creds_health(
            mount_label, account, creds, now_ms, config,
            refresh_age_days=_mount_refresh_age_days(account, slot, state, config),
            recently_refreshed=_recently_written(cred_path),
        )
        if cond is not None:
            out.append(cond)

    if mode in ("per_session", "hybrid"):
        for account, slots in occupied_slot_accounts(state).items():
            for slot in slots:
                _scan(slot, account, mount_creds_path(slot_path(slot)), slot)
    if mode in ("global", "hybrid"):
        active = state.get("active")
        if active:
            _scan("shared", active, CREDS_JSON, None)
    return out


# ---------------------------------------------------------------------------
# Fix 1 — PREEMPT the blank (proactive mount-token refresh, 2026-07-07)
# ---------------------------------------------------------------------------
# The single highest-value fix for the recurring blank-mount failure. The warn
# scan above emits a heads-up when a live lane's OAuth access token is near
# expiry AND its mount isn't observed refreshing — the EXACT condition that
# precedes the classic blank: Claude Code, seeing its token near expiry, attempts
# its OWN in-place refresh of the mount's .credentials.json, and a mid-write
# failure leaves the file blank (accessToken:"" / expiresAt:0) → "Not logged in".
# Rather than only WARN, this PROACTIVELY performs the refresh cus-side (a pure
# token-mint grant via `_refresh_account_token`, no quota, no session) and writes
# the fresh, valid token straight into the mount — so the mount stays ahead of
# expiry and Claude Code never needs its fragile in-place refresh. This breaks
# the heal→blank→heal cycle at its SOURCE. Incident it targets: 2026-07-07
# tabby-5/slot-7 (rayi6) sat "not logged in" until a human noticed.


def _preempt_live_lane_blanks(state: dict, config: dict, no_execute: bool = False) -> list[str]:
    """Proactively refresh live lane mounts that are near-expiry-AND-idle, BEFORE
    Claude Code's failure-prone in-place refresh can blank them (Fix 1).

    For every LIVE lane (per_session/hybrid; /proc-gated via `occupied_slot_accounts`)
    whose mount is still VALID but whose access token is within
    `creds_warn.access_expiry_minutes` of expiry and hasn't been rewritten within
    `creds_warn.recent_refresh_minutes` (i.e. it isn't self-maintaining — the exact
    predicate condition 1 of `_diagnose_mount_creds_health` warns on), mint a fresh
    token via `_refresh_account_token(account, creds_path=<mount>)` and let it
    rewrite the mount in place. Because a successful refresh bumps the mount mtime,
    the subsequent warn scan sees it "recently refreshed" and stays silent — a
    preempted lane neither warns nor blanks.

    Safety / degrade-to-safe posture (never crashes a poll, backward-compatible):
      * Gated by `creds_warn.enabled` AND `creds_warn.preempt.enabled` (default on);
        both off ⇒ the pre-2026-07-07 warn+heal-only behavior, bit-for-bit.
      * Only touches a lane whose mount is VALID and carries a usable refresh token
        (an ALREADY-blank mount belongs to the reactive heal, not the preempt).
      * Poll-burnout backoff: at most one ATTEMPT per lane per
        `preempt.cooldown_minutes`, success or fail, so a persistently-failing
        grant can't re-fire every poll (the 2026-06-19 burnout shape).
      * Family-safe (#104): refreshes using the mount's OWN refresh token and
        writes back ONLY to that mount — no other mount/store is touched; the
        existing save-back propagates the fresh token to the canonical store.
      * On ANY failure (refresh disabled, grant error, non-200) it does NOTHING to
        the mount and leaves the lane for the existing warn+heal path — degrade,
        never worsen.

    Returns the list of preempted slot names. `no_execute` logs intent only."""
    if config.get("mode", "global") not in ("per_session", "hybrid"):
        return []
    cw = config.get("creds_warn", {}) if isinstance(config, dict) else {}
    if not cw.get("enabled", True):
        return []
    pcfg = cw.get("preempt", {}) if isinstance(cw, dict) else {}
    if not pcfg.get("enabled", True):
        return []
    window = cw.get("access_expiry_minutes", 45)
    recent_min = cw.get("recent_refresh_minutes", 10)
    cooldown_min = pcfg.get("cooldown_minutes", 15)
    now = time.time()
    now_ms = int(now * 1000)
    preempted: list[str] = []
    for account, slots in occupied_slot_accounts(state).items():
        for slot in slots:
            mount = mount_creds_path(slot_path(slot))
            try:
                creds = read_json(mount) if mount.exists() else None
            except (json.JSONDecodeError, OSError):
                creds = None
            # An already-blank mount is the REACTIVE heal's job, not the preempt's
            # — the whole point of preempt is to act while the mount is still valid.
            if _live_mount_creds_invalid(creds):
                continue
            # Fix 3: the mount is valid → refresh its last-known-good shadow. Doing
            # it here (every cycle, on every live valid lane) keeps the shadow
            # current for instant heal + blank-vs-lastvalid forensic diffing.
            _update_lane_lastvalid(mount, creds)
            # Near-expiry gate. Skip the healthy (plenty of runway) and the already
            # -stale (minutes_left<=0 — that's the warn/heal path, not a token we
            # can safely pre-refresh: a stale mount may already be mid-fail).
            exp = _creds_expires_at(creds)
            if exp is None:
                continue
            minutes_left = (exp - now_ms) / 60_000.0
            if minutes_left <= 0 or minutes_left > window:
                continue
            # Self-maintaining? A mount rewritten within recent_min is actively
            # rotating its own token — leave it alone (matches the warn scan's
            # `recently_refreshed` suppression).
            try:
                if mount.exists() and (now - mount.stat().st_mtime) < recent_min * 60:
                    continue
            except OSError:
                continue
            # Need a usable refresh token to mint a new access token; without one
            # this lane needs a browser relogin (the refresh-TTL warning handles it).
            if _credential_refresh_token(creds) is None:
                continue
            # Poll-burnout backoff (per lane, success OR fail).
            key = (slot, account)
            last_attempt = _PREEMPT_ATTEMPT_MS.get(key)
            if last_attempt is not None and (now - last_attempt) < cooldown_min * 60:
                continue
            _PREEMPT_ATTEMPT_MS[key] = now
            old_fp = _audit_token_fp(creds)
            if no_execute:
                click.echo(f"  preempt (--no-execute): WOULD proactively refresh live lane {slot} "
                           f"({account}) mount token (~{minutes_left:.0f} min to expiry, idle) "
                           f"before Claude Code's in-place refresh can blank it (Fix 1)")
                continue
            # Proactive refresh straight into the mount. On failure, do NOT touch
            # the mount — degrade to the existing warn+heal path.
            if _refresh_account_token(account, creds_path=mount):
                try:
                    fresh = read_json(mount)
                except (json.JSONDecodeError, OSError):
                    fresh = None
                _cred_audit(
                    "blank-preempt", "wrote",
                    f"proactive mount-token refresh ahead of expiry (~{minutes_left:.0f}m left, idle)",
                    slot=slot, account=account, token_fp=_audit_token_fp(fresh),
                    extra=(f"prev_token_fp={old_fp} mount_mtime={_mtime_iso(mount)} "
                           f"new_expiry={_expiry_repr(fresh)}"))
                _update_lane_lastvalid(mount, fresh)
                click.echo(f"  PREEMPT: refreshed live lane {slot} ({account}) mount token ahead of "
                           f"expiry (~{minutes_left:.0f} min left, idle) — kept it ahead of Claude "
                           f"Code's failure-prone in-place refresh, so it can't blank (Fix 1).")
                preempted.append(slot)
            else:
                _cred_audit(
                    "blank-preempt", "skipped-refresh-failed",
                    "proactive refresh grant failed — degrading to the existing warn+heal path",
                    slot=slot, account=account, token_fp=old_fp,
                    extra=f"mount_mtime={_mtime_iso(mount)} expiry={_expiry_repr(creds)}")
    return preempted


# ---------------------------------------------------------------------------
# Fix 2 — STUCK-SESSION escalation (heal→blank→heal detector, 2026-07-07)
# ---------------------------------------------------------------------------
# The reactive file-heal restores a blanked mount, but that is SILENT success
# even when it doesn't actually un-stick the live session: once the running
# Claude Code process has cached the logged-out state ("Not logged in · Run
# /login"), restoring the file does NOT revive it — the process must be
# RELAUNCHED (hot_swap is disabled in this config, so nothing does it
# automatically). A lane blanking REPEATEDLY (heal→blank→heal) is the signature
# of exactly this: the file keeps getting fixed and re-blanked while the process
# stays wedged. This escalates that case to a distinct URGENT that tells the
# operator a file-heal won't help and names the relaunch command.


def _stuck_lane_sos(slot: str, account: str, heal_count: int, window_min: int) -> SOSCondition:
    """URGENT for a lane stuck in a heal→blank→heal cycle (Fix 2). The summary is
    STABLE (no live-changing counts) so `maybe_write_sos`'s severity:summary
    de-dup notifies once, not every poll; the volatile count/window ride the
    action."""
    return SOSCondition(
        severity="urgent",
        summary=(f"lane {slot} ({account}) keeps blanking / session stuck 'not logged in' — "
                 f"file healed but the live process needs a RELAUNCH"),
        action=(
            f"Lane {slot}'s mount for '{account}' has been auto-healed {heal_count} times within "
            f"{window_min} min — a heal→blank→heal cycle. Each heal restores the FILE, but the "
            f"running Claude Code process on this lane cached the logged-out state ('Not logged in "
            f"· Run /login') and a file-heal cannot revive it (hot_swap is disabled, so nothing "
            f"auto-relaunches). The 2026-07-07 tabby-5/slot-7 signature. The live process needs a "
            f"RELAUNCH — restore the session in place:\n"
            f"      cus launch {account} --lane {slot} -- --continue\n"
            f"    (Fix 1's proactive refresh should keep the mount ahead of expiry so this stops "
            f"recurring; if it persists, the account likely needs a browser re-login: "
            f"cus relogin {account}.)"),
        affected=account,
    )


def _diagnose_stuck_lanes(state: dict, config: dict) -> list[SOSCondition]:
    """Escalate every LIVE lane healed >= `creds_warn.stuck.heal_threshold` times
    within `creds_warn.stuck.window_minutes` to the stuck-session URGENT (Fix 2).

    Reads the in-process `_LANE_HEAL_HISTORY` populated by `_auto_heal_live_lanes`,
    pruning each lane's timestamps to the rolling window as it goes (so a lane that
    stops blanking ages out of the count and clears). Only reports lanes that are
    STILL live — a lane whose session exited is no longer stuck. Off entirely
    outside lane-serving modes."""
    if config.get("mode", "global") not in ("per_session", "hybrid"):
        return []
    cw = config.get("creds_warn", {}) if isinstance(config, dict) else {}
    scfg = cw.get("stuck", {}) if isinstance(cw, dict) else {}
    threshold = scfg.get("heal_threshold", 2)
    window_min = scfg.get("window_minutes", 30)
    now = time.time()
    live = {(slot, account)
            for account, slots in occupied_slot_accounts(state).items()
            for slot in slots}
    out: list[SOSCondition] = []
    for key in list(_LANE_HEAL_HISTORY.keys()):
        recent = [t for t in _LANE_HEAL_HISTORY[key] if (now - t) <= window_min * 60]
        if recent:
            _LANE_HEAL_HISTORY[key] = recent
        else:
            del _LANE_HEAL_HISTORY[key]  # fully aged out — forget the lane
            continue
        if key not in live:
            continue  # session exited → not stuck anymore
        if len(recent) >= threshold:
            slot, account = key
            out.append(_stuck_lane_sos(slot, account, len(recent), window_min))
    return out


def _premium_target_loss_reason(name: str, acct: dict, config: dict) -> str:
    """Human label for WHY a reachable account is NOT a valid premium swap target.

    Used only to name the LOST capacity in the pre-emptive headroom SOS
    (`_diagnose_premium_headroom`) — it does not drive the decision, it just
    tells the operator which lever to pull ("rayi3 TOKEN_STALE = revive it").
    The order mirrors the hard-then-soft filter order in `pick_swap_target`, and
    every branch is computed with the SAME helper the picker uses so the label
    can never disagree with the actual eligibility verdict.
    """
    # token_stale = our stored access token aged out (nothing polls an idle
    # account), but the refresh token is still good — recoverable. As of the
    # 2026-07-06 false-URGENT fix, an idle token_stale account WITH real headroom
    # is counted as a viable (refresh-on-swap) target upstream and never reaches
    # this labeller. It only lands here when it is ALSO lost for another reason
    # (its last-known numbers read over a cap). We deliberately label it
    # TOKEN_STALE rather than the cap reason: a stale account's cached usage % is
    # NOT trustworthy for a decision (2026-07-05 incident), so the honest operator
    # action is "revive it" (force-poll / relogin), after which its REAL numbers
    # decide — not "it's over cap" on a number we could not reconfirm.
    if acct.get("token_stale"):
        return "TOKEN_STALE"
    if acct.get("token_expired"):
        return "TOKEN_EXPIRED"
    if acct.get("poll_error"):
        return "POLL_ERROR"
    if acct.get("rate_limited"):
        return "RATE_LIMITED"
    est = _account_estimated_effective_pct(acct, config)
    if est >= config.get("never_swap_to_pct", 100):
        return f"SATURATED ({est:.0f}%)"
    hard_7d = _hard_7d_cap_for_config(config)
    if acct.get("current_7d_pct", 0) >= hard_7d:
        return f"over 7d cap ({acct.get('current_7d_pct', 0):.0f}% ≥ {hard_7d:.0f}%)"
    if _max_model_weekly_from_acct(acct, config) >= _model_weekly_target_cap_for_config(config):
        return "over per-model weekly cap"
    if _target_would_immediately_re_trip(acct, config):
        eff = _account_effective_pct(acct, config)
        return f"at ladder step ({eff:.0f}% ≥ {acct.get('next_swap_at_pct', 50):.0f}%)"
    return "unavailable"


def _candidate_is_valid_premium_target(
    leaver: str, candidate: str, state: dict, accounts: dict, premium_config: dict) -> bool:
    """True iff `candidate` is a GENUINE swap target for a premium lane leaving
    `leaver`, per the exact predicate the daemon's picker uses.

    Rather than re-implement the eligibility filters (token_expired / poll_error
    / rate_limited / saturation / 7d cap / per-model weekly cap / would-re-trip),
    we hand `pick_swap_target` a two-account shim — {leaver (active), candidate}
    under the PREMIUM pool config — so the only account it can possibly return is
    `candidate`. If it returns it CLEANLY (no `[DEGRADED: ...]` note) the account
    is really below every cap; a degraded pick means the picker only reached the
    candidate via a fallback tier (no target below 7d cap, or all candidates
    would re-trip their own ladder) — i.e. it is NOT below the caps that would
    force it back off, so it is not a valid pre-emptive target. This matches the
    "BELOW the caps that would force it off" definition in the SOS spec and is a
    stricter cousin of Condition 2b's `"[DEGRADED:" ... "no targets below"` test.
    token_stale is handled by the caller (pick_swap_target does not filter it).
    """
    shim = dict(state)
    shim["accounts"] = {leaver: accounts[leaver], candidate: accounts[candidate]}
    shim["active"] = leaver
    tgt = pick_swap_target(shim, premium_config)
    if tgt is None:
        return False
    if "[DEGRADED:" in tgt.reason:
        return False
    return tgt.name == candidate


def _diagnose_premium_headroom(
    state: dict,
    config: dict,
    occupied: dict[str, list[str]],
    free_family_accounts: set[str],
) -> SOSCondition | None:
    """Pre-emptive alarm: premium lanes are live but the pool of accounts they
    could swap ONTO has collapsed, so a lane will 429 the moment it caps (GH #150).

    Why this exists separately from Condition 2b: 2b only fires once a lane is
    ALREADY at/over its ladder step ("this lane cannot rotate NOW"). By then a
    live premium session is often already wedged. This check fires EARLY — while
    lanes are still climbing — the instant the count of VALID swap targets for
    the premium pool drops to zero (URGENT) or to a single target while a lane is
    already past the high-water step (WARNING). It is the exact 2026-07-05 shape:
    the one spare account went token_stale, so when live premium sessions hit
    their 5h caps the daemon had nowhere to swap and they 429'd.

    Pure predicate (mirrors `_diagnose_live_mount_creds`): the impure inputs —
    which accounts are LIVE-occupied (`occupied`, from /proc) and which of them
    carry a FREE pooled login family (`free_family_accounts`, from the login
    store on disk) — are computed by the caller and injected, so this function
    is unit-testable with plain dicts and never touches the filesystem.

    Returns one SOSCondition or None. None whenever there is headroom (>=1 valid
    target and no hot lane), in any global-only / no-premium-lane setup, and in
    every mode that does not serve slot lanes.
    """
    mode = config.get("mode", "global")
    # Only lane-serving modes have per-slot lanes; global-only never fires.
    if mode not in ("hybrid", "per_session"):
        return None
    accounts = state.get("accounts", {})

    # 1. Identify LIVE PREMIUM lanes: occupied accounts with >=1 premium-pool slot.
    #    Standard-pool lanes are excluded — they are not gated onto the scarce
    #    Fable-clean accounts, so their target scarcity is a different question.
    premium_lanes: dict[str, list[str]] = {}
    for acct_name, slots in occupied.items():
        prem = sorted(s for s in slots if _slot_pool(state, s, config) == "premium")
        if prem:
            premium_lanes[acct_name] = prem
    if not premium_lanes:
        return None  # no premium lanes to protect → nothing to warn about

    # 2. Double-booking exclusion (GH #104), mirroring Condition 2b: an account
    #    held by a LIVE mount cannot be a fresh swap target — a second live mount
    #    on one refresh-token family clobbers it — UNLESS it has a FREE pooled
    #    login family (then a lane can borrow a distinct family). In hybrid the
    #    shared-mount active account is also a live holder.
    shared_active = state.get("active")
    live_held = set(occupied.keys()) | (
        {shared_active} if (mode == "hybrid" and shared_active) else set())
    if independent_logins_enabled(config):
        blocking = {a for a in live_held if a not in free_family_accounts}
    else:
        blocking = set(live_held)

    # 3. Count usable targets and NAME the lost capacity. A candidate is any known
    #    account not blocked by a live mount. For each we drive the real picker with
    #    a distinct premium-lane leaver (cap/headroom filters do not depend on WHICH
    #    lane departs, so any premium leaver != candidate serves).
    #
    #    Two usable buckets, because they carry DIFFERENT urgency:
    #      • valid_targets     — FRESH, below every cap, refresh-token good. A swap
    #                            lands cleanly with no recovery step. Drives the
    #                            existing healthy / high-water-WARNING tiers unchanged.
    #      • stale_refreshable — idle token_stale (its stored access token aged out
    #                            because nothing polls an idle account) but the
    #                            refresh token is still VALID, and below every cap on
    #                            its last-known numbers. A swap installs the snapshot
    #                            creds and the existing auto-refresh fast-track (see
    #                            the token_stale fast-track near cus.py:2779 /
    #                            `_refresh_account_token`) revives the token on use —
    #                            so it IS a usable target, just not a zero-recovery one.
    #
    #    FALSE-URGENT INCIDENT (2026-07-06) this split fixes: the only free
    #    Fable-clean accounts were IDLE (rayi4 @ 5h 0%/Fable 54%, rayi3 @ 5h 0%/Fable
    #    82%). An idle account is never polled, so its stored access token aged out →
    #    token_stale. The OLD code blanket-EXCLUDED every token_stale account from the
    #    target count, so it reported "0 valid swap targets" and screamed the URGENT
    #    will-429 line — even though those accounts were perfectly swappable. Proof:
    #    `cus force-poll rayi4 rayi3` freshened them and `cus sos` flipped straight to
    #    all-clear with ZERO change in real capacity. A token_stale-but-not-
    #    token_expired account with real headroom is therefore a genuine target and
    #    must NOT trigger the 429 klaxon.
    #
    #    STALE-vs-REFRESHABLE distinction — DO NOT "fix" this back by re-excluding
    #    stale accounts: a token_stale account's cached USAGE numbers can be a wrong
    #    stale reading, and we must NOT trust a stale % as authoritative for a swap
    #    DECISION (that bit us on 2026-07-05 — see the stale-guard in pick_swap_target).
    #    But this ALARM is not executing a swap on the stale number — it only decides
    #    "does a plausibly-usable target EXIST." Counting a stale-but-refreshable
    #    account as a *candidate* is therefore correct and safe: if its last-known
    #    numbers show headroom it is a viable target; the refresh-on-swap then re-reads
    #    its REAL numbers before anything commits. token_EXPIRED (dead refresh token)
    #    is the genuinely-unusable case — pick_swap_target filters it, so it never
    #    becomes a usable target and it alone (with over-cap/blocked candidates) can
    #    still produce the URGENT line.
    premium_config = _config_for_pool(config, "premium")
    disabled = _disabled_accounts(config)   # operator-parked: never a target, not lost capacity
    valid_targets: list[str] = []       # fresh, below every cap → clean swap
    stale_refreshable: list[str] = []   # idle token_stale, below caps → refresh-on-swap
    lost: list[str] = []                # reachable-but-unusable = visible lost capacity
    for name, acct in sorted(accounts.items()):
        if name in blocking:
            continue  # held by a live mount, no free family → would double-book
        if name in disabled:
            # Operator took this account OUT of rotation (`cus disable`). It is
            # not a swap target, so it can't be valid_targets — and it is also
            # NOT "lost capacity" to be revived (naming a deliberately-parked
            # account as lost would tell the operator to un-park it, the opposite
            # of what they asked). Skip it entirely: neither counted nor named.
            # (pick_swap_target already excludes it, so _candidate_is_valid would
            # return False regardless; this makes the intent explicit and keeps
            # it out of the `lost` list.)
            continue
        leavers = [a for a in premium_lanes if a != name]
        if not leavers:
            continue  # the only premium lane IS this account; nothing swaps onto it
        leaver = leavers[0]
        # pick_swap_target does NOT filter token_stale (only token_expired /
        # poll_error), and its per-model gate reads a stale account as 0.0
        # (stale-guard) → eligible. So this predicate tells us whether the candidate
        # is below every cap on its LAST-KNOWN numbers — for fresh and stale alike.
        is_valid = _candidate_is_valid_premium_target(
            leaver, name, state, accounts, premium_config)
        if acct.get("token_stale") and not acct.get("token_expired"):
            # Refreshable spare: below caps → usable-after-refresh (counts toward
            # "a target exists"); over a cap on its (untrusted) last-known numbers →
            # revive it before trusting them, so it is named as lost, not counted.
            if is_valid:
                stale_refreshable.append(name)
            else:
                lost.append(f"{name} {_premium_target_loss_reason(name, acct, config)}")
            continue
        if is_valid:
            valid_targets.append(name)
        else:
            lost.append(f"{name} {_premium_target_loss_reason(name, acct, config)}")

    n_lanes = len(premium_lanes)
    m = len(valid_targets)           # FRESH, below-cap targets (drive the existing tiers)
    s = len(stale_refreshable)       # idle-stale-but-refreshable targets

    # High-water step: reuse thresholds.steps' top step (~90%), config-driven.
    steps = config.get("thresholds", {}).get("steps") or THRESHOLD_STEPS[:-1]
    top_step = max(steps) if steps else 90
    hot_lanes = []  # premium lanes already past the high-water mark
    for acct_name, prem_slots in sorted(premium_lanes.items()):
        eff = _account_effective_pct(accounts.get(acct_name, {}), config)
        if eff >= top_step:
            hot_lanes.append((acct_name, prem_slots, eff))

    lane_desc = "; ".join(
        f"{a} ({'/'.join(premium_lanes[a])} @ {_account_effective_pct(accounts.get(a, {}), config):.0f}%)"
        for a in sorted(premium_lanes)
    )
    lost_desc = (f" Lost capacity: {', '.join(lost)}." if lost else "")
    levers = (
        "Premium lanes rotate onto scarce Fable-clean accounts and the pool of viable "
        "targets has collapsed — when a live lane hits its 5h/7d cap it will have nowhere "
        "to swap and the session 429s. Levers: revive stale/offline accounts "
        "(`cus relogin <acct>`, or wait for auto-refresh), add capacity (`cus add <name>`), "
        "or reduce concurrent premium load (exit a premium session)."
        + lost_desc
        + f" Live premium lanes: {lane_desc}."
    )

    # ---- At least one FRESH target: unchanged healthy / high-water behavior ----
    # A fresh, below-cap target means a clean swap is available, so the only
    # question is whether the pool is thin. This is the backward-compatible path;
    # its verdicts are byte-for-byte what they were before the stale split.
    if m >= 1:
        if m > 1:
            return None  # >1 fresh target = healthy pool, even with a hot lane
        if not hot_lanes:
            return None  # single fresh target but no lane is hot yet → quiet
        # m == 1 AND a lane is already past the high-water step: a single mistake
        # (that last target caps, or the hot lane trips) away from the wall → WARNING.
        hot_names = ", ".join(f"{a} @ {eff:.0f}%" for a, _s, eff in hot_lanes)
        return SOSCondition(
            severity="warning",
            summary=(f"{n_lanes} premium lane(s) live, only 1 valid swap target — "
                     f"lane(s) past {top_step:.0f}% ({hot_names}) are near the wall"),
            action=levers,
            affected="system",
        )

    # ---- No FRESH target (m == 0), but idle-stale-but-refreshable targets exist ----
    # This is the 2026-07-06 false-URGENT shape: the only usable targets are idle
    # token_stale accounts that WILL refresh on swap. NOT a 429 klaxon — downgrade to
    # an informational note (severity "info", quieter than "warning"). The statusline
    # surfaces only urgent/warning, so this stays OFF the status bar and shows only in
    # `cus sos` / SOS.md. This is the ONLY behavior change vs the old URGENT-on-m==0.
    if s >= 1:
        stale_desc = ", ".join(stale_refreshable)
        action = (
            "The only pooled swap targets for the live premium lane(s) are idle and "
            "token_stale — their stored access token aged out because nothing polls an "
            "idle account, but the refresh token is still valid. They are NOT capped: a "
            "swap installs their snapshot creds and the auto-refresh fast-track revives "
            "the token on use, so no 429 results. Informational only. To freshen now: "
            f"`cus force-poll {stale_refreshable[0]}` (or `cus relogin <acct>`)."
            + lost_desc
            + f" Idle-stale targets: {stale_desc}. Live premium lanes: {lane_desc}."
        )
        return SOSCondition(
            severity="info",
            summary=(f"{n_lanes} premium lane(s) live; {s} swap target(s) are idle-stale "
                     f"and will refresh on swap"),
            action=action,
            affected="system",
        )

    # ---- m == 0 AND s == 0: genuinely zero usable targets ----
    # Every candidate is over a cap, poll_error, rate_limited, or token_EXPIRED (dead
    # refresh token). The next premium lane to cap 429s with no recourse → URGENT even
    # before any lane is hot. This is the ONLY remaining path to the will-429 klaxon;
    # idle-stale-but-refreshable spares no longer reach it (they route to INFO above).
    return SOSCondition(
        severity="urgent",
        summary=(f"{n_lanes} premium lane(s) live, 0 valid swap targets — "
                 f"a premium lane will 429 when it caps"),
        action=levers,
        affected="system",
    )


def diagnose(state: dict | None = None, config: dict | None = None) -> list[SOSCondition]:
    """Scan state + system for conditions requiring human intervention.

    Returns a list of SOSCondition. Empty list = all clear.

    Conditions checked:
      1. Any account with token_expired=true → re-login needed.
      2. Active account over threshold AND no swap target available.
      3. All accounts blocked (token_expired / rate_limited / poll_error).
      4. Stale poll — no successful poll in the last N minutes.
      5. Daemon supposedly running (pid file exists) but process dead.
      6. SwapStored credentials never refreshed and active is over threshold
         (hint to run `cus init --force`).
    """
    if state is None:
        state = load_state() if STATE_JSON.exists() else {"active": None, "accounts": {}}
    if config is None:
        config = load_config()
    out: list[SOSCondition] = []
    accounts = state.get("accounts", {})

    # Condition 0 (2026-07-05, GH #141): the LIVE shared mount itself is unusable.
    # Every other condition here trusts state.json + per-account snapshots; none
    # inspect the live ~/.claude/.credentials.json that BARE sessions on the
    # shared mount actually read. In the 2026-07-05 incident that file was blanked
    # (expiresAt 0 + empty accessToken) while the active account's snapshot stayed
    # healthy — a bare session showed "not logged in" but `cus sos` said all-clear.
    # Only meaningful where the shared mount serves sessions (global/hybrid); in
    # per_session the shared mount is observe-only/authless by design, so the pure
    # predicate skips it. Read the files here (impure) and hand the parsed payloads
    # + identities to the pure, unit-tested predicate.
    live_mode = config.get("mode", "global")
    if live_mode in ("global", "hybrid"):
        try:
            live_creds = read_json(CREDS_JSON) if CREDS_JSON.exists() else None
        except (json.JSONDecodeError, OSError):
            live_creds = None  # unreadable == unusable from a session's point of view
        try:
            live_ident = _identity_fields(read_json(CLAUDE_JSON)) if CLAUDE_JSON.exists() else {}
        except (json.JSONDecodeError, OSError):
            live_ident = {}
        live_active = state.get("active")
        active_ident = account_canonical_identity(live_active) if live_active else {}
        live_cond = _diagnose_live_mount_creds(
            live_creds, live_mode, live_active, live_ident, active_ident)
        if live_cond is not None:
            out.append(live_cond)

    # Condition 0b (2026-07-05, GH #141 follow-up): a LIVE LANE's own mount creds
    # are blanked. Condition 0 above only inspects the SHARED mount; a slot lane's
    # own slot-N/.credentials.json can be blanked independently (Claude's logout,
    # a crash mid-write) — the 2026-07-05 slot-2 incident, which `cus sos` reported
    # as all-clear because nothing scanned lane mounts. Only in lane-serving modes
    # (per_session/hybrid); `_blanked_live_lanes` self-gates on mode and returns
    # only LIVE lanes with a registered account whose mount is actually invalid
    # (idle/observe-only slots and mid-swap slots are excluded there). In the
    # daemon, `_emit_sos_after` auto-heals blanked lanes BEFORE this diagnose runs,
    # so a lane only reaches here when it has NO usable source (genuine relogin
    # needed); an ad-hoc `cus sos` still reports the blank so an operator sees it.
    for _lane_slot, _lane_acct in _blanked_live_lanes(state, config):
        out.append(_lane_mount_sos(_lane_slot, _lane_acct))

    # Condition 0c (2026-07-07, GH #104): a LIVE LANE shares its account with the
    # shared mount (or another live lane) but holds NO distinct login family —
    # the divergence-logout SETUP. Conditions 0/0b only fire AFTER a mount is
    # already blanked; this fires on the SETUP, before the token rotation clobbers
    # anyone, so an operator can re-provision a distinct family first. Exactly the
    # 2026-07-07 chats1a shape: slot-2 landed on merkos (also the shared-mount
    # account and on slot-1) with login_family=None, and ~5 min later a rotation
    # logged the live session out for an hour. `_divergence_risk_lanes` self-gates
    # on the independent-logins pool feature + lane-serving mode and never flags a
    # lane that holds a distinct family.
    for _dv_slot, _dv_acct, _dv_expired in _divergence_risk_lanes(state, config):
        # Log the clobber PRECONDITION the moment it exists — before any save-back
        # can carry a foreign token into the wrong store. This lane's live family
        # already runs on another live mount of `_dv_acct` (the divergence setup);
        # shared=true is that signal. token_fp names the shared family.
        try:
            _dv_fp = _audit_token_fp(read_json(mount_creds_path(slot_path(_dv_slot))))
        except (json.JSONDecodeError, OSError):
            _dv_fp = "unreadable"
        _cred_audit("divergence-detected", "detected",
                    "lane shares one live token family with another mount (#104)"
                    + ("; lane mount already blank/expired" if _dv_expired else ""),
                    slot=_dv_slot, account=_dv_acct, shared=True, token_fp=_dv_fp)
        out.append(_lane_divergence_sos(_dv_slot, _dv_acct, _dv_expired))

    # Condition 1: token expired anywhere
    for name, acct in accounts.items():
        if acct.get("token_expired"):
            cmd = _relogin_command_for(name)
            # GH #77 auto-heal: if the ACTIVE account's snapshot is fresher
            # than the live file, the re-login already happened under the
            # storage dir — the fix is a one-command sync, not another
            # browser round-trip. Lead with that instead of the generic
            # advice (which is what kept the #77 outage loop going).
            if state.get("active") == name and _snapshot_fresher_than_live(name):
                action = (f"The '{name}' snapshot holds FRESHER tokens than the live file — a storage-dir "
                          f"re-login already happened (GH #77). Install it live:\n"
                          f"      python3 ~/repos/claude-usage-swap/cus.py relogin {name} --finish\n"
                          f"    then verify:\n      python3 ~/repos/claude-usage-swap/cus.py poll")
            else:
                action = f"Run interactively in a terminal:\n      {cmd}\n    Complete the browser /login, then:\n      python3 ~/repos/claude-usage-swap/cus.py poll"
            # GH #79: if a rotated credential backup exists, mention its age —
            # a token that "expired" because a clobber bug replaced it can be
            # brought back with one command (refresh tokens live ~30 days), no
            # browser needed. Let the operator weigh restore vs re-login.
            backups = list_creds_backups(name)
            if backups:
                action += (f"\n    Or, if the token died from a credential overwrite (not real expiry), "
                           f"a restore may be faster:\n      cus restore-creds {name} --list\n"
                           f"    Newest backup: {_describe_creds_backup(backups[0])}")
            out.append(SOSCondition(
                severity="urgent",
                summary=f"{name}: OAuth token expired",
                action=action,
                affected=name,
            ))

    # Condition 2 + 3: targets available?
    # Honor allow_rate_limited_targets (issue #5): if set, rate_limited
    # accounts are still "valid" candidates from the picker's perspective,
    # so SOS shouldn't claim "no other account available" when the
    # orchestrator would have happily swapped to one.
    allow_rl = config.get("smart_strategy", {}).get("allow_rate_limited_targets", False)
    def is_valid(a: dict) -> bool:
        if a.get("token_expired") or a.get("poll_error"):
            return False
        if a.get("rate_limited") and not allow_rl:
            return False
        return True
    valid = [n for n, a in accounts.items() if is_valid(a)]
    active = state.get("active")
    if active and active in accounts:
        active_acct = accounts[active]
        threshold = active_acct.get("next_swap_at_pct", 50)
        # Honor thresholds.five_hour / thresholds.seven_day config so the SOS
        # message reflects the actual ladder calculation. GH #14: previously
        # this max'd over both windows unconditionally, giving misleading
        # "default at 75%" alerts when 7d=75% but the user had configured
        # seven_day:false so only 5h actually drives the swap ladder.
        thr_cfg = config.get("thresholds", {})
        candidates = []
        if thr_cfg.get("five_hour", True):
            candidates.append(active_acct.get("current_5h_pct", 0))
        if thr_cfg.get("seven_day", True):
            candidates.append(active_acct.get("current_7d_pct", 0))
        cur_pct = max(candidates) if candidates else 0

        if not valid:
            out.append(SOSCondition(
                severity="urgent",
                summary="All accounts blocked",
                action="Re-login any TOKEN_EXPIRED accounts (see above). For RATE_LIMITED accounts, wait for cooldown (5h or 7d window reset). Or set `smart_strategy.allow_rate_limited_targets: true` to use rate-limited accounts when no better option exists.",
                affected="system",
            ))
        elif cur_pct >= threshold:
            # Active is over its threshold step. Are there any UNBLOCKED others?
            targets = [n for n in valid if n != active]
            if not targets:
                out.append(SOSCondition(
                    severity="urgent",
                    summary=f"{active} at {cur_pct:.0f}% (≥{threshold}%) and no other account available",
                    action="Add another Claude account (run `claude /login` with CLAUDE_CONFIG_DIR=~/.claude-newname/) or wait for cooldown. Or set `smart_strategy.allow_rate_limited_targets: true` if you have a rate-limited account that works elsewhere.",
                    affected=active,
                ))

    # Condition 2b (2026-07-03): lanes/hybrid TARGET STARVATION. Condition 2
    # above only evaluates the SHARED-mount active account and treats "any
    # unblocked other account exists" as "a target is available" — so it is
    # doubly blind to the lanes failure mode: (a) it never looks at per-lane
    # (slot) accounts at all, and (b) an unblocked-but-saturated account (e.g.
    # rayi1 at 100%) counts as an available target. The observed symptom
    # ("no swaps since lanes"): with as many live mounts as accounts, GH #104's
    # no-double-book rule leaves a hot lane's only candidate(s) either excluded
    # or saturated, so decide_swap held every cycle while `cus sos` reported
    # all-clear and a live session kept burning on a 100% account. This check
    # replays the EXACT exclusion + picker decide_slot_swaps uses, per live-lane
    # account, and raises the operator-actionable flag (add an account / free a
    # lane / wait for a window reset). State-based only — pick_swap_target reads
    # stored current_*_pct, so no live poll is needed here.
    if config.get("mode") in ("hybrid", "per_session"):
        occupied = occupied_slot_accounts(state)
        shared_active = state.get("active")
        # Same cross-mount exclusion the cycle applies (GH #104): every live-lane
        # account, plus the shared-mount account in hybrid mode.
        exclude = set(occupied.keys()) | (
            {shared_active} if (config.get("mode") == "hybrid" and shared_active) else set())
        sat_pct = config.get("usage_growth_gate", {}).get("saturation_pct", 95)
        thr_cfg = config.get("thresholds", {})
        for acct_name, slots in sorted(occupied.items()):
            acct = accounts.get(acct_name, {})
            if not is_valid(acct):
                continue  # blocked accounts are already covered by Conditions 1/3
            lane_thr = acct.get("next_swap_at_pct", 50)
            cand = []
            if thr_cfg.get("five_hour", True):
                cand.append(acct.get("current_5h_pct", 0))
            if thr_cfg.get("seven_day", True):
                cand.append(acct.get("current_7d_pct", 0))
            lane_pct = max(cand) if cand else 0
            if lane_pct < lane_thr:
                continue  # lane below its ladder step — nothing to rotate
            # Build the same shim decide_slot_swaps builds for this group:
            # active = the lane's own account, candidate pool minus every OTHER
            # live-mount account (the lane's own account is retained — it's the
            # one being left). Then run the real picker.
            drop = exclude - {acct_name}
            # Mirror the pool rescue (2026-07-03): a held account with a FREE
            # pooled family is a legal target (execution claims a distinct
            # family), so it must NOT count toward starvation — else Condition 2b
            # false-positives on a lane that can actually rescue.
            if drop and independent_logins_enabled(config):
                drop = drop - {x for x in drop if has_free_login_family(x, state)}
            shim = dict(state)
            if drop:
                shim["accounts"] = {n: a for n, a in accounts.items() if n not in drop}
            shim["active"] = acct_name
            pool = _slot_pool(state, sorted(slots)[0], config)
            tgt = pick_swap_target(shim, _config_for_pool(config, pool))
            starved = tgt is None or (
                "[DEGRADED:" in tgt.reason and "no targets below" in tgt.reason)
            if not starved:
                continue
            # Remedy hint. With the pool feature ON, the starvation means every
            # headroom account is held AND has no free family — so provisioning
            # another family (raising a pool's depth) is the login-free unlock,
            # alongside add-account / free-a-lane / wait-for-reset.
            pool_hint = (" Or provision another independent login for a headroom account "
                         "(`cus login-mount <account>`) so this lane can borrow it."
                         if independent_logins_enabled(config) else "")
            # Tier severity: at/above saturation the lane is effectively frozen
            # and burning a live session → urgent; merely over the first ladder
            # step (still has headroom) → warning, an early nudge to add capacity.
            out.append(SOSCondition(
                severity="urgent" if lane_pct >= sat_pct else "warning",
                summary=f"lane {', '.join(sorted(slots))} on '{acct_name}' at {lane_pct:.0f}% "
                        f"(≥{lane_thr}%) with no swap target",
                action=(f"'{acct_name}' is over its ladder step but every other account is either "
                        f"held by another live mount (GH #104 forbids double-booking — it clobbers "
                        f"the OAuth token) or is saturated, so this lane cannot rotate. Add another "
                        f"account (`cus add <name>`), free a live lane, or wait for another account's "
                        f"5h/7d window to reset." + pool_hint),
                affected=acct_name,
            ))

    # Condition 2c (2026-07-05, GH #150): PRE-EMPTIVE premium-headroom alarm.
    # Condition 2b above only fires once a lane is ALREADY at/over its ladder
    # step — too late (a live premium session is usually wedged by then). This
    # fires EARLY: while lanes are still climbing, the instant the count of VALID
    # swap targets for the premium pool collapses. Exactly the 2026-07-05 shape —
    # the one spare account went token_stale, so live premium sessions 429'd at
    # their 5h caps with nowhere to swap. Impure inputs (which accounts are
    # live-occupied, which carry a free pooled family) are gathered here and
    # handed to the pure, unit-tested predicate. Only for lane-serving modes;
    # the pure helper re-checks mode and premium-lane presence and returns None
    # in every no-lane / global-only case.
    if config.get("mode") in ("hybrid", "per_session"):
        occ = occupied_slot_accounts(state)
        free_fam = ({a for a in occ if has_free_login_family(a, state)}
                    if independent_logins_enabled(config) else set())
        headroom_cond = _diagnose_premium_headroom(state, config, occ, free_fam)
        if headroom_cond is not None:
            out.append(headroom_cond)

    # Condition 3b (GH #9): account stuck in 429 poll-backoff for > 30 min.
    # The polling endpoint is per-IP throttled; another machine hammering it
    # can wedge our local view of an account for hours. Surface this so the
    # operator knows we've lost observability — not a fatal condition (the
    # account itself may still be usable for actual Claude Code traffic),
    # but worth flagging.
    now = datetime.now(timezone.utc)
    for name, acct in accounts.items():
        until = acct.get("poll_backoff_until_ts")
        if not until:
            continue
        last_poll = acct.get("last_poll_ts")
        if not last_poll:
            continue
        try:
            last_poll_dt = datetime.fromisoformat(last_poll.replace("Z", "+00:00"))
        except ValueError:
            continue
        consecutive = acct.get("poll_backoff_consecutive_429s", 0)
        backoff_age = (now - last_poll_dt).total_seconds()
        if backoff_age > 1800 and consecutive >= 3:
            out.append(SOSCondition(
                severity="warning",
                summary=f"{name}: stuck in 429 poll-backoff ({consecutive} consecutive 429s, last poll {int(backoff_age/60)}m ago)",
                action=f"Anthropic's per-IP throttle on /api/oauth/usage is engaged. Usage % shown for {name} is stale (frozen at last successful poll). To bypass once: `cus force-poll {name}`. To wait it out: usually clears in 30-60 min after the offending machine stops polling.",
                affected=name,
            ))

    # Condition 4: stale poll
    # Differential cadence (2026-07-02): staleness must be judged per-account
    # against the cadence that account is actually polled on — fast active /
    # slow inactive / legacy flat (_account_poll_interval). Judging every
    # account against the flat poll_interval_seconds would permanently flag
    # every idle account as "stale" whenever polling.inactive_interval_seconds
    # exceeds 4x the flat interval (e.g. flat 180s + inactive 900s), turning
    # the daemon-down detector into constant false SOS noise. 4 missed cycles
    # of an account's OWN cadence = real problem, same tolerance as before.
    stale_accounts: list[str] = []
    stale_worst_minutes = 0
    for name, acct in accounts.items():
        last_poll = acct.get("last_poll_ts")
        if not last_poll:
            continue
        poll_freshness_seconds = _account_poll_interval(state, config, name) * 4
        try:
            ts = datetime.fromisoformat(last_poll.replace("Z", "+00:00"))
            if (now - ts).total_seconds() > poll_freshness_seconds:
                stale_accounts.append(name)
                stale_worst_minutes = max(stale_worst_minutes, int(poll_freshness_seconds) // 60)
        except ValueError:
            continue
    if stale_accounts and any(accounts.keys()) and accounts.get(list(accounts.keys())[0], {}).get("last_poll_ts"):
        out.append(SOSCondition(
            severity="warning",
            summary=f"Stale usage data for: {', '.join(stale_accounts)} (no fresh poll within 4x each account's poll cadence; worst allowance {stale_worst_minutes} min)",
            action="Daemon may be down. Check: `systemctl --user status cus.service` or restart `python3 ~/repos/claude-usage-swap/cus.py daemon`.",
            affected="daemon",
        ))

    # Condition 5: daemon pid file exists but process is gone
    if DAEMON_PID.exists():
        try:
            pid = int(DAEMON_PID.read_text().strip())
            try:
                os.kill(pid, 0)  # signal 0 = "are you there"
            except ProcessLookupError:
                out.append(SOSCondition(
                    severity="warning",
                    summary=f"Daemon pid {pid} recorded but process is gone",
                    action="Restart: `systemctl --user restart cus.service` or `python3 ~/repos/claude-usage-swap/cus.py daemon`. Then `rm ~/claude-accounts/daemon.pid` if stale.",
                    affected="daemon",
                ))
        except (ValueError, OSError):
            pass

    # ---- per_session conditions (Phase 4.2 guardrail audit) ----

    # Condition 7: slot↔account drift — GH #2 reframed for slots. The slot's
    # on-disk identity is what its session actually uses; state.slots is what
    # the daemon BELIEVES. A mismatch means usage attribution, polling cadence
    # and swap decisions for that slot are all against the wrong account.
    for slot_name, entry in sorted(state.get("slots", {}).items()):
        acct = entry.get("account")
        if not acct:
            continue
        d = slot_path(slot_name)
        cj = mount_claude_json_path(d)
        if not cj.exists():
            continue
        try:
            slot_ids = _identity_fields(read_json(cj))
        except (json.JSONDecodeError, OSError):
            continue
        match = _identities_match(slot_ids, _dir_identity(acct))
        if match is False:  # None = no shared evidence — can't say, stay quiet
            out.append(SOSCondition(
                severity="urgent",
                summary=f"{slot_name} identity does not match its assigned account '{acct}' (slot↔state drift, GH #2 class)",
                action=(f"The slot's live files hold a different account than state.json claims. "
                        f"Check `python3 ~/repos/claude-usage-swap/cus.py slot list` and the slot's .claude.json oauthAccount; "
                        f"fix state.json slots.{slot_name}.account to match reality (record reality — do NOT swap first)."),
                affected=slot_name,
            ))

    # Condition 8: bare session burning a hot account. In per_session mode the
    # daemon never swaps ~/.claude/ (3.4) — so a bare session riding the global
    # active past its ladder is invisible to the per-slot loop and will hit the
    # account's caps. The remedy is launching slotted, not a swap.
    if config.get("mode", "global") == "per_session" and state.get("active"):
        active_name = state["active"]
        active_acct = accounts.get(active_name, {})
        threshold = active_acct.get("next_swap_at_pct", THRESHOLD_STEPS[0])
        eff = max(active_acct.get("current_5h_pct", 0), active_acct.get("current_7d_pct", 0))
        if eff >= threshold and mount_in_use(CLAUDE_DIR):
            out.append(SOSCondition(
                severity="warning",
                summary=(f"Bare session(s) on ~/.claude/ are burning '{active_name}' at {eff:.0f}% "
                         f"(>= its {threshold}% step) — per_session mode will NOT swap the global mount"),
                action=("Exit the bare session and relaunch slotted: `cus launch` "
                        "(or add the alias: alias claude='cus launch auto --')."),
                affected=active_name,
            ))

    # Condition 9: orphan slot dirs — real credentials on disk that no state
    # entry tracks (a crash between mkdir and state save, or hand-made dirs).
    known_slots = set(state.get("slots", {}))
    for d in list_slot_dirs():
        if d.name not in known_slots and mount_creds_path(d).exists():
            out.append(SOSCondition(
                severity="warning",
                summary=f"Orphan slot dir {d.name} holds credentials but has no state entry",
                action=f"Reap it (saves creds back first): `python3 ~/repos/claude-usage-swap/cus.py slot gc --slot {d.name}`",
                affected=d.name,
            ))

    # Condition 10: independent-login store health (GH #109). Fires only for
    # pairs the operator has ALREADY provisioned, so it stays silent for anyone
    # not using the feature (no false nags in the default copy-path config).
    provisioned = list_provisioned_logins()
    mismatched: list[str] = []
    expired: list[str] = []
    expiring: list[str] = []
    for rec in provisioned:
        acct, slot = rec["account"], rec["slot"]
        if not rec["has_login"]:
            continue  # dir with no usable creds — a half-finished provision, not an alarm
        # Wrong-account stored login: --finish refuses this, but a hand-edited
        # store or a --force override could leave one behind. It would seed a
        # swap with the wrong account's tokens, so treat it as critical.
        #
        # 2026-07-06 disambiguation: a login-store-vs-canonical mismatch has TWO
        # possible culprits. Historically this blamed the LOGIN store. But the
        # recurring clobber makes the CANONICAL .claude.json the wrong side while
        # the login store is right — so only blame the login store here when the
        # canonical store is anchor-CONSISTENT. When canonical is itself poisoned,
        # the dedicated Condition 10.5 below reports it (naming the canonical store,
        # not the correct login) and we skip the misleading login-blame message.
        if _identities_match(login_store_identity(acct, slot), account_canonical_identity(acct)) is False:
            if not account_canonical_store_poisoned(acct)[0]:
                mismatched.append(f"{slot}->{acct}")
        exp_state, _ = login_expiry_state(acct, slot, config)
        if exp_state == "expired":
            expired.append(f"{slot}->{acct}")
        elif exp_state == "near":
            expiring.append(f"{slot}->{acct}")
    if mismatched:
        out.append(SOSCondition(
            severity="urgent",
            summary=f"Independent login(s) stored under the wrong account: {', '.join(mismatched)}",
            action=("A stored login's identity does not match its account. Re-provision it: "
                    "`cus login-mount <slot> <account>` and log in as the correct account."),
            affected="system",
        ))

    # Condition 10.5 (2026-07-06, this PR): CANONICAL store poisoned by a wrong-
    # account clobber. This is the OTHER side of the identity-mismatch coin from
    # Condition 10: here account-<X>/.claude.json disagrees with X's TRUSTED anchor
    # (meta.yaml + login families), i.e. the canonical store was overwritten with
    # another account's identity while meta + families stayed correct (the live
    # 2026-07-06 rayi1<-rayi2 incident). Naming the CANONICAL store as the bad side
    # — not the (correct) login families — is the whole point of splitting this out.
    # The guard has already refused further wrong writes and the picker excludes the
    # account as a swap target; the operator fix is a browser re-login.
    for acct_name in state.get("accounts", {}):
        poisoned, detail = account_canonical_store_poisoned(acct_name)
        if not poisoned:
            continue
        out.append(SOSCondition(
            severity="urgent",
            summary=(f"'{acct_name}' CANONICAL store is clobbered with a wrong account's identity: "
                     f"{detail}"),
            action=(f"account-{acct_name}/.claude.json (and likely .credentials.json) hold ANOTHER "
                    f"account's identity/token, while '{acct_name}'s meta.yaml + login families are "
                    f"correct — so the CANONICAL store is the bad side, not the families. cus has "
                    f"refused further wrong-account writes and excluded '{acct_name}' as a swap target. "
                    f"Repair with a browser re-login: `cus relogin {acct_name}` then `cus poll`."),
            affected=acct_name,
        ))

    # Condition 10.6 (2026-07-07 merkos incident, this PR): DEAD SNAPSHOT with no
    # valid login family. The account's canonical snapshot refresh token is dead
    # (invalid_grant — flagged `snapshot_refresh_dead` by the un-stale sweep) AND it
    # has no free login family to seed a NEW lane from. Such an account can still
    # serve an EXISTING lane that already runs a live family (that lane is never
    # disturbed) — but it can no longer HOST a new lane: the swap install-point
    # refuses it (would blank the mount) and the picker excludes it. Surface it so
    # the operator reloginss before the account silently drops out of rotation. An
    # account that is dead-snapshot but HAS a free family is deliberately NOT alarmed
    # here — the install-point seeds new lanes from the family, so it's still usable.
    for acct_name, acct in state.get("accounts", {}).items():
        if not isinstance(acct, dict):
            continue
        if acct.get("snapshot_refresh_dead") and not has_free_login_family(acct_name, state):
            out.append(SOSCondition(
                severity="urgent",
                summary=(f"'{acct_name}' snapshot creds are dead and no valid login family remains — "
                         f"relogin required before it can host new lanes"),
                action=(f"account-{acct_name}/.credentials.json's refresh token is dead (the OAuth "
                        f"refresh grant returns invalid_grant) and '{acct_name}' has no free login "
                        f"family to seed a lane from, so cus has excluded it as a swap target and will "
                        f"refuse to install it onto a new lane — installing the dead snapshot would "
                        f"blank the mount (the 2026-07-07 merkos dead-snapshot incident, which blanked "
                        f"chats1a). Any existing lane already running a live family keeps working. "
                        f"Restore it with a browser re-login:\n"
                        f"      cus relogin {acct_name}   # then: cus poll\n"
                        f"    or add another independent login family:\n"
                        f"      cus login-mount {acct_name}"),
                affected=acct_name,
            ))
    if expired:
        out.append(SOSCondition(
            severity="warning",
            summary=f"Independent login(s) past assumed refresh-token lifetime: {', '.join(expired)}",
            action=("Re-provision so swaps don't fall back to the shared-snapshot copy: "
                    "`cus login-mount <slot> <account>` then `--finish`. "
                    "(Lifetime is an assumption — see #109 Phase 0.)"),
            affected="system",
        ))
    if expiring:
        out.append(SOSCondition(
            severity="warning",
            summary=f"Independent login(s) expiring soon (assumed): {', '.join(expiring)}",
            action="Re-provision before they lapse: `cus login-mount <slot> <account>` then `--finish`.",
            affected="system",
        ))
    # The precise clobber invariant: no two store entries may share ONE refresh
    # family. Violated by bootstrapping (`--from-existing`) an account onto two
    # slots, or hand-copying an entry. These two mounts WILL clobber on rotation
    # — exactly what independent logins exist to prevent. Critical regardless of
    # the gate, because the operator has provisioned them expecting safety.
    for dup in duplicate_login_families():
        out.append(SOSCondition(
            severity="urgent",
            summary=f"Independent-login families collide across mounts: {', '.join(dup['pairs'])} share one token family",
            action=("Two live mounts on one refresh-token family clobber on rotation. Give the extra "
                    "mount(s) their OWN login: `cus login-mount <slot> <account>` (a real /login, "
                    "NOT --from-existing — only the browser flow mints a distinct family)."),
            affected="system",
        ))
    # Same invariant measured at the LIVE mounts (2026-07-03). The store check
    # above only sees provisioned independent logins; a double-book created by
    # `--force` or the daemon's legacy fan-out double-up has NO store entry and
    # was silent (slot-3/slot-4 incident: sos said all-clear while two live
    # mounts shared rayi2's family — one refresh from a forced logout).
    for dup in duplicate_live_mount_families(state):
        if len(dup.get("accounts", [])) <= 1:
            continue  # single-account double-book: the by-account check below reports it with phase detail
        # One family live on 2+ mounts — the clobber precondition, naming BOTH
        # mounts + their accounts + the shared family, logged the moment it exists.
        _cred_audit("divergence-detected", "detected",
                    "one login family live on multiple mounts (#104)",
                    shared=True, token_fp=dup["refresh_fp"],
                    extra=("mounts=" + ",".join(dup["mounts"]).replace(" ", "")
                           + " accounts=" + ",".join(dup["accounts"])))
        out.append(SOSCondition(
            severity="urgent",
            summary=f"One login family live on {len(dup['mounts'])} mounts: {', '.join(dup['mounts'])}",
            action=("These mounts share ONE refresh token — the next rotation logs the others out. "
                    "Exit the extra session(s); relaunch joins the surviving lane (lane sharing) "
                    "or gets its own family via `cus login-mount <slot> <account>`."),
            affected="system",
        ))
    # By-account view of the same invariant (2026-07-03): catches the
    # POST-rotation phase too — once a doubled-up account's token rotates on
    # one mount, the families diverge (so the same-family check above goes
    # quiet) but the other mount now holds a dead refresh token and fails at
    # its next refresh (2026-07-01 duplicate-identity incident shape).
    for db in double_booked_live_accounts(state):
        if db["phase"] == "will_clobber":
            detail = "families still identical — the next token refresh logs the other(s) out"
        else:
            detail = ("families already DIVERGED — the mount(s) on the old family hold a dead "
                      "refresh token and will fail their next refresh")
        out.append(SOSCondition(
            severity="urgent",
            summary=f"'{db['account']}' is live on {len(db['mounts'])} mounts without independent logins "
                    f"({', '.join(db['mounts'])}): {detail}",
            action=("Exit the extra session(s) — relaunching then JOINS the surviving lane (lane sharing) "
                    "instead of copying. For deliberate multi-lane use of one account, provision "
                    "independent logins: `cus login-mount <slot> <account>`."),
            affected=db["account"],
        ))

    # Condition 11 (2026-07-03, pool model): the gate is ON but some accounts
    # backing live lanes have NO login pool, so a lane can't rescue onto them (or
    # safely double-book them) — a swap onto such an account while it's held just
    # HOLDS/refuses. Not a clobber (the pre-pool "copy-fallback" framing is gone:
    # execution claims a pooled family or refuses), so this is an informational
    # nudge to widen coverage, not an alarm. Only raised when the gate is on.
    if independent_logins_enabled(config):
        missing_accts = sorted({a for _, a in _slots_missing_logins(state)})
        if missing_accts:
            out.append(SOSCondition(
                severity="warning",
                summary=f"independent_logins is ON but these accounts backing live lanes have no login pool: "
                        f"{', '.join(missing_accts)}",
                action=("A lane can only rescue onto (or safely double-book) an account that has a login pool. "
                        "Provision one per account you want borrowable: `cus login-mount <account>` "
                        "(run pool_size times, then `--finish` each). Accounts without a pool still work as "
                        "today — they just can't be a rescue target."),
                affected="system",
            ))

    # ---- Operator-disabled accounts: silence their per-account klaxon -------
    # An account the operator deliberately took out of rotation (`cus disable`)
    # must not scream. The motivating case is a POISONED canonical store (rayi1,
    # 2026-07-06): before it's disabled it emits an URGENT "canonical store is
    # clobbered" line every poll; disabling it means the operator has SEEN that
    # and chosen to park it until a browser relogin. So for any disabled account
    # that has an outstanding URGENT/WARNING per-account condition (token
    # expired, poisoned canonical, ...), drop ALL of that account's conditions
    # and emit ONE soft INFO line instead. Non-loud disabled accounts (only INFO
    # or nothing) are left untouched — no new noise. SYSTEM-scoped conditions
    # (affected="system"/"daemon": all-accounts-blocked, premium-headroom
    # scarcity, ...) are never account-matched here, so a REAL capacity problem
    # that disabling this account causes still fires at full severity.
    disabled = _disabled_accounts(config)
    if disabled:
        loud = {c.affected for c in out
                if c.affected in disabled and c.severity in ("urgent", "warning")}
        if loud:
            cfg_accounts = {a.get("name"): a for a in config.get("accounts", [])
                            if isinstance(a, dict)}
            kept = [c for c in out if c.affected not in loud]
            for name in sorted(loud):
                reason = (cfg_accounts.get(name, {}) or {}).get("disabled_reason")
                rtxt = f", reason: {reason}" if reason else ""
                kept.append(SOSCondition(
                    severity="info",
                    summary=f"{name} is disabled and out of rotation{rtxt}",
                    action=(f"'{name}' was deliberately taken out of rotation with `cus disable` "
                            f"(its own alarms are suppressed while disabled). Re-enable with "
                            f"`cus enable {name}` after fixing (e.g. `cus relogin {name}` for a "
                            f"poisoned/expired store, then `cus force-poll {name}` to confirm)."),
                    affected=name,
                ))
            out = kept

    return out


def maybe_write_sos(conditions: list[SOSCondition], state: dict) -> None:
    """Write or remove SOS.md and (optionally) fire desktop notification.

    Idempotent: writes the same content given the same conditions. To avoid
    notify-send spam, we hash the conditions and only fire if it changed
    since the last notification.
    """
    if not conditions:
        if SOS_MD.exists():
            SOS_MD.unlink()
        return

    SOS_MD.parent.mkdir(parents=True, exist_ok=True)
    lines = [f"# SOS — claude-usage-swap needs you", f"", f"_Updated {now_iso()}_", f""]
    for c in conditions:
        lines.append(f"## [{c.severity.upper()}] {c.summary}")
        lines.append("")
        lines.append(c.action)
        lines.append("")
    atomic_write_bytes(SOS_MD, ("\n".join(lines) + "\n").encode())

    # Desktop notification — only when SOS hash changes (avoid spam every poll)
    signature = "|".join(f"{c.severity}:{c.summary}" for c in conditions)
    prev_sig = ""
    if LAST_NOTIFY.exists():
        try:
            prev_sig = read_json(LAST_NOTIFY).get("signature", "")
        except (json.JSONDecodeError, OSError):
            pass
    if signature != prev_sig:
        write_json(LAST_NOTIFY, {"signature": signature, "ts": now_iso()})
        if shutil.which("notify-send"):
            urgent = [c for c in conditions if c.severity == "urgent"]
            warning = [c for c in conditions if c.severity == "warning"]
            # Three-tier title/urgency: an info-only set (e.g. the premium-headroom
            # "targets are idle-stale but will refresh on swap" note) must not fire a
            # warning-styled critical-ish popup — keep it low-urgency and labelled info.
            if urgent:
                title, urgency = "🚨 cus SOS", "critical"
            elif warning:
                title, urgency = "⚠ cus warning", "normal"
            else:
                title, urgency = "ℹ cus", "low"
            body = conditions[0].summary
            if len(conditions) > 1:
                body += f" (and {len(conditions) - 1} more)"
            try:
                subprocess.run(
                    ["notify-send", "-u", urgency, "-a", "cus", title, body],
                    check=False, capture_output=True, timeout=5,
                )
            except (subprocess.SubprocessError, FileNotFoundError):
                pass


def maybe_reset_thresholds(state: dict, config: dict) -> None:
    """Reset next_swap_at_pct to config's first step under these conditions:
      1. 5h window appears to have reset: current_5h_pct < first_step. Since
         the ladder bumps up each time we swap AWAY (we always wait until 5h
         crosses some ladder step before swapping), a 5h reading below the
         FIRST step means the 5h window must have reset since we last swapped
         — start the ladder over so we can use gentle Tier 1 again.
      2. Both windows below `reset_below_pct` (legacy safety net for cases
         where 5h didn't reset but usage just decayed under both windows).
      3. The current value isn't in the configured ladder (config was edited
         since last state save) — migrate to the nearest matching step.

    Bug fixed 2026-05-20: previously condition 1 required BOTH windows to
    drop below `reset_below_pct`. When 5h reset but 7d (which decays much
    more slowly) stayed above 50%, the ladder got pinned at 100 (the
    sentinel for "any usage trips") and the daemon swapped accounts every
    cycle. The new "5h < first_step" rule reliably detects 5h window resets
    regardless of 7d state.
    """
    cfg_steps = config.get("thresholds", {}).get("steps") or [50, 75, 90]
    first_step = cfg_steps[0]
    ladder = list(cfg_steps) + [100]
    reset_below = config.get("thresholds", {}).get("reset_below_pct", 50)
    for name, acct in state["accounts"].items():
        cur = acct.get("next_swap_at_pct", first_step)
        cur_5h = acct.get("current_5h_pct", 0)
        cur_7d = acct.get("current_7d_pct", 0)
        # Condition 1 (GH #14, replaces overly-eager "5h < first_step" rule
        # from GH #12): only reset when the 5h window GENUINELY rolled over,
        # detected by comparing Anthropic's resets_at boundary against the
        # previously-stored value. The earlier rule fired any time 5h was low,
        # which broke for freshly-swapped-TO accounts: those have low 5h
        # naturally, so every swap reset the destination's ladder to first_step
        # and 7d immediately re-tripped it → infinite loop.
        cur_resets_at = acct.get("five_hour_resets_at")
        prev_resets_at = acct.get("prev_five_hour_resets_at")
        if cur_resets_at and prev_resets_at and cur_resets_at != prev_resets_at:
            if cur > first_step:
                acct["next_swap_at_pct"] = first_step
                acct["prev_five_hour_resets_at"] = cur_resets_at
                continue
        # Always track current resets_at so we can detect the next rollover.
        if cur_resets_at:
            acct["prev_five_hour_resets_at"] = cur_resets_at
        # Condition 2: legacy AND-both-below safety net.
        if cur_5h < reset_below and cur_7d < reset_below:
            if cur > first_step:
                acct["next_swap_at_pct"] = first_step
                continue
        # Condition 3: stale-config-value migration.
        if cur not in ladder:
            acct["next_swap_at_pct"] = next((s for s in ladder if s >= cur), first_step)


# --------------------------------------------------------------------------
# Live session tracking (Phase 3+)
# --------------------------------------------------------------------------

@dataclass
class LiveSession:
    """One Claude Code session believed to be alive based on hook log tails."""
    session_id: str | None  # None means "no resumable session-id found; relaunch fresh"
    account: str
    pane: str           # tmux pane id like %12, or "no-tmux"
    cwd: str
    started_at: str
    last_stop_at: str | None     # latest Stop hook entry; None if never stopped
    transcript_path: Path | None  # ~/.claude/projects/<cwd>/<id>.jsonl


def _parse_sessions_log() -> list[dict]:
    """Read sessions.log as a list of dicts. Order: oldest first."""
    if not SESSIONS_LOG.exists():
        return []
    entries: list[dict] = []
    with SESSIONS_LOG.open() as f:
        for line in f:
            parts = line.strip().split(",", 4)
            if len(parts) < 5:
                continue
            entries.append({"ts": parts[0], "session_id": parts[1], "account": parts[2], "pane": parts[3], "cwd": parts[4]})
    return entries


def _latest_stops_per_session() -> dict[str, str]:
    """Read stops.log and return {session_id: latest_stop_ts}."""
    if not STOPS_LOG.exists():
        return {}
    result: dict[str, str] = {}
    with STOPS_LOG.open() as f:
        for line in f:
            parts = line.strip().split(",", 2)
            if len(parts) < 2:
                continue
            ts, session_id = parts[0], parts[1]
            result[session_id] = ts  # later entries overwrite, so result is latest
    return result


# Per-scan cache of the transcript index, keyed by CLAUDE_DIR so tests with
# separate temp trees never share entries. GH #91 follow-up (review finding
# 2026-07-02): find_live_sessions can run more than once per daemon cycle
# (lazy_warm + migrating_panes), and each rebuild walks the whole projects
# tree; a short TTL lets the repeated calls in one cycle share a single scan.
_TRANSCRIPT_INDEX_CACHE: dict[str, tuple[float, dict]] = {}


class _TranscriptIndexData(dict):
    """Transcript index: the GH #91 by-stem dict[str, Path] (stem -> path,
    last-seen-wins) PLUS an `.exact` set of (project_dirname, stem) pairs
    captured in the SAME scan (GH #90/#95).

    Why a dict subclass: every existing caller and test relies on the plain
    by-stem mapping contract (`idx[stem]`, `idx.get(stem)`, `set(idx)`,
    `idx == {}`), so the fallback-lookup shape must not change. Subclassing
    keeps all of that intact while piggybacking the extra set from the same
    directory walk at no additional syscall cost.

    Why `.exact` exists: after GH #91 removed the per-session glob, the
    RESIDUAL scaling term in `cus status` (GH #90/#95) was the cwd-first
    candidate probe in _resolve_transcript — one live `.exists()` stat per
    UNIQUE session id ever logged. sessions.log is append-only, so status
    cost grew with total HISTORY (~13.5k unique ids -> ~13.5k stats ->
    multi-second, 100%-CPU status on the 2026-07-02 incident box) instead of
    with the transcripts actually on disk. The scan already visits every
    existing transcript, so "does <cwd-encoded-dir>/<sid>.jsonl exist?" can
    be answered by set membership at zero syscalls — see the
    `live_candidate_stat=False` path in _resolve_transcript.
    """

    def __init__(self) -> None:
        super().__init__()
        self.exact: set[tuple[str, str]] = set()


def _transcript_index(max_age_seconds: float = 5.0) -> dict[str, Path]:
    """One-shot {session_id: transcript_path} index of ~/.claude/projects.

    GH #91: find_live_sessions used to call _find_transcript() per session,
    whose fallback path globs EVERY project directory when the direct
    cwd-encoded candidate is missing. With a sessions.log accumulated over
    weeks (~13k unique ids, ~10.6k of them direct-path misses) against a
    ~300-dir / ~11k-file projects tree, that multiplied into ~3M stat calls
    — measured at 7.6 MINUTES on the 2026-07-02 incident box, which stalled
    the daemon loop mid-swap exactly when observability mattered most.

    A single glob("*/*.jsonl") walk visits each project dir once (~11k
    entries, sub-second) and makes every per-session lookup O(1). This index
    is used only as the FALLBACK for a session whose deterministic
    cwd-encoded path is missing — find_live_sessions still resolves the
    cwd-encoded candidate first (see there), so a duplicate session id in
    another project dir cannot shadow the transcript in the session's own
    cwd (review finding 2026-07-02). Among genuine duplicates with no cwd
    match, last-seen wins — close enough to the old fallback's arbitrary
    "first glob hit" for liveness inference.

    Cached for `max_age_seconds` (keyed by CLAUDE_DIR) so repeated
    find_live_sessions calls within one daemon cycle share the scan.
    """
    key = str(CLAUDE_DIR)
    now = time.monotonic()
    hit = _TRANSCRIPT_INDEX_CACHE.get(key)
    if hit and now - hit[0] < max_age_seconds:
        return hit[1]
    projects_root = CLAUDE_DIR / "projects"
    index = _TranscriptIndexData()
    if projects_root.exists():
        for jsonl in projects_root.glob("*/*.jsonl"):
            index[jsonl.stem] = jsonl
            # GH #90/#95: record the (project_dirname, stem) pair so bulk
            # scans can answer the cwd-first existence question by set
            # membership instead of a per-session-id stat. Same walk, no
            # extra syscalls — glob already enumerated this entry.
            index.exact.add((jsonl.parent.name, jsonl.stem))
    _TRANSCRIPT_INDEX_CACHE[key] = (now, index)
    return index


def _resolve_transcript(
    session_id: str,
    cwd: str,
    index: dict[str, Path],
    *,
    live_candidate_stat: bool = True,
) -> Path | None:
    """Deterministic cwd-first transcript resolution with an O(1) index
    fallback (GH #91 + review finding 2026-07-02).

    The session's own cwd-encoded path is tried FIRST — that is where Claude
    Code actually writes this session's transcript, so it is never shadowed
    by a same-id duplicate from another project dir. Only when that direct
    candidate is absent do we fall back to the prebuilt index (O(1)), never
    to the minutes-long full-tree glob that _find_transcript's fallback runs.

    `live_candidate_stat` (GH #90/#95) selects HOW the cwd-first candidate's
    existence is checked:

      True (default) — a live Path.exists() stat, exactly the pre-#90 code
        path. Kept as the default for the swap/orchestration callers
        (find_live_panes' _validate + step-1.5 loop): those probe a BOUNDED
        set (live panes x recent entries), so per-call stats are cheap, and
        freshness matters there — a transcript written milliseconds ago
        (the GH #23 SessionStart->first-JSONL race) must be seen the moment
        it lands, because a swap decision hangs on it.

      False — membership in the index's `.exact` set captured by the same
        scan that built the by-stem fallback map. Used by the BULK scan in
        find_live_sessions, where the candidate probe runs once per unique
        session id ever logged and was therefore the history-proportional
        cost behind the GH #90/#95 status hang. Within one call the index
        is built milliseconds earlier, so results are identical to the live
        stat modulo the same in-flight-file races the exists()->stat()
        sequence already had; across cached calls (daemon cycles sharing
        the 5s-TTL scan) a transcript CREATED inside the TTL window is
        seen up to 5s late — the same staleness bound the GH #91 review
        already accepted for the fallback map, and irrelevant to the
        one-shot `cus status` process where the cache is always cold.

    A plain-dict index (defensive: tests or callers holding a pre-#90-shape
    map) has no `.exact` set, so it always gets the live stat regardless of
    the flag — degraded to the old cost, never to wrong results.
    """
    if cwd:
        dirname = cwd.replace("/", "-")
        if live_candidate_stat or not isinstance(index, _TranscriptIndexData):
            candidate = CLAUDE_DIR / "projects" / dirname / f"{session_id}.jsonl"
            if candidate.exists():
                return candidate
        elif (dirname, session_id) in index.exact:
            # Construct the Path only AFTER the membership hit: on the bulk
            # path this function runs once per unique session id ever logged
            # and almost all of them miss (~10.6k of 13.5k on the incident
            # box), so building a 4-segment pathlib object per miss was
            # itself a measurable chunk of the scan (~µs each x tens of
            # thousands). A set probe on an interned tuple is ~100x cheaper.
            return CLAUDE_DIR / "projects" / dirname / f"{session_id}.jsonl"
    return index.get(session_id)


def find_live_sessions(account_filter: str | None = None) -> list[LiveSession]:
    """Best-effort enumeration of live Claude Code sessions on this machine.

    "Live" = appears in sessions.log AND either (a) has recent activity in
    its transcript JSONL, OR (b) has had a Stop signal recently.

    Caveat: there's no SessionEnd hook in Claude Code, so we infer liveness
    from staleness. A long-idle session may be wrongly flagged dead. This
    is fine for hot-swap: if a session is truly idle for 10+ minutes, swapping
    it is no different from a new session inheriting the new credentials.
    """
    entries = _parse_sessions_log()
    latest_stops = _latest_stops_per_session()
    transcripts = _transcript_index()  # GH #91: one scan, O(1) per-session lookups
    now = datetime.now(timezone.utc)

    # Deduplicate by session_id (keep latest entry — newest registration wins
    # in case of restarts that re-fire SessionStart with same id, though that
    # shouldn't happen with --resume which preserves the id).
    by_id: dict[str, dict] = {}
    for e in entries:
        by_id[e["session_id"]] = e

    live: list[LiveSession] = []
    for sid, e in by_id.items():
        if account_filter and e["account"] != account_filter:
            continue
        # Liveness: was there a Stop in the last hour? Or, fallback, is the
        # JSONL transcript recent? (Don't require both — either signal counts.)
        last_stop_at = latest_stops.get(sid)
        is_live = False
        if last_stop_at:
            try:
                last_stop_dt = datetime.fromisoformat(last_stop_at.replace("Z", "+00:00"))
                if (now - last_stop_dt).total_seconds() < 3600:
                    is_live = True
            except ValueError:
                pass

        # GH #91: deterministic cwd-first resolution + O(1) index fallback,
        # never the minutes-long glob that _find_transcript's fallback runs
        # (see _resolve_transcript / _transcript_index for the incident
        # numbers and the duplicate-id correctness note).
        #
        # GH #90/#95: live_candidate_stat=False — this loop runs once per
        # UNIQUE session id ever logged (append-only sessions.log, ~13.5k on
        # the incident box), so a live stat here made status cost grow with
        # history. Resolution now rides entirely on the index built at the
        # top of this call: zero syscalls per historical id.
        transcript = _resolve_transcript(
            sid, e.get("cwd", ""), transcripts, live_candidate_stat=False
        )
        if transcript:
            # Single guarded stat replaces the old exists()+stat() pair:
            # halves the syscalls for resolved ids (bounded by transcripts
            # on disk, not by history) AND closes the latent TOCTOU crash
            # where the file vanished between exists() and stat() — the old
            # sequence would have raised an unhandled OSError there. A
            # vanished file now just contributes no liveness signal, which
            # is exactly what exists()==False meant before.
            try:
                mtime = datetime.fromtimestamp(transcript.stat().st_mtime, tz=timezone.utc)
                if (now - mtime).total_seconds() < 3600:
                    is_live = True
            except OSError:
                pass

        if is_live:
            live.append(LiveSession(
                session_id=sid,
                account=e["account"],
                pane=e["pane"],
                cwd=e["cwd"],
                started_at=e["ts"],
                last_stop_at=last_stop_at,
                transcript_path=transcript,
            ))
    return live


def _find_transcript(session_id: str, cwd: str) -> Path | None:
    """Locate the JSONL transcript for a session. Returns None if not found.

    Claude Code stores transcripts at ~/.claude/projects/<cwd-encoded>/<session-id>.jsonl
    where cwd-encoded is the cwd path with `/` replaced by `-`.
    """
    projects_root = CLAUDE_DIR / "projects"
    if not projects_root.exists():
        return None
    if cwd:
        encoded = cwd.replace("/", "-")
        candidate = projects_root / encoded / f"{session_id}.jsonl"
        if candidate.exists():
            return candidate
    # Fallback: linear scan
    for jsonl in projects_root.glob(f"*/{session_id}.jsonl"):
        return jsonl
    return None


def cache_warm(transcript: Path, window_seconds: int) -> bool:
    """Return True if the transcript's last entry is within the cache window.

    Lifted concept from cux: Anthropic's prompt cache TTL is 5 minutes. If
    the last message was <5min ago, swapping now incurs cache-rebuild cost
    on the new account (the new account hasn't seen the conversation). If
    >5min ago, cache is gone anyway — swap is "free" cost-wise.
    """
    if not transcript or not transcript.exists():
        return False
    try:
        # Find last non-empty line (cheap; JSONL is line-delimited)
        with transcript.open("rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            # Walk back from end finding last newline of a non-empty entry
            chunk = b""
            offset = size
            while offset > 0 and b"\n" not in chunk[:-1]:
                read = min(4096, offset)
                offset -= read
                f.seek(offset)
                chunk = f.read(read) + chunk
            last_line = chunk.strip().split(b"\n")[-1]
        if not last_line:
            return False
        entry = json.loads(last_line)
        ts_str = entry.get("timestamp") or entry.get("ts")
        if not ts_str:
            # Fallback to file mtime
            ts = datetime.fromtimestamp(transcript.stat().st_mtime, tz=timezone.utc)
        else:
            ts = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
        return (datetime.now(timezone.utc) - ts).total_seconds() < window_seconds
    except (OSError, json.JSONDecodeError, ValueError):
        # On any parse failure, assume cache is cold (safer — don't block swap)
        return False


# --------------------------------------------------------------------------
# Tmux integration (Phase 3+)
# --------------------------------------------------------------------------

def tmux_is_available() -> bool:
    """Check if `tmux` binary is callable."""
    return shutil.which("tmux") is not None


def tmux_pane_exists(pane: str) -> bool:
    """Verify the given tmux pane id still exists."""
    if not pane or pane == "no-tmux" or not tmux_is_available():
        return False
    try:
        result = subprocess.run(
            ["tmux", "list-panes", "-a", "-F", "#{pane_id}"],
            capture_output=True, text=True, timeout=5,
        )
        return pane in result.stdout.split()
    except (subprocess.SubprocessError, FileNotFoundError):
        return False


def tmux_send_text(pane: str, text: str, then_enter: bool = True, delay_seconds: float = 0.3) -> bool:
    """Send `text` (optionally followed by Enter) to a tmux pane.

    Pattern documented in tmux-Claude integration guides: use -l (literal)
    to avoid escape interpretation, then send C-m separately with a small
    delay. Returns True on success.
    """
    if not tmux_is_available() or not pane or pane == "no-tmux":
        return False
    try:
        subprocess.run(["tmux", "send-keys", "-t", pane, "-l", text], check=True, timeout=5)
        if then_enter:
            time.sleep(delay_seconds)
            subprocess.run(["tmux", "send-keys", "-t", pane, "C-m"], check=True, timeout=5)
        return True
    except (subprocess.SubprocessError, FileNotFoundError):
        return False


def tmux_send_keys(pane: str, *keys: str) -> bool:
    """Send raw key sequences (e.g. 'Escape', 'C-c') to a tmux pane."""
    if not tmux_is_available() or not pane or pane == "no-tmux":
        return False
    try:
        subprocess.run(["tmux", "send-keys", "-t", pane, *keys], check=True, timeout=5)
        return True
    except (subprocess.SubprocessError, FileNotFoundError):
        return False


def _pane_pid(pane: str) -> int | None:
    """Return the shell PID running inside the given tmux pane, or None.

    Uses `tmux display-message #{pane_pid}`. That's the PID of the shell tmux
    spawned for the pane; the live claude/node process is a descendant.
    """
    if not tmux_is_available() or not pane or pane == "no-tmux":
        return None
    try:
        result = subprocess.run(
            ["tmux", "display-message", "-p", "-t", pane, "#{pane_pid}"],
            capture_output=True, text=True, timeout=5,
        )
        pid_str = result.stdout.strip()
        return int(pid_str) if pid_str else None
    except (subprocess.SubprocessError, FileNotFoundError, ValueError):
        return None


def _find_descendant_claude_pid(root_pid: int) -> int | None:
    """Walk /proc to find a descendant claude/node process under root_pid.

    Linux-only (uses /proc). Returns the deepest matching pid, since claude
    code typically runs as `node <claude-js>` and we want the leaf claude
    process. Returns None if /proc isn't available or no descendant matches.
    """
    proc_root = Path("/proc")
    if not proc_root.exists():
        return None
    children: dict[int, list[int]] = {}
    comm: dict[int, str] = {}
    for entry in proc_root.iterdir():
        if not entry.name.isdigit():
            continue
        pid = int(entry.name)
        try:
            stat_text = (entry / "stat").read_text()
            close_paren = stat_text.rfind(")")
            if close_paren == -1:
                continue
            comm_part = stat_text[: close_paren + 1]
            after = stat_text[close_paren + 2 :].split()
            ppid = int(after[1])
            children.setdefault(ppid, []).append(pid)
            name_start = comm_part.find("(")
            comm[pid] = comm_part[name_start + 1 : -1]
        except (OSError, ValueError, IndexError):
            continue
    found: list[int] = []
    stack = list(children.get(root_pid, []))
    while stack:
        pid = stack.pop()
        name = comm.get(pid, "")
        if name in ("claude", "node"):
            found.append(pid)
        stack.extend(children.get(pid, []))
    if not found:
        return None
    return max(found)


def _read_claude_session_id_from_cmdline(pid: int) -> str | None:
    """Parse /proc/<pid>/cmdline for the `--resume <id>` arg.

    The cmdline file is NUL-separated argv. When claude is launched with
    --resume <id>, this is the canonical id of the running session. Returns
    None if the file can't be read, claude wasn't launched with --resume
    (fresh session — there's no id we can recover from the live process),
    or the arg parsing fails.

    Important caveat: the earlier version of this function tried to read
    CLAUDE_CODE_SESSION_ID from /proc/<pid>/environ. That env var is NOT
    set in the claude process's own environ — claude only sets it when
    spawning hook subprocesses (validator, statusline, etc.) — so that
    lookup always returned None and silently fell back to sessions.log.
    cmdline is the only reliable signal for a running claude process.
    """
    try:
        raw = Path(f"/proc/{pid}/cmdline").read_bytes()
    except (OSError, PermissionError):
        return None
    args = [chunk.decode("utf-8", errors="replace") for chunk in raw.split(b"\x00") if chunk]
    for i, arg in enumerate(args):
        if arg == "--resume" and i + 1 < len(args):
            return args[i + 1]
        if arg.startswith("--resume="):
            return arg.split("=", 1)[1]
    return None


def pane_live_session_id(pane: str) -> str | None:
    """Authoritative current session-id for a tmux pane, read from the live
    claude process's cmdline (--resume arg). Returns None if claude was
    launched without --resume (fresh session) or any lookup step failed;
    callers should fall back to the sessions.log heuristic.

    See GH #10. The sessions.log heuristic (latest entry per pane) can pick
    a stale session-id when a pane has had multiple claude sessions over
    time and the SessionStart hook didn't fire for the live one. Parsing
    the cmdline gives the canonical answer for resumed sessions; for fresh
    sessions, the session-id isn't recoverable from the live process and
    we fall back to sessions.log.
    """
    pid = _pane_pid(pane)
    if pid is None:
        return None
    claude_pid = _find_descendant_claude_pid(pid)
    if claude_pid is None:
        return None
    return _read_claude_session_id_from_cmdline(claude_pid)


def tmux_pane_name(pane: str) -> str:
    """Return the tmux pane's command/process name, for whitelist matching."""
    if not tmux_is_available() or not pane:
        return ""
    try:
        result = subprocess.run(
            ["tmux", "display-message", "-p", "-t", pane, "#{pane_current_command}"],
            capture_output=True, text=True, timeout=5,
        )
        return result.stdout.strip()
    except (subprocess.SubprocessError, FileNotFoundError):
        return ""


def pane_is_claude(pane: str) -> bool:
    """True if a tmux pane is currently running a claude session (GH #37).

    Claude Code runs as `claude` or (more commonly) as a `node` process hosting
    the CLI, so its tmux `pane_current_command` is one of those. This is the
    canonical "is claude up in this pane" predicate — used at session discovery
    AND, critically, re-checked right before the hot-swap path injects
    keystrokes. Discovery and keystroke-injection are separated by the pre-swap
    verify-poll and minute-long per-pane Stop/pause waits, during which the user
    may have exited claude. Typing a pause-message or `/exit` into a pane that's
    back at a bare shell would run as SHELL input (e.g. `/exit` closes the
    shell). Guarding on this keeps cus from "doing anything" to a pane that is
    no longer a live claude session.
    """
    if not pane or pane == "no-tmux":
        return False
    return tmux_pane_name(pane) in ("claude", "node")


# --------------------------------------------------------------------------
# Hot-swap orchestrator (Phase 3)
# --------------------------------------------------------------------------

def session_is_pinned(session: LiveSession, config: dict) -> tuple[bool, str]:
    """Check if a session is locked — orchestrator will skip it during swaps.

    Semantics: any pane (or session-id) listed in `session_locks.pinned`
    is NEVER swapped by the orchestrator, regardless of current/target
    account. The dict-value (account name) records WHICH account the pane
    "should" be on, but the simple "if pinned, skip" rule applies always.

    Use case: protect an agent's own pane from being /exit'd by its own
    orchestration (the 2026-05-19 lesson — pane %2 was the agent that
    triggered the orchestrator, and the orchestrator kept /exit'ing it).
    """
    pinned = config.get("session_locks", {}).get("pinned", {}) or {}
    if session.pane in pinned:
        target = pinned[session.pane]
        return True, f"pinned to '{target}' (skip swap)"
    if session.session_id in pinned:
        target = pinned[session.session_id]
        return True, f"session-id pinned to '{target}' (skip swap)"
    return False, ""


def session_matches_whitelist(session: LiveSession, config: dict) -> tuple[bool, str]:
    """Check session.pane against never_restart_patterns regex list."""
    patterns = config.get("session_locks", {}).get("never_restart_patterns", []) or []
    if not patterns or not session.pane or session.pane == "no-tmux":
        return False, ""
    pane_cmd = tmux_pane_name(session.pane)
    for pat in patterns:
        try:
            if re.search(pat, pane_cmd):
                return True, f"whitelist: {pat} matches '{pane_cmd}'"
        except re.error:
            continue
    return False, ""


def subagent_active(session: LiveSession, config: dict, idle_seconds: int = 60) -> bool:
    """Estimate whether a session has an in-flight subagent or tool call.

    Heuristic: count tool_use.log entries for this session_id where the
    last entry is "start" (no matching "stop") within the recent window.

    Note: this function uses a deliberately SHORT 60s window because it
    feeds the swap-defer guard at the orchestrator (GH #27) — a tool stuck
    longer than 60s shouldn't be allowed to block swaps indefinitely. For
    the longer-lookback "session is doing active work" question used by
    pause-message routing (GH #33), see `session_has_recent_tool_activity`.
    """
    if not TOOL_USE_LOG.exists():
        return False
    in_flight = 0
    now = datetime.now(timezone.utc)
    cutoff = now.timestamp() - idle_seconds
    try:
        with TOOL_USE_LOG.open() as f:
            for line in f:
                parts = line.strip().split(",", 3)
                if len(parts) < 4:
                    continue
                ts_str, sid, _, phase = parts
                if sid != session.session_id:
                    continue
                try:
                    ts = datetime.fromisoformat(ts_str.replace("Z", "+00:00")).timestamp()
                except ValueError:
                    continue
                if ts < cutoff:
                    continue
                in_flight += 1 if phase == "start" else -1
    except OSError:
        return False
    return in_flight > 0


def session_has_recent_tool_activity(session_id: str, lookback_seconds: int = 300) -> bool:
    """Return True if the session had ANY tool_use.log entry in the last lookback window.

    Used by `session_is_idle` (GH #33) to catch sessions in the middle of an
    active multi-step task even when there's a multi-minute gap between the
    last tool's PostToolUse and the swap check moment. The JSONL-mtime check
    alone misses these because: (a) tool I/O between PreToolUse and PostToolUse
    doesn't always flush to the JSONL on its own clock, and (b) Claude's
    "thinking" text between tools may not update the file every second either.

    The 2026-05-26 incident: panes %14 and %18 were tool-active 3–4 minutes
    before the swap (one tool every ~30s before that gap) but happened to be
    in a between-tools lull at the exact 20:21:53 check moment. The 30s
    JSONL-mtime threshold marked them idle. cus skipped the pause-message,
    then `/exit` interrupted them mid-work.

    The user's mental model (queued-message-is-harmless): if the session is
    currently doing tools, the pause-message slips in via tmux send-keys and
    becomes the next thing claude sees after its current Stop. If the session
    is truly between turns (input prompt focused), the message is submitted
    as a billed user turn — that's the cost. GH #4 worried about this cost;
    GH #33 says the cost is worth it for the active-task case.

    Distinct from `subagent_active`:
      - `subagent_active` uses a 60s window because it powers the swap-defer
        guard (don't let a stuck tool block swaps indefinitely); it also
        tracks in-flight (start without matching stop) only.
      - This function takes a longer window (default 5min) and counts ANY
        entry — start OR stop, matched or not — as evidence the session is
        in active work. We don't care whether a specific tool is currently
        running, only whether the session is in an "is working" state.

    `lookback_seconds` defaults to 300 (5 min); operators tune via
    `hot_swap.activity_lookback_seconds` to make pause-message routing more
    or less aggressive.
    """
    if not TOOL_USE_LOG.exists() or not session_id:
        return False
    cutoff = time.time() - lookback_seconds
    try:
        with TOOL_USE_LOG.open() as f:
            for line in f:
                parts = line.strip().split(",", 3)
                if len(parts) < 4:
                    continue
                ts_str, sid, _, _ = parts
                if sid != session_id:
                    continue
                try:
                    ts = datetime.fromisoformat(ts_str.replace("Z", "+00:00")).timestamp()
                except ValueError:
                    continue
                if ts >= cutoff:
                    return True
    except OSError:
        return False
    return False


def wait_for_stop(session_id: str, timeout_seconds: int) -> bool:
    """Block until a fresh Stop signal is seen for session_id, or timeout."""
    deadline = time.monotonic() + timeout_seconds
    baseline = _latest_stops_per_session().get(session_id)
    while time.monotonic() < deadline:
        cur = _latest_stops_per_session().get(session_id)
        if cur and cur != baseline:
            return True
        time.sleep(2)
    return False


def session_is_idle(
    session: LiveSession,
    idle_seconds: int,
    activity_lookback_seconds: int = 300,
) -> bool:
    """Return True if the session is genuinely between turns (NOT actively working).

    Two-source check:
      (1) JSONL mtime — recent (< idle_seconds) write means Claude was just
          generating text or flushing tool I/O. Mid-turn.
      (2) tool_use.log entry in last `activity_lookback_seconds` — even with
          a stale JSONL mtime, a tool PreToolUse/PostToolUse in the recent
          past means the session is in an active multi-step task. The 30s
          JSONL window is too short to catch sessions doing tools at a slower
          cadence (one tool per few minutes, or long tool gaps with text
          generation in between).

    A session is idle ONLY if BOTH signals say so: stale JSONL mtime AND no
    recent tool activity. Either signal of life flips it back to mid-turn.

    GH #33 (2026-05-26): the user reported the pause-message landing in a
    "basically paused" session while skipping two "running" sessions. The
    "running" sessions had had tools every ~30s up until a multi-minute gap
    just before the swap — long enough for the 30s JSONL-mtime window to
    expire but still within the operator's intuitive "this session is
    actively doing things" window. The fix: extend the detection horizon to
    cover this gap by ORing in the tool_use.log lookback.

    Affects TWO call sites:
      - Pause-message routing (tier 2/3): the main reason for this change.
        More sessions classified mid-turn → more pause-messages sent. Cost:
        tier-2 pause-messages to "stale-but-not-truly-idle" sessions become
        billed user turns instead of queued-and-forgotten. The user accepted
        this cost: "a message queued will slip in and stop the AI."
      - Tier 1 cache-bust check (line ~2911): tier-1 swaps now defer more
        often on stale-but-active sessions. Probably also desired (don't
        burn the cache on a session that's been doing work) but a behavior
        change worth knowing.

    Returns False (= not idle) if we can't determine — defensive default,
    matches prior behavior for sessions without a transcript.
    """
    # Source (1): JSONL mtime.
    if session.transcript_path is None or not session.transcript_path.exists():
        return False
    try:
        mtime = datetime.fromtimestamp(session.transcript_path.stat().st_mtime, tz=timezone.utc)
        jsonl_stale = (datetime.now(timezone.utc) - mtime).total_seconds() > idle_seconds
    except OSError:
        return False
    if not jsonl_stale:
        return False  # JSONL was just written — definitely mid-turn

    # Source (2): tool_use.log recent activity. JSONL was stale, but the
    # session might still be in an active task (between tools with a gap).
    if session.session_id and session_has_recent_tool_activity(
        session.session_id, activity_lookback_seconds
    ):
        return False  # recent tool activity — treat as mid-turn

    return True


def _resolve_relaunch_session_id(
    session: LiveSession,
    max_lookback_seconds: int = 3600,
) -> tuple[str | None, Path | None]:
    """Re-validate session-id at relaunch time, walking back through sessions.log if needed.

    Called from the hot-swap orchestrator AFTER `/exit` + `wait_for_shell` —
    by that point the GH #22 race window (SessionStart fired but JSONL not yet
    written) has had 5–10 seconds to resolve. If the JSONL STILL doesn't
    exist, the session-id picked at decision time is a ghost: it had a
    SessionStart event but the session died before writing its first
    transcript line. `claude --resume <ghost-id>` will fail with "No
    conversation found."

    GH #32 (2026-05-26): pane %15 had two SessionStart events 5 seconds
    apart (490479c7 then fa0d9122). The first never wrote a JSONL. cus's
    `find_live_panes` step 2 (GH #23 fix) accepts sessions.log latest
    unconditionally to survive the race window — correct policy at decision
    time, wrong at relaunch time when the race has already resolved against
    us.

    Recovery strategy:
      1. If the chosen session_id now has a JSONL → use it as-is.
      2. Otherwise walk backwards through sessions.log entries for THIS
         pane, oldest-of-recent first, picking the most recent entry whose
         JSONL exists within `max_lookback_seconds`. Preserves the user's
         prior real session — strictly better than fresh launch when one
         is available, since they keep continuity.
      3. If no entry within the lookback has a JSONL, return (None, None).
         The caller (`build_relaunch_cmd`) renders a plain `claude` launch
         (no `--resume`) per the GH #20 directive: "we can just never do
         that I guess" (better fresh than failed-resume-leaves-dead-shell).

    Returns (session_id, transcript_path); either may be None.
    """
    # Step 1: revalidate the originally-picked session.
    if session.session_id:
        t = _find_transcript(session.session_id, session.cwd)
        if t and t.exists():
            return session.session_id, t

    # Step 2: walk back through sessions.log for this pane.
    entries = _parse_sessions_log()
    pane_entries = [e for e in entries if e.get("pane") == session.pane]
    pane_entries.reverse()  # latest first
    now = datetime.now(timezone.utc)
    for e in pane_entries:
        sid_candidate = e.get("session_id")
        if not sid_candidate:
            continue
        if sid_candidate == session.session_id:
            continue  # already failed above
        try:
            ts = datetime.fromisoformat(e["ts"].replace("Z", "+00:00"))
        except (ValueError, KeyError):
            continue
        if (now - ts).total_seconds() > max_lookback_seconds:
            break  # too far back; assume nothing useful past here
        cwd = e.get("cwd") or session.cwd
        t = _find_transcript(sid_candidate, cwd)
        if t and t.exists():
            return sid_candidate, t

    # Step 3: nothing valid found.
    return None, None


def find_live_panes(account_filter: str | None = None, verbose: bool = False) -> list[LiveSession]:
    """One LiveSession entry per tmux pane currently running claude.

    Differs from `find_live_sessions` (which dedups by session_id) — that
    function will return N entries for a pane that's had N historical
    claude invocations. For hot-swap, we want EXACTLY one entry per pane,
    using the pane's MOST RECENT session_id.

    The post-mortem on the 2026-05-19 hot-swap incident showed why this
    matters: orchestrator iterated find_live_sessions and typed N relaunch
    commands into the same pane with stale session-ids, none matching the
    one currently running. See docs/AUDIT.md item 11 and inbox.md.

    For each candidate pane:
      1. Read sessions.log entries, group by pane, take latest per pane.
      2. Account filter: drop panes whose latest session is on a different
         account than `account_filter`.
      3. Verify pane still exists in tmux (it may have been closed).
      4. Verify pane's current command is claude/node (i.e. claude is
         actually running there — not back to shell from a manual /exit).
      5. Optional liveness signal: recent Stop or recent transcript mtime.

    After all per-pane resolution, a cross-pane collision post-pass (GH #29)
    clears any session_id that ended up resolved for more than one pane —
    every step in the fallback chain can produce duplicates (cmdline gets
    "stuck" with the same `--resume <id>` for two panes after a prior bad
    swap; sessions.log can record the same id for two panes after a resume;
    newest-mtime was guarded by GH #22 but only for that one source). Fresh
    relaunch for both colliding panes is strictly safer than one stealing
    the other's chat.
    """
    entries = _parse_sessions_log()
    latest_stops = _latest_stops_per_session()
    now = datetime.now(timezone.utc)

    # Group by pane; latest entry overwrites (entries are oldest-first).
    # GH #90/#95: entries_by_pane keeps the FULL per-pane entry list (same
    # oldest-first order) for the step-1.5 cmdline-staleness sweep below.
    # That sweep used to re-scan ALL of `entries` once per live pane —
    # filtering on pane inside the loop and fromisoformat-parsing every
    # line's timestamp each time — an O(total_entries x live_panes) pass
    # over an append-only log (~14.6k lines on the incident box). Grouping
    # here is one O(total_entries) pass; the sweep then touches only its
    # own pane's entries. Identical subset, identical order, no semantic
    # change — pure loop restructure.
    by_pane: dict[str, dict] = {}
    entries_by_pane: dict[str, list[dict]] = {}
    for e in entries:
        pane = e.get("pane", "")
        if not pane or pane == "no-tmux":
            continue
        by_pane[pane] = e
        entries_by_pane.setdefault(pane, []).append(e)

    # GH #22: count how many panes share each cwd. The newest-mtime
    # fallback (step 3 below) is CWD-wide and can't disambiguate between
    # panes sharing a cwd — so we skip it entirely when 2+ panes share a
    # cwd. Better to relaunch fresh than to copy someone else's chat.
    cwd_pane_count: dict[str, int] = {}
    for _p, _e in by_pane.items():
        c = _e.get("cwd", "")
        if c:
            cwd_pane_count[c] = cwd_pane_count.get(c, 0) + 1

    # GH #91 follow-up (review finding 2026-07-02): build the transcript index
    # ONCE for the whole pane sweep and resolve every candidate through it
    # (cwd-first + O(1) index fallback), instead of _validate globbing the
    # projects tree per pane and the per-pane loop using a lossy cwd-only
    # lookup. Cached, so this shares the same scan as any find_live_sessions
    # call in the cycle.
    transcripts = _transcript_index()

    out: list[LiveSession] = []
    for pane, e in by_pane.items():
        if account_filter and e["account"] != account_filter:
            continue
        if not tmux_pane_exists(pane):
            continue
        if not pane_is_claude(pane):
            # Pane is back to shell (or running something else). No live claude.
            continue

        # Pick session-id with FALLBACK CHAIN (GH #21/#22, 2026-05-25):
        #   1. /proc cmdline --resume arg (GH #10) — strongest signal: it's
        #      pane-specific AND the canonical id the process was launched
        #      with. Used only if the JSONL exists.
        #   2. sessions.log latest for THIS PANE (SessionStart hook log) —
        #      pane-specific, validated to have an existing JSONL.
        #   3. newest-mtime .jsonl in cwd's project dir, mtime within 1h —
        #      LAST resort because it's CWD-WIDE, not pane-specific. When
        #      multiple panes share a cwd (very common: window splits in
        #      the same project), this picks the same JSONL for ALL of
        #      them — wrong for all but one. Only fall through to this if
        #      sessions.log entry is missing/invalid AND cmdline gave None.
        #   4. None → relaunch FRESH (no --resume) per user directive
        #
        # GH #22 (2026-05-25): user reported "the window for CKids1a opened
        # to the chat that was in the tmux window for gym3a." Both panes
        # (%11 and %12) had cwd /home/rayi/repos/vibeCoding; both fell through
        # to newest-mtime which returned the same id for both → both
        # relaunched into the same chat. Fix: promote sessions.log above
        # newest-mtime so pane-specific data wins.
        cwd = e.get("cwd", "")
        live_sid = pane_live_session_id(pane)

        def _validate(cand_sid: str) -> Path | None:
            # cwd-first + O(1) index fallback (no full-tree glob) — GH #91.
            t = _resolve_transcript(cand_sid, cwd, transcripts)
            return t if (t and t.exists()) else None

        sid = None
        transcript = None
        chosen_source = None

        # Step 1: cmdline (validated). Pane-specific. Validation is needed
        # here because the launch-time --resume id may point to a JSONL
        # that's since been deleted by compaction (GH #21, the 28eac355 case).
        if live_sid:
            t = _validate(live_sid)
            if t:
                sid, transcript, chosen_source = live_sid, t, "cmdline"

        # Step 1.5: cmdline-staleness override (GH #33-followup, 2026-05-26).
        #
        # /proc/<pid>/cmdline reflects the LAUNCH-TIME arguments. When the user
        # uses Claude Code's in-process `/resume` slash-command to switch
        # sessions WITHIN a running claude, the process doesn't fork — it just
        # switches which JSONL it writes to. cmdline keeps showing the
        # original --resume id forever, even after the user has moved to a
        # completely different session.
        #
        # The 2026-05-26 incident: pane %18 cmdline shows `--resume a7ad2f4d`
        # (launched ~hours ago), but the user /resume'd to c3c2cd5a inside
        # claude and has been writing 600+ tool entries to c3c2cd5a.jsonl
        # since. Step 1 happily picked a7ad2f4d (validation passed: that
        # JSONL exists, just hasn't been touched in 2+ hours). The swap then
        # relaunched into the wrong session, and idle-checks/pause-routing
        # consulted the wrong JSONL's mtime.
        #
        # Detection: among ALL sessions.log entries for THIS PANE (which
        # SessionStart fires for, including in-process /resume), find the
        # one whose JSONL has the MOST RECENT mtime. If that mtime is more
        # recent than cmdline's chosen JSONL's mtime, prefer it — the
        # actively-written-to JSONL is the canonical "what session is this
        # pane on" signal. cmdline is treated as a hint that may be stale.
        #
        # Pane-specificity: we filter `entries` by pane field, so we never
        # cross panes. No collision risk (the GH #29 post-pass remains as a
        # belt-and-suspenders check). Sessions.log records the pane each
        # SessionStart fired in, so cross-pane id-poisoning isn't possible
        # from this source.
        #
        # Discrimination strategy: among ALL sessions.log entries for this
        # pane within a generous SessionStart-age lookback (cap N for perf),
        # pick the one whose JSONL has the most recent mtime, provided that
        # mtime is itself within a tighter "actually-active" window. The two
        # windows are independent:
        #   - entry_age_lookback: caps how many sessions.log lines to scan
        #     and stat (perf concern only; 24h is generous — sessions.log
        #     grows to 3000+ lines on hot machines, and 24h × N panes keeps
        #     the scan under a few hundred ms)
        #   - active_mtime_window: the actual liveness filter — JSONL must
        #     have been written within this window to be considered live.
        #     Anything older is stale even if its SessionStart was recent.
        #
        # The 2026-05-26 incident proves entry_age can't be tight: c3c2cd5a's
        # SessionStart for pane %18 was at 18:32:18, the swap was at 20:21:53
        # — 1h49m later, OUTSIDE a 1h SessionStart-age filter. The session
        # had been continuously /resume'd-into for those 2 hours; the JSONL
        # mtime stayed current the whole time. Filtering on entry age would
        # have missed it; filtering on JSONL mtime correctly catches it.
        now_ts = now.timestamp()
        entry_age_lookback = 86400         # 24h — perf cap on stats
        active_mtime_window = 3600         # 1h — liveness filter on JSONL
        had_cmdline_pick = sid is not None
        best_mtime = transcript.stat().st_mtime if transcript and transcript.exists() else 0.0
        # The active_mtime_window check should also apply to the cmdline pick
        # — if cmdline's JSONL was last written >1h ago, treat it as
        # uncompetitive (best_mtime stays 0 and any newer-than-1h pe wins).
        if best_mtime > 0 and (now_ts - best_mtime) > active_mtime_window:
            best_mtime = 0.0
        # GH #90/#95: iterate only THIS pane's entries (pre-grouped above in
        # the same pass that built by_pane) instead of re-scanning the whole
        # log per pane. The old `if pe.get("pane") != pane: continue` filter
        # is exactly what the grouping applied, so subset and order are
        # unchanged — this is the O(n x panes) -> O(n) half of the fix.
        for pe in entries_by_pane.get(pane, ()):
            pe_ts_str = pe.get("ts")
            if not pe_ts_str:
                continue
            try:
                pe_ts = datetime.fromisoformat(pe_ts_str.replace("Z", "+00:00")).timestamp()
            except ValueError:
                continue
            if now_ts - pe_ts > entry_age_lookback:
                continue
            pe_sid = pe.get("session_id")
            if not pe_sid or pe_sid == sid:
                continue
            # cwd-first + O(1) index fallback (GH #91 review finding
            # 2026-07-02): the old code did an encoded-cwd-only lookup to
            # avoid _find_transcript's O(projects) glob — correct on the fast
            # path but lossy when a transcript moved cwd. The prebuilt index
            # gives the same fallback the glob provided, still O(1) per entry.
            pe_cwd = pe.get("cwd", cwd)
            pe_t = _resolve_transcript(pe_sid, pe_cwd, transcripts)
            if pe_t is None:
                continue
            try:
                pe_mtime = pe_t.stat().st_mtime
            except (OSError, FileNotFoundError):
                continue
            # Liveness: only consider entries whose JSONL is in the active
            # window. Stale-mtime entries can't be the "what is this pane
            # currently doing" session.
            if (now_ts - pe_mtime) > active_mtime_window:
                continue
            if pe_mtime > best_mtime:
                best_mtime = pe_mtime
                sid, transcript = pe_sid, pe_t
                # Distinguish "we overrode a stale cmdline" from "filled in
                # for missing cmdline" — the first is a regression-prone
                # case worth surfacing in logs; the second is routine.
                chosen_source = (
                    "active-jsonl-mtime (cmdline-stale)"
                    if had_cmdline_pick
                    else "active-jsonl-mtime"
                )

        # Step 2: sessions.log latest for THIS PANE. Pane-specific. NOT
        # validated against JSONL existence anymore (GH #23 fix): there's
        # a brief race between SessionStart firing and the first JSONL
        # line being written (~5s window). If we polled during that
        # window we'd reject the id and silently lose the session. The
        # 2026-05-25 incident: pane %13's session 927e2992 was
        # SessionStart-logged but its JSONL wasn't created until 5s after
        # the swap decision fired. We accept sessions.log unconditionally
        # here because if SessionStart fired, claude IS running that
        # session, and the JSONL will exist by relaunch time.
        if sid is None and e.get("session_id"):
            sid = e["session_id"]
            transcript = _validate(e["session_id"])  # may be None during race
            chosen_source = "sessions.log"

        # Step 3: newest-mtime .jsonl in cwd's project dir (validated by
        # mtime < 1h). CWD-WIDE, not pane-specific — only safe to use when
        # we have NO better signal AND only ONE live pane is in this cwd.
        # GH #22: if multiple panes share this cwd, the newest-mtime would
        # return the same id for ALL of them, causing them to relaunch into
        # the same chat. We skip the fallback in that case.
        if sid is None and cwd and cwd_pane_count.get(cwd, 0) <= 1:
            try:
                projects_root = Path.home() / ".claude" / "projects"
                encoded = cwd.replace("/", "-")
                proj_dir = projects_root / encoded
                if proj_dir.exists():
                    jsonls = sorted(
                        proj_dir.glob("*.jsonl"),
                        key=lambda p: p.stat().st_mtime,
                        reverse=True,
                    )
                    for j in jsonls:
                        mtime = datetime.fromtimestamp(j.stat().st_mtime, tz=timezone.utc)
                        if (now - mtime).total_seconds() < 3600:
                            sid, transcript, chosen_source = j.stem, j, "newest-jsonl-mtime"
                            break
            except OSError:
                pass

        # Step 4 (user directive 2026-05-25): no fallback to unvalidated.
        # If no candidate has a JSONL, sid stays None and the relaunch logic
        # below uses plain `claude` (no --resume). Better to start a fresh
        # session than to fail with "No conversation found" — user said:
        # "If theres no solution for that, we can just never do that I guess."
        if sid is None and verbose:
            click.echo(
                f"    pane {pane}: NO valid session-id found "
                f"(cmdline={live_sid[:8] if live_sid else 'None'}, sessions.log={e['session_id'][:8]}); "
                f"will relaunch WITHOUT --resume (fresh session)"
            )
        elif verbose and chosen_source != "cmdline":
            click.echo(
                f"    pane {pane}: session-id {sid[:8]} from {chosen_source} "
                f"(cmdline returned {live_sid[:8] if live_sid else 'None'})"
            )
        elif verbose and live_sid and live_sid != e["session_id"]:
            click.echo(
                f"    pane {pane}: live session-id {live_sid[:8]} differs "
                f"from sessions.log latest {e['session_id'][:8]}; using live"
            )
        last_stop_at = latest_stops.get(sid)

        # Liveness double-check — even though we confirmed pane_current_command
        # is claude/node, also require some recent (within 1h) signal of life.
        # This catches the case where claude is wedged or frozen.
        is_live = False
        if last_stop_at:
            try:
                last_stop_dt = datetime.fromisoformat(last_stop_at.replace("Z", "+00:00"))
                if (now - last_stop_dt).total_seconds() < 3600:
                    is_live = True
            except ValueError:
                pass
        if transcript and transcript.exists():
            mtime = datetime.fromtimestamp(transcript.stat().st_mtime, tz=timezone.utc)
            if (now - mtime).total_seconds() < 3600:
                is_live = True
        # If pane_current_command says claude but no recent activity, still
        # treat as live — better to attempt orchestration on a quiet pane
        # than to skip it and leave it stuck on the old account.
        if not is_live:
            is_live = True  # pane_cmd already confirmed claude is up

        out.append(LiveSession(
            session_id=sid,
            account=e["account"],
            pane=pane,
            cwd=e["cwd"],
            started_at=e["ts"],
            last_stop_at=last_stop_at,
            transcript_path=transcript,
        ))

    # Cross-pane collision detector (GH #29). The fallback chain above is
    # pane-specific at every step, but each individual source can still emit
    # duplicate session_ids when prior state is corrupted:
    #   - step 1 (/proc cmdline): once a bad swap relaunched panes A and B
    #     with `--resume X`, both processes' cmdline reports X forever.
    #   - step 2 (sessions.log latest): SessionStart can record the same
    #     session_id for two panes (e.g. resumed from the same id).
    #   - step 3 (newest-mtime in cwd): GH #22 guards this case but the
    #     guard depends on by_pane data quality (cwd field populated, etc.).
    # GH #22 only fixed step 3 in isolation. The 2026-05-25 live trace
    # showed the collision propagating across swaps through step 1 (cmdline)
    # well after the initial step-3 contamination — every subsequent swap
    # re-relaunched both panes with the same id; the operator saw "no
    # conversation found" in one pane every cycle.
    #
    # Post-pass: any session_id resolved for >=2 panes is contaminated.
    # Clear it for ALL such panes so they relaunch fresh. Strictly safer
    # than one pane stealing another's chat — fresh is a recoverable state;
    # cross-contaminated history is not. Catches every source uniformly,
    # including future code paths we might add.
    sid_to_panes: dict[str, list[LiveSession]] = {}
    for ls in out:
        if ls.session_id:
            sid_to_panes.setdefault(ls.session_id, []).append(ls)
    for sid, ls_list in sid_to_panes.items():
        if len(ls_list) > 1:
            panes_str = ", ".join(ls.pane for ls in ls_list)
            if verbose:
                click.echo(
                    f"    COLLISION: session-id {sid[:8]} resolved for {len(ls_list)} panes ({panes_str}); "
                    f"clearing all — they will relaunch FRESH (no --resume) to avoid stealing each other's chat"
                )
            for ls in ls_list:
                ls.session_id = None
                ls.transcript_path = None

    return out


def tmux_exit_claude(pane: str, draft_handling: str = "submit") -> None:
    """Cleanly /exit claude in a tmux pane, handling input-box-has-focus cases.

    The 2026-05-19 incident showed that a bare `/exit` typed via tmux send-keys
    gets eaten by claude's input box as TEXT when the input is focused or
    contains user-typed content — claude doesn't interpret it as a slash
    command. Result: pane stays at claude, never returns to shell.

    The 2026-05-20 follow-up incident showed that the earlier "Escape + C-u"
    sequence was insufficient against multi-line input drafts: claude's
    Ink-based TUI doesn't honor readline C-u semantics on multi-line buffers
    (at best, it clears the current visual line). `/exit` then got appended
    to the leftover text and submitted as a regular message.

    User feedback 2026-05-20 (GH #11): the brute-force-clear variant of this
    function destroyed user drafts. The user prefers a "submit" variant that
    *preserves* the draft by sending it as a normal chat message, then types
    `/exit` against the now-empty input box. Configurable via
    `hot_swap.draft_handling`:

      "submit" (DEFAULT) — preserves any user draft as a sent message:
        1. Send Enter — submits any draft in the input box as a chat message.
           If the input was empty, Enter is a no-op in claude's TUI.
        2. Send `/exit` + Enter into the now-empty input box.
        Trade-off: claude may start responding to the user's half-finished
        draft before getting exited. The draft is recoverable in history on
        --resume.

      "clear" (LEGACY) — wipes any user draft before /exit:
        1. Escape, Escape (dismiss modal/overlay, defocus input).
        2. C-u (best-effort readline clear; harmless if empty).
        3. BSpace x 400 (brute-force backspace flurry for multi-line drafts).
        4. `/exit` + Enter.
        Trade-off: ANY draft the user was composing is destroyed — gone for
        good, not recoverable.

    Best-effort either way: if claude is genuinely wedged, escape sequences
    may do nothing, and `wait_for_shell` will eventually time out + skip
    relaunch.
    """
    if not tmux_is_available() or not pane or pane == "no-tmux":
        return

    # CRITICAL safety prefix (GH #24, 2026-05-25): send Escape × 2 FIRST
    # in BOTH modes. User report: "apparently it sends the answers to
    # claude's questions too" — if claude is in an interactive prompt
    # state (permission dialog, choice picker, autocomplete dropdown,
    # slash-command picker), our blind Enter would have ANSWERED the
    # prompt. Escape dismisses any modal/picker/prompt without
    # submitting anything. Two Escapes to cover nested UI state
    # (autocomplete inside a prompt, etc.).
    tmux_send_keys(pane, "Escape")
    time.sleep(0.15)
    tmux_send_keys(pane, "Escape")
    time.sleep(0.15)

    if draft_handling == "clear":
        # Legacy brute-force-clear path. Discards user draft.
        tmux_send_keys(pane, "C-u")
        time.sleep(0.15)
        tmux_send_keys(pane, *(["BSpace"] * 400))
        time.sleep(0.3)
        tmux_send_text(pane, "/exit")
        return

    # Default: "submit" — preserve draft by sending it as a message first.
    # Now SAFE because Escape × 2 above dismissed any interactive prompt;
    # the Enter below only sees the main chat input. If the input is empty,
    # Enter is a no-op in claude's TUI. If non-empty, the draft is sent
    # as a normal user turn (safely persisted in JSONL, visible on --resume).
    tmux_send_keys(pane, "Enter")
    time.sleep(0.3)
    # Type /exit into the (now-empty) input box. tmux_send_text appends
    # Enter automatically.
    tmux_send_text(pane, "/exit")


def wait_for_shell(pane: str, timeout_seconds: int = 10, poll_interval: float = 0.25) -> bool:
    """Poll until the tmux pane's current command is no longer claude/node.

    Replaces the original hard-coded `time.sleep(2)` between /exit and the
    relaunch command. The fixed sleep was too short on slow machines (claude
    hadn't exited yet) — the relaunch text got typed INTO the still-loading
    claude TUI. Polling pane_current_command instead waits exactly long
    enough for the actual transition.

    Returns True if pane returned to shell within timeout, False on timeout.
    """
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        cmd = tmux_pane_name(pane)
        if cmd not in ("claude", "node", ""):
            return True
        time.sleep(poll_interval)
    return False


def build_relaunch_cmd(session: "LiveSession", hot: dict) -> str:
    """Build the shell command typed into a pane to relaunch claude after a swap.

    Single source of truth for the relaunch string so the live orchestrator and
    the `check-orchestrate` preview can never drift (they previously duplicated
    this construction). Shape:

        cd <cwd> && [MCP_TIMEOUT=<n> ]claude[ <flags>][ --resume <id>][ <wake>]

    The optional `MCP_TIMEOUT=<n>` env prefix (GH #25) widens Claude Code's
    per-stdio-server startup budget so slow-cold-starting MCP servers (e.g. gym)
    register their tools on resume instead of being silently dropped under the
    CPU contention of a multi-pane swap. See the `relaunch_mcp_timeout_ms`
    config comment for the full mechanism.
    """
    flags = hot.get("relaunch_flags", "").strip()
    flag_part = f" {flags}" if flags else ""
    use_resume = hot.get("relaunch_with_resume", True)
    wake = hot.get("wake_up_message", "").strip()
    wake_part = f" {shlex.quote(wake)}" if wake else ""

    # MCP_TIMEOUT env prefix. 0 (or missing) => don't set it; inherit Claude
    # Code's own default. We set it inline before `claude` so it scopes to that
    # process only and survives the `cd ... &&` (the env assignment binds to the
    # claude command, not the cd).
    mcp_timeout = hot.get("relaunch_mcp_timeout_ms", 0)
    env_prefix = ""
    if isinstance(mcp_timeout, int) and mcp_timeout > 0:
        env_prefix = f"MCP_TIMEOUT={mcp_timeout} "

    if use_resume and session.session_id:
        return (
            f"cd {shlex.quote(session.cwd)} && "
            f"{env_prefix}claude{flag_part} --resume {shlex.quote(session.session_id)}{wake_part}"
        )
    return f"cd {shlex.quote(session.cwd)} && {env_prefix}claude{flag_part}{wake_part}"


def hot_swap_orchestrate(decision: SwapDecision, state: dict, config: dict) -> None:
    """Hot-swap orchestrator (rewritten 2026-05-19 after the find_live_sessions incident).

    Wrapped in `fcntl.flock` on `ORCHESTRATE_LOCK`. If another orchestrator
    is already running (e.g. daemon + manual `cus auto-swap --orchestrate`),
    this invocation aborts immediately rather than racing on the same panes.
    The 2026-05-19 second incident was caused by lack of this lock: daemon
    ran a second orchestration 100s after a manual one, both typing /exit
    + relaunch into the same panes. See audit P0 item 11c.
    """
    ORCHESTRATE_LOCK.parent.mkdir(parents=True, exist_ok=True)
    # Open the lock file (create if needed). Use O_RDWR so we can write pid.
    lock_fd = os.open(str(ORCHESTRATE_LOCK), os.O_CREAT | os.O_RDWR, 0o644)
    try:
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            # Read the holder's pid for diagnostics
            try:
                holder = os.read(lock_fd, 64).decode().strip()
            except OSError:
                holder = "(unknown)"
            click.echo(f"  hot_swap: another orchestrator is running (pid {holder}); aborting this invocation")
            return
        # We hold the lock. Write our pid for debugging.
        os.lseek(lock_fd, 0, 0)
        pid_str = f"{os.getpid()}\n".encode()
        os.ftruncate(lock_fd, 0)
        os.write(lock_fd, pid_str)
        try:
            _hot_swap_orchestrate_impl(decision, state, config)
        finally:
            # Release lock + truncate pid
            try:
                os.lseek(lock_fd, 0, 0)
                os.ftruncate(lock_fd, 0)
            except OSError:
                pass
            try:
                fcntl.flock(lock_fd, fcntl.LOCK_UN)
            except OSError:
                pass
    finally:
        try:
            os.close(lock_fd)
        except OSError:
            pass


def _hot_swap_orchestrate_impl(decision: SwapDecision, state: dict, config: dict) -> None:
    """Inner implementation of hot_swap_orchestrate (called under flock).

    Per-pane iteration (not per-session-id), drops the wake-up positional
    arg, polls pane_current_command instead of sleeping, optionally passes
    relaunch flags (--dangerously-skip-permissions etc.).

    Sequence:
      1. Enumerate panes with live claude on the swap-out account.
      2. Filter: skip pinned panes, whitelist-matched panes.
      3. Subagent skip-guard (per-pane, not whole-abort — GH #27): drop
         subagent-active panes from `swappable` when tier <
         config.subagent_skip.defer_below_tier; cred swap and other panes'
         relaunches still proceed. Tier 3 always bypasses the guard.
      4. Cache-bust window (Tier 1 only, non-idle panes only): defer if any
         pane is mid-turn AND its transcript was written < cache_bust_window
         seconds ago (cache still warm — swap would burn rebuild cost).
      5. Per-tier prep (per pane):
         - Tier 1: if idle, no-op (already at turn boundary); else wait_for_stop.
         - Tier 2: inject pause-message, wait for resulting Stop.
         - Tier 3: double-Escape to interrupt running tool, log shell context.
      6. /exit each pane's claude.
      7. Poll each pane until its current command is back to shell (replaces
         the prior hard-coded sleep(2) which raced).
      8. Atomic credential swap.
      9. Per-pane relaunch: `cd <cwd> && claude [relaunch_flags] --resume <id>`.
         No positional wake-up message (that previously became a billed first
         user prompt + triggered Stop hook).

    Hot-swap remains disabled by default (`hot_swap.enabled: false`) until the
    user explicitly opts in. Even after the rewrite, smoke-test on a single
    pane before scaling up.
    """
    current = state["active"]
    hot = config.get("hot_swap", {})
    tier = decision.tier
    target = decision.target

    # Pre-swap verify-poll (GH #8): if the picker's view of `target` is more
    # than 60 seconds stale AND target isn't in poll-backoff, do a fresh poll
    # right before orchestrating. The picker may have decided based on
    # 5-10 min old data from the previous daemon cycle. Another machine using
    # `target` in that window could have pushed it close to its cap; we'd
    # rather catch that now than swap onto a hot account.
    # Abort scenarios (logged + return without orchestrating):
    #   - target is now token_expired
    #   - target's max usage is now >= 99% (no headroom, swap would be wasted)
    target_acct = state.get("accounts", {}).get(target, {})
    last_poll = target_acct.get("last_poll_ts")
    target_in_backoff, _ = account_in_backoff(state, target)
    poll_age_seconds = None
    if last_poll:
        try:
            last_poll_dt = datetime.fromisoformat(last_poll.replace("Z", "+00:00"))
            poll_age_seconds = (datetime.now(timezone.utc) - last_poll_dt).total_seconds()
        except ValueError:
            poll_age_seconds = None
    if poll_age_seconds is not None and poll_age_seconds > 60 and not target_in_backoff:
        click.echo(f"  pre-swap verify-poll: target {target} last polled {int(poll_age_seconds)}s ago; refreshing...")
        fresh = poll_account_usage(target)
        update_state_with_usage(state, {target: fresh})
        save_state(state)
        if fresh.token_expired:
            click.echo(f"    ABORT: {target} is now TOKEN_EXPIRED — letting next cycle pick a different target")
            return
        if fresh.raw.get("error") == "rate_limited":
            click.echo(f"    note: {target} polling endpoint is rate-limited; proceeding with cached numbers (account itself may still be usable)")
        else:
            fh = f"{fresh.five_hour.utilization:.1f}%" if fresh.five_hour else "—"
            sd = f"{fresh.seven_day.utilization:.1f}%" if fresh.seven_day else "—"
            click.echo(f"    fresh: 5h={fh}, 7d={sd}")
            max_pct = max(
                fresh.five_hour.utilization if fresh.five_hour else 0,
                fresh.seven_day.utilization if fresh.seven_day else 0,
            )
            if max_pct >= 99:
                click.echo(f"    ABORT: {target} is now at {max_pct:.0f}% — no headroom, letting next cycle pick a different target")
                return
    elif poll_age_seconds is not None and target_in_backoff:
        click.echo(f"  pre-swap verify-poll: target {target} in backoff; proceeding with cached numbers (age {int(poll_age_seconds)}s)")

    # CHANGED 2026-05-20 (issue #2 fix): query EVERY live pane, then keep only
    # those whose loaded account doesn't match the target. The pane's loaded
    # account is its latest SessionStart-recorded account (sessions.log,
    # latest entry per pane). Previous behavior filtered by account_filter=current
    # at the find_live_panes level, which missed panes that had drifted (their
    # SessionStart-recorded account differed from current.active after a
    # non-orchestrated swap). The new logic restarts panes that need to align
    # with the target, regardless of which account they're currently on.
    all_panes = find_live_panes(account_filter=None, verbose=True)
    already_aligned = [s for s in all_panes if s.account == target]
    misaligned = [s for s in all_panes if s.account != target]
    click.echo(f"  hot_swap: {len(all_panes)} live pane(s), {len(misaligned)} need restart to align with {target} ({len(already_aligned)} already on {target})")

    swappable: list[LiveSession] = []
    for s in misaligned:
        pinned, reason = session_is_pinned(s, config)
        if pinned:
            click.echo(f"    skip pane {s.pane} on {s.account} ({reason})")
            continue
        wl, reason = session_matches_whitelist(s, config)
        if wl:
            click.echo(f"    skip pane {s.pane} on {s.account} ({reason})")
            continue
        swappable.append(s)

    if not swappable:
        # No live tmux panes to orchestrate — just swap creds. Sessions outside
        # tmux (or no-claude panes) are unaffected.
        click.echo(f"    no swappable panes; doing simple cred swap")
        execute_swap(decision.target, trigger=f"auto-tier{tier}-simple")
        return

    # Subagent skip-guard — per-pane (GH #27).
    #
    # Two corrections vs. the original implementation:
    #
    # 1. **Per-pane skip, not whole-orchestration abort.** The old code did
    #    `for s in swappable: if subagent_active(s): return` — a single busy
    #    pane bailed before the cred swap, before any other pane's relaunch.
    #    That contradicted the "no swappable panes → simple cred swap" branch
    #    above and meant one stuck pane held up the whole pool. Now we
    #    *filter* swappable to drop subagent-active panes and proceed with
    #    the rest plus the cred swap. Each skipped pane is inbox-logged so
    #    the operator knows that pane drifted onto the old account until its
    #    next manual restart.
    #
    # 2. **Tier 3 always bypasses the guard.** The code-default
    #    `defer_below_tier: 3` was intended to mean "Tier 3 (force) proceeds
    #    regardless," but a config value > 3 (e.g. operators setting 100 to
    #    be cautious at low tiers) was silently disabling the saturation
    #    safety valve — at 96% the swap deferred indefinitely behind a busy
    #    pane. The growth gate already bypasses at saturation; mirror that
    #    posture here. Tier 3 means "we mean it, swap now even if a pane is
    #    busy" — exactly what saturation needs.
    if config.get("subagent_skip", {}).get("enabled", True) and tier < 3:
        defer_below = config["subagent_skip"].get("defer_below_tier", 3)
        if tier < defer_below:
            busy = [s for s in swappable if subagent_active(s, config)]
            if busy:
                kept = [s for s in swappable if s not in busy]
                for s in busy:
                    click.echo(
                        f"    skip pane {s.pane} (session {s.session_id[:8]}): subagent active; "
                        f"tier={tier} < {defer_below} — pane will drift onto {current} until next restart"
                    )
                    append_inbox(
                        "deviation",
                        f"Pane {s.pane} skipped during tier-{tier} swap (subagent active)",
                        f"Account `{current}` swap to `{decision.target}` proceeded, but pane {s.pane} "
                        f"(session `{s.session_id[:8]}`) had an active subagent/tool and was left on `{current}` "
                        f"until its next manual restart. Tier {tier} < `defer_below_tier`={defer_below}.\n\n"
                        f"**Walk-back**: in the affected pane, `/exit` then `claude --resume {s.session_id}` "
                        f"to pick up on the new active account; or `cus switch {current}` to revert globally.",
                    )
                if not kept:
                    click.echo(f"    all swappable panes had active subagents; doing simple cred swap")
                    execute_swap(decision.target, trigger=f"auto-tier{tier}-allskipped")
                    return
                swappable = kept

    # Cache-bust window (Tier 1 only, non-idle panes only)
    # GH #33: session_is_idle now ORs in a tool_use.log lookback so sessions
    # in active multi-step tasks (tool every few min with gaps) stay
    # classified mid-turn. Net effect here: tier-1 swaps defer more often
    # on stale-JSONL-but-tool-active sessions. Desired — don't burn cache.
    activity_lookback = hot.get("activity_lookback_seconds", 300)
    if tier == 1:
        window = hot.get("cache_bust_window_seconds", 300)
        idle_seconds = hot.get("mid_turn_idle_seconds", 30)
        for s in swappable:
            if session_is_idle(s, idle_seconds, activity_lookback):
                continue
            if s.transcript_path and cache_warm(s.transcript_path, window):
                click.echo(f"    DEFER: cache warm for active pane {s.pane} (Tier 1 swap would burn cache)")
                return

    # GH #37: re-verify each pane is STILL running claude right before we begin
    # injecting keystrokes. find_live_sessions confirmed claude at discovery,
    # but the pre-swap verify-poll above (and the per-pane Stop/pause waits
    # below) can elapse minutes — the user may have /exit'd in the meantime.
    # A pause-message or /exit typed into a bare shell would execute as shell
    # input. Drop any pane that's no longer claude; the account-level credential
    # swap downstream still proceeds (it benefits new sessions regardless).
    still_claude = []
    for s in swappable:
        if pane_is_claude(s.pane):
            still_claude.append(s)
        else:
            click.echo(
                f"    pane {s.pane}: no longer running claude "
                f"(pane_current_command changed) — skipping keystrokes (GH #37)"
            )
    swappable = still_claude

    # Per-tier prep. All tiers now check session_is_idle FIRST and skip
    # the disruptive action (pause-message, Escape) when the session is
    # already at a turn boundary. Issue #4: previously tier 2/3 sent their
    # signals unconditionally, billing tokens on idle sessions.
    idle_seconds = hot.get("mid_turn_idle_seconds", 30)
    for s in swappable:
        is_idle = session_is_idle(s, idle_seconds, activity_lookback)
        if tier == 2:
            if is_idle:
                # Idle — no pause-message needed; session already at turn boundary.
                click.echo(f"    pane {s.pane}: idle (>{idle_seconds}s) — skipping pause-message (no need)")
            else:
                click.echo(f"    pane {s.pane}: mid-turn — injecting pause-message")
                tmux_send_text(s.pane, hot.get("pause_message", "please pause; we're swapping accounts."))
                ok = wait_for_stop(s.session_id, hot.get("pause_response_timeout_seconds", 120))
                if not ok:
                    # GH #19: escalate to tier 3 on pause-response timeout
                    # rather than just proceeding anyway. The pause_message
                    # apparently didn't land (session stuck in tool call);
                    # force interrupt is the right next step.
                    click.echo(f"      pause_message timed out; escalating to tier 3 (force double-Escape)")
                    tmux_send_keys(s.pane, "Escape")
                    time.sleep(0.3)
                    tmux_send_keys(s.pane, "Escape")
                    time.sleep(0.5)
        elif tier == 3:
            if is_idle:
                # Idle — no running tool to interrupt; skip Escape.
                click.echo(f"    pane {s.pane}: idle (>{idle_seconds}s) — skipping force-interrupt (no running tool)")
            else:
                click.echo(f"    pane {s.pane}: mid-turn — force interrupt (double Escape)")
                # Single Escape may not interrupt — Claude's TUI sometimes
                # needs two to dismiss running tool calls.
                tmux_send_keys(s.pane, "Escape")
                time.sleep(0.3)
                tmux_send_keys(s.pane, "Escape")
                time.sleep(0.5)
                shell_note = _read_recent_tool_use_for_session(s.session_id, lookback_seconds=300)
                if shell_note:
                    append_inbox(
                        "deviation",
                        f"Force-interrupted pane {s.pane} (tier 3)",
                        f"Account `{current}` was force-swapped to `{decision.target}` while pane {s.pane} (session {s.session_id[:8]}) had active tool calls:\n\n```\n{shell_note}\n```\n\n**Walk-back**: re-issue the interrupted commands in a fresh session on the now-active account, or `cus switch {current}` to go back to the original.",
                    )
        else:  # tier 1
            if is_idle:
                click.echo(f"    pane {s.pane}: idle (>{idle_seconds}s) — already at turn boundary, no wait")
            else:
                # Escalation ladder (GH #19, 2026-05-25 user directive):
                # tier 1 timeout → escalate to tier 2 (send pause_message)
                # tier 2 timeout → escalate to tier 3 (force double-Escape)
                # tier 3 timeout → skip pane (proceed with others)
                #
                # The 2026-05-24 incident: tier 1 wait_for_stop timed out
                # after 300s on a stuck pane; the orchestrator aborted the
                # ENTIRE swap. User feedback: "its supposed to step in and
                # send a message to wrap up so we can swap (in the event
                # that the session is just going and going)." That's exactly
                # what tier 2 was designed for — but tier was determined
                # from next_swap_at_pct (which was 70 → tier 1), not from
                # actual stuck-state. The fix: on tier 1 timeout, ESCALATE
                # rather than abort.
                stop_timeout = hot.get("stop_wait_timeout_seconds", 300)
                click.echo(f"    pane {s.pane}: mid-turn — waiting for Stop (up to {stop_timeout}s)")
                ok = wait_for_stop(s.session_id, stop_timeout)
                if not ok:
                    # Escalate to tier 2: send pause_message and wait again.
                    click.echo(f"      tier 1 timed out; escalating to tier 2 (sending pause_message)")
                    tmux_send_text(s.pane, hot.get("pause_message", "please pause; we're swapping accounts."))
                    pause_timeout = hot.get("pause_response_timeout_seconds", 120)
                    ok2 = wait_for_stop(s.session_id, pause_timeout)
                    if not ok2:
                        # Escalate to tier 3: double-Escape force interrupt.
                        click.echo(f"      tier 2 timed out; escalating to tier 3 (force double-Escape)")
                        tmux_send_keys(s.pane, "Escape")
                        time.sleep(0.3)
                        tmux_send_keys(s.pane, "Escape")
                        time.sleep(0.5)
                        shell_note = _read_recent_tool_use_for_session(s.session_id, lookback_seconds=300)
                        if shell_note:
                            append_inbox(
                                "deviation",
                                f"Force-interrupted pane {s.pane} (tier 3 escalation from tier 1)",
                                f"Account `{current}` was force-swapped to `{decision.target}` after tier-1+tier-2 timeouts on pane {s.pane} (session {s.session_id[:8]}). Active tool calls at time of interrupt:\n\n```\n{shell_note}\n```\n\n**Walk-back**: re-issue the interrupted commands in a fresh session on the now-active account, or `cus switch {current}` to go back to the original.",
                            )
                        # We proceed regardless — escape is best-effort.
                        # If THIS also somehow fails, skip the pane.
                        # (Detected later via wait_for_shell failing.)

    # /exit each pane — see tmux_exit_claude docstring for the draft-handling
    # modes. Default "submit" preserves the user's in-progress draft as a sent
    # message; "clear" brute-force-wipes the input box before /exit (legacy,
    # destroys drafts). Configurable via hot_swap.draft_handling (GH #11).
    # Per GH #19: skip panes that timed out during the Stop-wait above.
    draft_handling = hot.get("draft_handling", "submit")
    for s in swappable:
        if getattr(s, "_stuck_skip", False):
            click.echo(f"    pane {s.pane}: skipping /exit (Stop-wait timed out — leaving on old account)")
            continue
        # GH #37: a minute-long Stop/pause wait may have just elapsed for this
        # pane — re-check it's still claude. If the user already exited during
        # the wait, typing "/exit" here would land in their shell and could
        # close the pane. Skip; the pane is already off the old account.
        if not pane_is_claude(s.pane):
            click.echo(f"    pane {s.pane}: no longer running claude — skipping /exit (GH #37; would type into shell)")
            continue
        click.echo(f"    pane {s.pane}: /exit (draft_handling={draft_handling})")
        tmux_exit_claude(s.pane, draft_handling=draft_handling)

    # Poll each pane until shell prompt is back. Replaces the original
    # sleep(2) which raced — claude wasn't always done exiting when the
    # relaunch command got typed, so the relaunch ended up as text INSIDE
    # the still-loading new claude.
    shell_timeout = hot.get("shell_return_timeout_seconds", 10)
    panes_ready: list[LiveSession] = []
    for s in swappable:
        if getattr(s, "_stuck_skip", False):
            # Already skipped /exit; don't wait for shell either.
            continue
        if wait_for_shell(s.pane, timeout_seconds=shell_timeout):
            panes_ready.append(s)
        else:
            click.echo(f"    WARN: pane {s.pane} didn't return to shell in {shell_timeout}s; skipping relaunch")

    if not panes_ready:
        # No panes reached shell prompt — could be all stuck or all timed out
        # on shell-return. Either way, do the cred swap anyway so future
        # claude invocations use the new account. This also handles the
        # GH #19 case where ALL panes were stuck mid-turn: we still want the
        # underlying account swap to happen.
        click.echo(f"    no panes reached shell prompt; doing cred swap only (no relaunch)")
        execute_swap(decision.target, trigger=f"auto-tier{tier}-noreloaunch")
        return

    # GH #32: re-validate each pane's session-id at relaunch time. By now
    # /exit has run and wait_for_shell returned — typically 5-10 seconds
    # past the find_live_panes() decision point. The GH #22 race-window
    # acceptance ("JSONL will exist by relaunch time") is invalidated if
    # the JSONL STILL doesn't exist after that wait: the original session
    # is a ghost (SessionStart fired but session died before first write).
    # Walk back through sessions.log for that pane to find a real prior
    # session, or fall back to fresh launch.
    walkback_lookback = hot.get("relaunch_walkback_lookback_seconds", 3600)
    for s in panes_ready:
        new_sid, new_t = _resolve_relaunch_session_id(s, walkback_lookback)
        if new_sid != s.session_id:
            old_short = s.session_id[:8] if s.session_id else "None"
            new_short = new_sid[:8] if new_sid else "None"
            click.echo(
                f"    pane {s.pane}: relaunch session-id changed from {old_short} → {new_short} "
                f"(post-/exit JSONL re-validation; original was a ghost)"
            )
            s.session_id = new_sid
            s.transcript_path = new_t

    # GH #32 + GH #29: collision detector. The walkback above can produce a
    # session_id that's now shared with another pane (e.g. two panes both
    # had ghost session_ids that walked back to the same prior session in a
    # shared cwd). The collision check inside find_live_panes ran BEFORE
    # walkback so it wouldn't see this — re-run it now over panes_ready and
    # fall back to fresh launch for any collisions, same policy as GH #29.
    sid_to_panes_post: dict[str, list[LiveSession]] = {}
    for s in panes_ready:
        if s.session_id:
            sid_to_panes_post.setdefault(s.session_id, []).append(s)
    for sid_dup, ls_list in sid_to_panes_post.items():
        if len(ls_list) > 1:
            panes_str = ", ".join(ls.pane for ls in ls_list)
            click.echo(
                f"    POST-WALKBACK COLLISION: session-id {sid_dup[:8]} resolved for {len(ls_list)} panes "
                f"({panes_str}); clearing all — they will relaunch FRESH (no --resume)"
            )
            for ls in ls_list:
                ls.session_id = None
                ls.transcript_path = None

    # Atomic credential swap
    execute_swap(decision.target, trigger=f"auto-tier{tier}")

    # Per-pane relaunch — no positional wake-up. The original "Continue
    # where you left off." positional caused: (a) Claude to respond
    # (Opus, billed), (b) validator Stop hook to fire (Haiku, billed).
    # With no wake-up, the relaunched claude opens at empty prompt; user
    # types whatever they want next (cost: zero).
    #
    # relaunch_flags lets the user inject `--dangerously-skip-permissions`
    # etc. via config — defaults to empty (no flags) for safety.
    #
    # relaunch_with_resume: when False, relaunch is plain `claude` (fresh
    # session, no continuity). Use this for stagnant or loop-driven sessions
    # that don't need conversation continuity — avoids loading the old JSONL
    # (which may have pollution from validator hook env-var leaks etc.).
    #
    # wake_up_message: defaults to empty — no positional sent. Set non-empty
    # ONLY if you understand the cost (positional becomes a first user
    # message → billed response → post-assistant hooks fire → also billed).
    use_resume = hot.get("relaunch_with_resume", True)
    stagger = hot.get("relaunch_stagger_seconds", 0) or 0
    for i, s in enumerate(panes_ready):
        # GH #21: if we couldn't find a valid session-id with an existing
        # JSONL, build_relaunch_cmd falls back to plain `claude` (no --resume)
        # — better a fresh session than a failed "No conversation found".
        if use_resume and not s.session_id:
            click.echo(f"    pane {s.pane}: no valid session-id found; relaunching FRESH (no --resume)")
        cmd = build_relaunch_cmd(s, hot)
        # GH #25: stagger relaunches so N panes' MCP stdio cold-starts don't all
        # contend for CPU at once (which is what pushes gym's ~8s cold start past
        # MCP_TIMEOUT and drops its tools). Sleep BEFORE every pane after the
        # first; the last pane adds no trailing delay.
        if i > 0 and stagger > 0:
            time.sleep(stagger)
        click.echo(f"    pane {s.pane}: relaunch → {cmd}")
        tmux_send_text(s.pane, cmd)


def _read_rate_limit_log_since(since_ts: str | None) -> list[dict]:
    """Return 429 entries newer than `since_ts` (ISO string)."""
    if not RATE_LIMIT_LOG.exists():
        return []
    out: list[dict] = []
    cutoff = 0.0
    if since_ts:
        try:
            cutoff = datetime.fromisoformat(since_ts.replace("Z", "+00:00")).timestamp()
        except ValueError:
            pass
    try:
        with RATE_LIMIT_LOG.open() as f:
            for line in f:
                parts = line.strip().split(",", 3)
                if len(parts) < 3:
                    continue
                try:
                    ts = datetime.fromisoformat(parts[0].replace("Z", "+00:00")).timestamp()
                except ValueError:
                    continue
                if ts <= cutoff:
                    continue
                out.append({"ts": parts[0], "session_id": parts[1], "match": parts[2]})
    except OSError:
        pass
    return out


def check_rate_limit_reactive(state: dict, config: dict, entries: list | None = None) -> SwapDecision | None:
    """If there's a recent 429 since last check, force a swap on the active account.

    Returns a Tier-3 SwapDecision (force) so hot_swap_orchestrate skips
    the gentle wait-for-Stop path.

    Hysteresis: respects swap_hysteresis.min_seconds_between_reactive_swaps
    (default 60s — shorter than the ladder gate because reactive 429s are
    real emergencies). Prevents rapid-fire reactive swaps when the
    rate_limit_log churns or sessions.log mapping is briefly stale.

    `entries` (GH #99 hybrid): when None (default) this reads the 429 log
    itself and advances the watermark — the global-mode behavior. Hybrid mode
    reads the log ONCE and partitions it (slot-attributable vs bare) so a
    slot's 429 doesn't also bounce the shared mount; it passes this function
    only the BARE entries and owns the watermark itself, so we neither re-read
    nor re-advance it here.
    """
    if not config.get("reactive", {}).get("enabled", True):
        return None
    owns_watermark = entries is None
    if owns_watermark:
        watermark = state.get("last_429_check_ts")
        entries = _read_rate_limit_log_since(watermark)
        # Bump watermark to now so we don't keep scanning old entries
        state["last_429_check_ts"] = now_iso()
    if not entries:
        return None

    # Only react if at least one 429 was on the active account's session
    active = state["active"]
    matched_session = None
    for entry in entries:
        sess_log = _parse_sessions_log()
        sess_account_map = {e["session_id"]: e["account"] for e in sess_log}
        if sess_account_map.get(entry["session_id"]) == active:
            matched_session = entry
            break
    if not matched_session:
        return None

    # Fix A1 (user 2026-06-23): only treat a 429 as a swap trigger if the active
    # account is plausibly near its budget cap. Observed failure mode: session
    # db246f7f emitted downstream/concurrency "rate limit" strings while `default`
    # sat at 4-23% 5h — nowhere near exhausted — and each one bounced the active
    # cred onto a full merkos. A real budget-429 cannot surface from 8%: the poll
    # lag (<= poll_interval) can't hide a ~90-point jump. Below the floor we treat
    # the 429 as noise (downstream API limit / RPM spike from parallel subagents)
    # and decline to swap — swapping wouldn't help and lands us on a worse account.
    min_active_pct = config.get("reactive", {}).get("min_active_pct", 50)
    active_acct = state.get("accounts", {}).get(active, {})
    active_pct = _account_effective_pct(active_acct, config)
    if active_pct < min_active_pct:
        click.echo(
            f"  429 on {active} session {matched_session['session_id'][:8]} ignored: "
            f"active at {active_pct:.0f}% (< reactive.min_active_pct={min_active_pct}%) — "
            "downstream/concurrency 429, not budget exhaustion; not swapping"
        )
        return None

    # Hysteresis check (GH #18): even legitimate reactive 429s respect a
    # short minimum interval. Reactive cooldown defaults to 60s rather than
    # the ladder's 300s — real 429s are emergencies, but we still need to
    # prevent ping-pong on flaky parsing or stale session→account mappings.
    hyst_cfg = config.get("swap_hysteresis", {})
    if hyst_cfg.get("enabled", True):
        min_seconds = hyst_cfg.get("min_seconds_between_reactive_swaps", 60)
        active_acct = state.get("accounts", {}).get(active, {})
        last_swap = active_acct.get("last_swap_ts")
        if last_swap:
            try:
                last_swap_dt = datetime.fromisoformat(last_swap.replace("Z", "+00:00"))
                elapsed = (datetime.now(timezone.utc) - last_swap_dt).total_seconds()
                if elapsed < min_seconds:
                    click.echo(f"  reactive 429 detected on {active} but last swap was {int(elapsed)}s ago (< {min_seconds}s); deferring")
                    return None
            except ValueError:
                pass

    target = pick_swap_target(state, config)
    if target is None:
        click.echo(f"  429 detected on {active} session {matched_session['session_id'][:8]} but no valid swap target")
        return None
    return SwapDecision(
        target=target.name,
        reason=f"429 reactive: {matched_session['match']} in session {matched_session['session_id'][:8]}",
        tier=3,
        gate="reactive_429",
        deferrable=False,  # GH #56: a real user-facing 429 is an emergency — never lazy-defer
    )


def _read_recent_tool_use_for_session(session_id: str, lookback_seconds: int) -> str:
    """Read recent tool_use.log entries for a session, formatted for inbox."""
    if not TOOL_USE_LOG.exists():
        return ""
    cutoff = time.time() - lookback_seconds
    out: list[str] = []
    try:
        with TOOL_USE_LOG.open() as f:
            for line in f:
                parts = line.strip().split(",", 3)
                if len(parts) < 4 or parts[1] != session_id:
                    continue
                try:
                    ts = datetime.fromisoformat(parts[0].replace("Z", "+00:00")).timestamp()
                except ValueError:
                    continue
                if ts < cutoff:
                    continue
                out.append(line.strip())
    except OSError:
        pass
    return "\n".join(out[-20:])  # most recent 20 entries


def shlex_quote(s: str) -> str:
    """Shell-quote a string for use in a tmux send-text command."""
    import shlex
    return shlex.quote(s)


# --------------------------------------------------------------------------
# Commands
# --------------------------------------------------------------------------

@click.group()
def cli() -> None:
    """claude-usage-swap (cus) — auto-rotate Claude Code OAuth accounts."""


def write_account_meta(name: str, source_dir: Path, dst: Path, existed: bool) -> dict:
    """(Re)write account-<name>/meta.yaml, refreshing the system-managed OAuth
    identity while preserving user-edited fields (priority, locked_sessions).

    Factored out of init() so it can be called from BOTH import paths:
      - importing a brand-new dir copied in from elsewhere, and
      - force-refreshing a dir that already lives in ~/claude-accounts/.

    The second path is the one that matters for `cus add`: that command writes
    meta.yaml with oauth_email="unknown (run /login)" BEFORE the interactive
    login exists, so the identity must be re-read from .claude.json on the next
    `init --force`. Without this refresh the account shows "unknown" in
    `cus list` forever. (Bug found 2026-06-12: add-account flow never registered.)

    Reads identity from the dir's own .claude.json (dst), since after `cus add`
    + login that's where the freshly-written oauthAccount lives.
    """
    cj = claude_json_for_config_dir(dst)
    identity = extract_identity(cj) if cj is not None and cj.exists() else {}
    meta_path = dst / "meta.yaml"
    prior_meta = read_yaml(meta_path) if meta_path.exists() else {}
    oauth = identity.get("oauthAccount") or {}
    meta = {
        "name": name,
        "source_dir": str(source_dir),
        "oauth_email": oauth.get("emailAddress", "unknown") if isinstance(oauth, dict) else "unknown",
        "oauth_account_uuid": oauth.get("accountUuid", "unknown") if isinstance(oauth, dict) else "unknown",
        "priority": prior_meta.get("priority", 1),
        "locked_sessions": prior_meta.get("locked_sessions", []),
        "imported_ts": prior_meta.get("imported_ts", now_iso()) if existed else now_iso(),
        "refreshed_ts": now_iso() if existed else None,
    }
    write_yaml(meta_path, meta)
    return meta


@cli.command()
@click.option("--dry-run", is_flag=True, help="Show what would happen without writing anything.")
@click.option("--force", is_flag=True, help="Overwrite existing account dirs (refreshes stale credential snapshots).")
def init(dry_run: bool, force: bool) -> None:
    """Discover existing Claude config dirs and import each as an account.

    Idempotent: re-running skips accounts that already exist in
    ~/claude-accounts/ unless --force is passed. The live ~/.claude/ is
    named "default"; sibling ~/.claude-<name>/ dirs are named "<name>".

    Use --force when your live tokens have refreshed and you want the
    storage-side snapshot to match. Existing state.json and config.yaml
    are preserved either way (swap_history is not lost).
    """
    candidates = discover_config_dirs()
    if not candidates:
        click.echo("No Claude config dirs found (looked for ~/.claude/ and ~/.claude-*/).")
        sys.exit(1)

    click.echo(f"Discovered {len(candidates)} config dir(s):")
    for name, p in candidates:
        cj = claude_json_for_config_dir(p)
        cj_note = f"+ {cj}" if cj else "+ (no .claude.json found)"
        click.echo(f"  {name}: {p} {cj_note}")
    click.echo()

    if dry_run:
        click.echo("(dry-run) Would create:")
        click.echo(f"  {ACCOUNTS_DIR}/")
        for name, _ in candidates:
            verb = "overwrite" if (ACCOUNTS_DIR / f"account-{name}").exists() and force else "create"
            click.echo(f"    account-{name}/credentials.json  ({verb})")
            click.echo(f"    account-{name}/claude-identity.json  ({verb})")
            click.echo(f"    account-{name}/meta.yaml  ({verb})")
        click.echo(f"  {STATE_JSON}  (only if missing)")
        click.echo(f"  {CONFIG_YAML}  (only if missing)")
        return

    ACCOUNTS_DIR.mkdir(exist_ok=True)
    imported = 0
    skipped = 0
    refreshed = 0
    migrated = 0

    for name, src_dir in candidates:
        dst = ACCOUNTS_DIR / f"account-{name}"

        # If src_dir IS dst (already in managed location), just migrate to new layout
        if src_dir == dst:
            migration = migrate_account_dir(dst)
            if migration["action"] in ("migrated", "patched_symlinks"):
                migrated += 1
                click.echo(f"  migrated {name} -> {dst} ({len(migration['details'])} changes)")
            elif force:
                # Force refresh: re-read identity from this dir's .claude.json and
                # rewrite meta.yaml. Critical for the `cus add` flow — that command
                # seeds meta with oauth_email="unknown" before login, so the real
                # identity only becomes available here, after the user has logged in.
                # (Bug 2026-06-12: this branch previously did nothing but bump the
                # counter, leaving newly-added accounts stuck at "unknown".)
                meta = write_account_meta(name, dst, dst, existed=True)
                click.echo(f"  refreshed {name} -> {dst} (identity: {meta['oauth_email']})")
                refreshed += 1
            continue

        if dst.exists() and not force:
            click.echo(f"  skip {name}: {dst} already exists (use --force to refresh from {src_dir})")
            skipped += 1
            continue
        existed = dst.exists()

        dst.mkdir(parents=True, exist_ok=True)

        # 1. Credentials — copy from source to .credentials.json (new layout)
        src_creds = src_dir / ".credentials.json"
        dst_creds = dst / ".credentials.json"
        # GH #79: an `init --force` re-import can overwrite a managed snapshot
        # from a possibly-stale legacy dir — keep a rotated backup of the
        # snapshot being replaced so the newer tokens remain recoverable.
        backup_credentials_file(dst_creds)
        shutil.copy2(src_creds, dst_creds)
        os.chmod(dst_creds, 0o600)

        # 2. .claude.json — copy source's .claude.json (or extract identity if not available)
        src_cj = claude_json_for_config_dir(src_dir)
        dst_cj = dst / ".claude.json"
        if src_cj is not None and src_cj.exists():
            # Copy the whole .claude.json; per-account state is fine to duplicate
            shutil.copy2(src_cj, dst_cj)
        else:
            write_json(dst_cj, {})

        # 3. Symlink shared subdirs to ~/.claude/ so projects/plugins/etc. are shared
        for sub in SHARED_SYMLINK_SUBDIRS:
            link = dst / sub
            target = CLAUDE_DIR / sub
            if not link.exists() and target.exists():
                link.symlink_to(target)

        # Clean up any legacy files if we somehow co-exist with old layout
        for legacy in ("credentials.json", "claude-identity.json"):
            legacy_path = dst / legacy
            if legacy_path.exists():
                legacy_path.unlink()

        # 3. Meta — preserve user-edited fields (priority, locked_sessions),
        # refresh the system-managed fields. Only happens on --force refresh.
        write_account_meta(name, src_dir, dst, existed=existed)
        if existed:
            click.echo(f"  refreshed {name} -> {dst}")
            refreshed += 1
        else:
            click.echo(f"  imported {name} -> {dst}")
            imported += 1

    # Default per-account state seed, shared by the create-fresh and merge-into-
    # existing paths below so a newly-registered account looks identical however
    # it got there. The daemon/poll fill in the live fields on the next cycle.
    def _seed_state_entry() -> dict:
        return {
            "current_5h_pct": 0.0,
            "current_7d_pct": 0.0,
            "next_swap_at_pct": 50,
            "last_swap_ts": None,
        }

    # 4. state.json — runtime state for the daemon
    if not STATE_JSON.exists():
        state = {
            "active": "default" if any(n == "default" for n, _ in candidates) else candidates[0][0],
            "accounts": {name: _seed_state_entry() for name, _ in candidates},
            "swap_history": [],
        }
        write_json(STATE_JSON, state)
        click.echo(f"  wrote {STATE_JSON} (active = {state['active']})")
    else:
        # state.json already exists — merge in any newly-discovered accounts.
        # BUGFIX 2026-06-12: previously this branch did nothing, so an account
        # created via `cus add` after the initial init was never inserted here.
        # poll(), status, and the daemon all iterate state["accounts"], so the
        # new account stayed permanently invisible despite a valid login.
        state = load_state()
        newly_registered = [n for n, _ in candidates if n not in state.get("accounts", {})]
        if newly_registered:
            state.setdefault("accounts", {})
            for name in newly_registered:
                state["accounts"][name] = _seed_state_entry()
            save_state(state)
            click.echo(f"  registered in state.json: {', '.join(newly_registered)}")

    # 5. config.yaml — user-editable defaults
    if not CONFIG_YAML.exists():
        config = {
            "accounts": [{"name": n, "priority": 1} for n, _ in candidates],
            "poll_interval_seconds": 600,
            "thresholds": {
                "steps": THRESHOLD_STEPS[:-1],  # exclude 100 (force) from user-config
                "five_hour": True,
                "seven_day": True,
            },
            "strategy": "lowest_usage",  # alternatives: round_robin, strict_priority
        }
        write_yaml(CONFIG_YAML, config)
        click.echo(f"  wrote {CONFIG_YAML}")
    else:
        # config.yaml is documented as "auto-populated by cus init" — append any
        # newly-discovered accounts to its accounts list (preserving everything
        # the user has edited). Needed so the strict_priority strategy can see
        # the new account; harmless for the other strategies. Part of the
        # 2026-06-12 add-account registration fix.
        config = read_yaml(CONFIG_YAML)
        existing_names = {a.get("name") for a in config.get("accounts", []) if isinstance(a, dict)}
        missing = [n for n, _ in candidates if n not in existing_names]
        if missing:
            config.setdefault("accounts", [])
            for name in missing:
                config["accounts"].append({"name": name, "priority": 1})
            write_yaml(CONFIG_YAML, config)
            click.echo(f"  added to config.yaml accounts: {', '.join(missing)}")

    click.echo()
    click.echo(f"Done. Imported {imported}, migrated {migrated}, refreshed {refreshed}, skipped {skipped}.")
    if (imported or refreshed or migrated) and any(n == "default" for n, _ in candidates):
        click.echo("Live ~/.claude/ is registered as 'default' (the currently-active account).")


@cli.command(name="list")
def list_cmd() -> None:
    """List configured accounts with their identities."""
    if not ACCOUNTS_DIR.exists():
        click.echo("Not initialized. Run `cus init` first.")
        sys.exit(1)

    state = read_json(STATE_JSON) if STATE_JSON.exists() else {"active": None}
    for acct_dir in sorted(ACCOUNTS_DIR.glob("account-*")):
        name = acct_dir.name.removeprefix("account-")
        meta_path = acct_dir / "meta.yaml"
        meta = read_yaml(meta_path) if meta_path.exists() else {}
        active_marker = " ← ACTIVE" if name == state.get("active") else ""
        click.echo(f"{name}{active_marker}")
        click.echo(f"  oauth_email: {meta.get('oauth_email', 'unknown')}")
        click.echo(f"  account_uuid: {meta.get('oauth_account_uuid', 'unknown')}")
        click.echo(f"  source_dir: {meta.get('source_dir', 'unknown')}")
        click.echo(f"  priority: {meta.get('priority', 1)}")
        click.echo()


@cli.command(name="whoami")
def whoami_cmd() -> None:
    """Print the currently active account — i.e. which account you're on.

    The active account is authoritative from state.json's "active" key, which
    is rewritten on every swap; new `claude` invocations use it. NOTE: a tmux
    pane launched BEFORE a swap keeps using whatever account it loaded at start
    until it's relaunched, so this reports the MACHINE-WIDE active account, not
    per-pane attribution — see `cus status` for live sessions and the
    statusline for the per-pane account.
    """
    if not STATE_JSON.exists():
        click.echo("Not initialized. Run `cus init` first.")
        sys.exit(1)
    state = read_json(STATE_JSON)
    active = state.get("active")
    if not active:
        click.echo("No active account recorded in state.json.")
        sys.exit(1)

    meta = account_meta(active)
    acct = state.get("accounts", {}).get(active, {})
    click.echo(f"Active account: {active}")
    click.echo(f"  oauth_email:  {meta.get('oauth_email', 'unknown')}")
    click.echo(f"  account_uuid: {meta.get('oauth_account_uuid', 'unknown')}")

    # Usage snapshot from the last successful poll (best-effort context).
    h5 = acct.get("current_5h_pct")
    h7 = acct.get("current_7d_pct")
    if h5 is not None or h7 is not None:
        h5s = f"{h5:.0f}" if isinstance(h5, (int, float)) else "?"
        h7s = f"{h7:.0f}" if isinstance(h7, (int, float)) else "?"
        click.echo(f"  usage:        5h={h5s}%  7d={h7s}%")
    # _five_hour_remaining_seconds returns None when the 5h clock isn't ticking
    # (5h at 0%), so we never print a misleading countdown for an idle window.
    remaining = _five_hour_remaining_seconds(None, acct)
    if remaining is not None and remaining > 0:
        click.echo(f"  5h resets in: {int(remaining // 60)} min")

    # Surface staleness/blocked flags so whoami never quietly reports old data.
    flags = [f for f in ("token_expired", "token_stale", "rate_limited", "poll_error") if acct.get(f)]
    if flags:
        click.echo(f"  flags:        {', '.join(flags)}")


@cli.command()
def status() -> None:
    """Show active account, per-account usage state, locks, and recent activity."""
    if not STATE_JSON.exists():
        click.echo("Not initialized. Run `cus init` first.")
        sys.exit(1)

    state = read_json(STATE_JSON)
    config = load_config()
    color_on = sys.stdout.isatty()
    mode = config.get("mode", "global")
    per_session = mode == "per_session"
    if per_session:
        click.echo(f"Mode: per_session (global mount observe-only). Bare-launch account: {state['active']}")
    elif mode == "hybrid":
        click.echo(f"Mode: hybrid (lanes + shared mount both managed). Shared-mount account: {state['active']}")
    else:
        click.echo(f"Active account: {state['active']}")
    click.echo()
    # Ladder steps are IDENTICAL across accounts — they're the config's
    # thresholds.steps progression, not a per-account value — so printing the
    # first step ("Next swap") on every row was pure noise. Show the whole
    # ladder ONCE here as a legend; the per-row value only reappears below for
    # an account whose next_swap_at_pct has advanced off the first step
    # (mid-ladder / non-default), which genuinely IS per-account.
    cfg_steps = config.get("thresholds", {}).get("steps") or THRESHOLD_STEPS[:-1]
    first_step = cfg_steps[0] if cfg_steps else 50
    click.echo("Ladder steps: " + " → ".join(f"{s}%" for s in cfg_steps))
    click.echo()
    click.echo(f"{'Account':<20} {'5h %':>8} {'7d %':>8} {'Status':<24} {'Last swap':<28}")
    click.echo("-" * 91)
    disabled = _disabled_accounts(config)
    for name, a in sorted(state["accounts"].items()):
        marker = " *" if name == state["active"] else ""
        last = a.get("last_swap_ts") or "never"
        flags = []
        # Operator out-of-rotation flag (config, not polled state) — surfaced
        # here so nobody wonders why the picker never chooses this account.
        if name in disabled:
            flags.append("DISABLED")
        if a.get("token_expired"):
            flags.append("TOKEN_EXPIRED")
        if a.get("rate_limited"):
            flags.append("RATE_LIMITED")
        if a.get("poll_error"):
            flags.append("POLL_ERROR")
        if a.get("token_stale"):
            flags.append("TOKEN_STALE")
        status_col = ",".join(flags) if flags else "ok"
        # Estimator (C): annotate the extrapolated CURRENT usage when it diverges
        # from the last poll, so a fast-climbing account is visible between polls
        # (this is what the picker now uses to judge target fullness).
        est_note = ""
        for win, key in (("5h", "current_5h_pct"), ("7d", "current_7d_pct")):
            polled = a.get(key, 0.0)
            est = estimate_window_pct(a, win, config)
            if est - polled >= 1.0:
                rate = a.get(f"burn_rate_{win}_pct_per_min", 0.0) or 0.0
                est_note += f"  [{win} est {est:.0f}% @ +{rate:.1f}%/min]"
        # Show next_swap_at_pct ONLY when it's advanced off the shared first
        # ladder step (the legend above already prints that) — i.e. this account
        # is mid-ladder / carries a non-default value, which is the only case
        # worth a per-row callout.
        nxt_val = a.get("next_swap_at_pct", first_step)
        nxt_note = f"  [next-swap: {nxt_val:.0f}%]" if nxt_val != first_step else ""
        click.echo(f"{name+marker:<20} {_fmt_pct(a, 'current_5h_pct', color_on=color_on)} {_fmt_pct(a, 'current_7d_pct', color_on=color_on)} {status_col:<24} {last:<28}{nxt_note}{est_note}")
        # Per-model WEEKLY utilization (fable/sonnet tracking). Shown as a compact
        # sub-line only for accounts that have any, sorted highest-first so a
        # model nearing its own weekly cap is the first thing the eye lands on.
        # 5h is intentionally absent — the API has no per-model 5h window.
        pm = a.get("per_model_weekly_pct") or {}
        if pm:
            # Mark per-model numbers stale (dim + trailing '~') for any account
            # we couldn't freshly observe — a token_stale account's cached
            # `Fable=100%` printed bare read as an authoritative current value
            # and drove a wrong swap (2026-07-05 incident). See _fmt_model_pct.
            parts = "  ".join(f"{m}={_fmt_model_pct(a, p, color_on=color_on)}"
                              for m, p in sorted(pm.items(), key=lambda kv: -kv[1]))
            click.echo(f"{'':<20}   └ 7d by model: {parts}")
    click.echo()

    # Lanes (per_session): lane → account → live pids, with any orphan dirs.
    # "Lane" is the operator-facing name (GH #109 Phase 4); the slot-N
    # identifiers underneath stay — they're on-disk dir names and live
    # sessions' CLAUDE_CONFIG_DIR paths, so renaming THEM is a breaking
    # migration (tracked separately), not a display fix.
    slots_state = state.get("slots", {}) or {}
    slot_dirs_on_disk = list_slot_dirs()
    if slots_state or slot_dirs_on_disk:
        click.echo("Lanes:")
        seen = set()
        locked_slot_names = _locked_slots(config)
        # GH #109 independent-login annotation. Stay invisible until the feature
        # is actually in use — either the Phase 2 gate is on, or at least one
        # login has been provisioned — so users not using it see no new column.
        il_cfg = config.get("independent_logins", {})
        il_flag = il_cfg.get("use_independent_logins", False)
        il_visible = (il_flag or bool(list_provisioned_logins())
                      or any(list_login_families(a) for a in state["accounts"]))
        for d in slot_dirs_on_disk:
            seen.add(d.name)
            entry = slots_state.get(d.name, {})
            pids = mount_pids(d)
            live_col = click.style(f"live ({len(pids)} pids)", fg="green") if pids else "idle"
            lock_col = click.style("  🔒locked", fg="yellow") if d.name in locked_slot_names else ""
            # Only surface the pool when it's the non-default (standard) — a
            # premium/default slot line stays uncluttered.
            pool = _slot_pool(state, d.name, config)
            pool_col = click.style(f"  [{pool}]", fg="cyan") if pool != config.get("per_session", {}).get("default_pool", "premium") else ""
            orphan = "" if d.name in slots_state else click.style("  [orphan — not in state]", fg="yellow")
            login_col = ""
            if il_visible and entry.get("account"):
                acct = entry["account"]
                _lease = slot_leased_family(state, d.name)
                if _lease and _lease[0] == acct:
                    # Pool model: this lane is running a borrowed backup family.
                    login_col = click.style(f"  [pool {_lease[1]}]", fg="green")
                elif list_login_families(acct):
                    # Account has a pool to borrow from → covered, not "missing".
                    # (This lane holds the primary; it can claim a family if it
                    # ever needs to double-book.)
                    login_col = click.style("  [pool avail]", fg="green")
                elif has_independent_login(acct, d.name):
                    login_col = click.style("  [indep-login ✓]", fg="green")
                elif il_flag:
                    # Gate is ON but no login provisioned → this swap will fall
                    # back to the shared-snapshot copy (clobber path). Actionable.
                    login_col = click.style("  [indep-login MISSING]", fg="yellow")
                else:
                    login_col = click.style("  [copy]", fg="cyan")
            click.echo(f"  {d.name:<10} account={entry.get('account') or '(empty)':<12} {live_col}{lock_col}{pool_col}{login_col}{orphan}")
        for name in sorted(set(slots_state) - seen):
            click.echo(f"  {name:<10} " + click.style("[state entry, dir missing]", fg="yellow"))
        click.echo()

    # Login pools (GH #109 pool model): per-account backup-family depth + how
    # many are currently free (not leased to a live lane). Stays invisible until
    # a pool exists, so users not using the feature see nothing new.
    pool_rows = [(n, list_login_families(n)) for n in sorted(state["accounts"])]
    pool_rows = [(n, fams) for n, fams in pool_rows if fams]
    if pool_rows:
        want = config.get("independent_logins", {}).get("pool_size", 3)
        gate_note = "" if config.get("independent_logins", {}).get("use_independent_logins", False) \
            else click.style("  (gate OFF — swaps still copy; set use_independent_logins: true)", fg="yellow")
        click.echo(f"Login pools (independent backup families):{gate_note}")
        for n, fams in pool_rows:
            free = [f for f in fams if f not in leased_families(n, state)]
            short = "" if len(fams) >= want else click.style(f"  (< pool_size {want})", fg="yellow")
            click.echo(f"  {n:<12} {len(fams)} family(ies), {len(free)} free{short}")
        click.echo()

    # Locks
    pins = config.get("session_locks", {}).get("pinned", {}) or {}
    patterns = config.get("session_locks", {}).get("never_restart_patterns", []) or []
    locked_slots_cfg = sorted(_locked_slots(config))
    if pins or patterns or locked_slots_cfg:
        click.echo("Locks:")
        for k, v in pins.items():
            click.echo(f"  pinned: {k} -> {v}")
        for s in locked_slots_cfg:
            click.echo(f"  locked slot: {s}")
        for p in patterns:
            click.echo(f"  never_restart: {p}")
        click.echo()

    # Live sessions (from sessions.log + stops.log)
    # GH #56: the `account` field records where a session was registered at
    # SessionStart, NOT where it actually is now. In background-swap mode every
    # unpinned session follows the single global credential file on its next
    # request, so its true account is the machine-active one. Show that truth
    # (and note the SessionStart drift) rather than the stale recorded value,
    # which read as misleading "this session is on a different account" rows.
    live = find_live_sessions()
    if live:
        machine_active = state.get("active", "?")
        hot_swap_on = config.get("hot_swap", {}).get("enabled", False)
        click.echo(f"Live sessions ({len(live)}):")
        for s in live:
            # per_session: the pane's own mount (via /proc) is ground truth —
            # slot sessions show pane → slot → account; anything on the global
            # mount is flagged `bare` (observe-only in this mode, 3.4).
            # Background mode: every session follows the global creds → its true
            # account is machine_active (pins are advisory swap-policy only, they
            # do NOT route creds per-session — see session_is_pinned). Hot-swap
            # mode: a pane keeps its loaded creds until relaunch, so the recorded
            # SessionStart account is the better estimate.
            if per_session:
                mnt = pane_mount_name(s.pane)
                if mnt and mnt.startswith(SLOT_PREFIX):
                    eff = state.get("slots", {}).get(mnt, {}).get("account") or s.account
                    where = f"slot={mnt:<8}"
                elif mnt:  # account-dir launch (relogin flow)
                    eff = mnt.removeprefix("account-")
                    where = f"mount={mnt} "
                else:
                    eff = machine_active
                    where = click.style("bare      ", fg="yellow")
                click.echo(f"  {s.session_id[:8]}  account={eff:<10} pane={s.pane:<8} {where} cwd={s.cwd}")
                continue
            eff = s.account if hot_swap_on else machine_active
            drift = "" if eff == s.account else f" (start:{s.account})"
            click.echo(f"  {s.session_id[:8]}  account={eff:<10} pane={s.pane:<8}{drift} cwd={s.cwd}")
        click.echo()

    history = state.get("swap_history", [])
    if history:
        click.echo(f"Recent swaps ({min(5, len(history))} of {len(history)}):")
        for entry in history[-5:]:
            click.echo(f"  {entry['ts']}  {entry['from']} -> {entry['to']}  ({entry.get('trigger', 'unknown')})")


# --------------------------------------------------------------------------
# `cus sessions` — one-shot per-pane → slot → account → binding-limit view
# --------------------------------------------------------------------------
# Motivation (GH #137): diagnosing "3 panes maxed out" used to take ~6 manual
# probes (status, slot list, force-poll, /proc greps) AND `cus status`'s Live-
# sessions column showed the WRONG account — it printed the sessions.log launch-
# time label (or, in background/hybrid mode, machine_active) for panes that were
# actually mounted on a different slot's account. This command resolves the TRUE
# mount from live /proc ground truth and folds usage + pool + the binding
# constraint into a single read-only table. See docs/DIAGNOSTICS.md.


def _session_binding(acct: dict, pool: str, config: dict) -> tuple[str, str]:
    """Plain-words BINDING constraint for a lane's account under its pool.

    Returns (severity, text) with severity in {"blocked", "warn", "ok"}:
      - "blocked": the lane cannot make progress on some axis right now — 5h/7d
        saturated, a dead/expired token, or (premium pool only) a per-model
        weekly cap over the gate. This is the wall a maxed-out pane is hitting.
      - "warn": at/over a ladder step — the daemon wants to swap this lane away
        soon, but it still works right now.
      - "ok": headroom on every axis the config enforces.

    Two-dimensional exhaustion (see docs/DIAGNOSTICS.md): the 5h/7d windows and
    the per-model weekly budget are INDEPENDENT walls. A premium lane under its
    5h cap can still be blocked by a Fable weekly cap; a STANDARD lane ignores
    the model gate entirely (its work doesn't touch the exhausted model — see
    _config_for_pool), so we surface the number but say it's ignored rather than
    hide it. Pure function of (account-state dict, pool, config) so the daemon-
    less unit tests can exercise every branch without /proc or tmux.
    """
    # Blocker flags first — a stale/expired token means the stored % may be a
    # lie (last-known-good, possibly hours old), so the token state IS the
    # binding fact, not whatever number happens to be cached.
    if acct.get("token_expired"):
        return ("blocked", "token expired (no live creds — relogin needed)")
    if acct.get("poll_error"):
        return ("blocked", "poll error (usage unknown)")
    if acct.get("rate_limited"):
        return ("blocked", "rate-limited (429)")

    five = acct.get("current_5h_pct", 0.0) or 0.0
    seven = acct.get("current_7d_pct", 0.0) or 0.0
    # Effective % honors thresholds.{five_hour,seven_day} exactly like the
    # ladder logic (current_max_pct / _account_effective_pct), so this reads the
    # same window the daemon actually swaps on. Label it by whichever window is
    # the higher of the two enabled ones for an at-a-glance "which wall".
    eff = _account_effective_pct(acct, config)
    win = "5h" if five >= seven else "7d"

    # 5h/7d hard wall — never_swap_to_pct is the "fully saturated, do not use"
    # line (default 100). At/over it the lane is churning context and re-tripping
    # 429s; that's the maxed-out signature the operator is chasing.
    full_line = config.get("never_swap_to_pct", 100)
    if eff >= full_line:
        return ("blocked", f"{win} {eff:.0f}% (hard cap {full_line:.0f}%)")

    # Per-model weekly gate. Only PREMIUM lanes honor it; standard lanes treat a
    # model-exhausted account as still usable (aggregate headroom serves
    # standard-model work). Gate must be enabled in config for this to bind.
    gate_enabled = config.get("per_model_weekly", {}).get("gate_enabled", False)
    pm = acct.get("per_model_weekly_pct") or {}
    top_model, top_pct = (max(pm.items(), key=lambda kv: kv[1]) if pm else (None, 0.0))
    model_cap = _model_weekly_cap_for_config(config)
    # A token_stale account passes the blocker-flag checks above (token_stale is
    # NOT among them — its 5h is still last-known-good), so it reaches here with
    # a cached per_model_weekly_pct that may be hours old. Never let a STALE
    # per-model % drive a hard "premium gate" block: that is exactly the verdict
    # that moved a live Fable session off an account which actually had Fable
    # headroom (2026-07-05 incident). Treat stale as unknown — skip the gate and
    # surface the number marked '~' in the headroom line below instead.
    model_stale = _model_pct_is_stale(acct)
    if gate_enabled and not model_stale and top_model is not None and top_pct >= model_cap:
        if pool == "standard":
            # Surface the number but make clear it does NOT bind this lane.
            return ("ok", f"ok; weekly-{top_model} {top_pct:.0f}% ignored (standard pool)")
        return ("blocked", f"weekly-{top_model} {top_pct:.0f}% >= {model_cap:.0f}% (premium gate)")

    # Graduated ladder pressure (swap-away, not a hard block). The highest
    # tripped step is what the daemon reacts to.
    steps = config.get("thresholds", {}).get("steps") or [50, 75, 90]
    tripped = [s for s in steps if eff >= s]
    if tripped:
        return ("warn", f"{win} {eff:.0f}% >= ladder step {max(tripped):.0f}% (swap-away due)")

    # Headroom on every enforced axis.
    txt = f"ok, headroom (5h {five:.0f}%, 7d {seven:.0f}%"
    if pm:
        # _fmt_model_pct marks the number '~' when it's stale (token_stale et
        # al.) so it never reads as an authoritative current value (2026-07-05).
        txt += f", {top_model} {_fmt_model_pct(acct, top_pct)}"
        if gate_enabled and pool == "standard":
            txt += " [std: model gate off]"
        elif model_stale:
            # Stale per-model was skipped by the gate above — say so, so the
            # operator repolls before trusting it rather than reading "ok" as
            # "Fable confirmed clear".
            txt += " [stale — repoll to confirm]"
    txt += ")"
    return ("ok", txt)


def _resolve_mount_account(mount: str | None, state: dict) -> tuple[str | None, str]:
    """Resolve a live mount's TRUE account + the evidence source used.

    Ground-truth ladder for GH #137, strongest evidence first:
      1. on-disk oauthAccount — the slot/account dir's .claude.json is what
         Claude Code actually authenticates as, independent of BOTH sessions.log
         (launch-time label) and state.slots (the daemon's record, which itself
         lags an in-place move by up to a cycle). Reverse-mapped to an account
         name via each account's canonical snapshot identity.
      2. state.slots[mount].account — the daemon's recorded occupant, used when
         the on-disk identity matches no known account (e.g. a pooled backup
         login the snapshots don't cover).
      3. account-<name> mount — a relogin-style launch where the dir IS the
         account name.

    Returns (account_or_None, source) where source is one of
    {"disk-identity", "state-slot", "account-dir", "unresolved"}.
    """
    if not mount:
        return (None, "unresolved")
    if mount.startswith("account-"):
        # A relogin/login-mount launch: the dir name is the account. Still
        # prefer the on-disk identity if it resolves (catches a mislabeled dir).
        disk = _account_for_mount_identity(slot_path(mount), state)
        if disk:
            return (disk, "disk-identity")
        return (mount.removeprefix("account-"), "account-dir")
    # Slot mount: on-disk identity wins, else the daemon's recorded occupant.
    disk = _account_for_mount_identity(slot_path(mount), state)
    if disk:
        return (disk, "disk-identity")
    recorded = (state.get("slots", {}).get(mount, {}) or {}).get("account")
    if recorded:
        return (recorded, "state-slot")
    return (None, "unresolved")


def _account_for_mount_identity(mount: Path, state: dict) -> str | None:
    """Reverse-map a live mount's ON-DISK oauthAccount to an account name.

    The slot dir's .claude.json oauthAccount is the authoritative account
    identity (the 2026-07-01 duplicate-identity incident proved meta.yaml can
    lie while oauthAccount holds truth). Matches on accountUuid/emailAddress
    against each account's canonical snapshot. Returns None when the mount has
    no readable identity or nothing matches (a login family the snapshots don't
    cover — the caller falls back to state.slots)."""
    cj = mount / ".claude.json"
    if not cj.exists():
        return None
    try:
        ident = _identity_fields(read_json(cj))
    except (json.JSONDecodeError, OSError):
        return None
    if not ident:
        return None
    for name in state.get("accounts", {}):
        if _identities_match(ident, _dir_identity(name)) is True:
            return name
    return None


def build_session_rows(resolved: list[dict], state: dict, config: dict) -> list[dict]:
    """Assemble display rows from already-resolved sessions. PURE (no I/O).

    Each `resolved` entry carries the /proc-resolved facts the command gathered:
      {session_id, pane, cwd, logged_account, mount, resolved_account, source}
    where `mount` is 'slot-N'/'account-X'/None, `logged_account` is the
    sessions.log launch-time label, and `resolved_account` is the TRUE account
    from _resolve_mount_account. Factored out so the drift + binding logic is
    unit-testable without tmux/proc (see tests/test_sessions_view.py).
    """
    default_pool = config.get("per_session", {}).get("default_pool", "premium")
    rows: list[dict] = []
    for r in resolved:
        mount = r.get("mount")
        is_slot = bool(mount) and mount.startswith(SLOT_PREFIX)
        resolved_account = r.get("resolved_account")
        acct = state.get("accounts", {}).get(resolved_account, {}) if resolved_account else {}
        pool = _slot_pool(state, mount, config) if is_slot else None

        if resolved_account and acct:
            sev, binding = _session_binding(acct, pool or default_pool, config)
        elif resolved_account:
            sev, binding = ("unknown", "no usage state for resolved account")
        else:
            sev, binding = ("unknown", "mount unresolved (bare/global — observe-only)")

        # DRIFT (GH #137): sessions.log's launch-time label disagrees with the
        # account the live mount actually resolves to. Only meaningful when we
        # resolved a real mount — a bare/global pane has no mount to compare and
        # "follows global creds" by design, which is not the #137 defect.
        logged = r.get("logged_account")
        drift = bool(mount) and bool(resolved_account) and logged != resolved_account

        rows.append({
            "session_id": r.get("session_id"),
            "pane": r.get("pane"),
            "cwd": r.get("cwd"),
            "mount": mount,
            "slot": mount if is_slot else None,
            "bare": mount is None,
            "pool": pool,
            "logged_account": logged,
            "account": resolved_account,
            "resolution_source": r.get("source", "unresolved"),
            "drift": drift,
            "five_h_pct": acct.get("current_5h_pct"),
            "seven_d_pct": acct.get("current_7d_pct"),
            "per_model_weekly": dict(acct.get("per_model_weekly_pct") or {}),
            "binding_severity": sev,
            "binding": binding,
        })
    return rows


def detect_slot_orphans(slot_pids: dict[str, int], panes_on_slot: set) -> list[dict]:
    """Slots holding live /proc pids that NO live tmux pane owns. PURE.

    Two shapes of the same leak (see docs/DIAGNOSTICS.md): a slot whose owning
    pane was closed while a subprocess kept the mount's CLAUDE_CONFIG_DIR alive,
    or a stray process carrying a slot's config dir with no pane at all. Both
    read as "the mount is in use, but nobody is driving it" — the slot won't be
    reaped by idle-gc and won't accept a fresh session cleanly.

    `slot_pids` is {slot_name: pid_count} for slots with >=1 live pid;
    `panes_on_slot` is the set of slot names a live pane currently resolves to.
    """
    return [{"slot": s, "pids": n}
            for s, n in sorted(slot_pids.items()) if s not in panes_on_slot]


@cli.command(name="sessions")
@click.option("--json", "as_json", is_flag=True, help="Emit machine-readable JSON instead of a table.")
def sessions_cmd(as_json: bool) -> None:
    """Per-pane -> slot -> account -> binding-limit view of live sessions.

    Resolves each live Claude pane's TRUE account from the live mount (/proc
    ground truth), NOT the stale sessions.log launch-time label, and shows in
    one shot: the slot it's mounted in, that account's 5h/7d/per-model usage,
    the slot's pool, and the BINDING constraint in plain words. Flags DRIFT
    (sessions.log account disagrees with the resolved mount — GH #137) and
    ORPHAN slots (live pids, no owning pane). READ-ONLY.
    """
    if not STATE_JSON.exists():
        click.echo("Not initialized. Run `cus init` first.")
        sys.exit(1)
    state = read_json(STATE_JSON)
    config = load_config()
    color_on = sys.stdout.isatty() and not as_json

    # Resolve every live session's TRUE mount from /proc, then hand the pure
    # facts to build_session_rows. pane_mount_name is the same primitive `status`
    # per_session mode trusts (~8886) — reused here across ALL modes, which is
    # the actual #137 fix (the non-per_session branches ignored the mount).
    live = find_live_sessions()
    resolved_inputs: list[dict] = []
    panes_on_slot: set[str] = set()
    for s in live:
        mount = pane_mount_name(s.pane)
        if mount and mount.startswith(SLOT_PREFIX):
            panes_on_slot.add(mount)
        acct, source = _resolve_mount_account(mount, state)
        resolved_inputs.append({
            "session_id": s.session_id,
            "pane": s.pane,
            "cwd": s.cwd,
            "logged_account": s.account,
            "mount": mount,
            "resolved_account": acct,
            "source": source,
        })
    rows = build_session_rows(resolved_inputs, state, config)

    # Orphan sweep: slots with live pids that no live pane resolves to.
    slot_pids: dict[str, int] = {}
    for d in list_slot_dirs():
        pids = mount_pids(d)
        if pids:
            slot_pids[d.name] = len(pids)
    orphans = detect_slot_orphans(slot_pids, panes_on_slot)

    if as_json:
        click.echo(json.dumps({
            "mode": config.get("mode", "global"),
            "active": state.get("active"),
            "sessions": rows,
            "orphans": orphans,
        }, indent=2, default=str))
        return

    mode = config.get("mode", "global")
    disabled_set = _disabled_accounts(config)
    click.echo(f"Mode: {mode}   Machine-active (bare-launch) account: {state.get('active', '?')}")
    click.echo()
    if not rows:
        click.echo("No live Claude sessions detected.")
    else:
        click.echo(f"Live sessions ({len(rows)}):")
        for r in rows:
            sid = (r["session_id"] or "????????")[:8]
            where = r["slot"] or (r["mount"] or click.style("bare", fg="yellow"))
            pool = f" [{r['pool']}]" if r["pool"] else ""
            acct = r["account"] or "?"
            # Mark an account the operator disabled — a lane already on it keeps
            # running (disable is future-rotation-only), so it CAN show up live;
            # the tag stops the operator wondering why a "disabled" account is in use.
            if r["account"] and r["account"] in disabled_set:
                acct = f"{acct} {click.style('[disabled]', fg='yellow')}"
            # Line 1: identity + where mounted.
            drift_tag = ""
            if r["drift"]:
                drift_tag = click.style(f"  DRIFT: sessions.log said '{r['logged_account']}'", fg="red", bold=True)
            click.echo(f"  {sid}  pane={r['pane']:<8} {str(where):<10}{pool}  account={acct}{drift_tag}")
            # Line 2: usage numbers.
            five = "?" if r["five_h_pct"] is None else f"{r['five_h_pct']:.0f}%"
            seven = "?" if r["seven_d_pct"] is None else f"{r['seven_d_pct']:.0f}%"
            pm = r["per_model_weekly"]
            pm_txt = ("  " + " ".join(f"{m}={p:.0f}%" for m, p in sorted(pm.items(), key=lambda kv: -kv[1]))) if pm else ""
            click.echo(f"            5h={five:<5} 7d={seven:<5}{pm_txt}")
            # Line 3: the binding constraint, colored by severity.
            sev = r["binding_severity"]
            color = {"blocked": "red", "warn": "yellow", "ok": "green"}.get(sev)
            label = click.style(r["binding"], fg=color) if (color_on and color) else r["binding"]
            click.echo(f"            -> {label}")
            click.echo(f"            cwd={r['cwd']}")
    click.echo()

    if orphans:
        click.echo(click.style(f"Orphan slots ({len(orphans)}) — live pids, no owning pane:", fg="yellow"))
        for o in orphans:
            click.echo(f"  {o['slot']:<10} {o['pids']} pid(s) held; no live tmux pane resolves here")
        click.echo("  -> a closed pane left a subprocess holding the mount, or a stray CLAUDE_CONFIG_DIR process.")
        click.echo("     idle-gc won't reap it and a fresh session won't mount cleanly. Investigate the pids.")
        click.echo()


@cli.command(name="check-orchestrate")
@click.option("--target", default=None, help="Pretend we're swapping to this account (for relaunch-command preview).")
def check_orchestrate_cmd(target: str | None) -> None:
    """Dry-run the hot-swap orchestrator. PRINTS what would happen without doing it.

    Use BEFORE enabling `hot_swap.enabled: true` to verify:
      - The right tmux panes are detected as having live claude
      - The session-id for each pane matches what you expect (most recent
        SessionStart per pane, not stale from earlier claude runs)
      - The relaunch command is what you want (especially `relaunch_flags`)

    After the 2026-05-19 incident, this is the safe way to validate
    orchestrator behavior on your real setup before letting the daemon
    do it autonomously.
    """
    state = load_state() if STATE_JSON.exists() else {"active": None, "accounts": {}}
    config = load_config()
    current = state.get("active") or "(no active)"
    hot = config.get("hot_swap", {})

    click.echo(click.style(f"Hot-swap dry-run for active account: {current}", bold=True))
    click.echo()

    # Query ALL live panes (regardless of recorded account). Per-pane
    # alignment is shown in the output: each pane's recorded account vs
    # current.active. Panes whose recorded account ≠ current.active are
    # "misaligned" — drift, fixed by orchestrator restart.
    live_panes = find_live_panes(account_filter=None)
    click.echo(f"Detected {len(live_panes)} pane(s) with live claude:")
    if not live_panes:
        click.echo("  (none — orchestrator would do simple cred swap with no relaunch)")
        return
    for s in live_panes:
        pane_cmd = tmux_pane_name(s.pane)
        idle = session_is_idle(s, hot.get("mid_turn_idle_seconds", 30))
        pinned, pin_reason = session_is_pinned(s, config)
        wl, wl_reason = session_matches_whitelist(s, config)
        aligned = s.account == current
        click.echo(f"  pane {s.pane}:")
        click.echo(f"    session_id: {s.session_id}")
        click.echo(f"    cwd: {s.cwd}")
        click.echo(f"    account: {s.account}{' (aligned with current)' if aligned else f' (DRIFT — current is {current})'}")
        click.echo(f"    pane_current_command: {pane_cmd}")
        click.echo(f"    started_at: {s.started_at}")
        click.echo(f"    last_stop_at: {s.last_stop_at or '(never)'}")
        click.echo(f"    idle (>{hot.get('mid_turn_idle_seconds', 30)}s since transcript write): {idle}")
        if pinned:
            click.echo(f"    PINNED: {pin_reason} → would SKIP")
        elif wl:
            click.echo(f"    WHITELISTED: {wl_reason} → would SKIP")
        elif aligned:
            click.echo(f"    aligned with current — orchestrator would SKIP (no restart needed)")
        else:
            click.echo(f"    would: orchestrate (restart to align)")

    click.echo()
    # Show planned relaunch commands — honor the same config knobs the
    # orchestrator uses.
    use_resume = hot.get("relaunch_with_resume", True)
    click.echo(click.style("Planned relaunch commands (would be typed via `tmux send-keys`):", bold=True))
    relaunch_count = 0
    for s in live_panes:
        if s.account == current:
            continue  # already aligned — no restart needed
        pinned, _ = session_is_pinned(s, config)
        wl, _ = session_matches_whitelist(s, config)
        if pinned or wl:
            continue
        # Same builder the live orchestrator uses — preview can't drift.
        cmd = build_relaunch_cmd(s, hot)
        click.echo(f"  pane {s.pane}: {cmd}")
        relaunch_count += 1
    if relaunch_count == 0:
        click.echo("  (no panes need restart — all aligned)")
    if not use_resume:
        click.echo()
        click.echo(click.style("  (relaunch_with_resume: false → no --resume; fresh sessions)", fg="yellow"))
    click.echo()
    click.echo(click.style("Tier-by-tier preview (assumes default's next_swap_at_pct → tier):", bold=True))
    active_acct = state.get("accounts", {}).get(current, {})
    inferred_tier = determine_tier(active_acct, config) if active_acct else 1
    click.echo(f"  inferred tier: {inferred_tier}")
    if inferred_tier == 1:
        click.echo("    Tier 1: idle panes → swap immediately; mid-turn panes → wait_for_stop")
    elif inferred_tier == 2:
        click.echo("    Tier 2: inject pause_message via tmux send-keys, then wait_for_stop")
    elif inferred_tier == 3:
        click.echo("    Tier 3: double-Escape to interrupt running tool, log shells to inbox.md")

    if not hot.get("enabled", False):
        click.echo()
        click.echo(click.style("Note: hot_swap.enabled is FALSE in your config.", fg="yellow"))
        click.echo("The above is purely a preview. The daemon will not execute orchestration.")
        click.echo("To enable: edit ~/claude-accounts/config.yaml → hot_swap.enabled: true")


@cli.command(name="auto-swap")
@click.argument("target", required=False)
@click.option("--trigger", default="manual-auto", help="Tag for the swap history entry.")
@click.option("--orchestrate", is_flag=True, help="Run the full hot-swap orchestrator (level 4): /exit + relaunch live tmux panes. Without this flag, level 3 (just swap creds for new sessions).")
@click.option("--tier", type=int, default=1, help="Orchestration tier (1=wait-for-stop, 2=pause-message, 3=force-interrupt). Only used with --orchestrate.")
def auto_swap_cmd(target: str | None, trigger: str, orchestrate: bool, tier: int) -> None:
    """Force-swap NOW, bypassing the threshold check.

    Two modes:
      cus auto-swap <name>      Swap to the named account explicitly.
      cus auto-swap             Swap to whatever the current `strategy`
                                picker chooses (e.g. `smart` will pick
                                the best non-blocked, 7d-safe candidate).

    Differs from `cus switch <name>` in that the no-argument mode uses the
    strategy picker to choose for you. Differs from waiting for `cus daemon`
    to act on threshold-crossing in that it acts NOW, regardless of usage %.

    Useful when you've manually paused your sessions and want to swap
    immediately without waiting for the daemon's next poll cycle (or
    when the daemon is deferring due to cache-warm / wait-for-Stop).
    """
    if not STATE_JSON.exists():
        click.echo("Not initialized. Run `cus init` first.")
        sys.exit(1)

    state = load_state()
    config = load_config()

    if target:
        if target not in state["accounts"]:
            click.echo(f"Unknown account '{target}'. Known: {sorted(state['accounts'].keys())}")
            sys.exit(1)
        chosen_name = target
        reason = f"explicit (auto-swap {target})"
    else:
        picked = pick_swap_target(state, config)
        if picked is None:
            click.echo("No valid swap target available. All non-current accounts are blocked.")
            click.echo("Run `cus sos` for diagnosis.")
            sys.exit(1)
        chosen_name = picked.name
        reason = f"strategy pick ({picked.reason})"

    current = state.get("active")
    if chosen_name == current:
        click.echo(f"{chosen_name} is already active. Nothing to do.")
        return

    click.echo(f"Swapping {current} -> {chosen_name}")
    click.echo(f"  reason: {reason}")
    click.echo(f"  mode: {'orchestrated hot-swap (level 4)' if orchestrate else 'cred swap only (level 3)'}")
    if orchestrate:
        # Build a synthetic SwapDecision and run the orchestrator. This is
        # the level-4 path the daemon would normally take when hot_swap.enabled
        # is true and a threshold trips — but here, manually invoked.
        decision = SwapDecision(target=chosen_name, reason=reason, tier=tier,
                                gate="manual", deferrable=False)
        try:
            hot_swap_orchestrate(decision, state, config)
        except Exception as e:
            click.echo(f"ERROR in orchestrator: {type(e).__name__}: {e}")
            click.echo("Falling back to plain cred swap.")
            execute_swap(chosen_name, trigger=trigger)
    else:
        try:
            execute_swap(chosen_name, trigger=trigger)
        except (FileNotFoundError, ValueError, RuntimeError) as e:
            click.echo(f"ERROR: {e}")
            sys.exit(1)
    click.echo(f"✓ Swapped. Next `claude` invocation uses {chosen_name}.")
    if not orchestrate:
        click.echo(f"  Live sessions keep their loaded tokens until refresh — restart them to immediately use {chosen_name}.")


@cli.command()
@click.argument("target")
@click.option("--dry-run", is_flag=True, help="Show what would happen without writing anything.")
@click.option("--trigger", default="manual", help="Tag for the swap history entry.")
@click.option("--force", is_flag=True, help="Swap even onto an account a live slot already holds (GH #104: normally refused — two live mounts on one account log one out).")
def switch(target: str, dry_run: bool, trigger: str, force: bool) -> None:
    """Atomically swap the shared ~/.claude/ mount to a different account.

    Delegates to execute_swap() which is the same primitive the daemon uses.
    Both paths use atomic_copy / atomic_write_bytes for all credential
    writes — no partial-content window readable by Claude mid-swap.

    Walk-back: a single failed swap is rewound by `cus switch <previous>`.
    Every swap is appended to state.json's swap_history.
    """
    if not STATE_JSON.exists():
        click.echo("Not initialized. Run `cus init` first.")
        sys.exit(1)

    state = load_state()
    if target not in state["accounts"]:
        click.echo(f"Unknown account '{target}'. Known: {sorted(state['accounts'].keys())}")
        sys.exit(1)

    current = state["active"]
    if target == current:
        click.echo(f"{target} is already active. Nothing to do.")
        return

    # Duplicate-mount guard (GH #104): putting the shared mount on an account a
    # LIVE slot already holds makes two live mounts share one account — their
    # single-use OAuth refresh tokens rotate and one session gets logged out
    # (this is exactly what happened 2026-07-02). Refuse unless --force.
    slot_owner = occupied_slot_accounts(state)  # account -> [live slot names]
    if target in slot_owner and not force:
        click.echo(click.style(
            f"refusing: a live slot ({', '.join(slot_owner[target])}) already runs '{target}'. "
            f"Putting it on the shared mount too would rotate its refresh token and log one session out "
            f"(GH #104). Pick an account no slot holds, or `--force` if you really mean it.", fg="red"))
        sys.exit(1)

    target_dir = ACCOUNTS_DIR / f"account-{target}"
    current_dir = ACCOUNTS_DIR / f"account-{current}"

    if dry_run:
        target_cj_path = target_dir / ".claude.json"
        target_creds_path = target_dir / ".credentials.json"
        if not target_creds_path.exists() or not target_cj_path.exists():
            click.echo(f"ERROR: Required files missing in {target_dir}. Re-run `cus init --force`.")
            sys.exit(1)
        try:
            live_cj = read_json(CLAUDE_JSON)
        except json.JSONDecodeError as e:
            click.echo(f"ERROR: {CLAUDE_JSON} is unparseable ({e}). Aborting.")
            sys.exit(2)
        target_account_cj = read_json(target_cj_path)
        target_identity = {k: target_account_cj[k] for k in ACCOUNT_BOUND_KEYS if k in target_account_cj}
        click.echo(f"(dry-run) Swap plan: {current} -> {target}")
        click.echo(f"  1. Save current identity from ~/.claude.json into {current_dir}/.claude.json")
        for k in ACCOUNT_BOUND_KEYS:
            if k in live_cj:
                preview = json.dumps(live_cj[k])[:60]
                click.echo(f"       {k}: {preview}...")
        click.echo(f"  2. Save current credentials from {CREDS_JSON} into {current_dir}/.credentials.json")
        click.echo(f"  3. Merge into live ~/.claude.json:")
        for k, v in target_identity.items():
            preview = json.dumps(v)[:60]
            click.echo(f"       {k}: {preview}...")
        click.echo(f"  4. Replace live {CREDS_JSON} with {target_dir}/.credentials.json")
        click.echo(f"  5. Mark state.json active = {target}")
        return

    try:
        execute_swap(target, trigger=trigger)
    except (FileNotFoundError, ValueError, RuntimeError) as e:
        click.echo(f"ERROR: {e}")
        sys.exit(1)

    click.echo(f"Swapped: {current} -> {target}")
    click.echo(f"  ~/.claude.json: userID + oauthAccount updated")
    click.echo(f"  ~/.claude/.credentials.json: replaced")
    click.echo(f"  Next claude invocation will use {target}.")


# --------------------------------------------------------------------------
# Phase 2 commands: poll, daemon, hooks
# --------------------------------------------------------------------------

@cli.command()
@click.option("--account", default=None, help="Poll just one account (default: all).")
@click.option("--no-write", is_flag=True, help="Don't update state.json — just print the response.")
def poll(account: str | None, no_write: bool) -> None:
    """One-shot usage poll. Useful for testing or cron-driven setups.

    Calls the Anthropic OAuth usage endpoint per account (same endpoint
    Claude Code itself uses for `/usage`). Updates state.json with the
    results unless `--no-write`.
    """
    if not STATE_JSON.exists():
        click.echo("Not initialized. Run `cus init` first.")
        sys.exit(1)

    state = load_state()
    config = load_config()
    targets = [account] if account else list(state["accounts"].keys())

    usage_by_account: dict[str, AccountUsage] = {}
    for name in targets:
        in_backoff, until = account_in_backoff(state, name)
        if in_backoff:
            click.echo(f"skipping {name}: in poll-backoff until {until} (avoid hammering Anthropic's per-IP throttle)")
            continue
        click.echo(f"polling {name}...")
        u = poll_account_usage(name)
        usage_by_account[name] = u
        # GH #68: token_stale must be checked BEFORE the generic raw["error"]
        # branch. poll_account_usage sets raw["error"] alongside token_stale
        # (the raw text is what an operator sees in --json dumps), so without
        # this branch a benign aged-out access token on an inactive account
        # fell through to the scary "ERROR: ..." line. That mislabel caused a
        # real operator to re-login a perfectly healthy account. force-poll
        # already had the friendly branch; this mirrors it. The account stays
        # swap-eligible (pick_swap_target only excludes token_expired /
        # poll_error), so say so explicitly to preempt the "will it ever swap
        # to it?" worry that GH #68 documents.
        if u.token_stale:
            minutes = u.raw.get("expired_minutes_ago", "?")
            click.echo(f"  TOKEN_STALE — stored access token expired {minutes}m ago (refresh token still valid; account remains usable and swap-eligible).")
            continue
        if u.token_expired:
            # GH #77 auto-heal hint: for the ACTIVE account, poll reads the
            # LIVE file — if the snapshot holds strictly newer tokens, the
            # user already re-logged in under the storage dir and only the
            # snapshot→live sync is missing. Without this branch the generic
            # advice sends them into a second (useless) re-login loop.
            if state.get("active") == name and _snapshot_fresher_than_live(name):
                click.echo(f"  TOKEN EXPIRED (live file) — but the '{name}' snapshot is FRESHER: "
                           f"a storage-dir re-login already happened. Run `cus relogin {name} --finish` "
                           f"to install it live (GH #77).")
                continue
            click.echo(f"  TOKEN EXPIRED — re-auth this account (`cus relogin {name}` shows the right command)")
            continue
        if u.raw.get("error"):
            click.echo(f"  ERROR: {u.raw['error']}")
            continue
        fh = f"{u.five_hour.utilization:.1f}%" if u.five_hour else "—"
        sd = f"{u.seven_day.utilization:.1f}%" if u.seven_day else "—"
        click.echo(f"  5h: {fh}    7d: {sd}    polled_at: {u.polled_at}")

    if not no_write:
        # GH #75: re-load state after the slow network loop above so a swap
        # that landed mid-poll isn't reverted by saving a stale snapshot
        # (same rationale as the daemon's one_cycle re-load).
        state = load_state()
        update_state_with_usage(state, usage_by_account)
        maybe_reset_thresholds(state, config)
        save_state(state)
        click.echo()
        click.echo(f"state.json updated.")


@cli.command(name="force-poll")
@click.argument("account", required=True)
def force_poll(account: str) -> None:
    """Poll an account immediately, bypassing the 429 backoff window.

    Use when you've verified Anthropic's per-IP throttle on /api/oauth/usage
    has cleared (e.g. another machine stopped polling) and you want a fresh
    read without waiting for the backoff to expire. Resets the backoff
    counter on success; does NOT reset on 429 (the next 429 starts a fresh
    backoff cycle). See GH #9.
    """
    if not STATE_JSON.exists():
        click.echo("Not initialized. Run `cus init` first.")
        sys.exit(1)
    state = load_state()
    if account not in state.get("accounts", {}):
        click.echo(f"Unknown account: {account}. Known: {', '.join(state.get('accounts', {}).keys())}")
        sys.exit(1)

    acct = state["accounts"][account]
    backoff_until = acct.get("poll_backoff_until_ts")
    consecutive = acct.get("poll_backoff_consecutive_429s", 0)
    if backoff_until:
        click.echo(f"Bypassing backoff for {account} (was: until {backoff_until}, {consecutive} consecutive 429s)")
    else:
        click.echo(f"No backoff active for {account}; polling anyway")

    click.echo(f"polling {account}...")
    u = poll_account_usage(account)
    config = load_config()
    # GH #75: the HTTP call above can take up to USAGE_API_TIMEOUT_SECONDS;
    # re-load state (and re-bind acct) so the saves below don't revert an
    # `active`/history update committed by a concurrent swap mid-poll.
    state = load_state()
    if account not in state.get("accounts", {}):
        click.echo(f"  account '{account}' disappeared from state.json mid-poll; not saving")
        sys.exit(4)
    acct = state["accounts"][account]
    if u.token_stale:
        # Reflect the stale flag in state first, so the Fix 4 un-stale helper
        # (which gates on acct['token_stale']) can act on it.
        update_state_with_usage(state, {account: u})
        # Fix 4 (2026-07-07): force-poll is the operator's explicit "revive it"
        # lever. Before accepting the stale flag, try to un-stale via a DIRECT
        # OAuth refresh grant (no session, no billed tokens) — writes a fresh
        # access token into the account snapshot and clears token_stale — then
        # re-poll for live numbers. force=True bypasses the burnout cooldown (an
        # operator explicitly asked). A DEAD refresh token can't be revived → keep
        # the stale message + exit (that one needs a browser relogin).
        if _unstale_account_snapshot(account, state, config, force=True):
            save_state(state)  # persist the cleared flag + fresh snapshot at once
            click.echo(f"  UN-STALED {account} — minted a fresh access token via a direct OAuth "
                       f"refresh grant and cleared token_stale; re-polling for live numbers.")
            u = poll_account_usage(account)
            state = load_state()
            if account not in state.get("accounts", {}):
                click.echo(f"  account '{account}' disappeared from state.json mid-repoll; not saving")
                sys.exit(4)
            acct = state["accounts"][account]
        if u.token_stale:
            # Un-stale failed (dead refresh token) OR the re-poll is somehow still
            # stale — don't loop; record and exit with the recoverable-stale note.
            minutes = u.raw.get("expired_minutes_ago", "?")
            click.echo(f"  TOKEN_STALE — stored access token expired {minutes}m ago (refresh token "
                       f"still valid; account remains usable). A direct refresh grant could not revive "
                       f"it now; if it persists, re-login: cus relogin {account}.")
            update_state_with_usage(state, {account: u})
            save_state(state)
            sys.exit(0)
    if u.token_expired:
        # GH #77 auto-heal hint (same as `cus poll`): a fresher snapshot means
        # the re-login already happened — only the snapshot→live sync is missing.
        if state.get("active") == account and _snapshot_fresher_than_live(account):
            click.echo(f"  TOKEN EXPIRED (live file) — but the '{account}' snapshot is FRESHER: "
                       f"run `cus relogin {account} --finish` to install it live (GH #77).")
        else:
            click.echo(f"  TOKEN EXPIRED — re-auth this account (`cus relogin {account}` shows the right command)")
        update_state_with_usage(state, {account: u})
        save_state(state)
        sys.exit(2)
    if u.raw.get("error") == "rate_limited":
        click.echo("  STILL 429 — Anthropic throttle still engaged. Backoff cycle restarted.")
        update_backoff(acct, success=False, config=config)
        update_state_with_usage(state, {account: u})
        save_state(state)
        sys.exit(3)
    if u.raw.get("error"):
        click.echo(f"  ERROR: {u.raw['error']}")
        sys.exit(4)
    fh = f"{u.five_hour.utilization:.1f}%" if u.five_hour else "—"
    sd = f"{u.seven_day.utilization:.1f}%" if u.seven_day else "—"
    click.echo(f"  5h: {fh}    7d: {sd}    polled_at: {u.polled_at}")
    click.echo("  Backoff cleared.")
    update_state_with_usage(state, {account: u})
    maybe_reset_thresholds(state, config)
    save_state(state)


def _known_account_names() -> set[str]:
    """Every account name cus knows about, from any of its three ledgers.

    disable/enable must accept a name the operator can plausibly reference even
    if it isn't in every ledger yet:
      - state.json accounts  — has been polled at least once,
      - config.yaml accounts — registered for strategy/priority,
      - account-<name>/ dir  — a snapshot exists on disk (e.g. freshly `cus add`ed
        but never polled, or the poisoned rayi1 whose canonical store is bad).
    Union of all three, so `cus disable rayi1` works the moment the dir exists.
    """
    names: set[str] = set()
    try:
        names |= set(load_state().get("accounts", {}).keys())
    except (json.JSONDecodeError, OSError):
        pass
    if CONFIG_YAML.exists():
        try:
            for a in read_yaml(CONFIG_YAML).get("accounts", []):
                if isinstance(a, dict) and a.get("name"):
                    names.add(str(a["name"]))
        except (yaml.YAMLError, OSError):
            pass
    if ACCOUNTS_DIR.exists():
        for d in ACCOUNTS_DIR.glob("account-*"):
            if d.is_dir():
                names.add(d.name[len("account-"):])
    return names


def _live_lanes_for_account(name: str) -> list[str]:
    """Where `name` is currently mounted live (shared active + occupied slots).

    Used to WARN on `cus disable`: disabling governs FUTURE rotation only — it
    never evicts a lane that is already running on the account (a locked slot
    especially must be untouched). This just tells the operator which live
    mounts will keep using the account until they exit on their own. Best-effort
    / degrade-to-empty; never blocks the disable on an I/O hiccup.
    """
    try:
        state = load_state()
    except (json.JSONDecodeError, OSError):
        return []
    where: list[str] = []
    if state.get("active") == name:
        where.append("shared mount (active)")
    try:
        for slot in occupied_slot_accounts(state).get(name, []):
            where.append(slot)
    except Exception:
        pass
    return where


@cli.command()
@click.argument("account", required=True)
@click.option("--reason", default=None, help="Free-text note on WHY this account is disabled (e.g. 'poisoned canonical store — needs relogin'). Shown in `cus status` and the disabled-account SOS line.")
def disable(account: str, reason: str | None) -> None:
    """Take an account OUT of rotation, persistently and reversibly.

    Motivating case (2026-07-06): rayi1's canonical store got clobbered with
    ANOTHER account's identity, so polling it returns the WRONG account's usage
    and it cannot be trusted as a swap target until a browser relogin repairs
    it. `cus disable rayi1` removes it cleanly until then; `cus enable rayi1`
    puts it back.

    What disabling does (writes `disabled: true` to the account's entry in
    config.yaml — the durable config, so it survives daemon restarts):
      - the account is NEVER chosen as a swap/launch target, for any strategy,
        even as the only candidate and with no degraded-pool fallback (operator
        mandate outranks "swap somewhere rather than sit hot") — see
        `_disabled_accounts` / `pick_swap_target`;
      - it stops counting toward premium-headroom "valid target" scarcity math;
      - any outstanding URGENT/WARNING SOS for it (poisoned canonical store,
        token expired, ...) is DOWNGRADED to one soft INFO line — a deliberately
        parked account should not scream.

    What disabling deliberately does NOT do — it governs FUTURE rotation only:
      - it does not delete the account dir, credentials, or state;
      - it does not move or evict a lane already mounted on the account (a
        locked slot especially is left alone) — those keep running until they
        exit on their own;
      - it does not stop `cus force-poll <account>` (so you can check the
        account after a relogin, before `cus enable`).

    Backward-compatible: absent/false key ⇒ enabled, so existing configs are
    unaffected. Idempotent: disabling an already-disabled account is a friendly
    no-op. Reverse with `cus enable <account>`.
    """
    if not CONFIG_YAML.exists():
        click.echo("Not initialized. Run `cus init` first.")
        sys.exit(1)
    known = _known_account_names()
    if account not in known:
        click.echo(click.style(
            f"Unknown account '{account}'. Known: {', '.join(sorted(known)) or '(none)'}", fg="red"))
        sys.exit(1)

    user_cfg = read_yaml(CONFIG_YAML)
    accounts = user_cfg.setdefault("accounts", [])
    entry = next((a for a in accounts if isinstance(a, dict) and a.get("name") == account), None)
    if entry is None:
        # The account exists on disk / in state but was never registered in
        # config.yaml's accounts list (the disabled flag has to live SOMEWHERE
        # the picker reads it — `_disabled_accounts` scans exactly this list).
        # Add a minimal entry, mirroring the `cus init` auto-registration path.
        entry = {"name": account, "priority": 1}
        accounts.append(entry)

    if entry.get("disabled"):
        # Idempotent no-op — do not re-stamp disabled_at or clobber the original
        # reason (that history is more useful than "re-disabled just now").
        prior = entry.get("disabled_reason")
        click.echo(f"{account} is already disabled"
                   + (f" (reason: {prior})" if prior else "")
                   + f". Re-enable with `cus enable {account}`.")
        return

    entry["disabled"] = True
    if reason:
        entry["disabled_reason"] = reason
    entry["disabled_at"] = now_iso()
    write_yaml(CONFIG_YAML, user_cfg)

    click.echo(click.style(f"Disabled '{account}' — it is now OUT of rotation.", fg="yellow", bold=True))
    if reason:
        click.echo(f"  reason: {reason}")
    click.echo(f"  The daemon will never swap or launch ONTO '{account}' until you re-enable it.")
    click.echo(f"  Re-enable with: cus enable {account}")
    # Live-lane warning: disable is future-rotation-only, so a lane already on
    # the account keeps running — say so, or the operator wonders why a live
    # session is still using a "disabled" account.
    live = _live_lanes_for_account(account)
    if live:
        click.echo(click.style(
            f"  note: '{account}' is CURRENTLY live on: {', '.join(live)}. "
            f"These are left running (disable governs future rotation only); "
            f"they'll release the account when they exit.", fg="yellow"))
    # The daemon re-reads config every cycle, but a systemd-managed daemon does
    # not pick up mid-cycle file edits instantly — nudge a restart for immediacy.
    click.echo("  (running daemon picks this up next cycle; `systemctl --user restart cus.service` to apply now.)")


@cli.command()
@click.argument("account", required=True)
def enable(account: str) -> None:
    """Put a previously `cus disable`d account BACK into rotation.

    Clears the `disabled` flag (and its `disabled_reason` / `disabled_at`
    companions) from the account's config.yaml entry. Idempotent: enabling an
    account that is already enabled is a friendly no-op. Errors if the account
    name is unknown.
    """
    if not CONFIG_YAML.exists():
        click.echo("Not initialized. Run `cus init` first.")
        sys.exit(1)
    known = _known_account_names()
    if account not in known:
        click.echo(click.style(
            f"Unknown account '{account}'. Known: {', '.join(sorted(known)) or '(none)'}", fg="red"))
        sys.exit(1)

    user_cfg = read_yaml(CONFIG_YAML)
    accounts = user_cfg.get("accounts", []) or []
    entry = next((a for a in accounts if isinstance(a, dict) and a.get("name") == account), None)
    if entry is None or not entry.get("disabled"):
        # Absent entry or absent/false key both mean "already in rotation" — a
        # no-op, not an error (backward-compat: unknown/absent key ⇒ enabled).
        click.echo(f"{account} is already enabled (in rotation). Nothing to do.")
        return

    entry.pop("disabled", None)
    entry.pop("disabled_reason", None)
    entry.pop("disabled_at", None)
    write_yaml(CONFIG_YAML, user_cfg)
    click.echo(click.style(f"Enabled '{account}' — it is back IN rotation.", fg="green", bold=True))
    click.echo("  (running daemon picks this up next cycle; `systemctl --user restart cus.service` to apply now.)")


# GH #76: fd holding the daemon single-instance flock. Module-level on
# purpose — the lock must live exactly as long as the process, and a local
# would be garbage-collected (closing the fd releases the flock).
_DAEMON_SINGLETON_FD: int | None = None


def _acquire_daemon_singleton() -> bool:
    """Claim the machine-wide "I am THE cus daemon" slot (GH #76 problem 3).

    Takes a non-blocking exclusive flock on daemon.pid and keeps the fd open
    for the process lifetime. A second persistent daemon (systemd unit +
    manual launch, double-enabled units, ...) finds the lock held and refuses
    to start instead of silently doubling every race window.

    Why flock instead of "read pid, kill -0": the probe approach races
    (two starters can both see a dead pid and both proceed) and misfires on
    pid reuse; a flock dies with its process, so it is both race-free and
    self-cleaning after a crash. The pid is still WRITTEN into the file so
    `cus sos` diagnostics and humans can see who holds it.

    Returns True when the slot was acquired (or when the guard can't run at
    all because the file is unopenable — preserving the pre-#76 lenient
    behavior of a best-effort pid write rather than refusing to start).
    """
    global _DAEMON_SINGLETON_FD
    try:
        DAEMON_PID.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(str(DAEMON_PID), os.O_CREAT | os.O_RDWR, 0o644)
    except OSError:
        return True  # can't guard → behave like the old best-effort pid write
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        try:
            os.lseek(fd, 0, 0)
            holder = os.read(fd, 64).decode(errors="replace").strip() or "(unknown)"
        except OSError:
            holder = "(unknown)"
        try:
            os.close(fd)
        except OSError:
            pass
        click.echo(f"Another cus daemon is already running (pid {holder}, flock held on {DAEMON_PID}). "
                   f"Refusing to start a second instance — two daemons double every swap-race window (GH #76). "
                   f"Check `systemctl --user status cus.service`.", err=True)
        return False
    try:
        os.ftruncate(fd, 0)
        os.write(fd, f"{os.getpid()}\n".encode())
    except OSError:
        pass
    _DAEMON_SINGLETON_FD = fd
    return True


def _release_daemon_singleton() -> None:
    """Release the single-instance slot (tests; symmetric cleanup)."""
    global _DAEMON_SINGLETON_FD
    if _DAEMON_SINGLETON_FD is None:
        return
    try:
        fcntl.flock(_DAEMON_SINGLETON_FD, fcntl.LOCK_UN)
    except OSError:
        pass
    try:
        os.close(_DAEMON_SINGLETON_FD)
    except OSError:
        pass
    _DAEMON_SINGLETON_FD = None


@cli.command()
@click.option("--once", is_flag=True, help="Run a single poll-decide-act cycle and exit.")
@click.option("--foreground", is_flag=True, default=True, help="Run in foreground (default; for systemd, tmux pane, etc.).")
@click.option("--no-execute", is_flag=True, help="Decide but don't actually swap. Useful for dry-run testing.")
def daemon(once: bool, foreground: bool, no_execute: bool) -> None:
    """Run the auto-rotation daemon.

    Loop:
      1. Poll all accounts' usage via the OAuth API.
      2. Update state.json.
      3. Decide whether to swap (progressive thresholds, strategy picker).
      4. Execute the swap (Phase 2: simple swap; Phase 3+: hot-swap orchestration).
      5. Sleep `poll_interval_seconds` from config.

    Crashes write a stack trace to ~/claude-accounts/daemon.log and exit
    non-zero. Restarting picks up from state.json — no in-memory state lost.

    Use --once for cron-driven setups: schedule cus daemon --once every
    5 minutes via crontab and skip running a persistent process.
    """
    if not STATE_JSON.exists():
        click.echo("Not initialized. Run `cus init` first.")
        sys.exit(1)

    # Daemon writes its pid so `cus sos` can detect "pid recorded but gone".
    # GH #35: only the long-running daemon (systemd / foreground) should
    # claim that slot. A `--once` invocation is transient — writing its own
    # pid and exiting leaves a dead pid in the file, which makes every
    # SUBSEQUENT cycle of the persistent daemon (or every `cus sos` call)
    # emit a spurious "Daemon pid X recorded but process is gone" warning.
    # `--once` callers (cron, smoke-tests, one-shot polls) don't need the
    # SOS-tracking slot at all; skip the write.
    #
    # GH #76 problem 3: the pid write is now also a SINGLE-INSTANCE guard —
    # the pid file is flock'd for the process lifetime, so a systemd unit +
    # a manually-launched `cus daemon` can no longer run concurrently and
    # double the swap/lost-update collision odds. flock (not pid-liveness
    # probing) because it is race-free and self-cleaning after a crash.
    if not once:
        if not _acquire_daemon_singleton():
            sys.exit(1)

    # GH #76: a swap that crashed mid-flight leaves its intent journal on
    # disk. Reconcile at startup (under the swap lock) rather than waiting
    # for the next swap — the desync poisons polling attribution in the
    # meantime. Short lock timeout: if another process is actively swapping
    # RIGHT NOW, it owns journal handling; skip quietly.
    try:
        with _swap_lock(timeout_seconds=5.0):
            _recover_pending_swap()
    except RuntimeError as e:
        click.echo(f"startup swap-journal check skipped: {e}")

    def _emit_sos_after(state: dict, config: dict) -> None:
        # Fix 1 (2026-07-07 blank-mount PREEMPT): FIRST — before any heal or
        # diagnose — proactively refresh live lane mounts that are near-expiry-AND
        # -idle, the state that PRECEDES the classic blank (Claude Code's own
        # in-place refresh failing mid-write). Keeping the mount ahead of expiry
        # means Claude Code never needs its fragile refresh, so the mount never
        # blanks — breaking the heal→blank→heal cycle at its source. A preempted
        # mount is rewritten (fresh mtime), so the warn scan below then sees it
        # self-maintaining and stays silent. No-op outside per_session/hybrid or
        # when disabled; degrades to the warn+heal path on any refresh failure.
        _preempt_live_lane_blanks(state, config, no_execute=no_execute)
        # GH #141 self-heal: BEFORE diagnosing, try to auto-restore a blanked
        # live shared mount (global/hybrid only; no-op elsewhere or when healthy).
        # Running it here — the one place every served-mount cycle path funnels
        # through after the existing detection — means a healable blank never
        # reaches the operator as an URGENT SOS: the heal fixes the live file and
        # the diagnose() below then sees it valid. Only a mount with NO usable
        # backup falls through to the URGENT re-login SOS (needs a human).
        _auto_heal_live_mount(state, config, no_execute=no_execute)
        # GH #141 follow-up (2026-07-05 slot-2): the shared-mount heal above never
        # looked at per-slot LANE mounts, so a blanked lane (Claude's own logout /
        # a crash mid-write) sat locked out until fixed by hand. Same funnel, same
        # discipline: heal every LIVE lane whose own creds are blanked BEFORE
        # diagnose(), so a healable lane never surfaces as an URGENT SOS. No-op in
        # global mode / when no lane is blanked.
        _auto_heal_live_lanes(state, config, no_execute=no_execute)
        conditions = diagnose(state, config)
        # PRE-EMPTIVE creds-health early-warning (2026-07-06): AFTER the reactive
        # auto-heals above (so a just-healed mount reads valid, not near-blank) and
        # AFTER diagnose() (so a genuinely blanked mount surfaces as its URGENT, not
        # doubled by a WARNING — the predicate returns None for blanks). Scans every
        # LIVE mount and appends soft WARNINGs for tokens AGING toward expiry/blank
        # before they fail. Merged into the same conditions list so they ride the
        # existing SOS.md write + notify de-dup (`maybe_write_sos`).
        conditions.extend(_diagnose_live_mounts_creds_health(state, config))
        # Fix 2 (2026-07-07 STUCK-SESSION escalation): a lane auto-healed
        # repeatedly (heal→blank→heal within the stuck window) means the file-heal
        # is NOT un-sticking the live process — it cached the logged-out state and
        # needs a RELAUNCH. Append the distinct URGENT so the operator knows a
        # file-heal won't help. Reads the in-process heal history recorded by
        # `_auto_heal_live_lanes` above.
        conditions.extend(_diagnose_stuck_lanes(state, config))
        maybe_write_sos(conditions, state)
        if conditions:
            for c in conditions:
                click.echo(f"  [{c.severity.upper()}] {c.summary}")

    def one_cycle() -> None:
        state = load_state()
        config = load_config()
        mode = config.get("mode", "global")
        per_session = mode == "per_session"
        hybrid = mode == "hybrid"
        click.echo(f"[{now_iso()}] cycle start. mode={mode} active={state['active']}")

        # 0. Reactive check — has anything 429'd since last cycle?
        # per_session + hybrid handle 429s AFTER polling (inside their cycle
        # fns), attributed to the offending slot — the pre-poll global variant
        # here would swap ~/.claude/, which per_session NEVER writes and which
        # hybrid handles itself (partitioned from slot 429s).
        reactive_decision = None if (per_session or hybrid) else check_rate_limit_reactive(state, config)
        if reactive_decision is not None:
            click.echo(f"  reactive (429): {reactive_decision.reason}")
            save_state(state)
            _log_decision(_build_decision_record(
                state, config,
                action=("would_swap" if no_execute else "swap"),
                gate="reactive_429", reason=reactive_decision.reason,
                target=reactive_decision.target, tier=reactive_decision.tier,
                where=_migrating_panes(),
            ))
            if no_execute:
                click.echo("    (--no-execute) skipping reactive swap")
            elif config.get("hot_swap", {}).get("enabled", False):
                try:
                    hot_swap_orchestrate(reactive_decision, state, config)
                except NameError:
                    execute_swap(reactive_decision.target, trigger="reactive-429")
            else:
                execute_swap(reactive_decision.target, trigger="reactive-429")
            return

        # 1. Poll — differential cadence (2026-07-02): the ACTIVE account is
        # polled fast (catch its usage ramp), inactive accounts slowly (they're
        # idle, only needed for target selection). This keeps the per-IP-throttled
        # /api/oauth/usage endpoint well under its burst limit while reacting
        # quickly on the one account that's actually burning — see
        # _account_poll_due / _account_poll_interval. Accounts in 429 poll-backoff
        # are skipped regardless of cadence.
        #
        # Anti-burst stagger: when >1 account falls due in the same cycle (e.g.
        # cold start, or two inactive intervals coinciding), space the successive
        # HTTP polls by `polling.stagger_seconds` so they don't hit the per-IP
        # throttle as a single burst. No delay before the first poll of a cycle.
        usage_by_account: dict[str, AccountUsage] = {}
        stagger = config.get("polling", {}).get("stagger_seconds", 0) or 0
        polled_this_cycle = 0
        for name in state["accounts"]:
            in_backoff, until = account_in_backoff(state, name)
            if in_backoff:
                click.echo(f"  skip {name}: in poll-backoff until {until}")
                continue
            due, why = _account_poll_due(state, config, name)
            if not due:
                click.echo(f"  skip {name}: not due ({why})")
                continue
            if polled_this_cycle > 0 and stagger > 0:
                time.sleep(stagger)
            usage_by_account[name] = poll_account_usage(name)
            polled_this_cycle += 1

        # GH #75 (lost-update race): the poll loop above holds the network
        # for seconds to ~40s (per-account HTTP calls, 10s timeout each). A
        # swap landing in that window commits active=<new> to state.json —
        # and saving the pre-poll snapshot below would silently revert it,
        # desyncing state from the live files and priming the NEXT swap's
        # save-back to destroy the departed account's refresh token. Re-load
        # state now, after the slow phase, and apply this cycle's usage
        # updates to the FRESH copy; the remaining load→save window is
        # milliseconds of pure local I/O. (The full fix — field-ownership
        # merge or an active-pointer split — is tracked in #75; this removes
        # the routinely-armed window.)
        state = load_state()

        # 2. Update state
        update_state_with_usage(state, usage_by_account)
        # Fix 4 (2026-07-07): cheap self-healing pass — un-stale every account the
        # poll just left token_stale (idle account, valid refresh token) via a
        # direct OAuth refresh grant. Keeps cus's readings correct and prevents the
        # "stale snapshot → blank on install" when a lane later swaps onto it.
        # Cooldown-bounded (no burnout); no-op when disabled or nothing is stale.
        for _unstaled in _sweep_unstale_idle_accounts(state, config):
            click.echo(f"  un-staled idle account {_unstaled}: minted a fresh access token via a "
                       f"direct OAuth refresh grant and cleared token_stale (Fix 4)")
        # clock_keepalive (2026-07-07 operator directive): keep every DORMANT
        # account's fixed 5h usage window TICKING with a cheap "hi" ping, so the
        # whole fleet cycles through 5h windows continuously and resets on a
        # rolling basis (always-fresh capacity on tap). Cooldown-bounded, skips
        # live/disabled/backoff/relogin accounts, and force-re-polls each pinged
        # account so five_hour_resets_at reflects the now-running clock. Gated by
        # clock_keepalive.enabled (default on); a failed ping only logs.
        for _pinged in _sweep_clock_keepalive(state, config):
            click.echo(f"  clock-keepalive: pinged dormant account {_pinged} to start a fresh "
                       f"5h window (keeping its clock ticking for rolling capacity)")
        # GH #59: countdown fallback — for any account whose 5h reset elapsed
        # without a fresh poll this cycle, trust the countdown and infer the
        # window reset (5h→0) BEFORE the ladder-reset + decision below, so both
        # act on reality rather than a stale-high %.
        for nm in _apply_countdown_reset_inference(state, config):
            click.echo(f"  countdown fallback: {nm} 5h reset elapsed without a fresh poll "
                       f"— inferring 5h→0% for this cycle (GH #59)")
        maybe_reset_thresholds(state, config)

        # per_session (Phase 3): decisions + execution are per SLOT from here.
        # The daemon never writes ~/.claude/ in this mode — bare launches are
        # observed (SOS) but not swapped, because swapping the global mount
        # moves every bare session at once: exactly the every-session cache
        # bust this mode exists to eliminate (3.4).
        if per_session:
            _per_session_cycle(state, config, usage_by_account, no_execute)
            _emit_sos_after(load_state(), config)
            return

        # Hybrid (GH #99): manage slots individually AND the shared mount for
        # bare sessions in one cycle. Like per_session, decisions happen after
        # polling; unlike it, the shared mount IS swapped (for bare sessions).
        if hybrid:
            _hybrid_cycle(state, config, usage_by_account, no_execute)
            _emit_sos_after(load_state(), config)
            return

        # 3. Decide. Even in global mode a machine can have live `cus launch`
        # slots alongside bare sessions; the shared-mount swap must not land on
        # an account a slot already holds, or the two mounts rotate each other's
        # refresh token and one gets logged out (GH #104). Exclude live-slot
        # accounts from the candidate pool (no-op when there are no slots).
        trace: dict = {}
        decide_state = _state_excluding_accounts(state, state.get("active"), _live_slot_accounts(state))
        decision = decide_swap(decide_state, config, usage_by_account, trace)

        # Persist usage updates BEFORE acting on swap (so a crash during
        # swap leaves valid usage state)
        save_state(state)

        if decision is None:
            # GH #56: log the no-swap decision with the exact gate that held it
            # (below_threshold, growth gate, min-improvement, hysteresis, ...).
            _log_decision(_build_decision_record(
                state, config,
                action="hold",
                gate=trace.get("gate", "no_swap"),
                reason=trace.get("reason", "no swap needed"),
            ))
            # Diagnose: was a swap WANTED but no target available?
            active = state["active"]
            active_acct = state["accounts"][active]
            threshold = active_acct.get("next_swap_at_pct", THRESHOLD_STEPS[0])
            active_usage = usage_by_account.get(active)
            wanted = (
                active_usage is not None
                and not active_usage.token_expired
                and current_max_pct(active_usage, config) >= threshold
            )
            if wanted:
                # Distinguish "no target available" from "gate suppressed."
                # The gate-suppression paths (growth gate, hysteresis) already
                # logged their own reason inside decide_swap. We only print
                # "no valid swap target" if pick_swap_target would also fail
                # — otherwise we'd be misleading the operator about WHY the
                # swap didn't fire. Bug fixed 2026-05-25 (GH #19).
                if pick_swap_target(state, config) is None:
                    click.echo(f"  threshold tripped on {active} ({current_max_pct(active_usage, config):.1f}% >= {threshold}%) but no valid swap target")
            for name, acct in state["accounts"].items():
                marker = " *" if name == state["active"] else "  "
                te = " (TOKEN_EXPIRED)" if acct.get("token_expired") else ""
                rl = " (RATE_LIMITED)" if acct.get("rate_limited") else ""
                click.echo(f"  {marker}{name}: 5h={acct.get('current_5h_pct', 0):.1f}%, 7d={acct.get('current_7d_pct', 0):.1f}%, next={acct.get('next_swap_at_pct', 50)}%{te}{rl}")
            _emit_sos_after(state, config)
            return

        click.echo(f"  swap decision: {state['active']} -> {decision.target} (tier {decision.tier})")
        click.echo(f"    reason: {decision.reason}")

        # GH #56: cache-aware lazy background swap. A background swap never
        # interrupts a session; its only cost is a prompt-cache rebuild on the
        # warm sessions that migrate. So defer a NON-URGENT swap while any live
        # session is still cache-warm — it'll fire on a later cycle once the
        # cache goes cold (free) or the swap turns urgent (deferrable=False).
        # Computed BEFORE the --no-execute return so the dry-run shows it too.
        lazy_warm = _lazy_warm_sessions(decision, config)
        if lazy_warm:
            warm_panes = sorted({s.pane for s in lazy_warm if s.pane and s.pane != "no-tmux"})
            msg = (
                f"lazy-swap DEFER: {len(lazy_warm)} cache-warm session(s) across "
                f"{len(warm_panes)} pane(s) — non-urgent {decision.gate} swap to "
                f"{decision.target} would burn cache; waiting for cold cache (GH #56)"
            )
            click.echo(f"  {msg}")
            _log_decision(_build_decision_record(
                state, config, action="defer", gate="lazy_swap",
                reason=msg, target=decision.target, tier=decision.tier,
                where={"warm_pane_count": len(warm_panes), "panes": warm_panes},
            ))
            # A defer is a real live-cycle outcome (unlike --no-execute), so it
            # must still refresh SOS — on a busy fleet the cache stays warm and
            # the active account can sit in a long 90–95% defer streak; without
            # this, SOS.md would freeze and hide a token-expiry / stale-poll /
            # no-target condition until a hold or swap cycle finally fired.
            _emit_sos_after(state, config)
            return

        if no_execute:
            click.echo("    (--no-execute) skipping actual swap")
            _log_decision(_build_decision_record(
                state, config, action="would_swap", gate=decision.gate,
                reason=decision.reason, target=decision.target, tier=decision.tier,
                where=_migrating_panes(),
            ))
            return

        # 4. Execute. Phase 2: simple swap. Phase 3+ will dispatch to hot-swap
        # orchestrator based on `decision.tier` and config['hot_swap']['enabled'].
        if config.get("hot_swap", {}).get("enabled", False):
            # Phase 3+ — defer to hot_swap_orchestrate (defined in Phase 3 section)
            try:
                hot_swap_orchestrate(decision, state, config)
            except NameError:
                click.echo("    hot_swap_orchestrate not yet implemented; doing simple swap")
                execute_swap(decision.target, trigger=f"auto-tier{decision.tier}")
        else:
            execute_swap(decision.target, trigger=f"auto-tier{decision.tier}")
            click.echo(f"    swapped (new sessions only — live sessions unaffected)")

        _log_decision(_build_decision_record(
            state, config, action="swap", gate=decision.gate,
            reason=decision.reason, target=decision.target, tier=decision.tier,
            where=_migrating_panes(),
        ))
        _emit_sos_after(load_state(), config)

    if once:
        one_cycle()
        return

    config = load_config()
    # Loop tick = the fastest cadence (active-account interval) so the active
    # account can actually be polled that often; per-account due-checks inside
    # one_cycle() gate which accounts are polled each tick. Falls back to the
    # flat poll_interval_seconds when the differential keys aren't set.
    polling_cfg = config.get("polling", {})
    flat = config.get("poll_interval_seconds", 300)
    interval = polling_cfg.get("active_interval_seconds") or flat
    inactive_interval = polling_cfg.get("inactive_interval_seconds") or flat
    click.echo(f"daemon starting. tick={interval}s (active), "
               f"inactive={inactive_interval}s. Ctrl-C to stop.")
    try:
        while True:
            cycle_t0 = time.monotonic()
            try:
                one_cycle()
            except Exception as e:
                click.echo(f"ERROR in cycle: {type(e).__name__}: {e}", err=True)
            # GH #59: adaptive repoll — wake just after the soonest known 5h
            # reset (if sooner than the normal interval) to refresh real data
            # promptly instead of running on the countdown-inferred value.
            intended = _adaptive_sleep_seconds(load_state(), config, interval)
            # Freeze `took` AFTER the adaptive-sleep computation (which itself
            # does a load_state()): otherwise the state read + these log lines
            # sit in a window counted by neither `took` nor `sleeping` — the
            # exact silent-gap blind spot this heartbeat exists to close
            # (review finding 2026-07-02). Everything from cycle_t0 to the
            # time.sleep() below is now attributed to `took`.
            cycle_secs = time.monotonic() - cycle_t0
            # GH #91 heartbeat: log cycle wall-clock + intended sleep every
            # tick, so an inter-cycle gap in the log is attributable — a slow
            # cycle body shows up in `took`, a deliberate long sleep in
            # `sleeping`, and an oversleep (scheduler starvation / system
            # suspend) in the post-sleep check below. The 2026-07-02 incident
            # (11-min silent gap during a swap+burn) was undiagnosable for
            # lack of exactly this line.
            click.echo(f"[{now_iso()}] cycle end. took {cycle_secs:.1f}s; sleeping {intended:.0f}s")
            # In-cycle-stall threshold: a cold-start cycle legitimately polls
            # every account at once, each with up to USAGE_API_TIMEOUT_SECONDS
            # plus polling.stagger_seconds between them, which can exceed one
            # tick without being a stall (review finding 2026-07-02). Warn only
            # past a bound that accounts for that worst-case poll budget.
            stagger = config.get("polling", {}).get("stagger_seconds", 0) or 0
            n_acct = len(load_state().get("accounts", {}))
            poll_budget = n_acct * (USAGE_API_TIMEOUT_SECONDS + stagger)
            stall_threshold = max(interval, poll_budget)
            if cycle_secs > stall_threshold:
                click.echo(f"[{now_iso()}]   WARNING: cycle body ({cycle_secs:.0f}s) exceeded "
                           f"the {stall_threshold:.0f}s stall threshold — in-cycle stall (GH #91)")
            slept_t0 = time.monotonic()
            time.sleep(intended)
            overslept = time.monotonic() - slept_t0 - intended
            # Oversleep slop scales with the intended sleep: adaptive repoll can
            # shrink `intended` to ~30s near a 5h reset, where a fixed 30s slop
            # would false-positive on ordinary jitter (review finding
            # 2026-07-02). max(30, 0.5*intended) keeps the absolute floor for
            # long sleeps while widening the band for short adaptive ones.
            slop = max(30.0, 0.5 * intended)
            if overslept > slop:
                click.echo(f"[{now_iso()}] WARNING: overslept by {overslept:.0f}s "
                           f"(intended {intended:.0f}s, slop {slop:.0f}s) — scheduler "
                           f"starvation or system suspend (GH #91)")
    except KeyboardInterrupt:
        click.echo("\ndaemon stopped.")


@cli.group()
def hooks() -> None:
    """Manage Claude Code hooks used by cus.

    Hooks ship as small bash scripts in <repo>/hooks/ and are registered
    in ~/.claude/settings.json under a signature-keyed entry so we don't
    clobber other tools.
    """


@hooks.command("install")
def hooks_install_cmd() -> None:
    """Install enabled hooks into ~/.claude/settings.json."""
    config = load_config()
    result = install_hooks(config)
    click.echo("Hook installation:")
    for event, status in result.items():
        click.echo(f"  {event:<30} {status}")


@hooks.command("uninstall")
def hooks_uninstall_cmd() -> None:
    """Remove all cus-marked entries from ~/.claude/settings.json."""
    result = uninstall_hooks()
    click.echo("Hook uninstall:")
    for event, status in result.items():
        click.echo(f"  {event:<30} {status}")


@hooks.command("list")
def hooks_list_cmd() -> None:
    """Show current cus hook install state."""
    result = list_hooks()
    click.echo("Hook state:")
    for event, status in result.items():
        click.echo(f"  {event:<30} {status}")


CONFIG_EXPLAIN_MAP: dict[str, str] = {
    "mode": "Live-mount topology (docs/plans/2026-07-02-per-session-accounts.md). `global` (default): one live mount (~/.claude/), the daemon swaps it in place, every session follows the active account; slots are ignored. `per_session`: each session launched via `cus launch` gets its own slot dir (~/claude-accounts/slot-N/) holding one account; the daemon swaps individual slots in place (sessions never restart) and NEVER writes ~/.claude/ (bare launches are observed, not swapped). `hybrid` (GH #99): does BOTH — manages slots individually AND swaps the shared ~/.claude/ mount for bare sessions each cycle (bare sessions follow the shared swap together; slots move independently). Reactive 429s are partitioned so a slot's 429 moves only that slot and a bare session's only the shared mount. Switch with `cus mode per-session|hybrid|global` — never edit this key by hand mid-flight.",
    "per_session": "per_session-mode tuning. `slot_gc_idle_hours: 72`: how long a slot may sit with no live session before the daemon reaps it (credentials save back to the owning account dir first). Long default because a free slot is a reuse candidate for the next `cus launch` (no swap needed when it already holds the right account). `default_pool: premium`: rotation-set a slot joins when it doesn't declare one at launch (GH #99). `premium` slots honor the per-model weekly gate (swap off an account once a tracked model's week hits per_model_weekly.cap_pct, never swap back); `standard` slots ignore it (keep using that account's aggregate headroom for standard-model work). Set per slot via `cus launch --pool <p>` / `cus pool <slot> <p>`. Only meaningful in per_session / hybrid mode.",
    "accounts": "List of OAuth accounts the daemon manages. Auto-populated by `cus init`. Each has a `name` and `priority`.",
    "poll_interval_seconds": "Fallback poll cadence, used when the differential `polling.active_interval_seconds`/`inactive_interval_seconds` keys are unset. Lower = faster reaction; higher = lighter API load. 60 is the practical floor; 180-300 is the recommended default. NOTE: polling ALL accounts this fast bursts the per-IP-throttled usage endpoint — prefer the differential `polling.*` keys below.",
    "polling": "Poll scheduling + 429 backoff. `active_interval_seconds` (fast, e.g. 45): how often the ACTIVE account is polled — it's the one burning usage, so poll it often enough to catch the ramp before it overshoots the swap threshold. `inactive_interval_seconds` (slow, e.g. 600): how often each idle account is polled (only needed for target selection). `stagger_seconds` (e.g. 2): anti-burst spacing between successive HTTP polls in one cycle so N accounts falling due together don't hit the per-IP throttle as a burst. `base_backoff_seconds`/`max_backoff_seconds` (300/600): exponential per-account backoff after a 429 on /api/oauth/usage. Differential cadence (2026-07-02) exists because flat 60s x N accounts throttled every account into 429 backoff. Both interval keys fall back to `poll_interval_seconds` when unset (backward-compat). See GH #84 for burn-adaptive active cadence.",
    "poll_accel": "Near-threshold responsiveness (2026-07-06 rayi3 incident: burned from under its 95% step to 100% between two ~180s polls, hit the native usage-limit menu before the swap). Two config-gated fixes: (A) the ladder/cap swap TRIGGER decides on burn-EXTRAPOLATED usage (last poll + rate x time-to-next-poll), so an account at 91%-last-poll but climbing fast trips the 95% step NOW instead of next cycle — extrapolation only ever trips EARLIER, never masks a real over-cap reading; (B) an account whose extrapolated usage is within `within_pct_of_step` (default 5) points of its next step — or projected to cross it before the next normal poll — is polled on `fast_interval_seconds` (default 45) instead of the normal cadence. `enabled: false` reverts BOTH to prior behavior bit-for-bit. Only accounts actually near a step accelerate (the fleet is not blanket-fast-polled), and the 429 poll-backoff gate still wins (a throttled account is never fast-polled). `cluster_within_bonus_pct` (default 10, 2026-07-06 rayi4 incident) WIDENS the near-step band by this many points per EXTRA live lane an account backs, because an account under N concurrent sessions climbs ~N× faster than its single-lane burn_rate predicts — so a clustered near-cap account (rayi4 sat at 84% on the slow cadence) fast-polls early enough to swap a lane off in time. Inert for one-lane-per-account operation.",
    "spread_lanes": "Anti-clustering lane spread (2026-07-06 incident: the daemon piled ALL live lanes onto ONE burn-soon account — first rayi5, then rayi4 — which over-subscribed its login families → the shared token diverged → a recurring 'mount blanked / not logged in' auto-heal loop, and then all lanes hit that account's 5h cap at once with no time to swap). The scorers rank accounts on usage/burn only, so the same magnet is every lane's top pick each cycle. Two coupled levers, both gated by `enabled: true` (false reverts bit-for-bit): (1) `cluster_penalty` (default 40) subtracts this many score points per live lane already backing a candidate target, so an account that already holds a lane loses to a distinct empty one — this breaks the cross-cycle convergence; (2) `max_stack` (default 1) stops a NON-urgent (deferrable, e.g. burn-before-reset) move from growing an account past this many lanes — the lane HOLDS on its current account instead. Urgent moves (hard-cap / reactive-429 escapes) may still stack via independent login families when clean accounts are genuinely scarce, guarded by the #104/#109 family accounting.",
    "strategy": "Swap target picker — see docs/STRATEGIES.md. `smart` (recommended): hard 7d cap + burn-before-reset for 5h windows about to expire. `headroom`: weighted 5h+7d score with hard 7d cap. `lowest_usage`: cux balanced — sort by 7d util only. `drain`: deplete-current. `strict_priority`: priority order. `round_robin`: cycle by name.",
    "smart_strategy": "Tuning for `smart` strategy. `hard_7d_cap_pct: 80` forces-swap active account at 80% 7d AND filters candidates above 80% (the user's explicit goal: never exceed 80% on the weekly window). `burn_window_hours: 2` + `burn_soon_weight: 1.0` boost candidates whose 5h is ticking AND resets within N hours (prefer to burn before reset). `cold_account_penalty: 0` (default off) deprioritizes accounts at 5h=0% (clock not ticking).",
    "headroom_strategy": "Tuning for `headroom` strategy. Same `hard_7d_cap_pct` filter. `five_hour_weight: 0.7` + `seven_day_weight: 0.3` weight the headroom score.",
    "thresholds": "Progressive per-account thresholds. `steps: [70, 85, 95]` means each account's next_swap_at_pct climbs through these levels as we swap out and return. `five_hour`/`seven_day` toggle which window the threshold applies to. `reset_below_pct` resets the ladder when both windows drop below this value.",
    "seven_day_reset_hours": "72h seven_day reset detection (2026-07-05, gist: monperrus/3ac4b303a84946bbeaf2b1123ee99491). The Claude `seven_day` \"weekly\" budget actually REFRESHES every ~72h at a fixed UTC anchor (~04:50-05:00 UTC), not every 7 days; the API's seven_day.resets_at is misleading (it points ~7 days out at the oldest-tokens-age-off boundary). cus detects the real reset from a sharp DROP in current_7d_pct and projects the next one this many hours forward (default 72). Tunable if Anthropic changes the cadence.",
    "seven_day_reset_drop_pct": "Min pct-point DROP in current_7d_pct between two consecutive polls to count as a real 72h seven_day reset rather than noise/jitter (default 15). Pairs with `seven_day_reset_hours`. Raise it if normal fluctuation trips false resets; lower it if a genuine reset is being missed.",
    "hot_swap": "Hot-swap of in-flight tmux sessions. `enabled: false` = swap creds for new sessions only (level 3). `enabled: true` = pause + relaunch existing sessions in their panes (level 4). `tier_2_at_pct` controls when pause-message injection starts; `tier_3_at_pct` controls when force-interrupt kicks in. `pause_message`/`wake_up_message` are the texts sent via tmux send-keys. `cache_bust_window_seconds` defers Tier 1 swaps when prompt cache is still warm (avoid burning rebuild cost).",
    "lazy_swap": "Cache-aware lazy background swap (GH #56). Consulted ONLY when `hot_swap.enabled` is false. A background swap never interrupts a session; its only cost is a one-time prompt-cache rebuild on the live sessions that migrate. So `enabled: true` (default) DEFERS a non-urgent swap (ladder below saturation, burn-before-reset) while any live session's last activity is younger than `cache_window_seconds: 300` (≈ Anthropic's prompt-cache TTL) — it re-fires once the cache is cold (free) or the swap turns urgent. Urgent swaps (hard 7d cap, 5h saturation, reactive 429) are never deferred. Set `enabled: false` to swap immediately on every ladder trip (old behavior). Inspect deferrals with `cus decisions`.",
    "reset_inference": "5h-window reset inference (GH #59). The statusline reset countdown ticks live per-render; this makes the daemon act on it. `enabled: true` (countdown fallback): when a poll didn't refresh an account past its 5h reset (poll failed / 429 backoff / token stale), trust the countdown — treat that window as reset (~0%) for decisions + SOS instead of the stale pre-reset %; the next successful poll supersedes it. `adaptive_repoll: true`: wake just after the soonest known 5h reset (+`repoll_buffer_seconds: 20`, floored by `min_repoll_seconds: 30`) to repoll promptly rather than running on the inferred value for a whole poll_interval. Only ever shortens the sleep.",
    "token_self_refresh": "OAuth self-refresh for `token_stale` accounts (2026-07-02, GH #13 follow-up). `enabled: true` (default): before flagging an inactive account token_stale, POST a refresh_token grant to the same undocumented endpoint the `claude` CLI itself uses (extracted from the installed binary — not a published API). Costs no usage quota and creates no session, unlike spawning a real `claude` probe session would. Any failure (network, non-200, malformed response) falls straight through to the pre-existing token_stale flag. Set `enabled: false` to revert to never talking to this endpoint, e.g. if Anthropic changes its shape and it starts erroring.",
    "subagent_skip": "Skip-guard for sessions with in-flight subagents/tool calls. `defer_below_tier: 3` means Tier 1/2 skip those panes (per-pane, not whole orchestration — cred swap + other panes still proceed); Tier 3 (force) ALWAYS bypasses the guard regardless of defer_below_tier (GH #27).",
    "reactive": "When `enabled: true`, the PostToolUseFailure hook detects 429s in tool error bodies and triggers immediate swap (no waiting for next poll).",
    "per_model_weekly": "Per-model WEEKLY usage tracking (fable/sonnet). The usage API exposes per-model data only weekly — there is NO per-model 5h window. Always parsed + shown in `cus status` (7d-by-model sub-line). `gate_enabled: false` (default) = surface-only. `gate_enabled: true` treats per-model weekly as a HARD CAP: force-swap the active account when a tracked model's week reaches the model cap, and never pick a swap target whose model-week is at/above it. It does NOT feed the progressive ladder — a weekly per-model budget is a hard line, so a model swaps ONLY at its cap, not gradually at ladder steps (fixed 2026-07-02). `cap_pct` (default null) sets that model cap explicitly; null inherits the strategy's `hard_7d_cap_pct`, so the two ceilings can differ (e.g. Fable gated at 97 while aggregate 7d stays capped at 80). `models: []` tracks every model the API reports; set e.g. [\"Fable\",\"Sonnet\"] to gate only on models you use.",
    "swap_hysteresis": "Anti-churn gates on the ladder swap path. `enabled: true` (default) turns them all on. `min_seconds_between_swaps: 300` = min interval between ladder swaps — keep this in MINUTES: it exists only to stop ping-pong between daemon swaps, and a long value strands a climbing account behind the lockout (2026-07-03: an unannotated 3000s (50 min) parked hot slots at 84-88%). Only DAEMON swaps arm this clock (last_auto_swap_ts, 2026-07-03) — a `cus launch` doesn't, so launches can't re-arm the cooldown out from under the ladder. `min_seconds_between_cap_swaps: 60` / `min_seconds_between_reactive_swaps: 60` = same for the hard-7d-cap and reactive-429 emergency paths (these still read last_swap_ts — counting launches in a 60s emergency spacing is harmless). `min_improvement_pct: 3` (GH #40) = a ladder swap must lower the active account's effective utilization by at least this many points; otherwise it's suppressed (prevents pingpong when both accounts sit in the same over-threshold band). The hard-7d-cap and reactive-429 paths bypass `min_improvement_pct` and `min_seconds_between_swaps` — they're emergencies.",
    "session_locks": "Per-session pinning + per-slot locks. `pinned: {pane_or_session_id: account_name}` — pinned sessions are never auto-swapped. `locked_slots: [slot-1, ...]` — per_session-mode slots the daemon never swaps or idle-gcs (manage via `cus lock`/`cus unlock`). `never_restart_patterns` is a regex list matched against tmux pane's current command name.",
    "hooks": "Which Claude Code hooks the installer manages. Each maps to a small bash script in <repo>/hooks/. `cus hooks install` reads this to know which to install.",
    "daemon": "Daemon-internal paths. log_path = where stdout/stderr goes; pid_path = where the daemon writes its PID (used by SOS to detect stale process).",
}


@cli.command()
@click.option("--edit", "edit_flag", is_flag=True, help="Open ~/claude-accounts/config.yaml in $EDITOR.")
@click.option("--explain", "explain_flag", is_flag=True, help="Annotated listing: each setting + description + current value.")
@click.option("--path", "path_flag", is_flag=True, help="Print the config file path and exit.")
def config(edit_flag: bool, explain_flag: bool, path_flag: bool) -> None:
    """Show, edit, or explain configuration.

    Default: print effective merged config (defaults + user overrides from
    ~/claude-accounts/config.yaml).

    Flags:
      --edit       Open the user config file in $EDITOR (default: vi)
      --explain    Annotated listing of every setting with current value + description
      --path       Print the file path and exit (useful for: `vim $(cus config --path)`)
    """
    if path_flag:
        click.echo(CONFIG_YAML)
        return

    if edit_flag:
        if not CONFIG_YAML.exists():
            click.echo(f"Config file doesn't exist yet at {CONFIG_YAML}. Run `cus init` first.")
            sys.exit(1)
        editor = os.environ.get("EDITOR") or os.environ.get("VISUAL") or "vi"
        try:
            subprocess.run([editor, str(CONFIG_YAML)], check=True)
        except (subprocess.SubprocessError, FileNotFoundError) as e:
            click.echo(f"Failed to open editor '{editor}': {e}")
            sys.exit(1)
        click.echo(f"Saved. If the daemon is running, restart it to pick up changes:")
        click.echo("  systemctl --user restart cus.service")
        return

    cfg = load_config()

    if explain_flag:
        click.echo(click.style("Effective config settings (current value + description):", bold=True))
        click.echo(f"Config file: {CONFIG_YAML}")
        click.echo(f"Edit with:   cus config --edit")
        click.echo()
        for key, desc in CONFIG_EXPLAIN_MAP.items():
            cur = cfg.get(key, "<not set>")
            click.echo(click.style(f"{key}:", bold=True, fg="cyan"))
            click.echo(f"  description: {desc}")
            if isinstance(cur, (dict, list)):
                rendered = yaml.safe_dump(cur, default_flow_style=False).rstrip()
                indented = "\n".join("    " + line for line in rendered.split("\n"))
                click.echo(f"  current value:\n{indented}")
            else:
                click.echo(f"  current value: {cur}")
            click.echo()
        return

    click.echo(yaml.safe_dump(cfg, sort_keys=True, default_flow_style=False))


# --------------------------------------------------------------------------
# Phase 6 commands — pin/unpin, statusline, init-systemd
# --------------------------------------------------------------------------

@cli.command()
@click.argument("pane_or_session")
@click.argument("account")
def pin(pane_or_session: str, account: str) -> None:
    """Pin a tmux pane (e.g. %12) or session-id to a specific account.

    The daemon will never swap pinned sessions during hot-swap. Useful for
    long-running autonomous work that you want kept on one account even if
    that account is the one we want to drain.

    The pin lives in config.yaml under session_locks.pinned. Persists
    across daemon restarts.
    """
    if not CONFIG_YAML.exists():
        click.echo("Not initialized. Run `cus init` first.")
        sys.exit(1)
    user_cfg = read_yaml(CONFIG_YAML)
    locks = user_cfg.setdefault("session_locks", {}).setdefault("pinned", {})
    locks[pane_or_session] = account
    write_yaml(CONFIG_YAML, user_cfg)
    click.echo(f"Pinned {pane_or_session} -> {account}")


@cli.command()
@click.argument("pane_or_session")
def unpin(pane_or_session: str) -> None:
    """Remove a pin from a pane or session-id."""
    if not CONFIG_YAML.exists():
        click.echo("Not initialized. Run `cus init` first.")
        sys.exit(1)
    user_cfg = read_yaml(CONFIG_YAML)
    locks = user_cfg.get("session_locks", {}).get("pinned", {}) or {}
    if pane_or_session in locks:
        del locks[pane_or_session]
        write_yaml(CONFIG_YAML, user_cfg)
        click.echo(f"Unpinned {pane_or_session}")
    else:
        click.echo(f"{pane_or_session} was not pinned")


def _normalize_slot_name(slot_name: str) -> str:
    """Accept both `slot-2` and bare `2` — the CLI convenience mirrors how
    slots are displayed (`slot-N`) vs how users abbreviate them."""
    return slot_name if slot_name.startswith("slot-") else f"slot-{slot_name}"


@cli.command()
@click.argument("slot_name")
def lock(slot_name: str) -> None:
    """Lock a slot (e.g. slot-1) so the daemon never swaps its account.

    Ladder, hard-cap, and reactive-429 slot moves all skip a locked slot,
    and idle slot-gc won't reap it. per_session-mode counterpart of `pin`
    (which protects a session from hot-swap orchestration): a lock freezes
    the slot's credential mount itself. Persists in config.yaml under
    session_locks.locked_slots. Undo with `cus unlock <slot>`.
    """
    if not CONFIG_YAML.exists():
        click.echo("Not initialized. Run `cus init` first.")
        sys.exit(1)
    name = _normalize_slot_name(slot_name)
    state = load_state()
    if name not in (state.get("slots") or {}) and not slot_path(name).exists():
        # Locking ahead of slot creation is allowed (the lock is pure config),
        # but flag it — the common case is a typo, not pre-provisioning.
        click.echo(click.style(f"note: {name} does not currently exist (no state entry or dir); locking anyway", fg="yellow"))
    user_cfg = read_yaml(CONFIG_YAML)
    locked = user_cfg.setdefault("session_locks", {}).setdefault("locked_slots", [])
    if name in locked:
        click.echo(f"{name} is already locked")
        return
    locked.append(name)
    write_yaml(CONFIG_YAML, user_cfg)
    click.echo(f"Locked {name} — the daemon will not swap or gc this slot (unlock with `cus unlock {name}`)")


@cli.command()
@click.argument("slot_name")
def unlock(slot_name: str) -> None:
    """Remove a slot lock set by `cus lock`."""
    if not CONFIG_YAML.exists():
        click.echo("Not initialized. Run `cus init` first.")
        sys.exit(1)
    name = _normalize_slot_name(slot_name)
    user_cfg = read_yaml(CONFIG_YAML)
    locked = user_cfg.get("session_locks", {}).get("locked_slots", []) or []
    if name in locked:
        locked.remove(name)
        write_yaml(CONFIG_YAML, user_cfg)
        click.echo(f"Unlocked {name}")
    else:
        click.echo(f"{name} was not locked")


@cli.command(name="pool")
@click.argument("slot_name")
@click.argument("new_pool", required=False, type=click.Choice(list(VALID_POOLS)))
def pool_cmd(slot_name: str, new_pool: str | None) -> None:
    """Show or set a slot's rotation-set pool (GH #99, per_session mode).

    `cus pool slot-1`            → show slot-1's current pool
    `cus pool slot-1 standard`   → retag slot-1 to the standard pool

    premium slots honor the per-model weekly gate (swap off an account once a
    tracked model's week hits per_model_weekly.cap_pct, and never swap back);
    standard slots ignore it (keep using that account's aggregate headroom for
    standard-model work). The pool lives on the slot's state entry, so it
    survives the daemon moving accounts through the slot. Takes effect on the
    daemon's next cycle — no restart needed (the daemon re-reads state).
    """
    state = load_state()
    name = _normalize_slot_name(slot_name)
    config = load_config()
    entry = state.get("slots", {}).get(name)
    if entry is None:
        click.echo(f"{name} has no state entry — launch it first (`cus launch --pool <p>`) or check `cus status`.")
        sys.exit(1)
    if new_pool is None:
        click.echo(f"{name}: pool={_slot_pool(state, name, config)}"
                   + ("" if entry.get("pool") else "  (default; not explicitly set)"))
        return
    entry["pool"] = new_pool
    save_state(state)
    click.echo(f"{name} → pool={new_pool} (effective next daemon cycle)")


def _pct_is_unknown(acct: dict, key: str | None = None) -> bool:
    """Return True when `key`'s stored current_*_pct should not be displayed.

    The percentage gets displayed as "?" instead of a number when ANY of the
    blocker flags is set, because we don't actually have current data — what's
    stored is last-known-good which may be hours old.

    `rate_limited` / `token_expired` / `poll_error` always count as unknown,
    for any key.

    `token_stale` is special-cased per-window:
      - `current_5h_pct` is treated as KNOWN (not unknown) under token_stale
        alone. GH #59's reset-inference (`_five_hour_rolled_since_poll` /
        `_apply_countdown_reset_inference`) already corrects `current_5h_pct`
        across a 5h reset boundary independent of polling — that inference
        runs every daemon cycle regardless of token_stale — so the preserved
        value is safe to surface (see `_pct_is_stale_known`, which callers
        use to decide whether to mark it as not-yet-reconfirmed).
      - `current_7d_pct` (and an unspecified/None key) stays unknown under
        token_stale. There is no equivalent reset-inference for the 7-day
        window, so a preserved value could silently lie across a week
        boundary — e.g. a pre-stale 95% surfaced as `7d:95%` after the week
        actually rolled over to ~0. Keep showing "?" there until a real poll
        reconfirms it.

    Shows "?" rather than the buggy "0.0" sentinel (or stale numbers) per the
    2026-05-20 user feedback: 0% implied "no usage" which was equally
    misleading as 100% from the prior sentinel bug.
    """
    if acct.get("rate_limited") or acct.get("token_expired") or acct.get("poll_error"):
        return True
    if acct.get("token_stale"):
        return key != "current_5h_pct"
    return False


def _pct_is_stale_known(acct: dict, key: str) -> bool:
    """True when `key`'s value is last-known-good but NOT reconfirmed by a live poll.

    Only ever true for `current_5h_pct` under `token_stale` (with no other
    blocking flag) — see `_pct_is_unknown`'s docstring for why 5h, but not
    7d, is safe to surface this way. Callers use this to mark the number
    (dim styling / trailing `~`) instead of showing it identically to a
    freshly-polled value.
    """
    return key == "current_5h_pct" and bool(acct.get("token_stale")) and not _pct_is_unknown(acct, key)


def _fmt_pct(acct: dict, key: str, width: int = 8, prec: int = 1, color_on: bool = False) -> str:
    """Format a percentage for display, returning '?' if unknown.

    Use this in any user-facing output of current_5h_pct or current_7d_pct.
    When the value is last-known-but-unvalidated (`_pct_is_stale_known`),
    appends a trailing `~` (and dims it if `color_on`) so it reads as
    "not reconfirmed" rather than a normal fresh reading.
    """
    if _pct_is_unknown(acct, key):
        return f"{'?':>{width}}"
    val = acct.get(key, 0.0)
    if _pct_is_stale_known(acct, key):
        raw = f"{val:.{prec}f}~"
        padded = f"{raw:>{width}}"
        return click.style(padded, dim=True) if color_on else padded
    return f"{val:>{width}.{prec}f}"


def _model_pct_is_stale(acct: dict) -> bool:
    """True when an account's per-model WEEKLY numbers can't be trusted as a
    CURRENT reading (2026-07-05 incident).

    Per-model weekly values are 7-day-window numbers, so they share the 7d
    window's staleness fate: whenever we can't freshly observe the account
    (token_stale / rate_limited / token_expired / poll_error — exactly the
    `_pct_is_unknown(acct, "current_7d_pct")` set) the cached
    per_model_weekly_pct is last-known-good and may be hours old, or even
    post-rollover — never an authoritative current number.

    The incident: an operator (and an agent) trusted a token_stale account's
    cached `Fable=100%` as current and moved a live Fable session off it — but
    the account actually had Fable headroom; the 100% was a STALE cached value.
    Bug 1 (this helper) is the DISPLAY half of the fix — mark such numbers stale
    everywhere they're shown so they can never look current. The DECISION half
    lives in `_max_model_weekly_from_acct`, which returns 0.0 under the same
    condition so a stale reading never forces or refuses a swap.
    """
    return _pct_is_unknown(acct, "current_7d_pct")


def _fmt_model_pct(acct: dict, val: float, color_on: bool = False) -> str:
    """Render a per-model weekly % with the SAME staleness treatment the
    aggregate 7d line uses (see `_fmt_pct`): a bare `NN%` only when freshly
    observed, a dim trailing `~` (`NN%~`) when it's last-known-but-unreconfirmed
    under token_stale et al. The `~` carries the "not reconfirmed" meaning even
    with NO_COLOR / a non-color terminal — mirrors `_sl_mark_stale`. Mirroring
    `_fmt_pct` guarantees a per-model number can never look more current than
    the aggregate 7d it derives from."""
    txt = f"{val:.0f}%"
    if _model_pct_is_stale(acct):
        marked = f"{txt}~"
        return click.style(marked, dim=True) if color_on else marked
    return txt


def _fmt_duration(seconds: float) -> str:
    """Format seconds as a short human duration: '5m', '1h23m', '3d', '12d6h'."""
    if seconds < 0:
        return "now"
    if seconds < 60:
        return f"{int(seconds)}s"
    if seconds < 3600:
        return f"{int(seconds // 60)}m"
    if seconds < 86400:
        h = int(seconds // 3600)
        m = int((seconds % 3600) // 60)
        return f"{h}h{m}m" if m else f"{h}h"
    d = int(seconds // 86400)
    h = int((seconds % 86400) // 3600)
    return f"{d}d{h}h" if h else f"{d}d"


def _time_until(iso_str: str | None) -> float | None:
    """Return seconds from now until iso_str, or None if unparseable."""
    if not iso_str:
        return None
    try:
        ts = datetime.fromisoformat(iso_str.replace("Z", "+00:00"))
        return (ts - datetime.now(timezone.utc)).total_seconds()
    except (ValueError, TypeError):
        return None


def _time_since(iso_str: str | None) -> float | None:
    """Return seconds since iso_str, or None if unparseable."""
    delta = _time_until(iso_str)
    return -delta if delta is not None else None


def _five_hour_rolled_since_poll(acct: dict) -> bool:
    """True when an account's 5h window has reset since our last poll (GH #59).

    The reset countdown shown in the statusline is computed live against the
    stored `five_hour_resets_at` from the last poll, so it ticks down between
    polls on its own. But once it reaches zero the window has rolled over — the
    real 5h usage is now ~0 — while the stored `current_5h_pct` is still the
    PRE-reset value (e.g. 96%) until the next poll (up to a poll interval away).

    This returns True exactly in that window: the reset time is in the past AND
    our most recent poll was taken at/before that reset (so we have NOT yet
    observed the post-reset state). It lets the statusline flag "this window
    just reset" live, without waiting to repoll — the thing the operator wants
    to see at a glance. Returns False for an idle account sitting at 0% with an
    old reset time, because there we DID already poll and saw the 0%.
    """
    resets_at = acct.get("five_hour_resets_at")
    if not resets_at:
        return False
    secs_to_reset = _time_until(resets_at)
    if secs_to_reset is None or secs_to_reset > 0:
        return False  # reset still in the future (or unparseable) — nothing rolled
    reset_age = -secs_to_reset                 # seconds since the reset fired
    # Use last real OBSERVATION, not last poll ATTEMPT (2026-07-03 token-stale
    # blindness incident, GH #130). This is the crux of the bug: a token_stale
    # account's Branch 0 keeps restamping last_poll_ts every cycle without ever
    # observing usage, so `poll_age` stayed tiny and this returned False
    # forever — the account's 5h window rolled over but the GH #59 countdown
    # inference never fired, leaving it frozen at a stale-high % (rayi2 sat at
    # ~99% for 3.7h). last_observed_ts only advances on a SUCCESSFUL poll, so
    # it correctly ages past the reset. `or last_poll_ts` keeps legacy state
    # files (written before last_observed_ts existed) on today's behavior until
    # their first successful poll populates the new field.
    last_observed = acct.get("last_observed_ts") or acct.get("last_poll_ts")
    if not last_observed:
        return True                            # never observed → can't have seen post-reset
    obs_age = _time_since(last_observed)       # seconds since the last real observation
    # Stale iff the last observation predates the reset (older than the reset).
    return obs_age is not None and obs_age > reset_age


def _apply_countdown_reset_inference(state: dict, config: dict) -> list[str]:
    """GH #59 countdown fallback: act on the reset countdown when polling can't.

    When a cycle did NOT refresh an account past its 5h reset (the poll failed,
    the account is in 429 backoff, or its token is stale), the stored
    `current_5h_pct` is the PRE-reset value (e.g. 96%) even though the window
    has actually rolled to ~0. Trust the live countdown: write 5h→0 into state
    (stashing the prior value in `five_hour_pct_pre_reset` for display) so the
    decision logic and SOS see reality instead of a stale-high number. The next
    successful poll supersedes the inference and clears the flags.

    Returns the names of accounts inferred-reset this cycle (for logging).
    Idempotent: a re-run on an already-inferred account is a no-op.
    """
    if not config.get("reset_inference", {}).get("enabled", True):
        return []
    inferred = []
    for name, acct in state.get("accounts", {}).items():
        rolled = _five_hour_rolled_since_poll(acct)
        if rolled and (acct.get("current_5h_pct") or 0) > 0:
            acct["five_hour_pct_pre_reset"] = acct.get("current_5h_pct")
            acct["current_5h_pct"] = 0.0
            acct["five_hour_reset_inferred"] = True
            inferred.append(name)
        elif acct.get("five_hour_reset_inferred") and not rolled:
            # A fresh poll has superseded the inference — clear the markers.
            acct.pop("five_hour_reset_inferred", None)
            acct.pop("five_hour_pct_pre_reset", None)
    return inferred


def _adaptive_sleep_seconds(state: dict, config: dict, base_interval: float) -> float:
    """GH #59 adaptive repoll: shorten the inter-cycle sleep so the daemon wakes
    just after the soonest known 5h reset and repolls promptly, instead of
    flying on the countdown-inferred value for a whole poll_interval.

    Only ever SHORTENS `base_interval` (returns it unchanged when no window
    resets sooner), and is floored by `min_repoll_seconds` so we never
    busy-loop. Accounts with no ticking 5h clock (util ≤ 0 → resets_at
    stale/meaningless) are ignored.
    """
    cfg = config.get("reset_inference", {})
    if not cfg.get("adaptive_repoll", True):
        return base_interval
    soonest = None
    for acct in state.get("accounts", {}).values():
        secs = _five_hour_remaining_seconds(None, acct)
        if secs is not None and secs > 0:
            soonest = secs if soonest is None else min(soonest, secs)
    if soonest is None:
        return base_interval
    buffer = cfg.get("repoll_buffer_seconds", 20)
    floor = cfg.get("min_repoll_seconds", 30)
    return max(floor, min(base_interval, soonest + buffer))


def _fmt_render_stamp_eastern() -> str:
    """Wall-clock stamp of WHEN THIS STATUSLINE WAS RENDERED, in US Eastern time.

    The statusline isn't re-rendered on a fixed cadence — Claude Code refreshes
    it on its own triggers (tmux events, new message, etc.), so a pane that's
    been idle can be displaying minutes-or-hours-old output. The `poll Nago`
    countdown only tells you when DATA was last polled relative to the render
    moment; it doesn't tell you how old the RENDER itself is.

    A wall-clock stamp lets the operator glance at their terminal clock and
    immediately tell the staleness of the line on screen: "shows 14:23ET but
    it's 14:45 now → this pane hasn't refreshed in 22min." Eastern because
    that's the operator's local timezone; format is short (HH:MMet) to fit
    inside the statusline budget without crowding the percentages.
    """
    return datetime.now(ZoneInfo("America/New_York")).strftime("%H:%Met")


def _sl_color_on(sl_cfg: dict) -> bool:
    """Whether to emit ANSI color in the statusline (GH #38).

    Claude Code's statusLine renders ANSI color codes, and `cus statusline` is
    normally piped to it (not a TTY) — so we force color through `click.echo`
    with `color=True` rather than letting click strip it on a non-TTY stream.
    Honors the NO_COLOR convention and the `statusline.color` config toggle.
    """
    if os.environ.get("NO_COLOR"):
        return False
    return bool(sl_cfg.get("color", True))


def _sl_pct(value: float, nxt: float, on: bool) -> str:
    """Format a usage percentage, colored by proximity to its swap point (GH #38).

    green below 80% of `nxt` · yellow approaching `nxt` · bold red at/over `nxt`,
    so the eye lands on whichever window is closest to triggering a swap.
    """
    txt = f"{value:.0f}%"
    if not on:
        return txt
    if value >= nxt:
        return click.style(txt, fg="red", bold=True)
    if value >= nxt * 0.8:
        return click.style(txt, fg="yellow")
    return click.style(txt, fg="green")


def _sl_mark_stale(txt: str, color_on: bool) -> str:
    """Mark a last-known (not-yet-reconfirmed) percentage: dim + trailing '~'.

    Used for `current_5h_pct` under `token_stale` (see `_pct_is_stale_known`).
    Deliberately skips `_sl_pct`'s proximity coloring (red/yellow/green) —
    the point here is "this number hasn't been reconfirmed by a live poll",
    not "how close is it to the swap threshold". The trailing '~' carries
    that meaning even with NO_COLOR or a non-color terminal.
    """
    marked = f"{txt}~"
    return click.style(marked, dim=True) if color_on else marked


def _sl_rolled_5h_label(acct: dict, current_pct: float, color_on: bool) -> str:
    """Render the `5h:↻reset(was X%)` badge for a rolled-over window (GH #59).

    Shown when `_five_hour_rolled_since_poll` says the 5h window reset after
    our last poll — the stored % is pre-reset stale, so displaying it bare
    would lie (e.g. `5h:96%` on a window that actually just reset to ~0).

    "was X%" prefers the `five_hour_pct_pre_reset` stash (written by the
    daemon's countdown-fallback inference when it zeroes `current_5h_pct`);
    falls back to the still-stale stored % when the statusline renders before
    the daemon's next cycle has run the inference. Shared by compact and
    verbose modes so the two renderings can't drift apart.
    """
    was_pct = acct.get("five_hour_pct_pre_reset")
    was_pct = was_pct if was_pct is not None else current_pct
    raw = f"5h:↻reset(was {was_pct:.0f}%)"
    return click.style(raw, fg="bright_magenta", bold=True) if color_on else raw
def _current_pane_pin(config: dict) -> tuple[str | None, str | None]:
    """Which pin (if any) applies to the pane rendering this statusline (GH #36).

    The statusline command is spawned by the claude process running inside the
    tmux pane, so it inherits TMUX_PANE (e.g. "%12") and CLAUDE_CODE_SESSION_ID
    from that pane's environment — the same two key shapes `cus pin` accepts
    and `session_is_pinned` matches for the daemon's skip-swap decision. Pane
    id is checked first, mirroring session_is_pinned's precedence, so both
    sides of the system always agree on which pin wins.

    Returns (matched_key, pinned_account) or (None, None) when this pane isn't
    pinned (or we're not under tmux and have no session id).
    """
    pinned = config.get("session_locks", {}).get("pinned", {}) or {}
    if not pinned:
        return None, None
    pane = os.environ.get("TMUX_PANE")
    if pane and pane in pinned:
        return pane, pinned[pane]
    sid = os.environ.get("CLAUDE_CODE_SESSION_ID")
    if sid and sid in pinned:
        return sid, pinned[sid]
    return None, None


def _sl_pin_label(pin_account: str | None, active: str, color_on: bool) -> str:
    """Format the statusline pin indicator (GH #36); empty string when unpinned.

    `📌<account>` when the pane is pinned to the account it's displayed on.
    `📌<account>!` (yellow bold when color is on) when the pin target differs
    from the account the pane is actually shown against — that mismatch is the
    interesting case: with hot-swap off (background-swap mode, the default
    here) a pinned pane still follows the single global credentials file, so
    a pin only prevents daemon-driven relaunches; it does NOT keep the pane's
    usage on the pinned account. The `!` tells the operator at a glance that
    their pin intent and the pane's real account have diverged.
    """
    if not pin_account:
        return ""
    if pin_account == active:
        raw = f"📌{pin_account}"
        return click.style(raw, dim=True) if color_on else raw
    raw = f"📌{pin_account}!"
    return click.style(raw, fg="yellow", bold=True) if color_on else raw


@cli.command(name="statusline")
@click.option("--verbose", "-v", is_flag=True, help="Verbose output: reset times + poll age + other accounts.")
@click.option("--compact", "-c", is_flag=True, help="Force compact output (overrides config).")
def statusline_cmd(verbose: bool, compact: bool) -> None:
    """One-line summary for Claude Code statusline integration.

    Compact (default): `cus:<account> 5h:NN% 7d:NN% nxt:NN%` or `🚨 SOS: ...`.
    Verbose: `cus <active>* 5h:NN%·1h23m 7d:NN%·3d nxt:NN% | <other> 5h:NN% 7d:NN% nxt:NN% | poll Nago`.

    Percentages are colored by proximity to the swap point (green/yellow/red)
    and the active account is bold-cyan when `statusline.color` is on (GH #38);
    NO_COLOR disables it. Claude Code's statusLine renders the ANSI.

    A 5h window that rolled over since the last poll is flagged live as
    `5h:↻reset(was X%)` in BOTH modes instead of showing the stale pre-reset
    percentage (GH #59).
    If THIS pane is pinned (`cus pin`), a `📌<account>` badge appears next to
    the account label in both modes; `📌<account>!` (yellow) means the pin
    target differs from the account the pane is actually shown on (GH #36).

    Verbosity priority: --compact flag > --verbose flag > config.statusline.verbose.

    Configure in ~/.claude/settings.json:
      "statusLine": {"type": "command", "command": "python3 /path/to/cus.py statusline --verbose"}
    or:
      "statusLine": {"type": "command", "command": "python3 /path/to/cus.py statusline"}
      (compact, controlled by ~/claude-accounts/config.yaml statusline.verbose)
    """
    if not STATE_JSON.exists():
        click.echo("cus: not init (run `cus init`)")
        return
    state = read_json(STATE_JSON)
    config = load_config()
    sl_cfg = config.get("statusline", {})
    color_on = _sl_color_on(sl_cfg)  # GH #38

    is_verbose = not compact and (verbose or sl_cfg.get("verbose", False))

    # Wall-clock stamp marking when THIS render fired (Eastern). Appended to
    # every output path (SOS / warning / compact / verbose) so the operator
    # can always tell render-time staleness regardless of which branch hit.
    render_stamp = _fmt_render_stamp_eastern()

    # SOS surfacing (2026-07-03): a human-action-needed condition is emitted as
    # its OWN first line, but we NO LONGER early-return — the normal account /
    # percentage line still renders below it. Before this, an SOS/warning
    # REPLACED the usage numbers entirely (early return), so once the lanes
    # target-starvation warning (diagnose Condition 2b) started firing every
    # cycle the operator lost sight of their percentages behind the alert.
    # User request 2026-07-03: "the SOS shouldn't block me from seeing the
    # percentages — can it be a new line." So the most-severe condition prints
    # on line 1 (loud), and the usage line always follows on line 2. Only the
    # top condition is shown here to keep the alert one line; `cus sos` lists
    # the full set.
    conditions = diagnose(state, config)
    urgent = [c for c in conditions if c.severity == "urgent"]
    warning = [c for c in conditions if c.severity == "warning"]
    if urgent:
        msg = f"🚨 cus SOS: {urgent[0].summary} — see ~/claude-accounts/SOS.md · {render_stamp}"
        click.echo(click.style(msg, fg="red", bold=True) if color_on else msg, color=color_on)
    elif warning:
        msg = f"⚠ cus: {warning[0].summary} · {render_stamp}"
        click.echo(click.style(msg, fg="yellow", bold=True) if color_on else msg, color=color_on)

    # Determine THIS session's account (the one this pane's claude actually
    # loaded tokens for) vs machine-wide active.
    #
    # Original assumption (pre-2026-06-08): tokens get loaded once at claude
    # start; if a swap happens after, the pane's claude still uses its loaded
    # creds until /exit + restart — so we show the SessionStart account and a
    # "pending swap / exit to apply" hint.
    #
    # Correction 2026-06-08 (GH #56, user-observed): that assumption only holds
    # for HOT-SWAP orchestration (which relaunches the pane). In BACKGROUND-swap
    # mode (hot_swap.enabled == False — the default here), the credential file
    # is swapped globally and Claude Code re-reads it on its next request, so a
    # running pane ALREADY follows the machine-active account with NO /exit.
    # The user confirmed empirically: background swap "takes usage to the right
    # account without restarting any session." So in background mode the
    # SessionStart account recorded in sessions.log is stale — the session is
    # really on machine_active — and the "→X (swap pending)" indicator is a
    # phantom (it pointed at the account the pane was ALREADY using). Treat the
    # session as being on machine_active and suppress the phantom pending hint.
    machine_active = state.get("active", "?")
    this_session_id = os.environ.get("CLAUDE_CODE_SESSION_ID")
    this_session_account = None
    if this_session_id:
        for e in reversed(_parse_sessions_log()):
            if e.get("session_id") == this_session_id:
                this_session_account = e.get("account")
                break
    # per_session (Phase 2.3): a slot-launched session carries
    # CLAUDE_CONFIG_DIR in its env (the statusline runs as a child of the
    # claude process, so it's inherited). The slot's CURRENT occupant per
    # state.slots beats every other signal — and must be re-resolved on
    # every render, because a per-slot swap changes the account under the
    # live session while the env var stays the same. Bare launches resolve
    # (None, None) and fall through to the global logic below.
    mount_name, mount_account = mount_account_from_env(state)
    is_mount_session = mount_account is not None
    # In background-swap mode the live session follows the global active
    # account on its next request, so the recorded SessionStart account is not
    # where it actually is. Only hot-swap mode has a genuine "loaded creds vs
    # machine-active" mismatch worth surfacing.
    hot_swap_on = config.get("hot_swap", {}).get("enabled", False)
    if is_mount_session:
        this_session_account = mount_account
    elif not hot_swap_on:
        this_session_account = machine_active
    # Display PRIMARY = this session's account if known, else machine-active.
    active = this_session_account or machine_active
    # A mount session differing from machine_active is NORMAL in per_session
    # mode (that's the point) — the pending-swap hint is a hot-swap-mode
    # concept only.
    pending_swap = (not is_mount_session) and this_session_account and this_session_account != machine_active

    # GH #36: is THIS pane pinned, and to which account? Computed once and
    # surfaced in both compact and verbose modes (SOS/warning early-returns
    # above intentionally stay uncluttered — human-action-needed beats pin
    # bookkeeping for the one line we get).
    _, pin_account = _current_pane_pin(config)
    pin_lbl = _sl_pin_label(pin_account, active, color_on)

    # per_session HARD pin badge: a slot session is physically bound to its
    # account (its own CLAUDE_CONFIG_DIR), unlike the advisory 📌 pin which in
    # background-swap mode doesn't actually route creds. 🔒<slot> (green) says
    # "this pane is locked to its own account and a global swap won't move it."
    # Re-resolved every render, so it tracks the slot's CURRENT occupant after
    # an in-place per-slot swap.
    slot_lbl = ""
    if is_mount_session and mount_name and mount_name.startswith(SLOT_PREFIX):
        raw = f"🔒{mount_name}"
        slot_lbl = click.style(raw, fg="green") if color_on else raw

    acct = state.get("accounts", {}).get(active, {})
    fh = acct.get("current_5h_pct", 0)
    sd = acct.get("current_7d_pct", 0)
    nx = acct.get("next_swap_at_pct", 50)

    # GH #38: a colored, bold active-account label reused by both compact and
    # verbose so the eye lands on which account is in play.
    active_lbl = click.style(active, fg="cyan", bold=True) if color_on else active
    nxt_lbl = (lambda n: click.style(f"nxt:{n}%", dim=True) if color_on else f"nxt:{n}%")

    if not is_verbose:
        # Compact mode. Render "?" per-window when we don't have reliable data
        # for that window — 5h and 7d are judged independently (see
        # _pct_is_unknown: token_stale alone leaves 5h displayable but 7d
        # still unknown).
        unknown5 = _pct_is_unknown(acct, "current_5h_pct")
        unknown7 = _pct_is_unknown(acct, "current_7d_pct")
        if unknown5 and unknown7:
            pin_bit = f" {pin_lbl}" if pin_lbl else ""
            slot_bit = f" {slot_lbl}" if slot_lbl else ""
            click.echo(f"cus:{active_lbl}{slot_bit}{pin_bit} 5h:? 7d:? {nxt_lbl(nx)} · {render_stamp}", color=color_on)
            return
        # GH #59: compact mode must flag a rolled-over 5h window live, exactly
        # like verbose mode already does. The reset countdown elapses between
        # polls; once it has, the stored 5h % is pre-reset stale (up to a full
        # poll interval of showing e.g. 96% on a window that actually reset to
        # ~0). Compact is the DEFAULT rendering, so before this it was the one
        # place the stale number still showed bare.
        if unknown5:
            fh_piece = "5h:?"
            marker_fh = 0.0
        else:
            rolled5 = _five_hour_rolled_since_poll(acct)
            if rolled5:
                fh_piece = _sl_rolled_5h_label(acct, fh, color_on)
                # The stale % must not drive the near-swap-point marker either —
                # the window just reset, so its real contribution is ~0. (If the
                # daemon's countdown inference already zeroed current_5h_pct this
                # is a no-op; this covers renders before that cycle runs.)
                marker_fh = 0.0
            elif _pct_is_stale_known(acct, "current_5h_pct"):
                # token_stale but not rolled: show the last-known 5h value,
                # marked as not-yet-reconfirmed, instead of hiding it or
                # showing it identically to a fresh poll.
                fh_piece = f"5h:{_sl_mark_stale(f'{fh:.0f}%', color_on)}"
                marker_fh = fh
            else:
                fh_piece = f"5h:{_sl_pct(fh, nx, color_on)}"
                marker_fh = fh
        # A masked (unknown) 7d value doesn't feed the near-swap-point marker —
        # we have no reset-inference for the 7-day window (unlike 5h above),
        # so a preserved pre-boundary number could falsely trip the warning.
        marker_sd = 0.0 if unknown7 else sd
        flag_marker = ""
        if max(marker_fh, marker_sd) >= nx:
            flag_marker = "⚠"
        elif max(marker_fh, marker_sd) >= nx * 0.8:
            flag_marker = "·"
        prefix_bits = [f"cus:{active_lbl}"]
        if slot_lbl:
            # Hard-pin (slot) badge rides next to the account so "which
            # account" and "locked to my own slot" read as one glance.
            prefix_bits.append(slot_lbl)
        if pin_lbl:
            # GH #36: pin badge rides directly next to the account label so
            # "which account" and "is it pinned here" read as one glance.
            prefix_bits.append(pin_lbl)
        if pending_swap:
            prefix_bits.append(f"→{machine_active}")
        if flag_marker:
            prefix_bits.append(flag_marker)
        prefix = " ".join(prefix_bits)
        # GH #25: add a small countdown to next poll so compact mode also
        # has a live "fresh data in X" signal that ticks down between polls.
        last_poll = acct.get("last_poll_ts")
        age = _time_since(last_poll)
        poll_part = ""
        if age is not None and age >= 0:
            poll_interval = config.get("poll_interval_seconds", 600)
            remaining = poll_interval - age
            if remaining > 0:
                poll_part = f" ({_fmt_duration(remaining)} to poll)"
            else:
                poll_part = " (poll due)"
        sd_piece = "7d:?" if unknown7 else f"7d:{_sl_pct(sd, nx, color_on)}"
        click.echo(
            f"{prefix} {fh_piece} {sd_piece} "
            f"{nxt_lbl(nx)}{poll_part} · {render_stamp}",
            color=color_on,
        )
        return

    # Verbose mode.
    show_resets = sl_cfg.get("show_reset_times", True)
    show_others = sl_cfg.get("show_other_accounts", True)
    show_poll = sl_cfg.get("show_poll_age", True)

    if pending_swap:
        # Lead with the mismatch so the user knows their pane's claude is
        # using a different account than what the machine just swapped to.
        # Restart this pane to pick up the machine-active account.
        pieces_prefix = f"cus this:{this_session_account} →{machine_active}(swap pending—/exit to apply) | "
    else:
        # Lead verbose with the hard-pin slot badge when this is a slot session.
        pieces_prefix = f"cus {slot_lbl} " if slot_lbl else "cus "

    def fmt_account(name: str, a: dict, is_active: bool) -> str:
        h5 = a.get("current_5h_pct", 0)
        h7 = a.get("current_7d_pct", 0)
        nxt = a.get("next_swap_at_pct", 50)
        # 5h and 7d are judged independently — token_stale alone leaves 5h
        # displayable (GH #59 reset-inference keeps it honest across a
        # reset boundary) but 7d still unknown (no equivalent inference).
        unknown5 = _pct_is_unknown(a, "current_5h_pct")
        unknown7 = _pct_is_unknown(a, "current_7d_pct")
        stale5 = _pct_is_stale_known(a, "current_5h_pct")
        flags = []
        if a.get("token_expired"):
            flags.append("EXP")
        if a.get("rate_limited"):
            flags.append("429")
        if a.get("token_stale"):
            flags.append("STALE")
        flag_str = "(" + ",".join(flags) + ")" if flags else ""
        marker = "*" if is_active else ""
        # GH #38: bold-cyan the active account; dim the others so the eye lands
        # on the one in play.
        name_lbl = f"{name}{marker}"
        if color_on:
            name_lbl = click.style(name_lbl, fg="cyan", bold=True) if is_active else click.style(name_lbl, dim=True)
        parts = [name_lbl]
        if unknown5:
            pct5 = "?"
        elif stale5:
            # token_stale but not rolled: last-known 5h value, marked as
            # not-yet-reconfirmed rather than shown identically to a fresh poll.
            pct5 = _sl_mark_stale(f"{h5:.0f}%", color_on)
        else:
            pct5 = _sl_pct(h5, nxt, color_on)
        pct7 = "?" if unknown7 else _sl_pct(h7, nxt, color_on)
        if show_resets:
            r5 = _time_until(a.get("five_hour_resets_at"))
            # 7d reset countdown uses the PROJECTED 72h refresh, not the raw API
            # resets_at (2026-07-05, gist: monperrus). The API points ~7 days out
            # at the oldest-tokens-age-off boundary; the real budget refresh lands
            # at the ~72h anchor, which is the number the operator actually wants.
            # Falls back to the API value on cold start (no observed drop yet).
            r7 = _time_until(projected_seven_day_reset(a, config))
            r7_raw = f"·{_fmt_duration(r7)}" if r7 is not None and r7 > 0 and not unknown7 else ""
            r7_str = click.style(r7_raw, dim=True) if color_on and r7_raw else r7_raw
            rolled5 = _five_hour_rolled_since_poll(a)
            if rolled5 and not unknown5:
                # GH #59: the live countdown has elapsed — the 5h window rolled
                # over since our last poll, so the displayed % is pre-reset
                # stale. Flag it (↻reset, was X%) rather than showing the stale
                # number bare, so the operator knows a reset happened without
                # waiting to repoll. Next poll confirms the new (~0%) value.
                # Rendering shared with compact mode via _sl_rolled_5h_label.
                parts.append(_sl_rolled_5h_label(a, h5, color_on))
            else:
                r5_raw = f"·{_fmt_duration(r5)}" if r5 is not None and r5 > 0 and not unknown5 else ""
                # Time left on the ACTIVE account's 5h clock is the key operational
                # number — it's the runway on the account you're actually using AND
                # what the burn-before-reset trigger (GH #42) keys on — so make it
                # POP (bold bright-magenta + a ↻ glyph) instead of dimming it like
                # everything else. Other accounts' 5h and everyone's 7d stay dim.
                if is_active and r5_raw:
                    r5_disp = f" ↻{_fmt_duration(r5)}"
                    r5_str = click.style(r5_disp, fg="bright_magenta", bold=True) if color_on else r5_disp
                else:
                    r5_str = click.style(r5_raw, dim=True) if color_on and r5_raw else r5_raw
                parts.append(f"5h:{pct5}{r5_str}")
            parts.append(f"7d:{pct7}{r7_str}")
        else:
            parts.append(f"5h:{pct5}")
            parts.append(f"7d:{pct7}")
        # GH #38: surface the next-swap-point (ladder threshold). It was only in
        # compact mode before — in verbose you couldn't tell how close each
        # account was to tripping a swap.
        parts.append(nxt_lbl(nxt))
        if flag_str:
            parts.append(click.style(flag_str, fg="red", bold=True) if color_on else flag_str)
        return " ".join(parts)

    pieces = [fmt_account(active, acct, True)]

    # GH #36: pin badge as its own segment right after the active account, so
    # verbose reads "cus <active> ... | 📌<pin> | <other accounts> ...".
    if pin_lbl:
        pieces.append(pin_lbl)

    if show_others:
        for name, a in sorted(state.get("accounts", {}).items()):
            if name == active:
                continue
            pieces.append(fmt_account(name, a, False))

    if show_poll:
        last_poll = acct.get("last_poll_ts")
        age = _time_since(last_poll)
        if age is not None and age >= 0:
            # GH #25: show countdown to next poll in addition to age. The
            # statusline is re-rendered on Claude Code's own cadence
            # (typically a few seconds), so each render shows a live count
            # toward the next poll. "polled 7m ago, next in 3m" is more
            # useful than just "polled 7m ago" — operator can see when
            # fresh data is coming.
            poll_interval = config.get("poll_interval_seconds", 600)
            remaining = poll_interval - age
            if remaining > 0:
                poll_piece = f"poll {_fmt_duration(age)}ago·next in {_fmt_duration(remaining)}"
            else:
                poll_piece = f"poll {_fmt_duration(age)}ago·due now"
            pieces.append(click.style(poll_piece, dim=True) if color_on else poll_piece)

    click.echo(pieces_prefix + " | ".join(pieces) + f" · {render_stamp}", color=color_on)


@cli.command(name="decisions")
@click.option("-n", "--tail", default=20, help="Show the last N decision records.")
@click.option("--swaps-only", is_flag=True, help="Hide routine 'hold' cycles; show only swap/defer/would-swap.")
@click.option("--json", "as_json", is_flag=True, help="Emit raw JSONL records instead of the formatted view.")
def decisions_cmd(tail: int, swaps_only: bool, as_json: bool) -> None:
    """Show recent swap decisions — who / what / where / when / why (GH #56).

    Reads ~/claude-accounts/decisions.jsonl, one structured record per daemon
    cycle: the active account + usage (who), the action (what: swap / hold /
    defer / would_swap), the affected panes (where), the timestamp (when), and
    the gate + reason that decided it (why).
    """
    if not DECISIONS_LOG.exists():
        click.echo("No decisions logged yet (decisions.jsonl absent — the daemon writes one per cycle).")
        return
    try:
        lines = DECISIONS_LOG.read_text().splitlines()
    except OSError as e:
        click.echo(f"Could not read {DECISIONS_LOG}: {e}")
        return
    records = []
    for ln in lines:
        ln = ln.strip()
        if not ln:
            continue
        try:
            records.append(json.loads(ln))
        except json.JSONDecodeError:
            continue
    if swaps_only:
        records = [r for r in records if r.get("action") != "hold"]
    records = records[-tail:]
    if not records:
        click.echo("No matching decision records.")
        return
    if as_json:
        for r in records:
            click.echo(json.dumps(r))
        return

    action_style = {
        "swap": ("SWAP", "green"),
        "would_swap": ("WOULD-SWAP", "cyan"),
        "defer": ("DEFER", "yellow"),
        "hold": ("hold", "bright_black"),
    }
    color_on = sys.stdout.isatty()
    for r in records:
        action = r.get("action", "?")
        label, color = action_style.get(action, (action.upper(), "white"))
        when = r.get("when", "?")
        who = r.get("who", {})
        active = who.get("active", "?")
        a5, a7 = who.get("5h"), who.get("7d")
        gate = r.get("gate", "?")
        target = r.get("target")
        tier = r.get("tier")
        arrow = (f" → {target}" + (f" (tier {tier})" if tier else "")) if target else ""
        head = click.style(f"{label:<11}", fg=color, bold=True) if color_on else f"{label:<11}"
        usage = f"{active} 5h={a5}% 7d={a7}%" if a5 is not None else str(active)
        click.echo(f"{when}  {head} {usage}{arrow}  [{gate}]")
        reason = r.get("reason")
        if reason:
            click.echo(f"             why:   {reason}")
        where = r.get("where") or {}
        panes = where.get("panes")
        if panes:
            shown = ", ".join(panes[:8]) + (f" +{len(panes) - 8} more" if len(panes) > 8 else "")
            click.echo(f"             where: {where.get('live_pane_count', len(panes))} panes — {shown}")


@cli.command(name="sos")
@click.option("--quiet", is_flag=True, help="No output if all clear (for scripting).")
def sos_cmd(quiet: bool) -> None:
    """Check for conditions requiring human action.

    Exit code 0 = all clear, 1 = action needed.

    Prints each condition with its severity, summary, and concrete next
    step. Conditions checked: token expired, all accounts blocked, active
    over threshold with no target, stale poll, daemon dead.

    Also writes ~/claude-accounts/SOS.md (or removes it if clear) so the
    next time you `cat` that file you see what's needed. Fires notify-send
    desktop notification on signature changes (if installed). The daemon
    does this too on every cycle.
    """
    state = load_state() if STATE_JSON.exists() else {"active": None, "accounts": {}}
    conditions = diagnose(state)
    maybe_write_sos(conditions, state)

    if not conditions:
        if not quiet:
            click.echo("✓ All clear — no human action needed.")
        sys.exit(0)

    click.echo("🚨 SOS — human action needed:\n")
    for c in conditions:
        # Three tiers: "info" is a soft, non-actionable heads-up (e.g. the premium
        # lane's only swap targets are idle-stale but WILL refresh on swap — GH
        # 2026-07-06 false-URGENT fix), so it renders [INFO], not the [WARNING]
        # that every non-urgent condition used to collapse to.
        prefix = ("[URGENT]" if c.severity == "urgent"
                  else "[WARNING]" if c.severity == "warning"
                  else "[INFO]")
        click.echo(f"  {prefix} {c.summary}")
        for line in c.action.split("\n"):
            click.echo(f"    {line}")
        click.echo()
    click.echo(f"Also written to: {SOS_MD}")
    sys.exit(1)


@cli.command(name="add")
@click.argument("name")
@click.option("--exec", "exec_flag", is_flag=True, help="Immediately exec `claude` under the new dir.")
def add_cmd(name: str, exec_flag: bool) -> None:
    """Create a new account dir and print the login command.

    Usage: `cus add work` creates ~/claude-accounts/account-work/ as a
    valid CLAUDE_CONFIG_DIR, symlinks shared subdirs to ~/.claude/, and
    prints the exact command to log in.

    After login, run `cus init --force && cus poll` to register the new
    account: init writes it into state.json and refreshes its OAuth identity
    in meta.yaml; poll then fills in usage. (poll alone cannot register a new
    account — it only iterates accounts already present in state.json.)
    """
    if not name.replace("-", "").replace("_", "").isalnum():
        click.echo(f"Bad name '{name}' — use alphanumeric + dashes/underscores only.")
        sys.exit(1)
    if name == "default":
        click.echo("'default' is reserved for the live ~/.claude/ dir.")
        sys.exit(1)
    dst = ACCOUNTS_DIR / f"account-{name}"
    if dst.exists():
        click.echo(f"{dst} already exists — use `cus relogin {name}` to refresh tokens.")
        sys.exit(1)

    dst.mkdir(parents=True)
    # Minimum-viable CLAUDE_CONFIG_DIR: empty .claude.json, no .credentials.json
    # (Claude /login will write it). Symlink shared dirs.
    write_json(dst / ".claude.json", {})
    for sub in SHARED_SYMLINK_SUBDIRS:
        link = dst / sub
        target = CLAUDE_DIR / sub
        if target.exists():
            link.symlink_to(target)

    # meta.yaml with sensible defaults
    write_yaml(dst / "meta.yaml", {
        "name": name,
        "source_dir": str(dst),
        "oauth_email": "unknown (run /login)",
        "oauth_account_uuid": "unknown",
        "priority": 1,
        "locked_sessions": [],
        "imported_ts": now_iso(),
    })

    cmd = _relogin_command_for(name)
    click.echo(f"Created {dst}")
    click.echo()
    click.echo(f"To log in to this account, run:")
    click.echo(f"  {cmd}")
    click.echo()
    click.echo(f"After logging in, register it in state.json:")
    click.echo(f"  python3 ~/repos/claude-usage-swap/cus.py init --force && python3 ~/repos/claude-usage-swap/cus.py poll")

    if exec_flag:
        click.echo()
        click.echo("Launching claude now...")
        env = os.environ.copy()
        env["CLAUDE_CONFIG_DIR"] = str(dst)
        os.execvpe("claude", ["claude"], env)


@cli.command(name="relogin")
@click.argument("name")
@click.option("--exec", "exec_flag", is_flag=True, help="Immediately exec `claude` in the right config context.")
@click.option("--finish", "finish_flag", is_flag=True,
              help="GH #77: after a storage-dir re-login of the ACTIVE account, install the fresh snapshot into the live files.")
def relogin_cmd(name: str, exec_flag: bool, finish_flag: bool) -> None:
    """Print the command to re-login an existing account (refresh OAuth tokens).

    Run this when `cus sos` reports a token expired for a given account.
    After the re-login, `cus poll` to update state.

    GH #77: the printed command depends on whether the account is ACTIVE.
    Active accounts re-login via bare `claude /login` (the live files are
    their authoritative slot); inactive accounts re-login under their
    managed storage dir. If you already re-logged an ACTIVE account under
    its storage dir, `--finish` installs the fresh snapshot into the live
    files (with an expiresAt freshness check so it can never install older
    tokens over newer ones).
    """
    dst = ACCOUNTS_DIR / f"account-{name}"
    if name != "default" and not dst.exists():
        click.echo(f"Unknown account '{name}'. Run `cus list` to see configured accounts.")
        sys.exit(1)

    if finish_flag:
        try:
            finish_active_relogin(name)
        except RuntimeError as e:
            click.echo(f"ERROR: {e}")
            sys.exit(1)
        click.echo(f"✓ Installed '{name}' snapshot into the live files ({CREDS_JSON}).")
        click.echo(f"  Verify with: python3 ~/repos/claude-usage-swap/cus.py poll --account {name}")
        return

    try:
        state = load_state() if STATE_JSON.exists() else {}
    except (json.JSONDecodeError, OSError):
        state = {}
    is_active = state.get("active") == name
    cmd = _relogin_command_for(name)
    click.echo(f"To re-login {name}, run:")
    click.echo(f"  {cmd}")
    if is_active:
        click.echo(f"  ('{name}' is the ACTIVE account, so the login writes the live files directly — GH #77.)")
        click.echo(f"  (Already logged in under {dst}/ instead? Run: cus relogin {name} --finish)")
    click.echo()
    click.echo(f"After logging in:")
    click.echo(f"  python3 ~/repos/claude-usage-swap/cus.py poll")
    if exec_flag:
        click.echo()
        click.echo("Launching claude now...")
        env = os.environ.copy()
        if is_active:
            # Live files are the active account's slot — no CLAUDE_CONFIG_DIR,
            # matching the printed command (GH #77).
            env.pop("CLAUDE_CONFIG_DIR", None)
        else:
            env["CLAUDE_CONFIG_DIR"] = str(dst)
        os.execvpe("claude", ["claude"], env)


def _login_mount_status_line(rec: dict, config: dict) -> str:
    """One-line human summary of a provisioned-login record for `--list`."""
    account, slot = rec["account"], rec["slot"]
    if not rec["has_login"]:
        return (f"  {account:<12} {slot:<10} "
                + click.style("dir exists but NO usable login — run login-mount ... --finish "
                              "after logging in", fg="yellow"))
    state, days_left = login_expiry_state(account, slot, config)
    email = rec.get("identity_email") or "?"
    prov = rec.get("provenance") or {}
    fp = prov.get("refresh_fp") or "?"
    if state == "expired":
        fresh = click.style(f"EXPIRED ~{-days_left:.0f}d ago (assumed)", fg="red")
    elif state == "near":
        fresh = click.style(f"expiring in ~{days_left:.0f}d (assumed)", fg="yellow")
    elif state == "ok":
        fresh = click.style(f"~{days_left:.0f}d left (assumed)", fg="green")
    else:
        fresh = "age unknown"
    return f"  {account:<12} {slot:<10} {email:<28} {fp}  {fresh}"


def _write_family_provenance(account: str, family_id: str, email: str | None,
                             bootstrapped: bool = False) -> dict:
    """Record provenance for a pooled family and return it."""
    rt = _credential_refresh_token(read_json(login_family_creds_path(account, family_id)))
    prov = {
        "account": account,
        "family_id": family_id,
        "minted_ts": now_iso(),
        "source_email": email or "unknown",
        "refresh_fp": _refresh_fingerprint(rt) if rt else None,
        "bootstrapped": bootstrapped,
    }
    write_json(login_family_provenance_path(account, family_id), prov)
    return prov


def _login_mount_pool(account: str, config: dict, exec_flag: bool, finish_flag: bool,
                      force: bool, from_existing: bool) -> None:
    """Account-keyed pool provisioning for `cus login-mount <account>` (2026-07-03).

    Provisions the account's NEXT backup family. Run it pool_size times at
    onboarding to build a pool a lane can borrow from without per-lane setup."""
    account_dir = ACCOUNTS_DIR / f"account-{account}"
    if not account_dir.exists():
        raise click.ClickException(
            f"Unknown account '{account}' — no {account_dir}. Run `cus list`, or `cus add {account}` first.")
    want = config.get("independent_logins", {}).get("pool_size", 3)

    if from_existing:
        # Seed family-1 from the snapshot (no browser). Only clobber-safe as the
        # PRIMARY (the account's own live mount) — a 2nd simultaneous mount needs
        # a real /login for a distinct family. Refuse if a pool already exists.
        if list_login_families(account):
            raise click.ClickException(
                f"{account} already has pooled families; --from-existing only seeds the first. "
                f"Add more with a real login: `cus login-mount {account}`.")
        snap = account_creds_path(account)
        try:
            ok = _credential_refresh_token(read_json(snap)) is not None
        except (json.JSONDecodeError, OSError):
            ok = False
        if not ok:
            raise click.ClickException(f"{account}'s snapshot ({snap}) has no usable creds to bootstrap from.")
        fam = "family-1"
        scaffold_login_family_dir(account, fam)
        atomic_copy(snap, login_family_creds_path(account, fam), mode=0o600)
        snap_cj = account_dir / ".claude.json"
        if snap_cj.exists():
            atomic_copy(snap_cj, login_family_dir(account, fam) / ".claude.json", mode=0o600)
        email = account_canonical_identity(account).get("emailAddress")
        prov = _write_family_provenance(account, fam, email, bootstrapped=True)
        click.echo(click.style(f"✓ Seeded {account}/{fam} from the on-disk snapshot (bootstrapped copy).", fg="green"))
        click.echo(f"  identity: {email or 'unknown'}   fingerprint: {prov['refresh_fp']}")
        click.echo(f"  This is NOT independent from the snapshot — for a 2nd live mount, add a real login.")
        return

    if finish_flag:
        fam = latest_family_id(account)
        if fam is None or not _family_creds_usable(account, fam):
            raise click.ClickException(
                f"No usable freshly-logged-in family for '{account}'. Did the /login complete? "
                f"Re-run `cus login-mount {account}` and log in as '{account}' first.")
        logged = family_identity(account, fam)
        canonical = account_canonical_identity(account)
        match = _identities_match(logged, canonical)
        logged_email = logged.get("emailAddress") or "unknown"
        canon_email = canonical.get("emailAddress") or "unknown"
        if match is False and not force:
            raise click.ClickException(
                f"Login identity mismatch: you logged in as '{logged_email}' but account '{account}' is "
                f"'{canon_email}' (the wrong-account trap, 2026-07-01). Re-login as '{account}', or --force.")
        if match is None:
            click.echo(click.style("  (could not verify identity — recording anyway)", fg="yellow"))
        prov = _write_family_provenance(account, fam, logged_email)
        n = len(list_login_families(account))
        click.echo(click.style(f"✓ Recorded {account}/{fam} (pool now {n} family(ies)).", fg="green"))
        click.echo(f"  identity: {logged_email}   fingerprint: {prov['refresh_fp']}")
        if n < want:
            click.echo(f"  {want - n} more to reach pool_size {want}: run `cus login-mount {account}` again.")
        if not independent_logins_enabled(config):
            click.echo("Note: swaps still copy the snapshot until you set "
                       "independent_logins.use_independent_logins: true.")
        return

    # --- provision (print/exec) mode: scaffold the next family ---
    fam = next_family_id(account)
    dst = scaffold_login_family_dir(account, fam)
    have = len(list_login_families(account))
    login_cmd = f"CLAUDE_CONFIG_DIR={dst}/ claude"
    click.echo(f"Provisioning {account}/{fam} (pool currently {have}, target pool_size {want}).")
    click.echo(f"Store dir: {dst}")
    click.echo()
    click.echo(f"1. Run this and log in AS '{account}' (a NEW independent session for the same account):")
    click.echo(f"     {login_cmd}")
    click.echo("2. After the login completes and you /exit, record it:")
    click.echo(f"     python3 ~/repos/claude-usage-swap/cus.py login-mount {account} --finish")
    if exec_flag:
        click.echo()
        click.echo(f"(--exec) launching claude under {dst} …")
        os.execvp("claude", ["claude"])


@cli.command(name="login-mount")
@click.argument("slot", required=False)
@click.argument("account", required=False)
@click.option("--exec", "exec_flag", is_flag=True,
              help="Immediately exec `claude` under the login-store dir to start the /login.")
@click.option("--finish", "finish_flag", is_flag=True,
              help="After the /login completes, verify it landed on the right account and record provenance.")
@click.option("--list", "list_flag", is_flag=True,
              help="List every provisioned independent login and exit.")
@click.option("--force", is_flag=True,
              help="With --finish: record provenance even if the login's identity does not match the account.")
@click.option("--from-existing", "from_existing", is_flag=True,
              help="Seed the store from the account's on-disk snapshot instead of an interactive /login. "
                   "No browser needed, but only clobber-safe as the SOLE live mount for that account.")
def login_mount_cmd(slot: str | None, account: str | None, exec_flag: bool,
                    finish_flag: bool, list_flag: bool, force: bool, from_existing: bool) -> None:
    """Provision an INDEPENDENT `/login` for an account into one slot (GH #109).

    Today a slot's live credentials are a COPY of one account snapshot; two
    slots on the same account share one OAuth refresh-token family and clobber
    each other on rotation (#104/#103/#3). This gives a (slot, account) pair its
    OWN login family so the same account can safely back multiple live mounts.

    Two-step, mirroring `cus relogin` (the /login flow is interactive — a browser
    + paste-back — and cannot be driven for you):

      1. `cus login-mount slot-3 rayi1`  → scaffolds the store dir and prints the
         `CLAUDE_CONFIG_DIR=... claude` command. Run it yourself and log in AS
         rayi1 (the SAME Anthropic account the slot will host).
      2. `cus login-mount slot-3 rayi1 --finish`  → verifies the login landed on
         rayi1 (not a wrong account — see the 2026-07-01 duplicate-identity
         incident) and records provenance.

    BOOTSTRAP (no login): `cus login-mount slot-3 rayi1 --from-existing` seeds the
    store from rayi1's on-disk snapshot. Use this for an account that backs just
    ONE live mount — it costs no browser login. It is NOT independent from the
    snapshot, so a SECOND simultaneous mount of the same account still needs a
    real `/login` (only the browser flow mints a distinct refresh-token family).
    `cus sos` flags any two mounts that end up sharing one family.

    Provisioning is LAZY: mint a login the first time a mount needs to host an
    account. `cus login-mount --list` (or `cus status`) shows what exists.

    The swap path consumes these only when independent_logins.use_independent_logins
    is on (Phase 2 gate; default off = today's shared-snapshot copy path).
    """
    config = load_config()

    def _is_slot_name(s: str) -> bool:
        return s.startswith(SLOT_PREFIX) and s.removeprefix(SLOT_PREFIX).isdigit()

    # POOL model (2026-07-03): `cus login-mount <account>` (single positional
    # that is NOT a slot name) provisions the account's NEXT backup family. Run
    # it pool_size times per account at onboarding — no per-lane setup. The
    # legacy two-arg `cus login-mount <slot> <account>` form still works below.
    if account is None and slot is not None and not _is_slot_name(slot):
        account, slot = slot, None
    pool_mode = slot is None and account is not None

    if list_flag:
        # Pool view: families per account (the onboarding-facing shape).
        root = login_store_root()
        pool_accts = sorted(p.name for p in root.iterdir() if p.is_dir()) if root.exists() else []
        pool_accts = [a for a in pool_accts if list_login_families(a)]
        if pool_accts:
            click.echo("Independent-login pools (GH #109):")
            want = config.get("independent_logins", {}).get("pool_size", 3)
            for a in pool_accts:
                fams = list_login_families(a)
                short = "  ".join(f"{f}[{login_expiry_state(a, f, config)[0]}]" for f in fams)
                flag = "" if len(fams) >= want else click.style(f"  (< pool_size {want})", fg="yellow")
                click.echo(f"  {a}: {len(fams)} family(ies)  {short}{flag}")
        # Legacy per-(slot,account) entries, if any remain.
        recs = list_provisioned_logins()
        if recs:
            click.echo("Legacy per-slot logins:")
            for rec in recs:
                click.echo(_login_mount_status_line(rec, config))
        if not pool_accts and not recs:
            click.echo("No independent logins provisioned yet.")
            click.echo("Provision a pool with: cus login-mount <account>   (run pool_size times)")
        return

    if pool_mode:
        _login_mount_pool(account, config, exec_flag, finish_flag, force, from_existing)
        return

    if not slot or not account:
        raise click.UsageError("login-mount needs an ACCOUNT (e.g. `cus login-mount merkos`), "
                               "or use --list.")

    # Validate the slot name shape. We allow a slot dir that doesn't exist yet:
    # a login can be provisioned ahead of the slot being created (lazy).
    if not (slot.startswith(SLOT_PREFIX) and slot.removeprefix(SLOT_PREFIX).isdigit()):
        raise click.UsageError(f"Bad slot '{slot}' — expected 'slot-<n>' (e.g. slot-3).")

    # Validate the account is one we know about (has a canonical snapshot dir).
    account_dir = ACCOUNTS_DIR / f"account-{account}"
    if not account_dir.exists():
        raise click.ClickException(
            f"Unknown account '{account}' — no {account_dir}. Run `cus list` to see accounts, "
            f"or `cus add {account}` first.")

    if from_existing:
        # Bootstrap: copy the account's on-disk snapshot into the store. No
        # login. NOT independent from the snapshot — safe only as the sole mount.
        snap_creds = account_creds_path(account)
        try:
            snap_ok = _credential_refresh_token(read_json(snap_creds)) is not None
        except (json.JSONDecodeError, OSError):
            snap_ok = False
        if not snap_ok:
            raise click.ClickException(
                f"{account}'s snapshot ({snap_creds}) has no usable credentials to bootstrap from. "
                f"Log in normally instead: `cus login-mount {slot} {account}`.")
        scaffold_login_store_dir(account, slot)
        # Copy creds + identity so the store entry mirrors the snapshot exactly.
        atomic_copy(snap_creds, login_store_creds_path(account, slot), mode=0o600)
        snap_cj = account_dir / ".claude.json"
        if snap_cj.exists():
            atomic_copy(snap_cj, login_store_cj_path(account, slot), mode=0o600)
        email = account_canonical_identity(account).get("emailAddress")
        prov = write_login_provenance(account, slot, email, bootstrapped=True)
        click.echo(click.style(f"✓ Bootstrapped ({slot}, {account}) from the on-disk snapshot.", fg="green"))
        click.echo(f"  identity: {email or 'unknown'}   fingerprint: {prov['refresh_fp']}   (bootstrapped copy)")
        # Warn if this now duplicates a family already on another mount.
        dups = [d for d in duplicate_login_families() if prov["refresh_fp"] == d["refresh_fp"]]
        if dups:
            click.echo(click.style(
                f"  ⚠ this family is now on multiple mounts: {dups[0]['pairs']}. Two mounts on ONE "
                f"family clobber on rotation — the 2nd+ mount must be a real login "
                f"(`cus login-mount <slot> {account}`), not --from-existing.", fg="yellow"))
        return

    if finish_flag:
        creds_path = login_store_creds_path(account, slot)
        if not has_independent_login(account, slot):
            raise click.ClickException(
                f"No usable login at {creds_path}. Did the /login complete? "
                f"Re-run the login command (without --finish) and log in as '{account}' first.")

        # Duplicate-identity guard (2026-07-01 incident): the store dir's
        # .claude.json carries whatever account the operator actually logged
        # into. If that isn't the account this pair is FOR, recording it would
        # seed a swap with the wrong account's tokens — refuse unless --force.
        logged = login_store_identity(account, slot)
        canonical = account_canonical_identity(account)
        match = _identities_match(logged, canonical)
        logged_email = logged.get("emailAddress") or "unknown"
        canon_email = canonical.get("emailAddress") or "unknown"
        if match is False and not force:
            raise click.ClickException(
                f"Login identity mismatch: you logged in as '{logged_email}' but slot/account "
                f"'{account}' is '{canon_email}'. This is the wrong-account trap (2026-07-01). "
                f"Re-login as '{account}', or pass --force if you are certain the identities are "
                f"the same account under different emails.")
        if match is None:
            click.echo(click.style(
                f"  (could not verify identity — no comparable oauthAccount fields on "
                f"'{account}'s snapshot; recording anyway)", fg="yellow"))

        prov = write_login_provenance(account, slot, logged_email)
        click.echo(click.style(f"✓ Recorded independent login for ({slot}, {account}).", fg="green"))
        click.echo(f"  identity: {logged_email}   fingerprint: {prov['refresh_fp']}")
        click.echo(f"  stored at: {login_store_dir(account, slot)}")
        missing = _slots_missing_logins(load_state() if STATE_JSON.exists() else {})
        if missing:
            click.echo()
            click.echo("Still needing an independent login (for the accounts their slots currently hold):")
            for m_slot, m_acct in missing:
                click.echo(f"  cus login-mount {m_slot} {m_acct}")
        if not config.get("independent_logins", {}).get("use_independent_logins", False):
            click.echo()
            click.echo("Note: the swap path still copies the shared snapshot until you enable Phase 2 "
                       "(independent_logins.use_independent_logins: true in config.yaml).")
        return

    # --- provision (print/exec) mode ---
    dst = scaffold_login_store_dir(account, slot)
    login_cmd = f"CLAUDE_CONFIG_DIR={dst}/ claude"
    click.echo(f"Provisioning an independent login for ({slot}, {account}).")
    click.echo(f"Store dir: {dst}")
    click.echo()
    click.echo(f"1. Run this and log in AS '{account}' (the same Anthropic account this slot will host):")
    click.echo(f"     {login_cmd}")
    click.echo("2. After the login completes and you /exit, record it:")
    click.echo(f"     python3 ~/repos/claude-usage-swap/cus.py login-mount {slot} {account} --finish")
    click.echo()
    click.echo("Logging in as a DIFFERENT account than '" + account + "' will be refused at --finish "
               "(wrong-account trap, 2026-07-01).")

    if exec_flag:
        click.echo()
        click.echo("Launching claude now (log in, then /exit and run --finish)...")
        env = os.environ.copy()
        env["CLAUDE_CONFIG_DIR"] = str(dst)
        os.execvpe("claude", ["claude"], env)


def _describe_creds_backup(path: Path) -> str:
    """One human-readable line about a backup generation: timestamp, age,
    and whether the payload still looks restorable (has a refresh token)."""
    ts_raw = path.name.rsplit(".bak.", 1)[-1]
    age = "age unknown"
    try:
        ts_dt = datetime.strptime(ts_raw, "%Y%m%dT%H%M%S.%fZ").replace(tzinfo=timezone.utc)
        delta = datetime.now(timezone.utc) - ts_dt
        hours = delta.total_seconds() / 3600
        age = f"{hours / 24:.1f}d old" if hours >= 48 else f"{hours:.1f}h old"
    except ValueError:
        pass
    payload = "unreadable"
    try:
        oauth = read_json(path).get("claudeAiOauth", {})
        if isinstance(oauth, dict) and oauth.get("refreshToken"):
            exp = oauth.get("expiresAt")
            exp_note = ""
            if isinstance(exp, (int, float)):
                exp_dt = datetime.fromtimestamp(exp / 1000, tz=timezone.utc)
                exp_note = f", access token {'expired' if exp_dt < datetime.now(timezone.utc) else 'valid'} (refresh token may still work)"
            payload = f"has refresh token{exp_note}"
        else:
            payload = "NO refresh token — not restorable"
    except (json.JSONDecodeError, OSError):
        pass
    return f"{ts_raw}  ({age}; {payload})"


@cli.command(name="restore-creds")
@click.argument("account")
@click.option("--list", "list_flag", is_flag=True, help="List available backup generations and exit.")
@click.option("--from", "from_ts", default=None, metavar="TS",
              help="Timestamp of the generation to restore (as shown by --list). Default: newest.")
@click.option("--live", "live_flag", is_flag=True,
              help="Also install the restored creds as the live ~/.claude/.credentials.json (only when the account is active).")
def restore_creds_cmd(account: str, list_flag: bool, from_ts: str | None, live_flag: bool) -> None:
    """Restore an account's credentials snapshot from a rotated backup (GH #79).

    Every write that replaces a credentials file first preserves the old
    content as `.credentials.json.bak.<ts>` (newest 5 kept). When a clobber
    bug (#3/#70/#75/#76/#77 class) destroys a snapshot's refresh token, this
    command brings back a previous generation — usually faster than an
    interactive browser re-login, and the only option at 2am.

    Restores are reversible: the current snapshot is backed up before being
    replaced, so a bad restore is undone by restoring the newest generation.
    """
    acct_dir = ACCOUNTS_DIR / f"account-{account}"
    if not acct_dir.exists():
        click.echo(f"Unknown account '{account}' ({acct_dir} does not exist). Run `cus list`.")
        sys.exit(1)

    backups = list_creds_backups(account)
    if list_flag:
        if not backups:
            click.echo(f"No credential backups for '{account}' yet. Backups accumulate as swaps overwrite the snapshot.")
            return
        click.echo(f"Credential backups for '{account}' (newest first, keep={CREDS_BACKUP_KEEP}):")
        for b in backups:
            click.echo(f"  {_describe_creds_backup(b)}")
        click.echo()
        click.echo(f"Restore the newest:  cus restore-creds {account}")
        click.echo(f"Restore a specific:  cus restore-creds {account} --from <TS>")
        return

    if not backups:
        click.echo(f"No credential backups for '{account}' — nothing to restore. "
                   f"If the account is locked out, re-login instead: `cus relogin {account}`.")
        sys.exit(1)

    if from_ts:
        chosen = next((b for b in backups if b.name.endswith(f".bak.{from_ts}")), None)
        if chosen is None:
            click.echo(f"No backup with timestamp '{from_ts}'. Available:")
            for b in backups:
                click.echo(f"  {b.name.rsplit('.bak.', 1)[-1]}")
            sys.exit(1)
    else:
        chosen = backups[0]

    try:
        state = load_state() if STATE_JSON.exists() else {}
    except (json.JSONDecodeError, OSError):
        state = {}
    is_active = state.get("active") == account

    if live_flag and not is_active:
        click.echo(f"--live refused: '{account}' is not the active account "
                   f"(active: {state.get('active')}). The live file belongs to the active "
                   f"account; restoring another account's tokens into it would clobber it.")
        sys.exit(1)

    click.echo(f"Restoring '{account}' snapshot from: {_describe_creds_backup(chosen)}")
    restore_creds_backup(account, chosen, into_live=live_flag)
    click.echo(f"  ✓ snapshot restored -> {acct_dir / '.credentials.json'}")
    if live_flag:
        click.echo(f"  ✓ live file replaced -> {CREDS_JSON}")
    elif is_active:
        click.echo(f"  NOTE: '{account}' is the ACTIVE account, and Claude reads the LIVE file, "
                   f"not the snapshot. Re-run with --live to install the restored tokens live.")
    click.echo("  Restore is best-effort: if the newer refresh token was already used, "
               "Anthropic may have invalidated this one server-side. Verify with `cus poll`.")


@cli.command(name="install")
@click.option("--skip-hooks", is_flag=True, help="Don't install Claude Code hooks.")
@click.option("--skip-statusline", is_flag=True, help="Don't wire the statusLine in ~/.claude/settings.json.")
@click.option("--skip-systemd", is_flag=True, help="Don't install/enable the systemd --user service.")
@click.option("--skip-wrapper", is_flag=True, help="Don't symlink ~/bin/cus.")
@click.option("--force", is_flag=True, help="Pass --force to init (refresh stale credential snapshots).")
def install_cmd(skip_hooks: bool, skip_statusline: bool, skip_systemd: bool, skip_wrapper: bool, force: bool) -> None:
    """One-command setup: init + hooks + statusline + systemd + ~/bin/cus.

    Idempotent. Re-run safely; skipped/already-done steps are reported.
    Use --skip-* to opt out of individual pieces.

    After running this, you can:
      cus status              # see what's happening
      cus add <name>          # add another account
      cus relogin <name>      # refresh an expired account's token
    """
    click.echo(click.style("=== claude-usage-swap installation ===", bold=True))
    click.echo()

    # 1. Init
    click.echo(click.style("[1/5] Discover and import accounts...", bold=True))
    ctx = click.get_current_context()
    ctx.invoke(init, dry_run=False, force=force)
    click.echo()

    # 2. Hooks
    if not skip_hooks:
        click.echo(click.style("[2/5] Install Claude Code hooks...", bold=True))
        cfg = load_config()
        result = install_hooks(cfg)
        for event, status in result.items():
            click.echo(f"  {event:<30} {status}")
    else:
        click.echo(click.style("[2/5] Skipped hook install (--skip-hooks).", bold=True))
    click.echo()

    # 3. StatusLine wiring
    if not skip_statusline:
        click.echo(click.style("[3/5] Wire statusLine in ~/.claude/settings.json...", bold=True))
        settings_path = CLAUDE_DIR / "settings.json"
        if settings_path.exists():
            settings = read_json(settings_path)
            if "statusLine" in settings:
                click.echo(f"  statusLine already configured; leaving untouched:")
                click.echo(f"    {json.dumps(settings['statusLine'])}")
                click.echo(f"  To replace: edit {settings_path} manually.")
            else:
                cus_py_path = Path(__file__).resolve()
                settings["statusLine"] = {
                    "type": "command",
                    "command": f"python3 {cus_py_path} statusline",
                }
                # Backup before write
                backup = settings_path.with_suffix(f".json.bak.pre-cus-statusline-{datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S')}")
                shutil.copy2(settings_path, backup)
                write_json(settings_path, settings)
                click.echo(f"  Added statusLine → {cus_py_path} statusline")
                click.echo(f"  Backup at: {backup}")
        else:
            click.echo(f"  {settings_path} doesn't exist yet. Run Claude Code at least once first.")
    else:
        click.echo(click.style("[3/5] Skipped statusLine wiring (--skip-statusline).", bold=True))
    click.echo()

    # 4. systemd unit
    if not skip_systemd:
        click.echo(click.style("[4/5] Install systemd --user service...", bold=True))
        ctx.invoke(init_systemd_cmd, user_unit_dir=str(HOME / ".config/systemd/user"), enable=True)
    else:
        click.echo(click.style("[4/5] Skipped systemd install (--skip-systemd).", bold=True))
    click.echo()

    # 5. Wrapper in ~/bin or ~/.local/bin
    if not skip_wrapper:
        click.echo(click.style("[5/5] Install ~/bin/cus wrapper...", bold=True))
        target = None
        for candidate in [HOME / "bin", HOME / ".local" / "bin"]:
            if candidate.exists() and candidate.is_dir():
                target = candidate / "cus"
                break
        if target is None:
            target = HOME / ".local" / "bin" / "cus"
            target.parent.mkdir(parents=True, exist_ok=True)
        wrapper_source = Path(__file__).resolve().parent / "cus"
        if target.exists() or target.is_symlink():
            click.echo(f"  {target} already exists; leaving untouched")
        else:
            target.symlink_to(wrapper_source)
            click.echo(f"  Symlinked {target} → {wrapper_source}")
            # Check PATH
            path = os.environ.get("PATH", "")
            if str(target.parent) not in path.split(":"):
                click.echo(f"  WARNING: {target.parent} is not in $PATH. Add to your shell rc:")
                click.echo(f"    export PATH=\"{target.parent}:$PATH\"")
    else:
        click.echo(click.style("[5/5] Skipped wrapper install (--skip-wrapper).", bold=True))

    click.echo()
    click.echo(click.style("=== install complete ===", bold=True, fg="green"))
    click.echo()
    click.echo("Next steps:")
    click.echo("  cus status                 # check state")
    click.echo("  cus sos                    # check if any human action is needed")
    click.echo("  cus config --explain       # see every setting + description")
    click.echo("  cus add <name>             # add another account")


@cli.command(name="uninstall")
@click.option("--keep-data", is_flag=True, help="Don't delete ~/claude-accounts/ (account dirs + logs + state).")
@click.option("--yes", is_flag=True, help="Don't prompt for confirmation.")
def uninstall_cmd(keep_data: bool, yes: bool) -> None:
    """Reverse `cus install`: stop daemon, uninstall hooks, remove statusLine, unsymlink wrapper.

    By default, leaves ~/claude-accounts/ untouched (your credentials and
    history are preserved). Use --keep-data=false to also remove that
    (irreversible).

    Does NOT touch your ~/.claude/ dir, the cus.py source, or any
    legacy ~/.claude-<name>/ dirs.
    """
    if not yes:
        click.echo("This will:")
        click.echo("  - Stop + disable the systemd service")
        click.echo("  - Uninstall Claude Code hooks from ~/.claude/settings.json")
        click.echo("  - Remove the statusLine entry (if it points at cus)")
        click.echo("  - Unsymlink ~/bin/cus or ~/.local/bin/cus")
        if not keep_data:
            click.echo(click.style(f"  - DELETE {ACCOUNTS_DIR} (use --keep-data to skip)", fg="red"))
        click.confirm("Continue?", abort=True)

    # 1. systemd
    click.echo("[1/4] Stop + disable systemd service...")
    try:
        subprocess.run(["systemctl", "--user", "disable", "--now", "cus.service"], check=False, capture_output=True)
        unit_path = HOME / ".config/systemd/user/cus.service"
        if unit_path.exists():
            unit_path.unlink()
            click.echo(f"  removed {unit_path}")
        subprocess.run(["systemctl", "--user", "daemon-reload"], check=False, capture_output=True)
    except Exception as e:
        click.echo(f"  warning: {e}")

    # 2. Hooks
    click.echo("[2/4] Uninstall hooks...")
    result = uninstall_hooks()
    for event, status in result.items():
        click.echo(f"  {event:<30} {status}")

    # 3. StatusLine
    click.echo("[3/4] Remove statusLine entry...")
    settings_path = CLAUDE_DIR / "settings.json"
    if settings_path.exists():
        settings = read_json(settings_path)
        sl = settings.get("statusLine", {})
        if isinstance(sl, dict) and "cus.py" in sl.get("command", ""):
            del settings["statusLine"]
            write_json(settings_path, settings)
            click.echo(f"  removed statusLine pointing at cus.py")
        else:
            click.echo(f"  no cus statusLine found (your statusLine is unmodified)")
    else:
        click.echo(f"  {settings_path} doesn't exist; nothing to remove")

    # 4. Wrapper symlink
    click.echo("[4/4] Remove ~/bin/cus wrapper...")
    for candidate in [HOME / "bin" / "cus", HOME / ".local" / "bin" / "cus"]:
        if candidate.is_symlink() and "claude-usage-swap" in str(candidate.resolve()):
            candidate.unlink()
            click.echo(f"  removed {candidate}")

    # 5. Optionally remove storage
    if not keep_data and ACCOUNTS_DIR.exists():
        click.echo("Deleting ~/claude-accounts/ ...")
        shutil.rmtree(ACCOUNTS_DIR)
        click.echo(f"  removed {ACCOUNTS_DIR}")
    elif keep_data:
        click.echo(f"Preserved {ACCOUNTS_DIR} (use without --keep-data to also remove).")

    click.echo()
    click.echo(click.style("=== uninstall complete ===", bold=True, fg="green"))
    click.echo("Your ~/.claude/ dir and any legacy ~/.claude-*/ dirs are untouched.")


@cli.command(name="rename")
@click.argument("old_name")
@click.argument("new_name")
def rename_cmd(old_name: str, new_name: str) -> None:
    """Rename an account: ~/claude-accounts/account-<old> → account-<new>.

    Updates state.json, config.yaml, swap_history, session_locks.pinned, and
    meta.yaml to reference the new name. The folder is renamed atomically;
    symlinks inside the dir continue to work.

    Use this when you want to switch naming conventions (e.g. from
    `account-merkos` to `account-01`) without losing config or history.
    """
    if not new_name.replace("-", "").replace("_", "").isalnum():
        click.echo(f"Bad new_name '{new_name}' — alphanumeric + dashes/underscores only.")
        sys.exit(1)
    if new_name == "default":
        click.echo("'default' is reserved for the live ~/.claude/ dir.")
        sys.exit(1)

    old_dir = ACCOUNTS_DIR / f"account-{old_name}"
    new_dir = ACCOUNTS_DIR / f"account-{new_name}"
    if not old_dir.exists():
        click.echo(f"Source dir {old_dir} doesn't exist.")
        sys.exit(1)
    if new_dir.exists():
        click.echo(f"Target dir {new_dir} already exists.")
        sys.exit(1)

    # 1. Rename the dir (atomic on same filesystem)
    os.rename(old_dir, new_dir)
    click.echo(f"renamed {old_dir} → {new_dir}")

    # 2. Update meta.yaml inside
    meta_path = new_dir / "meta.yaml"
    if meta_path.exists():
        meta = read_yaml(meta_path)
        meta["name"] = new_name
        meta["renamed_from"] = old_name
        meta["renamed_ts"] = now_iso()
        write_yaml(meta_path, meta)
        click.echo(f"  updated {meta_path}")

    # 3. Update state.json
    if STATE_JSON.exists():
        state = load_state()
        if old_name in state.get("accounts", {}):
            state["accounts"][new_name] = state["accounts"].pop(old_name)
        if state.get("active") == old_name:
            state["active"] = new_name
        # per_session: a slot holding the renamed account must follow, or the
        # per-slot decision loop shims a now-missing account name into
        # decide_swap and raises KeyError every cycle (swallowed by the daemon
        # loop → silent no-op; fatal for `cus daemon --once`). Review finding
        # 2026-07-02.
        for slot_entry in (state.get("slots", {}) or {}).values():
            if slot_entry.get("account") == old_name:
                slot_entry["account"] = new_name
        for entry in state.get("swap_history", []):
            if entry.get("from") == old_name:
                entry["from"] = new_name
            if entry.get("to") == old_name:
                entry["to"] = new_name
        save_state(state)
        click.echo(f"  updated state.json")

    # 4. Update config.yaml
    if CONFIG_YAML.exists():
        cfg = read_yaml(CONFIG_YAML)
        changed = False
        for acct in cfg.get("accounts", []):
            if acct.get("name") == old_name:
                acct["name"] = new_name
                changed = True
        pinned = cfg.get("session_locks", {}).get("pinned", {}) or {}
        for k, v in list(pinned.items()):
            if v == old_name:
                pinned[k] = new_name
                changed = True
        if changed:
            write_yaml(CONFIG_YAML, cfg)
            click.echo(f"  updated config.yaml")

    click.echo()
    click.echo(click.style(f"Done. Account '{old_name}' is now '{new_name}'.", fg="green"))
    click.echo("If the daemon is running, restart it: systemctl --user restart cus.service")


# --------------------------------------------------------------------------
# per_session commands — slot / doctor / sync-config
# (docs/plans/2026-07-02-per-session-accounts.md, Phases 1.2 + 1.3)
# --------------------------------------------------------------------------

@cli.group()
def slot() -> None:
    """Manage slot dirs — per-session live mounts (per_session mode)."""


@slot.command("create")
def slot_create_cmd() -> None:
    """Create a new slot dir (lowest free index) with the canonical layout.

    The slot starts empty (no account credentials) — `cus launch` installs an
    account into it. Walk-back: `cus slot gc --slot <name>`.
    """
    state = load_state()
    name, d = create_slot(state)
    save_state(state)
    sync_result = None
    if CLAUDE_JSON.exists():
        try:
            sync_result = sync_mount_claude_json(d, read_json(CLAUDE_JSON))
        except (json.JSONDecodeError, OSError) as e:
            click.echo(click.style(f"  warning: could not seed .claude.json from canonical: {e}", fg="yellow"))
    click.echo(f"Created {click.style(name, bold=True)} at {d}")
    if sync_result and sync_result["changed"]:
        click.echo(f"  seeded .claude.json with {len(sync_result['keys_updated'])} keys from ~/.claude.json")
    click.echo(f"  launch under it: CLAUDE_CONFIG_DIR={d} claude   (or `cus launch`)")


@slot.command("list")
def slot_list_cmd() -> None:
    """List slot dirs: account held, live PIDs, state registration."""
    state = load_state()
    reg = state.get("slots", {})
    dirs = list_slot_dirs()
    if not dirs and not reg:
        click.echo("No slots. `cus slot create` or `cus launch` makes one.")
        return
    seen: set[str] = set()
    for d in dirs:
        seen.add(d.name)
        entry = reg.get(d.name, {})
        pids = mount_pids(d)
        status = click.style(f"live ({len(pids)} pids)", fg="green") if pids else "idle"
        acct = entry.get("account") or "(empty)"
        unreg = "" if d.name in reg else click.style("  [not in state — doctor?]", fg="yellow")
        click.echo(f"  {d.name:<10} account={acct:<12} {status}{unreg}")
    for name in reg:
        if name not in seen:
            click.echo(f"  {name:<10} " + click.style("[state entry, dir missing — gc will drop]", fg="yellow"))


@slot.command("gc")
@click.option("--slot", "slot_name_opt", default=None, help="Reap one slot by name (default: all idle slots).")
@click.option("--force", is_flag=True, help="Skip the in-use refusal (save-back guards still run).")
def slot_gc_cmd(slot_name_opt: str | None, force: bool) -> None:
    """Reap idle slots: save credentials back to the owning account dir, remove.

    A slot whose session exited stays on disk holding real credentials until
    gc'd. The daemon runs this each cycle in per_session mode; the command is
    for manual cleanup.
    """
    state = load_state()
    names = [slot_name_opt] if slot_name_opt else sorted(set(list(state.get("slots", {}))) | {d.name for d in list_slot_dirs()})
    if not names:
        click.echo("Nothing to gc.")
        return
    for name in names:
        result = gc_slot(name, state, force=force)
        action = result["action"]
        if action == "refused_in_use":
            click.echo(f"  {name}: in use (pids {result['pids']}) — skipped")
        elif action == "dropped_stale_entry":
            click.echo(f"  {name}: state entry with no dir — dropped")
        else:
            sb = result["saveback"]
            click.echo(f"  {name}: reaped (creds save-back: {sb['action']}" + (f" → account-{sb['account']}" if sb.get("account") else "") + ")")
    save_state(state)


def _slot_move_plan(state: dict, config: dict, slot_name: str, target: str) -> dict:
    """Pure planning preview for `cus slot move` — decides, WITHOUT any side
    effect, what execute_swap would do for the move (slot_name -> target).

    This mirrors the install-source decision inside _execute_swap_locked so that
    both `--dry-run` and the command's pre-flight double-book guard agree with
    what the real swap would actually do. Factored out as a pure helper (no
    execute_swap, no disk writes) precisely so it is unit-testable on its own —
    the swap primitive itself is awkward to drive through in every branch.

    Verdicts:
      - "noop"     target is already the slot's account (nothing to move).
      - "snapshot" target is NOT live on any OTHER mount, so a plain snapshot
                   copy installs cleanly — no login family needed (today's copy
                   path; the gate-off default).
      - "claim"    target IS live on another mount, but the login pool can back
                   a DISTINCT token family (gate on + a free pooled family).
                   execute_swap claims that family — two live mounts, two token
                   families, no clobber (GH #109).

                   NOTE (2026-07-07, GH #104 chats1a): a legacy per-slot
                   independent login now only yields "claim" if its token is
                   GENUINELY distinct from every live mount's family. A stale-copy
                   legacy store (same family as the shared mount — the chats1a
                   signature) collides, so execute_swap's `_live_family_would_collide`
                   guard REFUSES it and this preview falls through to "refuse" too.
                   Before the fix, `has_independent_login` presence alone counted
                   as "claim", so the dry-run said CLAIM while the real move left
                   the lane on the shared family and a rotation logged it out.
      - "refuse"   target IS live on another mount and there is NO distinct
                   family to claim. Installing a plain copy would put two live
                   mounts on ONE OAuth refresh-token family; the next rotation
                   invalidates the other mount's token and logs that session out
                   (GH #104). The move is refused rather than clobber.

    Returns {current, target, plan, held_by, gate, detail}. `held_by` is a
    human-readable list of the OTHER live mounts already on `target` (live slots
    plus the shared ~/.claude mount), for the operator-facing message.
    """
    current = (state.get("slots", {}).get(slot_name, {}) or {}).get("account")
    # Ground-truth occupancy (max_age_seconds=0 bypasses the cache) — the same
    # source the real clobber guard reads, so preview and reality can't diverge.
    held_by = [s for s in occupied_slot_accounts(state, max_age_seconds=0.0).get(target, []) if s != slot_name]
    # Shared-mount holder: mode-aware, NOT gated on mount_in_use(CLAUDE_DIR) —
    # bare sessions on ~/.claude set no CLAUDE_CONFIG_DIR so mount_pids can't see
    # them (issue #141). _shared_mount_holds treats global/hybrid state["active"]
    # as an unconditional live holder; per_session keeps the detectable-only rule.
    if _shared_mount_holds(target, state, config):
        held_by = held_by + ["~/.claude (shared mount)"]
    held_elsewhere = _account_held_by_other_live_mount(state, target, slot_name, config)
    gate = independent_logins_enabled(config)
    if target == current:
        plan, detail = "noop", f"{slot_name} is already on '{target}'"
    elif not held_elsewhere:
        plan, detail = "snapshot", (
            f"'{target}' is not live on any other mount — a plain snapshot install, no login family needed")
    elif gate and (has_free_login_family(target, state)
                   or (has_independent_login(target, slot_name)
                       and not _live_family_would_collide(
                           target, login_store_creds_path(target, slot_name), slot_name, state, config))):
        # Claimable = a free POOLED family, OR a legacy per-slot login whose token
        # is GENUINELY distinct from every live mount's family. The legacy branch
        # is now token-CHECKED (2026-07-07 chats1a): a stale-copy legacy store
        # shares the shared-mount family, so execute_swap's `_live_family_would_collide`
        # guard REFUSES it — and the preview must agree, else the dry-run lied
        # "CLAIM" while the real move refused (the exact divergence that let the
        # incident's `slot move` print success and then get clobbered). A genuinely
        # independent legacy login (distinct token) still previews and swaps as a
        # clean claim, so the supported Track-B behavior is preserved.
        plan, detail = "claim", (
            f"'{target}' is live on {held_by} — execute_swap will claim a free login family "
            f"(distinct token, no clobber; GH #109)")
    else:
        plan, detail = "refuse", (
            f"'{target}' is live on {held_by} and has no free login family to claim — installing a copy "
            f"would clobber the shared token family and log a session out (GH #104). "
            f"Provision another family with `cus login-mount {target}`"
            + ("" if gate else " (and enable the login pool: independent_logins.use_independent_logins)"))
    return {"current": current, "target": target, "plan": plan, "held_by": held_by, "gate": gate, "detail": detail}


@slot.command("move")
@click.argument("slot_name")
@click.argument("account")
@click.option("--dry-run", is_flag=True,
              help="Print the plan (old account, target, whether a login family would be claimed or the move would refuse) WITHOUT moving anything.")
@click.option("--force", is_flag=True,
              help="Bypass cus's pre-flight refusals: move a LOCKED slot (see `cus lock`) and skip the double-book guard. "
                   "Does NOT override the GH #104 pool-exhaustion safety inside execute_swap — with the login pool on, a "
                   "genuinely exhausted target still refuses rather than clobber a live token family. Mirrors `cus switch --force`.")
def slot_move_cmd(slot_name: str, account: str, dry_run: bool, force: bool) -> None:
    """Move a slot's credential mount onto ACCOUNT, IN PLACE.

    The manual counterpart to the daemon's per-lane rotation. The daemon already
    moves a live slot's account under a running session without restarting it
    (decide_slot_swaps -> execute_swap(..., slot=...)); this command exposes that
    exact primitive for a human who wants to say "put slot-5 on merkos" NOW.
    Because the swap is in place, the session on top of the slot is never
    interrupted — it keeps its loaded tokens until the next refresh, then reads
    the new account's mount. (Contrast a relaunch, which loses the session.)

    Reuses `execute_swap(account, trigger="manual-slot-move", slot=<slot>)` — the
    same primitive `cus launch` and the daemon call — so the credential-safety is
    INHERITED, not reinvented:

      * GH #104 double-book: if ACCOUNT is already live on another mount (a live
        slot or the shared ~/.claude mount), a plain snapshot copy would put two
        live mounts on ONE OAuth refresh-token family; the next rotation logs one
        session out. When the login pool is on, execute_swap instead CLAIMS a
        free, distinct login family (GH #109) so both mounts stay logged in. If
        the pool is exhausted (no free family), execute_swap REFUSES rather than
        clobber — this command surfaces that refusal verbatim and points at
        `cus login-mount ACCOUNT` to provision another family.

    This command adds only the operator ergonomics around that primitive:
    slot/account validation, bare-number slot normalization (`5` == `slot-5`),
    the `cus lock` guard, an already-on-target short-circuit, a `--dry-run`
    preview, and a printed walk-back line. `--force` bypasses the local
    pre-flight refusals (lock + double-book) exactly as `cus switch --force`
    does; it can NOT force a pool-exhausted clobber, because execute_swap itself
    refuses that with no force path (and inventing one would defeat GH #104).

    Walk-back: the move is undone by moving the slot back — `cus slot move
    <slot> <old-account>`.
    """
    if not STATE_JSON.exists():
        click.echo("Not initialized. Run `cus init` first.")
        sys.exit(1)

    name = _normalize_slot_name(slot_name)
    state = load_state()
    config = load_config()

    # Validate account (mirror `switch`'s known-accounts error).
    if account not in state.get("accounts", {}):
        click.echo(f"Unknown account '{account}'. Known: {sorted(state.get('accounts', {}).keys())}")
        sys.exit(1)
    # Validate slot: a state entry OR an on-disk dir is enough to move it (an
    # unregistered-but-present dir is a real, movable mount; `cus slot list`
    # flags the registration gap separately).
    if name not in (state.get("slots") or {}) and not slot_path(name).exists():
        known = sorted((state.get("slots") or {}).keys())
        click.echo(f"Unknown slot '{name}'. Known: {known or '(none)'}. `cus slot list` to see slots.")
        sys.exit(1)

    current = (state.get("slots", {}).get(name, {}) or {}).get("account")

    # Already on the target — nothing to do (say so, don't error).
    if account == current:
        click.echo(f"{name} is already on '{account}', nothing to do.")
        return

    # Lock guard: a locked slot is frozen for the daemon AND for this command
    # (locks are user intent — "this slot stays put"). --force overrides once.
    if name in _locked_slots(config) and not force:
        click.echo(click.style(
            f"refusing: {name} is locked (`cus lock`) — the daemon won't move it and neither will this command. "
            f"`cus unlock {name}` to release it, or pass --force to move it once anyway.", fg="red"))
        sys.exit(1)

    plan = _slot_move_plan(state, config, name, account)

    if dry_run:
        # Preview only — reuse the same planning helper the guard uses, and make
        # ZERO state changes (no execute_swap call).
        click.echo(f"(dry-run) plan for `cus slot move {name} {account}`:")
        click.echo(f"  current account: {current or '(empty slot)'}")
        click.echo(f"  target account:  {account}")
        click.echo(f"  login pool gate: {'on' if plan['gate'] else 'off'}")
        click.echo(f"  verdict: {plan['plan'].upper()} — {plan['detail']}")
        if plan["plan"] == "refuse":
            click.echo("  (this move would be refused as-is; nothing has changed)")
        return

    # Pre-flight double-book guard (mirrors `cus switch`): refuse a move that
    # execute_swap could only satisfy by clobbering a live token family. --force
    # bypasses THIS local check exactly like switch; the pool safety inside
    # execute_swap is still absolute when the gate is on (it raises below and we
    # surface it).
    if plan["plan"] == "refuse" and not force:
        click.echo(click.style(f"refusing: {plan['detail']}", fg="red"))
        click.echo("Pass --force to bypass this pre-flight check (with the login pool on, execute_swap still refuses a pool-exhausted clobber).")
        sys.exit(1)

    try:
        execute_swap(account, trigger="manual-slot-move", slot=name)
    except (FileNotFoundError, ValueError, RuntimeError) as e:
        # execute_swap's OWN GH #104 pool-exhaustion refusal lands here as a
        # RuntimeError ("pool exhausted for '<acct>': ...") — surface its
        # operator-readable guidance verbatim rather than reformatting it.
        click.echo(click.style(f"ERROR: {e}", fg="red"))
        sys.exit(1)

    # Report result. Re-read state to see the lease execute_swap recorded if it
    # claimed a distinct login family (state.slots[name].login_family).
    new_state = load_state()
    lease = (new_state.get("slots", {}).get(name, {}) or {}).get("login_family")
    click.echo(click.style(
        f"moved {name}: {current or '(empty)'} -> {account} (in place; session continues uninterrupted)", fg="green"))
    if lease:
        click.echo(f"  claimed login family: {lease} (distinct token family — no clobber, GH #109)")
    if current:
        click.echo(f"  undo: cus slot move {name} {current}")


@cli.command(name="doctor")
@click.option("--fix-dirs", is_flag=True, help="Heal findings (create/repoint symlinks, fold settings stubs, merge stray dirs).")
def doctor_cmd(fix_dirs: bool) -> None:
    """Check account dirs + slot dirs against the canonical mount layout.

    Read-only by default; --fix-dirs heals idempotently (re-run is a no-op).
    Layout rationale: docs/ARCHITECTURE.md "Per-session slot dirs".
    """
    mounts: list[Path] = []
    if ACCOUNTS_DIR.exists():
        mounts += [p for p in sorted(ACCOUNTS_DIR.glob("account-*")) if p.is_dir()]
    mounts += list_slot_dirs()
    if not mounts:
        click.echo("No account or slot dirs found.")
        return
    total = 0
    for m in mounts:
        findings = doctor_mount(m, fix=fix_dirs)
        if not findings:
            continue
        total += len(findings)
        click.echo(click.style(m.name, bold=True))
        for f in findings:
            click.echo(f"  {f['entry']}: {f['problem']} — {f['action']}")
    if total == 0:
        click.echo(click.style("✓ all mounts canonical", fg="green"))
    elif not fix_dirs:
        click.echo(f"\n{total} finding(s). Re-run with --fix-dirs to heal.")
        sys.exit(1)


@cli.command(name="sync-config")
@click.option("--from", "from_path", default=None, help="Canonical .claude.json (default: ~/.claude.json).")
@click.option("--dry-run", is_flag=True, help="Show what would change without writing.")
def sync_config_cmd(from_path: str | None, dry_run: bool) -> None:
    """Sync non-account keys of the canonical .claude.json into every slot.

    Account-bound keys (userID, oauthAccount) are preserved per slot — this
    moves everything ELSE (MCP registrations, per-project state, onboarding
    flags) so N live copies don't diverge. Slots with a live session are
    skipped (Claude Code rewrites its own .claude.json; concurrent merge =
    lost-update race) — they pick the sync up at next launch.
    """
    canonical_path = Path(from_path).expanduser() if from_path else CLAUDE_JSON
    if not canonical_path.exists():
        click.echo(f"Canonical {canonical_path} does not exist.", err=True)
        sys.exit(1)
    try:
        canonical = read_json(canonical_path)
    except (json.JSONDecodeError, OSError) as e:
        click.echo(f"Canonical {canonical_path} unreadable: {e}", err=True)
        sys.exit(1)
    slots = list_slot_dirs()
    if not slots:
        click.echo("No slots to sync.")
        return
    for d in slots:
        if mount_in_use(d):
            click.echo(f"  {d.name}: live session — skipped (syncs at next launch)")
            continue
        if dry_run:
            cj_path = mount_claude_json_path(d)
            existing = read_json(cj_path) if cj_path.exists() else {}
            would = [k for k, v in canonical.items() if k not in ACCOUNT_BOUND_KEYS and existing.get(k) != v]
            click.echo(f"  {d.name}: would update {len(would)} keys" + (f" ({', '.join(sorted(would)[:8])}{'…' if len(would) > 8 else ''})" if would else ""))
            continue
        result = sync_mount_claude_json(d, canonical)
        click.echo(f"  {d.name}: {'updated ' + str(len(result['keys_updated'])) + ' keys' if result['changed'] else 'already in sync'}")


def _launch_prepare(account: str | None, state: dict, config: dict,
                    pool: str | None = None, force: bool = False,
                    lane: str | None = None) -> tuple[str, Path, str]:
    """Everything `cus launch` does BEFORE the exec: pick account, acquire +
    heal + sync a slot, install the account's credentials into it.

    Factored out of the command so tests can exercise the whole flow (the
    exec itself is untestable in-process). Returns (slot_name, slot_dir,
    account). Raises click.ClickException with an operator-readable message
    on every refusal.

    `pool` (GH #99): rotation-set the new slot joins ("premium"/"standard");
    None falls back to per_session.default_pool. A standard-pool launch that
    auto-picks its account ignores the per-model weekly gate, so it may pick a
    model-exhausted account (that's the point — standard work drains stranded
    aggregate headroom).
    """
    pool = pool or config.get("per_session", {}).get("default_pool", "premium")
    if pool not in VALID_POOLS:
        raise click.ClickException(f"unknown pool '{pool}'. Valid: {list(VALID_POOLS)}")
    if account in (None, "auto"):
        target = pick_launch_account(state, _config_for_pool(config, pool))
        if target is None:
            raise click.ClickException(
                "no launchable account: every account is expired, erroring, or on a live mount with "
                "per_session.lane_sharing off (GH #104 — won't double-book a live account; lane sharing "
                "makes live accounts joinable). Exit a session, wait for a 5h/weekly reset, fix logins, "
                "or add an account. See `cus status` / `cus sos`.")
        account = target.name
        click.echo(f"launch: picked '{account}' ({target.reason})")
    elif account not in state.get("accounts", {}):
        raise click.ClickException(f"unknown account '{account}'. Known: {sorted(state.get('accounts', {}))}")

    # Lane sharing (GH #109 lanes): if the chosen account is already live on a
    # slot lane, JOIN that lane (share its config dir) rather than refusing
    # (#104) or minting a second mount. Multiple sessions on one lane share one
    # login and swap together — the no-extra-login path. The lane already holds
    # the account, so there is NO swap and NO clobber (same dir, same family).
    # We only join a SLOT lane, never the global mount (a slot on the global
    # active would be a genuine second mount of that account → falls through to
    # the #104 guard / independent-login hatch below).
    if lane is None and config.get("per_session", {}).get("lane_sharing", False):
        occ = occupied_slot_accounts(state)  # account -> [live slot names]
        lanes = sorted(occ.get(account, []),
                       key=lambda n: int(n.removeprefix(SLOT_PREFIX)) if n.removeprefix(SLOT_PREFIX).isdigit() else 1 << 30)
        if lanes:
            lane = lanes[0]
            lane_dir = slot_path(lane)
            click.echo(f"launch: joining lane {lane} already on '{account}' (lane sharing — shared login, swap together)")
            # Heal the lane's layout, but DON'T sync .claude.json: it has a live
            # writer (the sessions already on this lane), so a sync would race
            # their writes (same rule as the daemon's periodic save-back).
            for f in doctor_mount(lane_dir, fix=True):
                click.echo(f"launch: doctor healed {lane}/{f['entry']} ({f['problem']})")
            state = load_state()
            entry = state.setdefault("slots", {}).setdefault(lane, {"account": account, "created_ts": now_iso()})
            entry["last_launch_ts"] = now_iso()
            save_state(state)
            return lane, lane_dir, account
        if account == state.get("active") and mount_in_use(CLAUDE_DIR):
            # Shared-mount join (2026-07-03): the account's only live mount is
            # the global ~/.claude pair. A bare session already IS that mount,
            # so joining it is the same move as joining a slot lane — same dir,
            # same login family, no second mount, no clobber. The session runs
            # bare (no CLAUDE_CONFIG_DIR override; launch_cmd skips the env var
            # for the shared dir) and the daemon manages it with the other bare
            # sessions. No slot bookkeeping: the shared mount isn't a slot.
            click.echo(f"launch: joining the shared mount already on '{account}' (lane sharing — bare session, swaps with the shared mount)")
            return "shared", CLAUDE_DIR, account

    if lane is not None and not (lane.startswith(SLOT_PREFIX) and lane.removeprefix(SLOT_PREFIX).isdigit()):
        raise click.ClickException(f"bad --lane '{lane}' — expected slot-<n> (e.g. slot-8).")

    # Duplicate-mount guard (GH #104): refuse to launch onto an account already
    # live on another mount (another slot, or the shared mount with live bare
    # sessions) — the two would rotate each other's single-use refresh token
    # and log one session out. `--force` overrides for the rare intentional case.
    live_mount_accts = _live_slot_accounts(state)
    if state.get("active") and mount_in_use(CLAUDE_DIR):
        live_mount_accts.add(state["active"])
    if account in live_mount_accts and not force:
        # Phase 3b (#109) escape hatch: a SECOND live mount on one account is
        # safe only if it uses its OWN independent login family. Allow it when
        # the gate is on AND the operator named an explicit --lane that already
        # has an independent login provisioned for this account — the install
        # below then uses that family (swap_install_source), not a clobbering
        # copy. Everything else still refuses (lane sharing SHARES a mount, this
        # hatch adds a distinct one). --force still overrides, but that IS the
        # clobber.
        independent_ok = (lane is not None and independent_logins_enabled(config)
                          and has_independent_login(account, lane))
        if not independent_ok:
            raise click.ClickException(
                f"'{account}' is already running on a live mount. A second session on it would rotate "
                f"its login token and sign one out (GH #104). Options: pick another account "
                f"(`cus launch auto`); turn on per_session.lane_sharing to SHARE its lane; or give it a "
                f"second INDEPENDENT lane — enable independent_logins, run "
                f"`cus login-mount <free-slot> {account}`, then `cus launch {account} --lane <free-slot>`. "
                f"(`--force` overrides, but that reintroduces the clobber.)")
        click.echo(f"launch: '{account}' gets a 2nd independent lane {lane} (its own login — GH #109)")

    acct_state = state.get("accounts", {}).get(account, {})
    if acct_state.get("token_expired"):
        click.echo(click.style(f"warning: '{account}' is flagged token_expired — the session may demand /login "
                               f"(fix first: `cus relogin {account}`)", fg="yellow"))

    if lane is not None:
        # Explicit lane (Phase 3b escape hatch, or just pinning a launch to a
        # known slot). Use it as-is instead of auto-picking. Refuse if it's live
        # on a DIFFERENT account (that would collide); a free or same-account
        # lane is fine.
        cur = state.get("slots", {}).get(lane, {}).get("account")
        slot_dir = slot_path(lane)
        if slot_dir.exists() and mount_in_use(slot_dir) and cur not in (None, account):
            raise click.ClickException(f"--lane {lane} is live on '{cur}', not '{account}'. Pick a free lane.")
        scaffold_mount_dir(slot_dir)  # idempotent
        slot_name = lane
    else:
        # acquire_slot persists the reservation itself (under the swap lock), so
        # a concurrent launch / the daemon's gc already see this slot as claimed
        # — no save_state here (it would re-write our possibly-staler `state`
        # over acquire's fresh persist).
        slot_name, slot_dir = acquire_slot(state, prefer_account=account)

    # Pre-flight: heal the slot's layout quietly (a launch should never come
    # up bare-hooked because a symlink rotted), then align its .claude.json
    # with the canonical — UNLESS the mount has a live writer (an explicit --lane
    # onto an already-live same-account lane), where a sync would race it.
    for f in doctor_mount(slot_dir, fix=True):
        click.echo(f"launch: doctor healed {slot_name}/{f['entry']} ({f['problem']})")
    if CLAUDE_JSON.exists() and not mount_in_use(slot_dir):
        try:
            sync_mount_claude_json(slot_dir, read_json(CLAUDE_JSON))
        except (json.JSONDecodeError, OSError) as e:
            click.echo(click.style(f"warning: canonical .claude.json sync skipped: {e}", fg="yellow"))

    # Install the account into the slot — the same in-place swap primitive the
    # daemon uses (no-op when the slot already holds it). Reloads and persists
    # state itself, under the swap lock.
    if state.get("slots", {}).get(slot_name, {}).get("account") != account:
        execute_swap(account, trigger="launch", slot=slot_name)

    state = load_state()
    entry = state.setdefault("slots", {}).setdefault(slot_name, {"account": account, "created_ts": now_iso()})
    entry["last_launch_ts"] = now_iso()
    entry["pool"] = pool  # GH #99: bind the slot to its rotation-set for its life
    save_state(state)
    return slot_name, slot_dir, account


@cli.command(name="launch", context_settings={"ignore_unknown_options": True})
@click.argument("account", required=False)
@click.option("--pool", type=click.Choice(list(VALID_POOLS)), default=None,
              help="Rotation-set for this slot (GH #99). premium: honor the per-model weekly gate (swap off model-exhausted accounts). standard: ignore it (keep using their aggregate headroom). Default: per_session.default_pool.")
@click.option("--force", is_flag=True, help="Launch even onto an account already live on another mount (GH #104: normally refused — two live mounts on one account sign one out).")
@click.option("--lane", default=None, help="Launch into a specific slot (e.g. slot-8) instead of auto-picking. With independent_logins on + an independent login provisioned for (lane, account), this is how you give one account a 2nd independently-swappable lane (GH #109).")
@click.argument("claude_args", nargs=-1, type=click.UNPROCESSED)
def launch_cmd(account: str | None, pool: str | None, force: bool, lane: str | None, claude_args: tuple[str, ...]) -> None:
    """Launch claude in its own slot, pinned to an account (per_session).

    ACCOUNT is an account name, or omitted/'auto' to pick the best by the
    same headroom scoring swaps use. Everything after `--` passes through to
    claude:

        cus launch                       # auto-pick, plain session
        cus launch rayi1 -- --resume X   # explicit account + claude args
        cus launch --pool standard auto  # standard-pool slot (see `cus pool`)
        cus launch rayi1 --lane slot-8   # pin to slot-8 (see below)

    The session keeps its slot (and its pool) for life; the daemon moves
    ACCOUNTS through slots (in-place, no restart), never sessions. Optional
    alias to make every launch slotted:  alias claude='cus launch auto --'

    An account already live on another mount is refused (GH #104) — it would
    sign one of the two out. Three ways past it: `--force` (accepts the
    clobber); turn on per_session.lane_sharing to SHARE the existing lane; or,
    for a second INDEPENDENTLY-swappable lane, enable independent_logins, run
    `cus login-mount <free-slot> <account>`, then `cus launch <account> --lane
    <free-slot>` (GH #109).
    """
    state = load_state()
    config = load_config()
    slot_name, slot_dir, account = _launch_prepare(account, state, config, pool=pool, force=force, lane=lane)
    click.echo(f"launch: {slot_name} ← {account}; exec claude {' '.join(claude_args)}")
    env = dict(os.environ)
    if slot_dir != CLAUDE_DIR:
        env["CLAUDE_CONFIG_DIR"] = str(slot_dir)
    else:
        # Shared-mount join (2026-07-03 lane sharing): run BARE so hooks and
        # statusline classify the session as shared-mount, exactly like a
        # hand-launched `claude`. Pop rather than set — a launch issued from
        # INSIDE a slotted session inherits that slot's CLAUDE_CONFIG_DIR,
        # which would silently re-slot this "bare" session.
        env.pop("CLAUDE_CONFIG_DIR", None)
    # execvpe replaces this process — claude runs as if launched directly
    # from the shell (signals, tty, exit code all pass through untouched).
    os.execvpe("claude", ["claude", *claude_args], env)


def _set_config_mode(new_mode: str) -> None:
    """Flip config.yaml's mode key with a TEXTUAL edit, not a YAML rewrite.

    write_yaml would round-trip the whole file through safe_dump and destroy
    every hand-written comment in the production config — the mode line is
    one key, so edit one line.
    """
    if CONFIG_YAML.exists():
        text = CONFIG_YAML.read_text()
        if re.search(r"(?m)^mode:", text):
            text = re.sub(r"(?m)^mode:.*$", f"mode: {new_mode}", text)
        else:
            text = f"mode: {new_mode}\n{text}"
        atomic_write_bytes(CONFIG_YAML, text.encode())
    else:
        write_yaml(CONFIG_YAML, {"mode": new_mode})


@cli.command(name="mode")
@click.argument("new_mode", required=False, type=click.Choice(["per-session", "per_session", "global", "hybrid"]))
@click.option("--force", is_flag=True, help="global transition: proceed even with live slot sessions (they keep running, unmanaged).")
def mode_cmd(new_mode: str | None, force: bool) -> None:
    """Show or switch the live-mount topology (global | per_session | hybrid).

    per-session: validates every pool account (doctor-heals its dir, checks
    its snapshot carries a refresh token), ensures one free slot exists, and
    flips config. The global mount keeps whatever account it holds — bare
    launches continue working, observe-only (never swapped).

    hybrid (GH #99): same slot setup as per_session, BUT the daemon ALSO swaps
    the shared ~/.claude/ mount for bare sessions each cycle — so a machine
    with both `cus launch` slots AND plain `claude` sessions has everything
    managed. Slots move independently (no restart); bare sessions follow the
    shared-mount swap (they mass-bounce together, inherent to a shared mount).

    global: reaps every idle slot (credentials saved back), flips config.
    Walk-back is always `cus mode global` — global-mode code paths are
    untouched by per_session/hybrid, so it restores today's exact behavior.

    Both directions need a daemon restart to take effect mid-cycle:
    `systemctl --user restart cus.service` (the daemon re-reads config each
    cycle, so a running daemon picks it up next cycle anyway).
    """
    config = load_config()
    current = config.get("mode", "global")
    if new_mode is None:
        click.echo(f"mode: {current}")
        return
    new_mode = "per_session" if new_mode == "per-session" else new_mode
    if new_mode == current:
        click.echo(f"already in mode {current}")
        return

    state = load_state()
    if new_mode in ("per_session", "hybrid"):
        # Both modes manage slots, so both need every account to be a healthy
        # launch source and at least one slot to exist. hybrid additionally
        # keeps swapping the shared mount (handled in the daemon, not here).
        # Validation: every pool account must be a healthy launch source.
        problems: list[str] = []
        for name in sorted(state.get("accounts", {})):
            d = ACCOUNTS_DIR / f"account-{name}"
            if not d.exists():
                problems.append(f"account-{name}: dir missing")
                continue
            healed = doctor_mount(d, fix=True)
            for f in healed:
                click.echo(f"  doctor healed account-{name}/{f['entry']} ({f['problem']})")
            creds = d / ".credentials.json"
            try:
                oauth = read_json(creds).get("claudeAiOauth", {}) if creds.exists() else {}
            except (json.JSONDecodeError, OSError):
                oauth = {}
            if not oauth.get("refreshToken"):
                problems.append(f"account-{name}: snapshot has no refresh token — `cus relogin {name}` first")
            if state["accounts"][name].get("token_expired"):
                problems.append(f"account-{name}: flagged token_expired — `cus relogin {name}` first")
        if problems:
            click.echo(click.style("cannot enter per_session mode:", fg="red"))
            for p in problems:
                click.echo(f"  - {p}")
            sys.exit(1)
        if not any(not mount_in_use(d) for d in list_slot_dirs()):
            name, _ = create_slot(state)
            save_state(state)
            click.echo(f"  created first slot: {name}")
        _set_config_mode(new_mode)
        click.echo(click.style(f"mode: {new_mode}", fg="green", bold=True))
        click.echo("  launch sessions with `cus launch` (or: alias claude='cus launch auto --')")
        if new_mode == "hybrid":
            click.echo("  bare `claude` on ~/.claude/ IS managed too — the daemon swaps the shared")
            click.echo("  mount for bare sessions (they follow it together on the next request)")
        else:
            click.echo("  bare `claude` keeps working on ~/.claude/ — observed, never swapped")
        click.echo("  rollout note: consider relaxing polling.active_interval_seconds (e.g. 300) —")
        click.echo("  N occupied accounts poll at the fast cadence now (429-budget, see plan 3.3)")
        click.echo("  walk-back: `cus mode global`")
        return

    # → global
    live = [d.name for d in list_slot_dirs() if mount_in_use(d)]
    if live and not force:
        click.echo(click.style(f"live slot session(s): {', '.join(live)} — exit them first, or --force "
                               f"(they keep running but become unmanaged)", fg="red"))
        sys.exit(1)
    for d in list_slot_dirs():
        if mount_in_use(d):
            click.echo(f"  {d.name}: still live — left in place (unmanaged)")
            continue
        # force=True so a lingering reservation (an in-flight launch that never
        # exec'd) doesn't block teardown — we've already refused above if any
        # slot has a LIVE process, so a non-live reserved slot is safe to reap
        # (save-back still runs). Without force, mode global could leave
        # reserved-but-dead slots behind.
        result = gc_slot(d.name, state, force=True)
        if result["action"] == "reaped":
            sb = result["saveback"]
            click.echo(f"  {d.name}: reaped (save-back {sb['action']}" + (f" → account-{sb['account']}" if sb.get("account") else "") + ")")
    save_state(state)
    _set_config_mode("global")
    click.echo(click.style("mode: global", fg="green", bold=True))
    click.echo(f"  ~/.claude/ still holds '{state.get('active')}' — `cus switch <name>` to change")


@cli.command(name="init-systemd")
@click.option("--user-unit-dir", default=str(HOME / ".config/systemd/user"), help="Where to install the unit file.")
@click.option("--enable", is_flag=True, help="Run systemctl --user enable + start after writing.")
def init_systemd_cmd(user_unit_dir: str, enable: bool) -> None:
    """Write a systemd --user unit file for `cus daemon`.

    Walk-back: `systemctl --user disable cus.service` and delete the unit file.
    """
    unit_path = Path(user_unit_dir) / "cus.service"
    unit_path.parent.mkdir(parents=True, exist_ok=True)
    cus_path = Path(__file__).resolve()
    unit = f"""[Unit]
Description=claude-usage-swap daemon (auto-rotate Claude Code OAuth accounts)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
ExecStart=/usr/bin/env python3 {cus_path} daemon --foreground
Restart=on-failure
RestartSec=10
StandardOutput=append:{DAEMON_LOG}
StandardError=append:{DAEMON_LOG}

[Install]
WantedBy=default.target
"""
    atomic_write_bytes(unit_path, unit.encode())
    click.echo(f"Wrote {unit_path}")
    if enable:
        try:
            subprocess.run(["systemctl", "--user", "daemon-reload"], check=True)
            subprocess.run(["systemctl", "--user", "enable", "--now", "cus.service"], check=True)
            click.echo("Enabled + started cus.service (user)")
        except subprocess.SubprocessError as e:
            click.echo(f"systemctl failed: {e}")
            sys.exit(1)
    else:
        click.echo("To enable: systemctl --user daemon-reload && systemctl --user enable --now cus.service")


if __name__ == "__main__":
    cli()
