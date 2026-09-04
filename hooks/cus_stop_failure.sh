#!/usr/bin/env bash
# cus StopFailure hook — the RELIABLE Anthropic rate-limit (429) detector.
#
# A model-level rate limit ends the turn via StopFailure (NOT Stop, and NOT
# PostToolUseFailure), carrying a documented, categorized `error` enum:
#   rate_limit | overloaded | authentication_failed | billing_error | ...
# Source: https://code.claude.com/docs/en/hooks.md  (StopFailure section)
#
# This supersedes cus_post_tool_use_failure.sh as the primary detector. That
# hook only ever saw TOOL-execution failures, so it never observed a genuine
# model-level 429 — it just substring-matched "rate limit" in arbitrary tool
# output, which manufactured the 2026-06-23 false-swap incident.
#
# We log ONLY the budget-relevant categories (rate_limit, overloaded). We match
# the top-level `error` enum EXACTLY, so prose in error_details /
# last_assistant_message ("...rate limit...") can't false-trigger.
#
# Appends one line per detection to ~/claude-accounts/429.log:
#   <ts>,<session_id>,<error_enum>,stopfailure,<event_slot>,<event_account>
#
# Event-time binding prevents a delayed hook record from being applied to a
# different account that was installed into the same slot before the daemon woke.
#
# Walk-back: set hooks.install_stop_failure: false (or remove the StopFailure
# entry from ~/.claude/settings.json). The daemon still swaps proactively on
# poll-threshold crossings — just without the reactive backstop.

set -euo pipefail

ACCOUNTS_DIR="${CUS_ACCOUNTS_DIR:-$HOME/claude-accounts}"
LOG="$ACCOUNTS_DIR/429.log"

EVENT=$(cat || true)

# Match the precise JSON shape of the top-level `error` field for the two
# budget-relevant categories only. `"error"` + colon won't match inside
# `"error_details"` (no closing quote before the underscore), so we read the
# enum, not surrounding prose.
ERR=$(echo "$EVENT" \
    | grep -oE '"error"[[:space:]]*:[[:space:]]*"(rate_limit|overloaded)"' \
    | grep -oE '(rate_limit|overloaded)' \
    | head -1 || true)

if [[ -z "$ERR" ]]; then
    exit 0
fi

SESSION_ID=$(echo "$EVENT" | grep -o '"session_id"[[:space:]]*:[[:space:]]*"[^"]*"' | sed 's/.*"\([^"]*\)"$/\1/' || echo "unknown")
TS=$(date -u +"%Y-%m-%dT%H:%M:%SZ")

BINDING=$(ACCOUNTS_DIR="$ACCOUNTS_DIR" python3 -c '
import json, os
from pathlib import Path
cfg = os.environ.get("CLAUDE_CONFIG_DIR", "").rstrip("/")
slot = Path(cfg).name if cfg and Path(cfg).name.startswith("slot-") else ""
try:
    state = json.loads((Path(os.environ["ACCOUNTS_DIR"]) / "state.json").read_text())
except Exception:
    state = {}
account = (state.get("slots", {}).get(slot, {}).get("account", "")
           if slot else state.get("active", ""))
print(f"{slot}\t{account}")
' 2>/dev/null || true)
SLOT=${BINDING%%$'\t'*}
ACCOUNT=${BINDING#*$'\t'}

mkdir -p "$ACCOUNTS_DIR"
echo "$TS,$SESSION_ID,$ERR,stopfailure,$SLOT,$ACCOUNT" >> "$LOG"

exit 0
