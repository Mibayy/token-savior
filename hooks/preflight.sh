#!/bin/bash
# PreToolUse — pre-flight self-verification reflex. Injects a short checklist
# before an irreversible/high-consequence action and logs a `preflight` event.
# NON-BLOCKING (hard denies live in rules-enforce.sh). Fails open.
# Kill-switch: TS_RULES_DISABLE=1.
if [ "$TS_RULES_DISABLE" = "1" ]; then
    exit 0
fi
ERR_LOG="${XDG_STATE_HOME:-$HOME/.local/state}/token-savior/hook-errors.log"
mkdir -p "$(dirname "$ERR_LOG")" 2>/dev/null || true
/root/.local/token-savior-venv/bin/python3 -m token_savior.memory.preflight_hook 2>>"$ERR_LOG"
exit 0
