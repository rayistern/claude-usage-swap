#!/usr/bin/env bash
# cus PostToolUseFailure hook — SECONDARY, best-effort 429 detector.
#
# A2 (2026-06-23): the PRIMARY, reliable detector is now cus_stop_failure.sh.
# Model-level Anthropic 429s fire StopFailure, NEVER PostToolUseFailure (which
# only fires for tool-execution failures). This hook is kept solely as a
# best-effort catch for an Anthropic API error that leaks into a SUBAGENT's
# (Agent/Task) tool-failure body — undocumented territory, so treat as a bonus.
#
# Hardened vs the original loose match, which caused the 2026-06-23 false-swap
# incident (it matched "rate limit"/"usage limit"/"ratelimit" anywhere in the
# event — downstream Bash output, or a file that merely mentions rate limits):
#   - matches ONLY the precise Anthropic API wire tokens `rate_limit_error` /
#     `overloaded_error` (the error-body `error.type` values), never prose;
#   - ignores matches that live ONLY inside `tool_input` (so reading / grepping /
#     editing files containing those tokens — e.g. API-client code, this repo —
#     does not trip);
#   - records the failing tool_name in 429.log for forensics.
#
# Appends to ~/claude-accounts/429.log:
#   <ts>,<session_id>,<token>,<tool_name>,<event_slot>,<event_account>
#
# Walk-back: set hooks.install_post_tool_use_failure: false (StopFailure remains
# the real detector) or remove the entry from ~/.claude/settings.json.

set -euo pipefail

ACCOUNTS_DIR="${CUS_ACCOUNTS_DIR:-$HOME/claude-accounts}"
LOG="$ACCOUNTS_DIR/429.log"

EVENT=$(cat || true)

# Fast path: if neither precise token appears anywhere, there is nothing to do.
# Avoids spawning python on the overwhelming majority of tool failures.
if ! echo "$EVENT" | grep -qE 'rate_limit_error|overloaded_error'; then
    exit 0
fi

# A precise token is present somewhere. Confirm via JSON parse that it is NOT
# only inside tool_input (which would be the session reading/editing such text,
# not an actual API failure). Python runs only in this rare token-present case.
RESULT=$(echo "$EVENT" | python3 -c '
import json, sys
try:
    e = json.load(sys.stdin)
except Exception:
    sys.exit(0)
TOKENS = ("rate_limit_error", "overloaded_error")
# Search every field EXCEPT tool_input (the session-controlled payload).
surface = {k: v for k, v in e.items() if k != "tool_input"}
blob = json.dumps(surface)
hit = next((t for t in TOKENS if t in blob), None)
if not hit:
    sys.exit(0)
sid = e.get("session_id", "unknown")
tool = e.get("tool_name", "unknown")
print(f"{sid}\t{tool}\t{hit}")
' || true)

if [[ -z "$RESULT" ]]; then
    exit 0
fi

SESSION_ID=$(printf '%s' "$RESULT" | cut -f1)
TOOL=$(printf '%s' "$RESULT" | cut -f2)
TOKEN=$(printf '%s' "$RESULT" | cut -f3)
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
echo "$TS,$SESSION_ID,$TOKEN,$TOOL,$SLOT,$ACCOUNT" >> "$LOG"

exit 0
