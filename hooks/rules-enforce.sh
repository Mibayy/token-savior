#!/bin/bash
# PreToolUse — hard-rule enforcement. Emits permissionDecision:deny for a
# matching hard rule (force-push to main, blanket DELETE, push without
# preflight). FAILS OPEN: any error → allow. Kill-switch: TS_RULES_DISABLE=1.
# --- resolution des chemins (voir scripts/deroot_hooks.py) ---
# Aucun chemin en dur : ces scripts sont livres dans la roue PyPI et doivent
# fonctionner sur la machine de l'utilisateur, pas sur celle de l'auteur.
TS_DATA="${TOKEN_SAVIOR_DATA_DIR:-${XDG_DATA_HOME:-$HOME/.local/share}/token-savior}"
TS_BACKUP="${TOKEN_SAVIOR_BACKUP_DIR:-$TS_DATA/memory-backup}"
# Interpreteur : celui qui sait importer token_savior. Un venv dedie l'emporte
# s'il est declare, sinon on prend le python du PATH.
if [ -n "${TOKEN_SAVIOR_PYTHON:-}" ]; then
  TS_PY="$TOKEN_SAVIOR_PYTHON"
elif command -v python3 >/dev/null 2>&1 && python3 -c "import token_savior" 2>/dev/null; then
  TS_PY="$(command -v python3)"
else
  TS_PY="${TS_PY:-python3}"
fi
# Checkout source : utile en developpement seulement. Apres `pip install`,
# token_savior est importable sans rien ajouter a sys.path.
TS_SRC="${TOKEN_SAVIOR_SRC:-}"
TS_SCRIPTS="${TOKEN_SAVIOR_SCRIPTS:-$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")/.." 2>/dev/null && pwd)/scripts}"
# --- fin --- resolution des chemins (voir scripts/deroot_hooks.py) --- fin ---

if [ "$TS_RULES_DISABLE" = "1" ]; then
    exit 0
fi
ERR_LOG="${XDG_STATE_HOME:-$HOME/.local/state}/token-savior/hook-errors.log"
mkdir -p "$(dirname "$ERR_LOG")" 2>/dev/null || true
# stdin (the PreToolUse payload) flows into the entrypoint; its stdout (the
# deny JSON, or nothing) flows straight back to Claude Code; stderr → log.
$TS_PY -m token_savior.memory.rules_hook 2>>"$ERR_LOG"
exit 0
