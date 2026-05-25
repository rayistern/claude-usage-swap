#!/usr/bin/env bash
# cus PostToolUse hook — close the per-session tool-use ledger (GH #27).
#
# Mirror of cus_pre_tool_use.sh, writing "stop" instead of "start". Without
# this hook, every successful tool completion left its "start" line in
# tool_use.log dangling — only failures (PostToolUseFailure) and subagent
# ends (SubagentStop) closed entries. Result: 5106 start vs 167 stop rows
# in a live log, so `subagent_active`'s `net = starts - stops > 0` heuristic
# misfired on any session that had run a normal tool in the last 60s,
# blocking swaps indefinitely.
#
# Appends one line per successful tool completion to
# ~/claude-accounts/tool_use.log:
#   <ts>,<session_id>,<tool_name>,stop
#
# The PostToolUseFailure hook ALREADY writes its own "stop" line via the
# 429.log code path, but it does NOT touch tool_use.log; this hook does,
# so the ledger closes regardless of success/failure.
#
# Walk-back: removing the hook re-introduces the leak. The orchestrator
# tier-3-always-bypass (GH #27) is the safety net if this hook is absent;
# disable only if you understand that mid-turn tool calls will look like
# in-flight subagents to the skip-guard at tier 1/2.

set -euo pipefail

ACCOUNTS_DIR="${CUS_ACCOUNTS_DIR:-$HOME/claude-accounts}"
LOG="$ACCOUNTS_DIR/tool_use.log"

EVENT=$(cat || true)
SESSION_ID=$(echo "$EVENT" | grep -o '"session_id"[[:space:]]*:[[:space:]]*"[^"]*"' | sed 's/.*"\([^"]*\)"$/\1/' || echo "unknown")
TOOL=$(echo "$EVENT" | grep -o '"tool_name"[[:space:]]*:[[:space:]]*"[^"]*"' | sed 's/.*"\([^"]*\)"$/\1/' || echo "unknown")

TS=$(date -u +"%Y-%m-%dT%H:%M:%SZ")
mkdir -p "$ACCOUNTS_DIR"
echo "$TS,$SESSION_ID,$TOOL,stop" >> "$LOG"

exit 0
