#!/bin/bash
# PreToolUse — pre-flight self-verification reflex. Injects a short checklist
# before an irreversible/high-consequence action and logs a `preflight` event.
# NON-BLOCKING (hard denies live in rules-enforce.sh). Fails open.
# Kill-switch: TS_RULES_DISABLE=1.
# --- resolution des chemins (voir scripts/deroot_hooks.py) ---
# Aucun chemin en dur : ces scripts sont livres dans la roue PyPI et doivent
# fonctionner sur la machine de l'utilisateur, pas sur celle de l'auteur.
TS_DATA="${TOKEN_SAVIOR_DATA_DIR:-${XDG_DATA_HOME:-$HOME/.local/share}/token-savior}"
# shellcheck disable=SC2034  # preambule partage : chaque hook n en consomme qu un sous-ensemble
TS_BACKUP="${TOKEN_SAVIOR_BACKUP_DIR:-$TS_DATA/memory-backup}"
# Interpreteur : celui qui sait importer token_savior. Un venv dedie l'emporte
# s'il est declare, sinon on prend le python du PATH.
# La sonde `python3 -c "import token_savior"` demarre un interpreteur complet,
# mesure a ~127 ms sur ce VPS, et elle etait payee a CHAQUE appel de hook, donc
# a chaque outil utilise par l'agent. Le resultat est desormais memorise, et
# invalide des que le binaire python change (chemin + mtime + taille).
if [ -n "${TOKEN_SAVIOR_PYTHON:-}" ]; then
  TS_PY="$TOKEN_SAVIOR_PYTHON"
else
  _ts_py_bin="$(command -v python3 2>/dev/null)"
  if [ -z "$_ts_py_bin" ]; then
    TS_PY="${TS_PY:-python3}"
  else
    _ts_cache="${XDG_CACHE_HOME:-$HOME/.cache}/token-savior/interpreteur"
    _ts_sig="$_ts_py_bin:$(stat -c '%Y:%s' "$_ts_py_bin" 2>/dev/null || echo 0)"
    if [ -r "$_ts_cache" ] && IFS='|' read -r _c_sig _c_py < "$_ts_cache" 2>/dev/null \
       && [ "$_c_sig" = "$_ts_sig" ] && [ -n "$_c_py" ]; then
      TS_PY="$_c_py"
    else
      if "$_ts_py_bin" -c "import token_savior" 2>/dev/null; then
        TS_PY="$_ts_py_bin"
      else
        TS_PY="${TS_PY:-python3}"
      fi
      mkdir -p "$(dirname "$_ts_cache")" 2>/dev/null \
        && printf '%s|%s\n' "$_ts_sig" "$TS_PY" > "$_ts_cache" 2>/dev/null || true
    fi
    unset _ts_py_bin _ts_cache _ts_sig _c_sig _c_py
  fi
fi
# Checkout source : utile en developpement seulement. Apres `pip install`,
# token_savior est importable sans rien ajouter a sys.path.
# shellcheck disable=SC2034  # preambule partage : chaque hook n en consomme qu un sous-ensemble
TS_SRC="${TOKEN_SAVIOR_SRC:-}"
# shellcheck disable=SC2034  # preambule partage : chaque hook n en consomme qu un sous-ensemble
TS_SCRIPTS="${TOKEN_SAVIOR_SCRIPTS:-$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")/.." 2>/dev/null && pwd)/scripts}"
# --- fin --- resolution des chemins (voir scripts/deroot_hooks.py) --- fin ---

if [ "$TS_RULES_DISABLE" = "1" ]; then
    exit 0
fi
ERR_LOG="${XDG_STATE_HOME:-$HOME/.local/state}/token-savior/hook-errors.log"
mkdir -p "$(dirname "$ERR_LOG")" 2>/dev/null || true
$TS_PY -m token_savior.memory.preflight_hook 2>>"$ERR_LOG"
exit 0
