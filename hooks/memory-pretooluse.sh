#!/bin/bash
# Memory Engine — PreToolUse hook
# Injecte l'historique mémoire pour :
#   - les tools Token Savior de lecture de code (par symbole/fichier)
#   - les commandes Bash significatives (par keyword extrait de la commande)


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
PAYLOAD=$(cat)

TOOL=$(echo "$PAYLOAD" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('tool_name',''))" 2>>"$ERR_LOG")

# Strip the mcp__<server>__ prefix so we can match plain names.
SHORT_TOOL="${TOOL##*__}"

CODE_TOOLS_RE='^(get_function_source|get_class_source|get_edit_context|find_symbol|get_file_dependencies)$'
EDIT_TOOLS_RE='^(replace_symbol_source|insert_near_symbol|apply_symbol_change_and_validate|apply_symbol_change_validate_with_rollback)$'
READ_TOOLS_RE='^(Read|View|NotebookRead)$'

if [[ "$SHORT_TOOL" =~ $CODE_TOOLS_RE ]]; then
    MODE=code
elif [[ "$SHORT_TOOL" =~ $EDIT_TOOLS_RE ]] || [[ "$TOOL" == "Edit" ]] || [[ "$TOOL" == "Write" ]] || [[ "$TOOL" == "MultiEdit" ]]; then
    MODE=edit
elif [[ "$TOOL" =~ $READ_TOOLS_RE ]]; then
    MODE=read
elif [[ "$TOOL" == "Bash" ]]; then
    MODE=bash
else
    exit 0
fi

