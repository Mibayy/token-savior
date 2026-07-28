#!/bin/bash
# Memory Engine — PreCompact hook
# Injects recent summaries + memory index before Claude Code compacts the conversation.
# Goal: prevent loss of memory context when the conversation gets compacted.
#
# TS_MEMORY_DISABLE=1 -> short-circuit (tsbench / clean ctx workloads).
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

if [ "$TS_MEMORY_DISABLE" = "1" ]; then
    exit 0
fi

# -- token-savior hook error log (see GitHub #15) ---------------------------
# Re-routes stderr from Python / claude sub-shells so a broken import, a
# missing venv, or a corrupt DB surfaces somewhere instead of vanishing.
# Rotates at 2 MB (keeps tail 1 MB) so it never fills the disk.
ERR_LOG="${XDG_STATE_HOME:-$HOME/.local/state}/token-savior/hook-errors.log"
mkdir -p "$(dirname "$ERR_LOG")" 2>/dev/null || true
if [ -f "$ERR_LOG" ] && [ "$(stat -c%s "$ERR_LOG" 2>/dev/null || echo 0)" -gt 2000000 ]; then
    tail -c 1000000 "$ERR_LOG" > "$ERR_LOG.tmp" 2>/dev/null && mv "$ERR_LOG.tmp" "$ERR_LOG"
fi
# -- end token-savior hook error log -----------------------------------------
# PreCompact event payload (stdin). Claude Code pipes the event JSON here; we
# keep it to extract transcript_path for superseded-context detection (B).
_TS_PAYLOAD=$(cat)
RESULT=$($TS_PY -c "
import sys, os
sys.path.insert(0, '$TS_SRC')
from token_savior import memory_db

db = memory_db.get_db()
row = db.execute(
    'SELECT project_root FROM observations GROUP BY project_root ORDER BY COUNT(*) DESC LIMIT 1'
).fetchone()
db.close()

if not row:
    sys.exit(0)

project = row[0]

db = memory_db.get_db()
summaries = db.execute(
    '''SELECT content, created_at FROM summaries
       WHERE project_root=?
       ORDER BY created_at_epoch DESC LIMIT 3''',
    [project]
).fetchall()
db.close()

recent = memory_db.get_recent_index(project, limit=10)

mode = memory_db.get_current_mode()
mode_name = mode.get('name', 'code')

print('## Memory Context (pre-compaction)')
print(f'Mode: {mode_name} | Project: {project}')
print()

# --- Session budget (Step B) — auto-inject when pct_used > 75% -----------
try:
    bstats = memory_db.get_session_budget_stats(project)
    if bstats.get('pct_used', 0) > 75:
        print('### Session Budget (auto-injected: > 75% used)')
        print('\`\`\`')
        print(memory_db.format_session_budget_box(bstats))
        print('\`\`\`')
        print()
except Exception:
    pass

if summaries:
    print('### Recent Summaries')
    for s in summaries:
        print(f'**{s[1][:10]}**')
        print(s[0])
        print()

if recent:
    print('### Memory Index')
    for r in recent:
        day = r.get('day') or (r.get('created_at') or '')[:10]
        print(f\"  #{r['id']}  [{r['type']}]  {r['title']}  {day}\")
" 2>>"$ERR_LOG")

if [ -n "$RESULT" ]; then
    echo "$RESULT"
fi

# --- Superseded context (feature B, Claude Code only) ----------------------
# Flag tool results a later action already invalidated (re-read, read-then-edit,
# re-run) so compaction can drop them. Read-only, best-effort — never blocks.
STALE=$(printf '%s' "$_TS_PAYLOAD" | $TS_PY -c "
import sys, json
sys.path.insert(0, '$TS_SRC')
try:
    tp = (json.load(sys.stdin) or {}).get('transcript_path', '') or ''
except Exception:
    tp = ''
if tp:
    from token_savior.discover.superseded import precompact_report
    out = precompact_report(tp)
    if out:
        print(out)
" 2>>"$ERR_LOG")
if [ -n "$STALE" ]; then
    echo "$STALE"
fi
exit 0
