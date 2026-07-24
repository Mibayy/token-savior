#!/bin/bash
# PreToolUse — hard-rule enforcement. Emits permissionDecision:deny for a
# matching hard rule (force-push to main, blanket DELETE, push without
# preflight). FAILS OPEN: any error → allow. Kill-switch: TS_RULES_DISABLE=1.
if [ "$TS_RULES_DISABLE" = "1" ]; then
    exit 0
fi
ERR_LOG="${XDG_STATE_HOME:-$HOME/.local/state}/token-savior/hook-errors.log"
mkdir -p "$(dirname "$ERR_LOG")" 2>/dev/null || true
# stdin (the PreToolUse payload) flows into the entrypoint; its stdout (the
# deny JSON, or nothing) flows straight back to Claude Code; stderr → log.
/root/.local/token-savior-venv/bin/python3 -m token_savior.memory.rules_hook 2>>"$ERR_LOG"
exit 0
