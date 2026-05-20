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
    429.log                      PostToolUseFailure detections.
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
    "smart_strategy": {
        "hard_7d_cap_pct": 80,           # force-swap active when 7d crosses this; never SWAP TO an account above
        "burn_window_hours": 2,          # if 5h resets within N hours and clock is ticking, boost score
        "burn_soon_weight": 1.0,         # multiplier on burn-soon bonus
        "five_hour_headroom_weight": 0.6,
        "seven_day_headroom_weight": 0.4,
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
    },
    "subagent_skip": {
        "enabled": True,
        "defer_below_tier": 3,   # if a subagent is active, defer Tier 1/2; Tier 3 proceeds anyway
    },
    "reactive": {
        "enabled": True,         # detect 429s via PostToolUseFailure hook
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
        "install_post_tool_use_failure": True,
        "install_pre_tool_use": False,    # only needed if subagent_skip.enabled
        "install_subagent_stop": False,   # only needed if subagent_skip.enabled
    },
    "statusline": {
        "verbose": False,                 # cus statusline output verbosity
        "show_other_accounts": True,      # in verbose mode, include other account states
        "show_poll_age": True,            # include "polled Nago"
        "show_reset_times": True,         # show time until 5h / 7d resets
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
    """All usage windows for a single OAuth account, plus error state."""
    five_hour: UsageWindow | None = None
    seven_day: UsageWindow | None = None
    seven_day_sonnet: UsageWindow | None = None
    seven_day_opus: UsageWindow | None = None
    token_expired: bool = False
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
    With base=300, max=1800: 5min → 10min → 20min → 30min (capped).
    """
    polling_cfg = config.get("polling", {})
    base = polling_cfg.get("base_backoff_seconds", 300)
    cap = polling_cfg.get("max_backoff_seconds", 1800)
    if success:
        acct.pop("poll_backoff_until_ts", None)
        acct.pop("poll_backoff_consecutive_429s", None)
        return
    n = acct.get("poll_backoff_consecutive_429s", 0) + 1
    delay = min(base * (2 ** (n - 1)), cap)
    until = datetime.now(timezone.utc) + timedelta(seconds=delay)
    acct["poll_backoff_consecutive_429s"] = n
    acct["poll_backoff_until_ts"] = until.isoformat().replace("+00:00", "Z")


def poll_account_usage(account_name: str) -> AccountUsage:
    """Query the Anthropic OAuth usage endpoint for one account.

    The same endpoint Claude Code itself uses for /usage. Returns parsed
    AccountUsage. On 401, sets `token_expired=True` so the caller can
    surface it without crashing the daemon. On other errors, returns
    empty `AccountUsage` with raw error in `raw['error']`.
    """
    token = _read_access_token(account_name)
    if not token:
        u = AccountUsage.empty()
        u.raw = {"error": f"no access_token for account {account_name}"}
        return u

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
            u.token_expired = True
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

    strategy = config.get("strategy", "lowest_usage")

    if strategy == "lowest_usage":
        # cux balanced: sort ascending by 7d util, ties broken by 5h util
        candidates.sort(key=lambda kv: (kv[1].get("current_7d_pct", 0.0), kv[1].get("current_5h_pct", 0.0)))
        chosen, _ = candidates[0]
        return SwapTarget(name=chosen, reason="lowest_usage: lowest 7d util")

    if strategy == "drain":
        # cux drain: prefer candidates with both windows under their own next_swap_at
        ordered = [
            (name, acct) for name, acct in candidates
            if acct.get("current_5h_pct", 0.0) < acct.get("next_swap_at_pct", 50)
            and acct.get("current_7d_pct", 0.0) < acct.get("next_swap_at_pct", 50)
        ]
        if ordered:
            ordered.sort(key=lambda kv: (-kv[1].get("current_7d_pct", 0.0),))  # closest-to-cap first
            chosen, _ = ordered[0]
            return SwapTarget(name=chosen, reason="drain: 7d under cap")
        # Pass 2: any candidate with 5h headroom
        ordered = [(n, a) for n, a in candidates if a.get("current_5h_pct", 0.0) < 100]
        if ordered:
            ordered.sort(key=lambda kv: -kv[1].get("current_7d_pct", 0.0))
            chosen, _ = ordered[0]
            return SwapTarget(name=chosen, reason="drain: 5h has room")
        return None

    if strategy == "strict_priority":
        # Sort by priority ascending (1 = highest); pick first with room
        cfg_accounts = {a["name"]: a for a in config.get("accounts", [])}
        candidates.sort(key=lambda kv: cfg_accounts.get(kv[0], {}).get("priority", 99))
        for name, acct in candidates:
            if acct.get("current_5h_pct", 0.0) < acct.get("next_swap_at_pct", 50) \
               and acct.get("current_7d_pct", 0.0) < acct.get("next_swap_at_pct", 50):
                return SwapTarget(name=name, reason="strict_priority: highest priority with headroom")
        return None

    if strategy == "round_robin":
        names = sorted(accounts.keys())
        idx = names.index(current)
        nxt = names[(idx + 1) % len(names)]
        return SwapTarget(name=nxt, reason="round_robin")

    if strategy == "headroom":
        # Weighted score of 5h + 7d headroom, with hard 7d cap filter.
        # See docs/STRATEGIES.md for the design rationale.
        cfg = config.get("headroom_strategy", {})
        hard_7d = cfg.get("hard_7d_cap_pct", 80)
        w5 = cfg.get("five_hour_weight", 0.7)
        w7 = cfg.get("seven_day_weight", 0.3)

        # Hard 7d cap: among candidates, prefer those below cap. If ALL are
        # at/over cap, fall back to all (we have to swap somewhere).
        safe_7d = [(n, a) for n, a in candidates if a.get("current_7d_pct", 0) < hard_7d]
        pool = safe_7d if safe_7d else candidates

        def score(acct: dict) -> float:
            return (100 - acct.get("current_5h_pct", 0.0)) * w5 + (100 - acct.get("current_7d_pct", 0.0)) * w7

        pool.sort(key=lambda kv: -score(kv[1]))
        chosen, chosen_acct = pool[0]
        s = score(chosen_acct)
        note = "" if safe_7d else f" (no 7d-safe targets — best of {len(candidates)})"
        return SwapTarget(name=chosen, reason=f"headroom: 5h={chosen_acct.get('current_5h_pct', 0):.0f}% 7d={chosen_acct.get('current_7d_pct', 0):.0f}% score={s:.1f}{note}")

    if strategy == "smart":
        # Smart strategy designed for: never go over 80% 7d; route on 5h
        # dynamics; prefer "burn-before-reset" candidates (those whose 5h
        # window will reset soon — using them now means we don't waste
        # remaining capacity). See docs/STRATEGIES.md for the design.
        cfg = config.get("smart_strategy", {})
        hard_7d = cfg.get("hard_7d_cap_pct", 80)
        burn_window_hours = cfg.get("burn_window_hours", 2)
        burn_soon_weight = cfg.get("burn_soon_weight", 1.0)
        w5 = cfg.get("five_hour_headroom_weight", 0.6)
        w7 = cfg.get("seven_day_headroom_weight", 0.4)
        cold_penalty = cfg.get("cold_account_penalty", 0)  # 0 = no penalty (default)

        # Hard 7d cap filter (same as headroom)
        safe_7d = [(n, a) for n, a in candidates if a.get("current_7d_pct", 0) < hard_7d]
        pool = safe_7d if safe_7d else candidates

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

            # Cold-account penalty: deprioritize accounts with 5h=0% if
            # configured (so we don't always pick the "freshest" — useful
            # for the future "straddle" strategy where you want a mix of
            # warm/cold accounts).
            if h5 == 0.0 and cold_penalty:
                s -= cold_penalty
                reasons.append(f"cold-penalty(-{cold_penalty})")

            return s, " ".join(reasons) if reasons else "(headroom only)"

        scored = [(n, a, *smart_score(a)) for n, a in pool]
        scored.sort(key=lambda x: -x[2])
        chosen, chosen_acct, chosen_score, chosen_reasons = scored[0]
        note = "" if safe_7d else f" [no 7d-safe targets — best of {len(candidates)}]"
        return SwapTarget(
            name=chosen,
            reason=f"smart: 5h={chosen_acct.get('current_5h_pct', 0):.0f}% 7d={chosen_acct.get('current_7d_pct', 0):.0f}% score={chosen_score:.1f} {chosen_reasons}{note}",
        )

    return None


# --------------------------------------------------------------------------
# Swap primitive (callable from daemon, not just CLI)
# --------------------------------------------------------------------------

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
    atomic_copy(CREDS_JSON, current_dir / ".credentials.json", mode=0o600)

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
    "PostToolUseFailure": ("cus_post_tool_use_failure.sh", "install_post_tool_use_failure"),
    "PreToolUse": ("cus_pre_tool_use.sh", "install_pre_tool_use"),
    "SubagentStop": ("cus_subagent_stop.sh", "install_subagent_stop"),
}


def _settings_json_path() -> Path:
    return CLAUDE_DIR / "settings.json"


def install_hooks(config: dict) -> dict[str, str]:
    """Install enabled hooks into ~/.claude/settings.json.

    Each hook entry is tagged with a signature in its `_cus_marker` field
    so we can identify our entries and avoid clobbering others'. Returns
    {event_name: "installed" | "already_installed" | "skipped (disabled)"}.
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
        entry = next(
            (e for e in event_entries if isinstance(e, dict) and e.get("_cus_marker") == HOOK_SETTINGS_KEY),
            None,
        )
        new_entry = {
            "_cus_marker": HOOK_SETTINGS_KEY,
            "matcher": "",
            "hooks": [{"type": "command", "command": str(script_path)}],
        }
        if entry:
            if entry == new_entry:
                result[event] = "already_installed"
                continue
            event_entries[event_entries.index(entry)] = new_entry
            result[event] = "updated"
        else:
            event_entries.append(new_entry)
            result[event] = "installed"

    write_json(settings_path, settings)
    return result


def uninstall_hooks() -> dict[str, str]:
    """Remove all cus-marked entries from ~/.claude/settings.json."""
    settings_path = _settings_json_path()
    if not settings_path.exists():
        return {event: "no settings.json" for event in HOOK_EVENTS}
    settings = read_json(settings_path)
    hooks = settings.get("hooks", {})

    result: dict[str, str] = {}
    for event in HOOK_EVENTS:
        entries = hooks.get(event, [])
        new_entries = [e for e in entries if not (isinstance(e, dict) and e.get("_cus_marker") == HOOK_SETTINGS_KEY)]
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
        marked = [e for e in entries if isinstance(e, dict) and e.get("_cus_marker") == HOOK_SETTINGS_KEY]
        if marked:
            cmds = ",".join(h.get("command", "?") for e in marked for h in e.get("hooks", []))
            result[event] = f"installed -> {cmds}"
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


def determine_tier(active_acct: dict, config: dict) -> int:
    """Map current next_swap_at_pct to a tier based on config.

    Tier 1: at first step (default 50) — gentlest, wait for Stop, defer if cache warm.
    Tier 2: at tier_2_at_pct step (default 75) — inject pause-message.
    Tier 3: at tier_3_at_pct step or force (default 90 or 100) — interrupt, log shells.
    """
    step = active_acct.get("next_swap_at_pct", 50)
    hot = config.get("hot_swap", {})
    if step >= hot.get("tier_3_at_pct", 90):
        return 3
    if step >= hot.get("tier_2_at_pct", 75):
        return 2
    return 1


def decide_swap(state: dict, config: dict, usage_by_account: dict[str, AccountUsage]) -> SwapDecision | None:
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
    """
    current = state.get("active")
    if not current:
        return None

    active_acct = state["accounts"][current]
    cur_usage = usage_by_account.get(current)
    if cur_usage is None:
        return None

    # Trigger 0: active account just returned HTTP 429.
    # Replaces the prior sentinel approach (set current_*_pct=100 → ladder
    # trips → swap). After the 2026-05-20 fix preserving last known good
    # percentages on 429, we need this explicit trigger instead.
    if cur_usage.raw.get("rate_limited"):
        target = pick_swap_target(state, config)
        if target is None:
            return None
        return SwapDecision(
            target=target.name,
            reason=f"active {current} is rate-limited (HTTP 429); target: {target.reason}",
            tier=determine_tier(active_acct, config),
        )

    # Trigger 1: hard 7d cap.
    # Active strategy must support this; smart and headroom do via their
    # respective config blocks. Other strategies pick up the default 80%.
    strategy_cfg = config.get(f"{config.get('strategy', '')}_strategy", {})
    hard_7d_cap = strategy_cfg.get("hard_7d_cap_pct") or config.get("smart_strategy", {}).get("hard_7d_cap_pct", 80)
    cur_7d = cur_usage.seven_day.utilization if cur_usage.seven_day else 0.0
    if cur_7d >= hard_7d_cap:
        target = pick_swap_target(state, config)
        if target is None:
            return None
        return SwapDecision(
            target=target.name,
            reason=f"hard 7d cap: {current} at {cur_7d:.1f}% >= {hard_7d_cap}%; target: {target.reason}",
            tier=determine_tier(active_acct, config),
        )

    # Trigger 2: progressive ladder.
    cfg_steps = config.get("thresholds", {}).get("steps") or [50, 75, 90]
    threshold = active_acct.get("next_swap_at_pct", cfg_steps[0])
    if threshold >= 100:  # force sentinel: any usage trips
        threshold = 0

    cur_pct = current_max_pct(cur_usage, config)
    if cur_pct < threshold:
        return None

    target = pick_swap_target(state, config)
    if target is None:
        return None
    return SwapDecision(
        target=target.name,
        reason=f"{target.reason}; current {current} at {cur_pct:.1f}% >= {threshold}%",
        tier=determine_tier(active_acct, config),
    )


def update_state_with_usage(state: dict, usage_by_account: dict[str, AccountUsage]) -> dict:
    """Mutate state.json's per-account current_*_pct from a poll cycle.

    Each per-account update follows one of four exclusive branches:
      1. token_expired (HTTP 401)    — flag and preserve prior percentages
      2. rate_limited (HTTP 429)     — flag and preserve prior percentages
      3. poll_error (other failures) — flag and preserve prior percentages
      4. success — clear all flags, store new percentages + reset times

    The four-way exclusive structure replaces a buggy version where 429
    fell through to the poll_error branch (both flags getting set
    simultaneously). The picker filters by any of (token_expired,
    rate_limited, poll_error); each is exclusive on a per-cycle basis.
    """
    config = load_config()
    for name, acct in state["accounts"].items():
        u = usage_by_account.get(name)
        if u is None:
            continue

        # Branch 1: token expired (401)
        if u.token_expired:
            acct["token_expired"] = True
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
        acct["token_expired"] = False
        update_backoff(acct, success=True, config=config)
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
    valid = [n for n, a in accounts.items() if not a.get("token_expired") and not a.get("rate_limited") and not a.get("poll_error")]
    active = state.get("active")
    if active and active in accounts:
        active_acct = accounts[active]
        threshold = active_acct.get("next_swap_at_pct", 50)
        cur_pct = max(active_acct.get("current_5h_pct", 0), active_acct.get("current_7d_pct", 0))

        if not valid:
            out.append(SOSCondition(
                severity="urgent",
                summary="All accounts blocked",
                action="Re-login any TOKEN_EXPIRED accounts (see above). For RATE_LIMITED accounts, wait for cooldown (5h or 7d window reset).",
                affected="system",
            ))
        elif cur_pct >= threshold:
            # Active is over its threshold step. Are there any UNBLOCKED others?
            targets = [n for n in valid if n != active]
            if not targets:
                out.append(SOSCondition(
                    severity="urgent",
                    summary=f"{active} at {cur_pct:.0f}% (≥{threshold}%) and no other account available",
                    action="Add another Claude account (run `claude /login` with CLAUDE_CONFIG_DIR=~/.claude-newname/) or wait for cooldown.",
                    affected=active,
                ))

    # Condition 4: stale poll
    poll_freshness_seconds = config.get("poll_interval_seconds", 300) * 4  # 4 cycles of staleness = real problem
    now = datetime.now(timezone.utc)
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
    """Reset next_swap_at_pct to config's first step under two conditions:
      1. Both windows have dropped below `reset_below_pct` (window-reset case).
      2. The current value isn't in the configured ladder (config was edited
         since last state save) — migrate to the nearest matching step.

    Without this, after a week's reset an account's ladder stays at 95 and
    we'd never use the gentle Tier 1 again. AND, without the migration arm,
    editing `thresholds.steps` in config.yaml wouldn't take effect on
    already-populated state.
    """
    cfg_steps = config.get("thresholds", {}).get("steps") or [50, 75, 90]
    first_step = cfg_steps[0]
    ladder = list(cfg_steps) + [100]
    reset_below = config.get("thresholds", {}).get("reset_below_pct", 50)
    for name, acct in state["accounts"].items():
        cur = acct.get("next_swap_at_pct", first_step)
        # Window-reset case
        if acct.get("current_5h_pct", 0) < reset_below and acct.get("current_7d_pct", 0) < reset_below:
            if cur > first_step:
                acct["next_swap_at_pct"] = first_step
                continue
        # Stale-config-value migration
        if cur not in ladder:
            # Snap to the nearest step >= current value (don't lower thresholds silently)
            acct["next_swap_at_pct"] = next((s for s in ladder if s >= cur), first_step)


# --------------------------------------------------------------------------
# Live session tracking (Phase 3+)
# --------------------------------------------------------------------------

@dataclass
class LiveSession:
    """One Claude Code session believed to be alive based on hook log tails."""
    session_id: str
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


def session_is_idle(session: LiveSession, idle_seconds: int) -> bool:
    """Return True if the session's transcript has had no new writes in `idle_seconds`.

    This is the "already past its last Stop" check: if no JSONL activity for
    `idle_seconds`, the session is between turns — Claude is at the input
    prompt, not generating. We can swap *immediately* in this state, no
    need to wait for a fresh Stop signal (which won't fire until the user
    types something next).

    Returns False if we can't determine (no transcript path, can't stat).
    """
    if session.transcript_path is None or not session.transcript_path.exists():
        return False
    try:
        mtime = datetime.fromtimestamp(session.transcript_path.stat().st_mtime, tz=timezone.utc)
        return (datetime.now(timezone.utc) - mtime).total_seconds() > idle_seconds
    except OSError:
        return False


def find_live_panes(account_filter: str | None = None) -> list[LiveSession]:
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

    out: list[LiveSession] = []
    for pane, e in by_pane.items():
        if account_filter and e["account"] != account_filter:
            continue
        if not tmux_pane_exists(pane):
            continue
        pane_cmd = tmux_pane_name(pane)
        if pane_cmd not in ("claude", "node"):
            # Pane is back to shell (or running something else). No live claude.
            continue

        sid = e["session_id"]
        transcript = _find_transcript(sid, e.get("cwd", ""))
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
    return out


def tmux_exit_claude(pane: str) -> None:
    """Cleanly /exit claude in a tmux pane, handling input-box-has-focus cases.

    The 2026-05-19 incident showed that a bare `/exit` typed via tmux send-keys
    gets eaten by claude's input box as TEXT when the input is focused or
    contains user-typed content — claude doesn't interpret it as a slash
    command. Result: pane stays at claude, never returns to shell.

    Defensive sequence:
      1. Escape — drop modal state, lose input-box focus (no-op if already at shell)
      2. C-u — clear input line (kills any text the user typed)
      3. `/exit` + Enter — typed cleanly, recognized as slash command

    Best-effort: if claude is genuinely wedged, escape sequences may do
    nothing, and `wait_for_shell` will eventually time out + skip relaunch.
    """
    if not tmux_is_available() or not pane or pane == "no-tmux":
        return
    # Step 1: Escape (lose focus / dismiss modal)
    tmux_send_keys(pane, "Escape")
    time.sleep(0.2)
    # Step 2: C-u (clear line in any readline-style input)
    tmux_send_keys(pane, "C-u")
    time.sleep(0.2)
    # Step 3: type /exit and press Enter
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
      3. Subagent skip-guard: defer if any pane has active subagent and
         tier < config.subagent_skip.defer_below_tier.
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

    # CHANGED 2026-05-20 (issue #2 fix): query EVERY live pane, then keep only
    # those whose loaded account doesn't match the target. The pane's loaded
    # account is its latest SessionStart-recorded account (sessions.log,
    # latest entry per pane). Previous behavior filtered by account_filter=current
    # at the find_live_panes level, which missed panes that had drifted (their
    # SessionStart-recorded account differed from current.active after a
    # non-orchestrated swap). The new logic restarts panes that need to align
    # with the target, regardless of which account they're currently on.
    all_panes = find_live_panes(account_filter=None)
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

    # Subagent skip-guard (per-pane)
    if config.get("subagent_skip", {}).get("enabled", True):
        defer_below = config["subagent_skip"].get("defer_below_tier", 3)
        if tier < defer_below:
            for s in swappable:
                if subagent_active(s, config):
                    click.echo(f"    DEFER: subagent active in pane {s.pane} (session {s.session_id[:8]}); tier={tier} < {defer_below}")
                    return

    # Cache-bust window (Tier 1 only, non-idle panes only)
    if tier == 1:
        window = hot.get("cache_bust_window_seconds", 300)
        idle_seconds = hot.get("mid_turn_idle_seconds", 30)
        for s in swappable:
            if session_is_idle(s, idle_seconds):
                continue
            if s.transcript_path and cache_warm(s.transcript_path, window):
                click.echo(f"    DEFER: cache warm for active pane {s.pane} (Tier 1 swap would burn cache)")
                return

    # Per-tier prep
    for s in swappable:
        if tier == 2:
            click.echo(f"    pane {s.pane}: injecting pause-message")
            tmux_send_text(s.pane, hot.get("pause_message", "please pause; we're swapping accounts."))
            ok = wait_for_stop(s.session_id, hot.get("pause_response_timeout_seconds", 120))
            if not ok:
                click.echo(f"      timed out waiting for Stop; proceeding anyway")
        elif tier == 3:
            click.echo(f"    pane {s.pane}: force interrupt (double Escape)")
            # Single Escape may not interrupt — Claude's TUI sometimes needs
            # two to dismiss running tool calls.
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
            idle_seconds = hot.get("mid_turn_idle_seconds", 30)
            if session_is_idle(s, idle_seconds):
                click.echo(f"    pane {s.pane}: idle (>{idle_seconds}s) — already at turn boundary, no wait")
            else:
                stop_timeout = hot.get("stop_wait_timeout_seconds", 300)
                click.echo(f"    pane {s.pane}: mid-turn — waiting for Stop (up to {stop_timeout}s)")
                ok = wait_for_stop(s.session_id, stop_timeout)
                if not ok:
                    click.echo(f"      timed out; aborting (will retry next cycle)")
                    return

    # /exit each pane — use tmux_exit_claude which sends Escape + C-u first
    # to drop input-box focus and clear any pending text, then types /exit.
    # Bare `tmux send-keys "/exit"` gets eaten as input-box text when claude's
    # input is focused — the 2026-05-19 incident on pane %3 showed this.
    for s in swappable:
        click.echo(f"    pane {s.pane}: /exit (with Escape + C-u prefix)")
        tmux_exit_claude(s.pane)

    # Poll each pane until shell prompt is back. Replaces the original
    # sleep(2) which raced — claude wasn't always done exiting when the
    # relaunch command got typed, so the relaunch ended up as text INSIDE
    # the still-loading new claude.
    shell_timeout = hot.get("shell_return_timeout_seconds", 10)
    panes_ready: list[LiveSession] = []
    for s in swappable:
        if wait_for_shell(s.pane, timeout_seconds=shell_timeout):
            panes_ready.append(s)
        else:
            click.echo(f"    WARN: pane {s.pane} didn't return to shell in {shell_timeout}s; skipping relaunch")

    if not panes_ready:
        click.echo(f"    no panes reached shell prompt; doing cred swap only (no relaunch)")
        execute_swap(decision.target, trigger=f"auto-tier{tier}-noreloaunch")
        return

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
    flags = hot.get("relaunch_flags", "").strip()
    flag_part = f" {flags}" if flags else ""
    use_resume = hot.get("relaunch_with_resume", True)
    wake = hot.get("wake_up_message", "").strip()
    wake_part = f" {shlex.quote(wake)}" if wake else ""
    for s in panes_ready:
        if use_resume:
            cmd = f"cd {shlex.quote(s.cwd)} && claude{flag_part} --resume {shlex.quote(s.session_id)}{wake_part}"
        else:
            cmd = f"cd {shlex.quote(s.cwd)} && claude{flag_part}{wake_part}"
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

    target = pick_swap_target(state, config)
    if target is None:
        click.echo(f"  429 detected on {active} session {matched_session['session_id'][:8]} but no valid swap target")
        return None
    return SwapDecision(
        target=target.name,
        reason=f"429 reactive: {matched_session['match']} in session {matched_session['session_id'][:8]}",
        tier=3,
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
                # Force refresh: re-read creds + identity from this dir (no-op if same)
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
            identity = extract_identity(dst_cj)
        else:
            identity = {}
            write_json(dst_cj, identity)

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
        meta_path = dst / "meta.yaml"
        prior_meta = read_yaml(meta_path) if meta_path.exists() else {}
        oauth = identity.get("oauthAccount") or {}
        meta = {
            "name": name,
            "source_dir": str(src_dir),
            "oauth_email": oauth.get("emailAddress", "unknown") if isinstance(oauth, dict) else "unknown",
            "oauth_account_uuid": oauth.get("accountUuid", "unknown") if isinstance(oauth, dict) else "unknown",
            "priority": prior_meta.get("priority", 1),
            "locked_sessions": prior_meta.get("locked_sessions", []),
            "imported_ts": prior_meta.get("imported_ts", now_iso()) if existed else now_iso(),
            "refreshed_ts": now_iso() if existed else None,
        }
        write_yaml(dst / "meta.yaml", meta)
        if existed:
            click.echo(f"  refreshed {name} -> {dst}")
            refreshed += 1
        else:
            click.echo(f"  imported {name} -> {dst}")
            imported += 1

    # 4. state.json — runtime state for the (future) daemon
    if not STATE_JSON.exists():
        state = {
            "active": "default" if any(n == "default" for n, _ in candidates) else candidates[0][0],
            "accounts": {
                name: {
                    "current_5h_pct": 0.0,
                    "current_7d_pct": 0.0,
                    "next_swap_at_pct": 50,
                    "last_swap_ts": None,
                }
                for name, _ in candidates
            },
            "swap_history": [],
        }
        write_json(STATE_JSON, state)
        click.echo(f"  wrote {STATE_JSON} (active = {state['active']})")

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
        status_col = ",".join(flags) if flags else "ok"
        click.echo(f"{name+marker:<20} {_fmt_pct(a, 'current_5h_pct')} {_fmt_pct(a, 'current_7d_pct')} {a.get('next_swap_at_pct', 50):>12} {status_col:<24} {last:<28}")
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
    live = find_live_sessions()
    if live:
        click.echo(f"Live sessions ({len(live)}):")
        for s in live:
            click.echo(f"  {s.session_id[:8]}  account={s.account:<10} pane={s.pane:<8} cwd={s.cwd}")
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
    flags = hot.get("relaunch_flags", "").strip()
    flag_part = f" {flags}" if flags else ""
    use_resume = hot.get("relaunch_with_resume", True)
    wake = hot.get("wake_up_message", "").strip()
    wake_part = f" {shlex.quote(wake)}" if wake else ""
    click.echo(click.style("Planned relaunch commands (would be typed via `tmux send-keys`):", bold=True))
    relaunch_count = 0
    for s in live_panes:
        if s.account == current:
            continue  # already aligned — no restart needed
        pinned, _ = session_is_pinned(s, config)
        wl, _ = session_matches_whitelist(s, config)
        if pinned or wl:
            continue
        if use_resume:
            cmd = f"cd {shlex.quote(s.cwd)} && claude{flag_part} --resume {shlex.quote(s.session_id)}{wake_part}"
        else:
            cmd = f"cd {shlex.quote(s.cwd)} && claude{flag_part}{wake_part}"
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
        decision = SwapDecision(target=chosen_name, reason=reason, tier=tier)
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

    # Daemon writes its pid so `cus sos` can detect "pid recorded but gone"
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
        maybe_reset_thresholds(state, config)

        # 3. Decide
        decision = decide_swap(state, config, usage_by_account)

        # Persist usage updates BEFORE acting on swap (so a crash during
        # swap leaves valid usage state)
        save_state(state)

        if decision is None:
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

        if no_execute:
            click.echo("    (--no-execute) skipping actual swap")
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
            time.sleep(interval)
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
    "subagent_skip": "Skip-guard for sessions with in-flight subagents/tool calls. `defer_below_tier: 3` means Tier 1/2 defer when subagent active; Tier 3 (force) proceeds regardless.",
    "reactive": "When `enabled: true`, the PostToolUseFailure hook detects 429s in tool error bodies and triggers immediate swap (no waiting for next poll).",
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

    Shows "?" rather than the buggy "0.0" sentinel (or stale numbers) per the
    2026-05-20 user feedback: 0% implied "no usage" which was equally
    misleading as 100% from the prior sentinel bug.
    """
    return bool(acct.get("rate_limited") or acct.get("token_expired") or acct.get("poll_error"))


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


