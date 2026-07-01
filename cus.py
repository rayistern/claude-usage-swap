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

import fcntl
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
INBOX_MD = ACCOUNTS_DIR / "inbox.md"
SOS_MD = ACCOUNTS_DIR / "SOS.md"
LAST_NOTIFY = ACCOUNTS_DIR / ".last_notify.json"

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
SHARED_SYMLINK_SUBDIRS = ["projects", "plugins", "agents", "skills", "commands", "memory", "hooks", "scripts"]

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


# --------------------------------------------------------------------------
# Defaults — everything overridable via config.yaml
# --------------------------------------------------------------------------

DEFAULT_CONFIG: dict[str, Any] = {
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


def _read_access_token_with_expiry(account_name: str) -> tuple[str | None, int | None]:
    """Like _read_access_token but also returns the stored expiresAt timestamp.

    expiresAt is the Unix milliseconds at which the stored access token
    expires (per the OAuth flow). Returns (None, None) if the file can't be
    read. The expiresAt is used to detect "token stale, not actually expired"
    (GH #13) — when an inactive account's stored access token has aged out
    but its refresh token is still valid, polling will 401 even though the
    account is fully usable.
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
        return None, None
    try:
        creds = read_json(creds_path)
        oauth = creds.get("claudeAiOauth", {})
        return oauth.get("accessToken"), oauth.get("expiresAt")
    except (json.JSONDecodeError, OSError):
        return None, None


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
                u = AccountUsage.empty()
                u.token_stale = True
                u.raw = {
                    "error": "stored access token expired (refresh token still valid)",
                    "expired_minutes_ago": (now_ms - int(expires_at)) // 60_000,
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
        polled_at=now_iso(),
        raw=data,
    )


def current_max_pct(usage: AccountUsage, config: dict) -> float:
    """Return the higher of 5h / 7d utilization (whichever the config enables)."""
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
    last_poll = acct.get("last_poll_ts")
    if rate <= 0 or not last_poll:
        return polled
    if now is None:
        now = datetime.now(timezone.utc)
    try:
        t0 = datetime.fromisoformat(last_poll.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return polled
    # Cap how far we trust a stale rate: a burn rate measured one interval ago
    # says little about usage 30 min later (the session may have gone idle).
    max_min = est_cfg.get("max_extrapolation_minutes", 10)
    dt_min = max(0.0, min((now - t0).total_seconds() / 60.0, max_min))
    return min(100.0, polled + rate * dt_min)


def _account_estimated_effective_pct(acct: dict, config: dict, now=None) -> float:
    """Like _account_effective_pct, but using extrapolated per-window values (C).
    Used by the target-fullness judgments (saturation filter + would-re-trip) so
    a fast-climbing account near the line is treated as already-full."""
    thr_cfg = config.get("thresholds", {})
    vals = []
    if thr_cfg.get("five_hour", True):
        vals.append(estimate_window_pct(acct, "5h", config, now))
    if thr_cfg.get("seven_day", True):
        vals.append(estimate_window_pct(acct, "7d", config, now))
    return max(vals) if vals else 0.0


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
    # at all. token_expired and poll_error always exclude.
    candidates: list[tuple[str, dict]] = [
        (name, acct) for name, acct in accounts.items()
        if name != current
        and not acct.get("token_expired", False)
        and not acct.get("poll_error")
    ]

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

    # Universal filter (GH #17, all strategies): exclude candidates above
    # the user-stated weekly ceiling. Previously only headroom/smart applied
    # this — lowest_usage/drain/round_robin would happily pick an exhausted
    # account. Falls back to "any candidate" if ALL are above cap (system
    # is degraded; swap somewhere rather than stay on a hot active).
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
        # by 5h for stability.
        candidates.sort(key=lambda kv: (_account_effective_pct(kv[1], config), kv[1].get("current_5h_pct", 0.0)))
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

        candidates.sort(key=lambda kv: -score(kv[1]))
        chosen, chosen_acct = candidates[0]
        s = score(chosen_acct)
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

        scored = [(n, a, *smart_score(a)) for n, a in candidates]
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


def execute_swap(target_name: str, trigger: str = "manual") -> dict:
    """Atomically swap to `target_name`. Returns updated state dict.

    Shared between the CLI `cus switch` command and the daemon's auto-swap.
    Caller is responsible for any preflight (hot-swap orchestration, etc.).

    Reads from + writes to the post-migration layout (.credentials.json +
    .claude.json inside each account-* dir). Pre-migration storage is
    auto-migrated on first read via migrate_account_dir().
    """
    state = load_state()
    if target_name not in state["accounts"]:
        raise ValueError(f"Unknown account '{target_name}'. Known: {sorted(state['accounts'].keys())}")
    current = state["active"]
    if target_name == current:
        return state

    target_dir = ACCOUNTS_DIR / f"account-{target_name}"
    current_dir = ACCOUNTS_DIR / f"account-{current}"

    # Auto-migrate if dir is in old layout
    migrate_account_dir(target_dir)
    migrate_account_dir(current_dir)

    target_creds = target_dir / ".credentials.json"
    target_cj = target_dir / ".claude.json"
    if not target_creds.exists():
        raise FileNotFoundError(f"{target_creds} missing — re-run `cus init --force`")
    if not target_cj.exists():
        raise FileNotFoundError(f"{target_cj} missing — re-run `cus init --force`")

    try:
        live_cj = read_json(CLAUDE_JSON)
    except json.JSONDecodeError as e:
        raise RuntimeError(f"{CLAUDE_JSON} unparseable ({e}) — Claude may be mid-write. Retry in 1s.")

    # Save current identity + creds back to current's storage. We update the
    # ENTIRE .claude.json in the source dir (not just account-bound keys)
    # so that any per-account state Claude writes there persists.
    current_identity = {k: live_cj[k] for k in ACCOUNT_BOUND_KEYS if k in live_cj}
    # Read existing per-account .claude.json (if any), overlay current account-bound keys
    if (current_dir / ".claude.json").exists():
        existing = read_json(current_dir / ".claude.json")
    else:
        existing = {}
    existing.update(current_identity)
    write_json(current_dir / ".claude.json", existing)

    # ---- GH #3: refresh-time drift detection on the credentials save-back ----
    # The live file is read into memory EXACTLY ONCE and those same bytes are
    # what gets written to storage. This closes the read-check-write race: a
    # pane's token refresh can rewrite the live file at any moment, and a
    # naive "check the file, then atomic_copy the file" would validate one
    # version and copy another. (atomic_write_bytes still makes the *write*
    # atomic; the residual race — a refresh landing between this read and the
    # target-creds install below — existed before this change and only loses
    # at most one access-token refresh, never a snapshot's lineage.)
    if not CREDS_JSON.exists():
        # Preserve pre-GH#3 behavior: atomic_copy(CREDS_JSON, ...) raised
        # FileNotFoundError here, and `switch`/daemon callers handle exactly
        # that exception type.
        raise FileNotFoundError(f"{CREDS_JSON} missing — cannot save active account's tokens back to storage")
    live_creds_bytes = CREDS_JSON.read_bytes()
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
        click.echo(f"creds-save-back: SKIPPED — live {CREDS_JSON} unparseable ({e}); "
                   f"keeping '{current}' snapshot as-is")
    if live_creds is not None:
        verdict, owner, detail = classify_live_creds_owner(live_creds, current, state)
        if verdict in ("expected", "unknown"):
            # "expected": provably current's tokens — the normal save-back.
            # "unknown": no snapshot matched, which is what a legitimate
            # refresh-token rotation on the active account looks like; the
            # save-back is the only way that rotated token gets persisted, so
            # keep the historical behavior (see classify_live_creds_owner).
            if verdict == "unknown":
                click.echo(f"creds-save-back: {detail}; assuming they are '{current}'s and saving back")
            atomic_write_bytes(current_dir / ".credentials.json", live_creds_bytes, mode=0o600)
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
            atomic_write_bytes(ACCOUNTS_DIR / f"account-{owner}" / ".credentials.json",
                               live_creds_bytes, mode=0o600)
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

    # Merge target's account-bound keys into live ~/.claude.json
    target_account_cj = read_json(target_cj)
    target_identity = {k: target_account_cj[k] for k in ACCOUNT_BOUND_KEYS if k in target_account_cj}
    for k, v in target_identity.items():
        live_cj[k] = v
    write_json(CLAUDE_JSON, live_cj)
    atomic_copy(target_creds, CREDS_JSON, mode=0o600)

    # Update state with progressive-threshold bookkeeping
    ts = now_iso()
    state["active"] = target_name
    state["accounts"][target_name]["last_swap_ts"] = ts

    # Bump current account's next_swap_at_pct ladder using CONFIG steps.
    # Falls back to the hardcoded default ladder if config is missing.
    config = load_config()
    cfg_steps = config.get("thresholds", {}).get("steps") or [50, 75, 90]
    ladder = list(cfg_steps) + [100]  # 100 = force sentinel
    current_acct = state["accounts"][current]
    cur_step = current_acct.get("next_swap_at_pct", ladder[0])
    # Find next step strictly greater than cur_step; if none, stay at last (force)
    next_step = next((s for s in ladder if s > cur_step), ladder[-1])
    current_acct["next_swap_at_pct"] = next_step

    state.setdefault("swap_history", []).append({
        "ts": ts, "from": current, "to": target_name, "trigger": trigger,
    })
    save_state(state)
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
    if cur_7d >= hard_7d_cap:
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
            _note("hard_7d_cap_no_target", "hold",
                  f"hard 7d cap tripped on {current} ({cur_7d:.1f}%) but no valid swap target")
            return None
        # If the picker had to fall back to a DEGRADED candidate (also above
        # hard_7d_cap), swapping doesn't actually help — both accounts are
        # exhausted. Don't churn; the SOS layer will surface it.
        if "[DEGRADED:" in target.reason and f"no targets below 7d cap {int(hard_7d_cap)}%" in target.reason:
            msg = (
                f"hard 7d cap tripped on {current} ({cur_7d:.1f}%) but all "
                f"other accounts are ALSO above cap — not swapping (would just churn); SOS will fire"
            )
            click.echo(f"  {msg}")
            _note("hard_7d_cap_degraded", "hold", msg)
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
        last_swap = active_acct.get("last_swap_ts")
        if last_swap:
            try:
                last_swap_dt = datetime.fromisoformat(last_swap.replace("Z", "+00:00"))
                elapsed = (datetime.now(timezone.utc) - last_swap_dt).total_seconds()
                if elapsed < min_seconds:
                    msg = (
                        f"ladder check deferred: last swap was {int(elapsed)}s ago "
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

    cur_pct = current_max_pct(cur_usage, config)
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
        if five_h_over and not seven_d_over and cur_5h < max_defer_pct:
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
        _note("no_target", "hold",
              f"ladder tripped on {current} ({cur_pct:.1f}% >= {threshold}%) but no valid swap target")
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
        update_backoff(acct, success=True, config=config)
        # Estimator (C, 2026-06-23): measure per-window burn rate (%/min) from the
        # gap between the PRIOR poll and this one, before the old values are
        # overwritten below. Only valid WITHIN a window: if the 5h/7d window rolled
        # over (resets_at changed), the prior % is from a different window, so the
        # rate is meaningless — store 0 and let the next interval re-measure from
        # the post-reset base. Stored on the account for estimate_window_pct().
        old_5h = acct.get("current_5h_pct")
        old_7d = acct.get("current_7d_pct")
        old_poll_ts = acct.get("last_poll_ts")
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
        if u.five_hour and u.five_hour.resets_at:
            acct["five_hour_resets_at"] = u.five_hour.resets_at
        if u.seven_day and u.seven_day.resets_at:
            acct["seven_day_resets_at"] = u.seven_day.resets_at
    return state


# --------------------------------------------------------------------------
# SOS diagnostics — surfaces conditions requiring human action
# --------------------------------------------------------------------------

@dataclass
class SOSCondition:
    """One thing that needs human action. Severity drives statusline color."""
    severity: str  # "urgent" (red) | "warning" (yellow)
    summary: str   # one-line headline
    action: str    # concrete next step
    affected: str  # account name or "daemon" or "system"


def _relogin_command_for(account_name: str, source_dir: str | None = None) -> str:
    """Return the exact shell command to re-login a given account.

    Prefers the managed-tree location (`~/claude-accounts/account-<name>/`)
    over legacy `~/.claude-<name>/` ad-hoc dirs. If the managed dir doesn't
    exist yet, this still returns the managed-dir command — running it
    creates the dir.
    """
    if account_name == "default":
        # default = live ~/.claude/, no env var needed
        return "claude /login"
    managed = ACCOUNTS_DIR / f"account-{account_name}"
    return f"CLAUDE_CONFIG_DIR={managed}/ claude"


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

    # Condition 1: token expired anywhere
    for name, acct in accounts.items():
        if acct.get("token_expired"):
            cmd = _relogin_command_for(name)
            out.append(SOSCondition(
                severity="urgent",
                summary=f"{name}: OAuth token expired",
                action=f"Run interactively in a terminal:\n      {cmd}\n    Complete the browser /login, then:\n      python3 ~/repos/claude-usage-swap/cus.py poll",
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
    poll_freshness_seconds = config.get("poll_interval_seconds", 300) * 4  # 4 cycles of staleness = real problem
    stale_accounts: list[str] = []
    for name, acct in accounts.items():
        last_poll = acct.get("last_poll_ts")
        if not last_poll:
            continue
        try:
            ts = datetime.fromisoformat(last_poll.replace("Z", "+00:00"))
            if (now - ts).total_seconds() > poll_freshness_seconds:
                stale_accounts.append(name)
        except ValueError:
            continue
    if stale_accounts and any(accounts.keys()) and accounts.get(list(accounts.keys())[0], {}).get("last_poll_ts"):
        out.append(SOSCondition(
            severity="warning",
            summary=f"Stale usage data for: {', '.join(stale_accounts)} (no fresh poll in >{poll_freshness_seconds // 60} min)",
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
            title = "🚨 cus SOS" if urgent else "⚠ cus warning"
            body = conditions[0].summary
            if len(conditions) > 1:
                body += f" (and {len(conditions) - 1} more)"
            try:
                subprocess.run(
                    ["notify-send", "-u", "critical" if urgent else "normal", "-a", "cus", title, body],
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

        transcript = _find_transcript(sid, e.get("cwd", ""))
        if transcript and transcript.exists():
            mtime = datetime.fromtimestamp(transcript.stat().st_mtime, tz=timezone.utc)
            if (now - mtime).total_seconds() < 3600:
                is_live = True

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
    by_pane: dict[str, dict] = {}
    for e in entries:
        pane = e.get("pane", "")
        if not pane or pane == "no-tmux":
            continue
        by_pane[pane] = e

    # GH #22: count how many panes share each cwd. The newest-mtime
    # fallback (step 3 below) is CWD-wide and can't disambiguate between
    # panes sharing a cwd — so we skip it entirely when 2+ panes share a
    # cwd. Better to relaunch fresh than to copy someone else's chat.
    cwd_pane_count: dict[str, int] = {}
    for _p, _e in by_pane.items():
        c = _e.get("cwd", "")
        if c:
            cwd_pane_count[c] = cwd_pane_count.get(c, 0) + 1

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
            t = _find_transcript(cand_sid, cwd)
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
        for pe in entries:
            if pe.get("pane") != pane:
                continue
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
            # Direct path lookup only (skip _find_transcript's fallback
            # glob across all project dirs — that scan is O(projects) per
            # call and would push the whole function back over 5s on
            # 3000-entry logs). Sessions.log entries written by our own
            # SessionStart hook always record an absolute cwd, so the
            # canonical encoded-path lookup is sufficient.
            pe_cwd = pe.get("cwd", cwd)
            if not pe_cwd:
                continue
            pe_t = CLAUDE_DIR / "projects" / pe_cwd.replace("/", "-") / f"{pe_sid}.jsonl"
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


def check_rate_limit_reactive(state: dict, config: dict) -> SwapDecision | None:
    """If there's a recent 429 since last check, force a swap on the active account.

    Returns a Tier-3 SwapDecision (force) so hot_swap_orchestrate skips
    the gentle wait-for-Stop path.

    Hysteresis: respects swap_hysteresis.min_seconds_between_reactive_swaps
    (default 60s — shorter than the ladder gate because reactive 429s are
    real emergencies). Prevents rapid-fire reactive swaps when the
    rate_limit_log churns or sessions.log mapping is briefly stale.
    """
    if not config.get("reactive", {}).get("enabled", True):
        return None
    watermark = state.get("last_429_check_ts")
    fresh = _read_rate_limit_log_since(watermark)
    if not fresh:
        # Bump watermark to now so we don't keep scanning old entries
        state["last_429_check_ts"] = now_iso()
        return None

    state["last_429_check_ts"] = now_iso()
    # Only react if at least one 429 was on the active account's session
    active = state["active"]
    matched_session = None
    for entry in fresh:
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
    click.echo(f"Active account: {state['active']}")
    click.echo()
    click.echo(f"{'Account':<20} {'5h %':>8} {'7d %':>8} {'Next swap':>12} {'Status':<24} {'Last swap':<28}")
    click.echo("-" * 104)
    for name, a in sorted(state["accounts"].items()):
        marker = " *" if name == state["active"] else ""
        last = a.get("last_swap_ts") or "never"
        flags = []
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
        click.echo(f"{name+marker:<20} {_fmt_pct(a, 'current_5h_pct')} {_fmt_pct(a, 'current_7d_pct')} {a.get('next_swap_at_pct', 50):>12} {status_col:<24} {last:<28}{est_note}")
    click.echo()

    # Locks
    pins = config.get("session_locks", {}).get("pinned", {}) or {}
    patterns = config.get("session_locks", {}).get("never_restart_patterns", []) or []
    if pins or patterns:
        click.echo("Locks:")
        for k, v in pins.items():
            click.echo(f"  pinned: {k} -> {v}")
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
            # Background mode: every session follows the global creds → its true
            # account is machine_active (pins are advisory swap-policy only, they
            # do NOT route creds per-session — see session_is_pinned). Hot-swap
            # mode: a pane keeps its loaded creds until relaunch, so the recorded
            # SessionStart account is the better estimate.
            eff = s.account if hot_swap_on else machine_active
            drift = "" if eff == s.account else f" (start:{s.account})"
            click.echo(f"  {s.session_id[:8]}  account={eff:<10} pane={s.pane:<8}{drift} cwd={s.cwd}")
        click.echo()

    history = state.get("swap_history", [])
    if history:
        click.echo(f"Recent swaps ({min(5, len(history))} of {len(history)}):")
        for entry in history[-5:]:
            click.echo(f"  {entry['ts']}  {entry['from']} -> {entry['to']}  ({entry.get('trigger', 'unknown')})")


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
def switch(target: str, dry_run: bool, trigger: str) -> None:
    """Atomically swap to a different account.

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
            click.echo(f"  TOKEN EXPIRED — re-auth this account (claude login under its config dir)")
            continue
        if u.raw.get("error"):
            click.echo(f"  ERROR: {u.raw['error']}")
            continue
        fh = f"{u.five_hour.utilization:.1f}%" if u.five_hour else "—"
        sd = f"{u.seven_day.utilization:.1f}%" if u.seven_day else "—"
        click.echo(f"  5h: {fh}    7d: {sd}    polled_at: {u.polled_at}")

    if not no_write:
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
    if u.token_stale:
        minutes = u.raw.get("expired_minutes_ago", "?")
        click.echo(f"  TOKEN_STALE — stored access token expired {minutes}m ago (refresh token still valid; account remains usable). State updated to clear any prior token_expired flag.")
        update_state_with_usage(state, {account: u})
        save_state(state)
        sys.exit(0)
    if u.token_expired:
        click.echo("  TOKEN EXPIRED — re-auth this account (`cus relogin <account>`)")
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
    if not once:
        try:
            DAEMON_PID.write_text(str(os.getpid()))
        except OSError:
            pass

    def _emit_sos_after(state: dict, config: dict) -> None:
        conditions = diagnose(state, config)
        maybe_write_sos(conditions, state)
        if conditions:
            for c in conditions:
                click.echo(f"  [{c.severity.upper()}] {c.summary}")

    def one_cycle() -> None:
        state = load_state()
        config = load_config()
        click.echo(f"[{now_iso()}] cycle start. active={state['active']}")

        # 0. Reactive check — has anything 429'd since last cycle?
        reactive_decision = check_rate_limit_reactive(state, config)
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

        # 1. Poll — skip accounts currently in poll-backoff (per-account
        # exponential delay after 429, to avoid prolonging the throttle).
        usage_by_account: dict[str, AccountUsage] = {}
        for name in state["accounts"]:
            in_backoff, until = account_in_backoff(state, name)
            if in_backoff:
                click.echo(f"  skip {name}: in poll-backoff until {until}")
                continue
            usage_by_account[name] = poll_account_usage(name)

        # 2. Update state
        update_state_with_usage(state, usage_by_account)
        # GH #59: countdown fallback — for any account whose 5h reset elapsed
        # without a fresh poll this cycle, trust the countdown and infer the
        # window reset (5h→0) BEFORE the ladder-reset + decision below, so both
        # act on reality rather than a stale-high %.
        for nm in _apply_countdown_reset_inference(state, config):
            click.echo(f"  countdown fallback: {nm} 5h reset elapsed without a fresh poll "
                       f"— inferring 5h→0% for this cycle (GH #59)")
        maybe_reset_thresholds(state, config)

        # 3. Decide
        trace: dict = {}
        decision = decide_swap(state, config, usage_by_account, trace)

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
    interval = config.get("poll_interval_seconds", 300)
    click.echo(f"daemon starting. poll_interval={interval}s. Ctrl-C to stop.")
    try:
        while True:
            try:
                one_cycle()
            except Exception as e:
                click.echo(f"ERROR in cycle: {type(e).__name__}: {e}", err=True)
            # GH #59: adaptive repoll — wake just after the soonest known 5h
            # reset (if sooner than the normal interval) to refresh real data
            # promptly instead of running on the countdown-inferred value.
            time.sleep(_adaptive_sleep_seconds(load_state(), config, interval))
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
    "accounts": "List of OAuth accounts the daemon manages. Auto-populated by `cus init`. Each has a `name` and `priority`.",
    "poll_interval_seconds": "How often the daemon calls Anthropic's OAuth usage endpoint per account. Lower = faster reaction; higher = lighter API load. 60 is the practical floor; 180-300 is the recommended default.",
    "strategy": "Swap target picker — see docs/STRATEGIES.md. `smart` (recommended): hard 7d cap + burn-before-reset for 5h windows about to expire. `headroom`: weighted 5h+7d score with hard 7d cap. `lowest_usage`: cux balanced — sort by 7d util only. `drain`: deplete-current. `strict_priority`: priority order. `round_robin`: cycle by name.",
    "smart_strategy": "Tuning for `smart` strategy. `hard_7d_cap_pct: 80` forces-swap active account at 80% 7d AND filters candidates above 80% (the user's explicit goal: never exceed 80% on the weekly window). `burn_window_hours: 2` + `burn_soon_weight: 1.0` boost candidates whose 5h is ticking AND resets within N hours (prefer to burn before reset). `cold_account_penalty: 0` (default off) deprioritizes accounts at 5h=0% (clock not ticking).",
    "headroom_strategy": "Tuning for `headroom` strategy. Same `hard_7d_cap_pct` filter. `five_hour_weight: 0.7` + `seven_day_weight: 0.3` weight the headroom score.",
    "thresholds": "Progressive per-account thresholds. `steps: [70, 85, 95]` means each account's next_swap_at_pct climbs through these levels as we swap out and return. `five_hour`/`seven_day` toggle which window the threshold applies to. `reset_below_pct` resets the ladder when both windows drop below this value.",
    "hot_swap": "Hot-swap of in-flight tmux sessions. `enabled: false` = swap creds for new sessions only (level 3). `enabled: true` = pause + relaunch existing sessions in their panes (level 4). `tier_2_at_pct` controls when pause-message injection starts; `tier_3_at_pct` controls when force-interrupt kicks in. `pause_message`/`wake_up_message` are the texts sent via tmux send-keys. `cache_bust_window_seconds` defers Tier 1 swaps when prompt cache is still warm (avoid burning rebuild cost).",
    "lazy_swap": "Cache-aware lazy background swap (GH #56). Consulted ONLY when `hot_swap.enabled` is false. A background swap never interrupts a session; its only cost is a one-time prompt-cache rebuild on the live sessions that migrate. So `enabled: true` (default) DEFERS a non-urgent swap (ladder below saturation, burn-before-reset) while any live session's last activity is younger than `cache_window_seconds: 300` (≈ Anthropic's prompt-cache TTL) — it re-fires once the cache is cold (free) or the swap turns urgent. Urgent swaps (hard 7d cap, 5h saturation, reactive 429) are never deferred. Set `enabled: false` to swap immediately on every ladder trip (old behavior). Inspect deferrals with `cus decisions`.",
    "reset_inference": "5h-window reset inference (GH #59). The statusline reset countdown ticks live per-render; this makes the daemon act on it. `enabled: true` (countdown fallback): when a poll didn't refresh an account past its 5h reset (poll failed / 429 backoff / token stale), trust the countdown — treat that window as reset (~0%) for decisions + SOS instead of the stale pre-reset %; the next successful poll supersedes it. `adaptive_repoll: true`: wake just after the soonest known 5h reset (+`repoll_buffer_seconds: 20`, floored by `min_repoll_seconds: 30`) to repoll promptly rather than running on the inferred value for a whole poll_interval. Only ever shortens the sleep.",
    "subagent_skip": "Skip-guard for sessions with in-flight subagents/tool calls. `defer_below_tier: 3` means Tier 1/2 skip those panes (per-pane, not whole orchestration — cred swap + other panes still proceed); Tier 3 (force) ALWAYS bypasses the guard regardless of defer_below_tier (GH #27).",
    "reactive": "When `enabled: true`, the PostToolUseFailure hook detects 429s in tool error bodies and triggers immediate swap (no waiting for next poll).",
    "swap_hysteresis": "Anti-churn gates on the ladder swap path. `enabled: true` (default) turns them all on. `min_seconds_between_swaps: 300` = min interval between ladder swaps. `min_seconds_between_cap_swaps: 60` / `min_seconds_between_reactive_swaps: 60` = same for the hard-7d-cap and reactive-429 emergency paths. `min_improvement_pct: 3` (GH #40) = a ladder swap must lower the active account's effective utilization by at least this many points; otherwise it's suppressed (prevents pingpong when both accounts sit in the same over-threshold band). The hard-7d-cap and reactive-429 paths bypass `min_improvement_pct` and `min_seconds_between_swaps` — they're emergencies.",
    "session_locks": "Per-session pinning. `pinned: {pane_or_session_id: account_name}` — pinned sessions are never auto-swapped. `never_restart_patterns` is a regex list matched against tmux pane's current command name.",
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


def _pct_is_unknown(acct: dict) -> bool:
    """Return True when an account's stored current_*_pct should not be displayed.

    The percentages get displayed as "?" instead of a number when ANY of the
    blocker flags is set, because we don't actually have current data — what's
    stored is last-known-good which may be hours old.

    `token_stale` is included because when the stored access token has aged out
    we skip the poll entirely and preserve the prior `current_*_pct` values.
    Across a 5h reset boundary that preserved value silently lies — e.g. a
    pre-stale 100% gets surfaced as `5h:100%` even after the window has reset
    to zero. Treating token_stale as unknown surfaces `?` until the account is
    actually used and the refresh-token-driven mint refreshes our data.

    Shows "?" rather than the buggy "0.0" sentinel (or stale numbers) per the
    2026-05-20 user feedback: 0% implied "no usage" which was equally
    misleading as 100% from the prior sentinel bug.
    """
    return bool(
        acct.get("rate_limited")
        or acct.get("token_expired")
        or acct.get("poll_error")
        or acct.get("token_stale")
    )


def _fmt_pct(acct: dict, key: str, width: int = 8, prec: int = 1) -> str:
    """Format a percentage for display, returning '?' if stale.

    Use this in any user-facing output of current_5h_pct or current_7d_pct.
    """
    if _pct_is_unknown(acct):
        return f"{'?':>{width}}"
    val = acct.get(key, 0.0)
    return f"{val:>{width}.{prec}f}"


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
    last_poll = acct.get("last_poll_ts")
    if not last_poll:
        return True                            # never polled → can't have seen post-reset
    poll_age = _time_since(last_poll)          # seconds since the last poll
    # Stale iff the last poll predates the reset (poll older than the reset).
    return poll_age is not None and poll_age > reset_age


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

    # SOS takes priority — if human action is needed, surface it loudly
    conditions = diagnose(state, config)
    urgent = [c for c in conditions if c.severity == "urgent"]
    if urgent:
        msg = f"🚨 cus SOS: {urgent[0].summary} — see ~/claude-accounts/SOS.md · {render_stamp}"
        click.echo(click.style(msg, fg="red", bold=True) if color_on else msg, color=color_on)
        return
    warning = [c for c in conditions if c.severity == "warning"]
    if warning:
        msg = f"⚠ cus: {warning[0].summary} · {render_stamp}"
        click.echo(click.style(msg, fg="yellow", bold=True) if color_on else msg, color=color_on)
        return

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
    # In background-swap mode the live session follows the global active
    # account on its next request, so the recorded SessionStart account is not
    # where it actually is. Only hot-swap mode has a genuine "loaded creds vs
    # machine-active" mismatch worth surfacing.
    hot_swap_on = config.get("hot_swap", {}).get("enabled", False)
    if not hot_swap_on:
        this_session_account = machine_active
    # Display PRIMARY = this session's account if known, else machine-active.
    active = this_session_account or machine_active
    pending_swap = this_session_account and this_session_account != machine_active

    # GH #36: is THIS pane pinned, and to which account? Computed once and
    # surfaced in both compact and verbose modes (SOS/warning early-returns
    # above intentionally stay uncluttered — human-action-needed beats pin
    # bookkeeping for the one line we get).
    _, pin_account = _current_pane_pin(config)
    pin_lbl = _sl_pin_label(pin_account, active, color_on)

    acct = state.get("accounts", {}).get(active, {})
    fh = acct.get("current_5h_pct", 0)
    sd = acct.get("current_7d_pct", 0)
    nx = acct.get("next_swap_at_pct", 50)

    # GH #38: a colored, bold active-account label reused by both compact and
    # verbose so the eye lands on which account is in play.
    active_lbl = click.style(active, fg="cyan", bold=True) if color_on else active
    nxt_lbl = (lambda n: click.style(f"nxt:{n}%", dim=True) if color_on else f"nxt:{n}%")

    if not is_verbose:
        # Compact mode. Render "?" if we don't have reliable data.
        if _pct_is_unknown(acct):
            pin_bit = f" {pin_lbl}" if pin_lbl else ""
            click.echo(f"cus:{active_lbl}{pin_bit} 5h:? 7d:? {nxt_lbl(nx)} · {render_stamp}", color=color_on)
            return
        # GH #59: compact mode must flag a rolled-over 5h window live, exactly
        # like verbose mode already does. The reset countdown elapses between
        # polls; once it has, the stored 5h % is pre-reset stale (up to a full
        # poll interval of showing e.g. 96% on a window that actually reset to
        # ~0). Compact is the DEFAULT rendering, so before this it was the one
        # place the stale number still showed bare.
        rolled5 = _five_hour_rolled_since_poll(acct)
        if rolled5:
            fh_piece = _sl_rolled_5h_label(acct, fh, color_on)
            # The stale % must not drive the near-swap-point marker either —
            # the window just reset, so its real contribution is ~0. (If the
            # daemon's countdown inference already zeroed current_5h_pct this
            # is a no-op; this covers renders before that cycle runs.)
            marker_fh = 0.0
        else:
            fh_piece = f"5h:{_sl_pct(fh, nx, color_on)}"
            marker_fh = fh
        flag_marker = ""
        if max(marker_fh, sd) >= nx:
            flag_marker = "⚠"
        elif max(marker_fh, sd) >= nx * 0.8:
            flag_marker = "·"
        prefix_bits = [f"cus:{active_lbl}"]
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
        click.echo(
            f"{prefix} {fh_piece} 7d:{_sl_pct(sd, nx, color_on)} "
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
        pieces_prefix = "cus "

    def fmt_account(name: str, a: dict, is_active: bool) -> str:
        h5 = a.get("current_5h_pct", 0)
        h7 = a.get("current_7d_pct", 0)
        nxt = a.get("next_swap_at_pct", 50)
        unknown = _pct_is_unknown(a)
        flags = []
        if a.get("token_expired"):
            flags.append("EXP")
        if a.get("rate_limited"):
            flags.append("429")
        flag_str = "(" + ",".join(flags) + ")" if flags else ""
        marker = "*" if is_active else ""
        # GH #38: bold-cyan the active account; dim the others so the eye lands
        # on the one in play.
        name_lbl = f"{name}{marker}"
        if color_on:
            name_lbl = click.style(name_lbl, fg="cyan", bold=True) if is_active else click.style(name_lbl, dim=True)
        parts = [name_lbl]
        pct5 = "?" if unknown else _sl_pct(h5, nxt, color_on)
        pct7 = "?" if unknown else _sl_pct(h7, nxt, color_on)
        if show_resets:
            r5 = _time_until(a.get("five_hour_resets_at"))
            r7 = _time_until(a.get("seven_day_resets_at"))
            r7_raw = f"·{_fmt_duration(r7)}" if r7 is not None and r7 > 0 and not unknown else ""
            r7_str = click.style(r7_raw, dim=True) if color_on and r7_raw else r7_raw
            rolled5 = _five_hour_rolled_since_poll(a)
            if rolled5 and not unknown:
                # GH #59: the live countdown has elapsed — the 5h window rolled
                # over since our last poll, so the displayed % is pre-reset
                # stale. Flag it (↻reset, was X%) rather than showing the stale
                # number bare, so the operator knows a reset happened without
                # waiting to repoll. Next poll confirms the new (~0%) value.
                # Rendering shared with compact mode via _sl_rolled_5h_label.
                parts.append(_sl_rolled_5h_label(a, h5, color_on))
            else:
                r5_raw = f"·{_fmt_duration(r5)}" if r5 is not None and r5 > 0 and not unknown else ""
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
        prefix = "[URGENT]" if c.severity == "urgent" else "[WARNING]"
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
@click.option("--exec", "exec_flag", is_flag=True, help="Immediately exec `claude` under the account dir.")
def relogin_cmd(name: str, exec_flag: bool) -> None:
    """Print the command to re-login an existing account (refresh OAuth tokens).

    Run this when `cus sos` reports a token expired for a given account.
    After the re-login, `cus poll` to update state.
    """
    dst = ACCOUNTS_DIR / f"account-{name}"
    if name != "default" and not dst.exists():
        click.echo(f"Unknown account '{name}'. Run `cus list` to see configured accounts.")
        sys.exit(1)
    cmd = _relogin_command_for(name)
    click.echo(f"To re-login {name}, run:")
    click.echo(f"  {cmd}")
    click.echo()
    click.echo(f"After logging in:")
    click.echo(f"  python3 ~/repos/claude-usage-swap/cus.py poll")
    if exec_flag:
        click.echo()
        click.echo("Launching claude now...")
        env = os.environ.copy()
        if name == "default":
            env.pop("CLAUDE_CONFIG_DIR", None)
        else:
            env["CLAUDE_CONFIG_DIR"] = str(dst)
        os.execvpe("claude", ["claude"], env)


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