RESULT=$(/root/.local/token-savior-venv/bin/python3 -c "
import sys, json, re
sys.path.insert(0, '/root/token-savior/src')
from token_savior import memory_db

payload = json.loads('''$PAYLOAD''')
args = payload.get('tool_input', {})
mode = '$MODE'

db = memory_db.get_db()
row = db.execute(
    'SELECT project_root FROM observations GROUP BY project_root ORDER BY COUNT(*) DESC LIMIT 1'
).fetchone()
db.close()
if not row:
    sys.exit(0)
project = row[0]

if mode == 'code':
    symbol = args.get('name') or args.get('symbol_name', '')
    file_path = args.get('file_path', '')
    if not (symbol or file_path):
        sys.exit(0)
    obs = memory_db.observation_get_by_symbol(
        project, symbol, file_path=file_path or None, limit=5
    )
    if obs:
        print(f'📌 Memory for {symbol or file_path}:')
        for o in obs:
            age = o.get('age') or (o.get('created_at') or '')[:10]
            stale = '⚠️ ' if o.get('stale') else ''
            glob = '🌐 ' if o.get('is_global') else ''
            print(f\"  #{o['id']}  [{o['type']}]  {stale}{glob}{o['title']}  —  {age}\")
    sys.exit(0)

if mode == 'read':
    # P4: inject compact file-context header when obs exist for the target file.
    # Nothing is printed when no obs are tied to the file (silent no-op).
    file_path = (args.get('file_path') or '').strip()
    if not file_path:
        sys.exit(0)
    obs = memory_db.observation_get_by_file(project, file_path, limit=5)
    if not obs:
        sys.exit(0)
    import os as _os
    display = _os.path.basename(file_path) or file_path
    print(f'[Memory: {len(obs)} obs on {display}]')
    for o in obs:
        glob = '🌐 ' if o.get('is_global') else ''
        imp = o.get('importance') or 0
        print(f\"  \u2022 [{o['type']}] {glob}{o['title']} (imp {imp}) [ts://obs/{o['id']}]\")
    sys.exit(0)

if mode == 'edit':
    # Surface ruled_out negative memory aggressively before any mutation.
    symbol = args.get('symbol_name') or args.get('name', '')
    file_path = args.get('file_path', '')
    target = symbol or file_path
    try:
        conn = memory_db.get_db()
        if target:
            tgt_like = f'%{target}%'
            rows = conn.execute(
                \"SELECT id, title, content, why, symbol, file_path, created_at_epoch \"
                \"FROM observations \"
                \"WHERE archived=0 AND type='ruled_out' \"
                \"  AND (project_root=? OR is_global=1) \"
                \"  AND (symbol LIKE ? OR file_path LIKE ? OR title LIKE ? OR content LIKE ?) \"
                \"ORDER BY created_at_epoch DESC LIMIT 5\",
                (project, tgt_like, tgt_like, tgt_like, tgt_like),
            ).fetchall()
        else:
            rows = conn.execute(
                \"SELECT id, title, content, why, symbol, file_path, created_at_epoch \"
                \"FROM observations \"
                \"WHERE archived=0 AND type='ruled_out' \"
                \"  AND (project_root=? OR is_global=1) \"
                \"ORDER BY created_at_epoch DESC LIMIT 5\",
                (project,),
            ).fetchall()
        conn.close()
    except Exception:
        sys.exit(0)
    if rows:
        label = target or 'this edit'
        print(f'🚫 Ruled-out memory for {label}:')
        for r in rows:
            d = dict(r)
            age = memory_db.relative_age(d.get('created_at_epoch'))
            why = f\" — {d['why']}\" if d.get('why') else ''
            print(f\"  #{d['id']}  {d['title']}{why}  {age}\")
    sys.exit(0)

# mode == 'bash'
command = args.get('command') or ''
if not command:
    sys.exit(0)

PATTERNS = [
    (r'systemctl\s+\w+\s+(\S+)',                       lambda m: m.group(1)),
    (r'journalctl\s+(?:-[^ ]+\s+)*(?:-u\s+)?(\S+)',    lambda m: m.group(1)),
    (r'docker\s+(?:\w+\s+)?(\S+)',                     lambda m: m.group(1)),
    (r'(nginx|caddy|hermes|sirius|claude-telegram|vps-monitor|eclatauto)', lambda m: m.group(1)),
    (r'python3?\s+([\w/.-]+\.py)',                     lambda m: m.group(1)),
    (r'(?:pip|npm|apt|pnpm)\s+install\s+(\S+)',        lambda m: m.group(1)),
]

keyword = None
for pat, extract in PATTERNS:
    m = re.search(pat, command)
    if m:
        keyword = extract(m)
        break

if not keyword:
    sys.exit(0)

keyword = keyword.strip().strip('.service').strip('/')
if len(keyword) < 3:
    sys.exit(0)

# Search obs matching keyword across command/infra/config/guardrail/warning
ctx_like = f'%{keyword}%'
try:
    conn = memory_db.get_db()
    rows = conn.execute(
        'SELECT id, type, title, context, created_at, created_at_epoch, is_global '
        'FROM observations '
        'WHERE archived=0 AND (project_root=? OR is_global=1) '
        '  AND type IN (\\'command\\',\\'infra\\',\\'config\\',\\'guardrail\\',\\'warning\\') '
        '  AND (title LIKE ? OR context LIKE ? OR content LIKE ?) '
        'ORDER BY created_at_epoch DESC LIMIT 3',
        (project, ctx_like, ctx_like, ctx_like),
    ).fetchall()
    conn.close()
except Exception:
    sys.exit(0)

if not rows:
    sys.exit(0)

print(f'📌 Memory for \`{keyword}\`:')
for r in rows:
    d = dict(r)
    age = memory_db.relative_age(d.get('created_at_epoch'))
    ctx = f\" · {d['context']}\" if d.get('context') else ''
    glob = '🌐 ' if d.get('is_global') else ''
    print(f\"  #{d['id']}  [{d['type']}]  {glob}{d['title']}{ctx}  {age}\")
" 2>>"$ERR_LOG")

if [ -n "$RESULT" ]; then
    echo "$RESULT"
fi
exit 0