@cli.command(name="statusline")
@click.option("--verbose", "-v", is_flag=True, help="Verbose output: reset times + poll age + other accounts.")
@click.option("--compact", "-c", is_flag=True, help="Force compact output (overrides config).")
def statusline_cmd(verbose: bool, compact: bool) -> None:
    """One-line summary for Claude Code statusline integration.

    Compact (default): `cus:<account> 5h:NN% 7d:NN% nxt:NN%` or `🚨 SOS: ...`.
    Verbose: `cus:<active> 5h NN%·1h23m 7d NN%·3d | <other> 5h NN% 7d NN% | poll Nago`.

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

    is_verbose = not compact and (verbose or sl_cfg.get("verbose", False))

    # SOS takes priority — if human action is needed, surface it loudly
    conditions = diagnose(state, config)
    urgent = [c for c in conditions if c.severity == "urgent"]
    if urgent:
        click.echo(f"🚨 cus SOS: {urgent[0].summary} — see ~/claude-accounts/SOS.md")
        return
    warning = [c for c in conditions if c.severity == "warning"]
    if warning:
        click.echo(f"⚠ cus: {warning[0].summary}")
        return

    # Determine THIS session's account (the one this pane's claude actually
    # loaded tokens for) vs machine-wide active. Tokens get loaded once at
    # claude start; if a swap happens after, the pane's claude still uses
    # its loaded creds until /exit + restart. Showing only machine-wide
    # active is misleading for that pane.
    machine_active = state.get("active", "?")
    this_session_id = os.environ.get("CLAUDE_CODE_SESSION_ID")
    this_session_account = None
    if this_session_id:
        for e in reversed(_parse_sessions_log()):
            if e.get("session_id") == this_session_id:
                this_session_account = e.get("account")
                break
    # Display PRIMARY = this session's account if known, else machine-active.
    active = this_session_account or machine_active
    pending_swap = this_session_account and this_session_account != machine_active

    acct = state.get("accounts", {}).get(active, {})
    fh = acct.get("current_5h_pct", 0)
    sd = acct.get("current_7d_pct", 0)
    nx = acct.get("next_swap_at_pct", 50)

    if not is_verbose:
        # Compact mode. Render "?" if we don't have reliable data.
        if _pct_is_unknown(acct):
            click.echo(f"cus:{active} 5h:? 7d:? nxt:{nx}%")
            return
        flag_marker = ""
        if max(fh, sd) >= nx:
            flag_marker = "⚠"
        elif max(fh, sd) >= nx * 0.8:
            flag_marker = "·"
        prefix_bits = [f"cus:{active}"]
        if pending_swap:
            prefix_bits.append(f"→{machine_active}")
        if flag_marker:
            prefix_bits.append(flag_marker)
        prefix = " ".join(prefix_bits)
        click.echo(f"{prefix} 5h:{fh:.0f}% 7d:{sd:.0f}% nxt:{nx}%")
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
        unknown = _pct_is_unknown(a)
        flags = []
        if a.get("token_expired"):
            flags.append("EXP")
        if a.get("rate_limited"):
            flags.append("429")
        flag_str = "(" + ",".join(flags) + ")" if flags else ""
        marker = "*" if is_active else ""
        parts = [f"{name}{marker}"]
        pct5 = "?" if unknown else f"{h5:.0f}%"
        pct7 = "?" if unknown else f"{h7:.0f}%"
        if show_resets:
            r5 = _time_until(a.get("five_hour_resets_at"))
            r7 = _time_until(a.get("seven_day_resets_at"))
            r5_str = f"·{_fmt_duration(r5)}" if r5 is not None and r5 > 0 and not unknown else ""
            r7_str = f"·{_fmt_duration(r7)}" if r7 is not None and r7 > 0 and not unknown else ""
            parts.append(f"5h:{pct5}{r5_str}")
            parts.append(f"7d:{pct7}{r7_str}")
        else:
            parts.append(f"5h:{pct5}")
            parts.append(f"7d:{pct7}")
        if flag_str:
            parts.append(flag_str)
        return " ".join(parts)

    pieces = [fmt_account(active, acct, True)]

    if show_others:
        for name, a in sorted(state.get("accounts", {}).items()):
            if name == active:
                continue
            pieces.append(fmt_account(name, a, False))

    if show_poll:
        last_poll = acct.get("last_poll_ts")
        age = _time_since(last_poll)
        if age is not None and age >= 0:
            pieces.append(f"poll {_fmt_duration(age)}ago")

    click.echo(pieces_prefix + " | ".join(pieces))


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

    After login, run `cus poll` to register the new account in state.json.
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
