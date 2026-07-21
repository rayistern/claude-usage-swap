#!/usr/bin/env bash
# cus SessionStart hook — log new sessions for visibility + tmux pane registry.
#
# Reads the SessionStart hook JSON event from stdin (per Claude Code hooks spec):
#   {"session_id": "...", "transcript_path": "...", "cwd": "...", ...}
# Writes one line per session to ~/claude-accounts/sessions.log:
#   <ts>,<session_id>,<account>,<tmux_pane>,<tmux_socket>,<cwd>
#
# Account is whichever is active in state.json at the moment this fires.
# TMUX_PANE comes from the env (set by tmux if running under it).
# TMUX_SOCKET is the server socket path (${TMUX%%,*}) so pane ids — which are
# only unique WITHIN one tmux server — can be disambiguated across servers;
# "no-tmux" when not running under tmux. cwd stays LAST so it may contain commas.
#
# Walk-back: this hook only writes to a log file. Removing the hook entry
# from ~/.claude/settings.json reverts visibility. The log itself is
# safe to delete.

set -euo pipefail

ACCOUNTS_DIR="${CUS_ACCOUNTS_DIR:-$HOME/claude-accounts}"
LOG="$ACCOUNTS_DIR/sessions.log"
STATE="$ACCOUNTS_DIR/state.json"

# Read the JSON event from stdin (Claude Code passes hooks data this way)
EVENT=$(cat || true)

# Extract session_id and cwd via grep+sed (avoid jq dep)
SESSION_ID=$(echo "$EVENT" | grep -o '"session_id"[[:space:]]*:[[:space:]]*"[^"]*"' | sed 's/.*"\([^"]*\)"$/\1/' || echo "unknown")
CWD=$(echo "$EVENT" | grep -o '"cwd"[[:space:]]*:[[:space:]]*"[^"]*"' | sed 's/.*"\([^"]*\)"$/\1/' || echo "unknown")

# Account resolution (per_session Phase 2.3): a slot-launched session inherits
# CLAUDE_CONFIG_DIR from `cus launch` — resolve the slot's occupant from
# state.json (python3 for the nested lookup; cus itself requires python3, and
# grep can't index into slots.<name>.account reliably). An account-dir launch
# (relogin flow) IS the account. Bare launches fall back to the global active,
# exactly the pre-per_session behavior.
ACCOUNT="unknown"
if [[ -n "${CLAUDE_CONFIG_DIR:-}" ]]; then
    BASE=$(basename "$CLAUDE_CONFIG_DIR")
    case "$BASE" in
        slot-*)
            if [[ -f "$STATE" ]]; then
                ACCOUNT=$(python3 -c "import json,sys; s=json.load(open(sys.argv[1])); print(s.get('slots',{}).get(sys.argv[2],{}).get('account') or 'unknown')" "$STATE" "$BASE" 2>/dev/null || echo "unknown")
            fi
            ;;
        account-*)
            ACCOUNT="${BASE#account-}"
            ;;
    esac
fi
if [[ "$ACCOUNT" == "unknown" && -f "$STATE" ]]; then
    ACCOUNT=$(grep -o '"active"[[:space:]]*:[[:space:]]*"[^"]*"' "$STATE" | sed 's/.*"\([^"]*\)"$/\1/' || echo "unknown")
fi

TS=$(date -u +"%Y-%m-%dT%H:%M:%SZ")
PANE="${TMUX_PANE:-no-tmux}"
# $TMUX is "<socket-path>,<server-pid>,<session-id>"; the socket path is the
# leading comma-delimited field. Unset ⇒ not under tmux ⇒ "no-tmux".
TMUX_SOCKET="no-tmux"
if [[ -n "${TMUX:-}" ]]; then
    TMUX_SOCKET="${TMUX%%,*}"
fi

mkdir -p "$ACCOUNTS_DIR"
echo "$TS,$SESSION_ID,$ACCOUNT,$PANE,$TMUX_SOCKET,$CWD" >> "$LOG"

# Hooks should not block the session — exit 0 always.
exit 0
