#!/bin/bash
# Memory Engine — UserPromptSubmit hook
# - synchronous: inject top-3 relevant observations into context (stdout)
# - background: strip private tags, trigger phrases, archive prompt

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

# --- P1: strip <private>…</private> BEFORE injection or any derived obs save
_REDACTED=$(printf '%s' "$PAYLOAD" | /root/.local/token-savior-venv/bin/python3 -c "
import sys, json, re
try:
    p = json.loads(sys.stdin.read())
    if isinstance(p.get('prompt'), str):
        p['prompt'] = re.sub(r'<private>[\s\S]*?</private>', '[redacted]', p['prompt'])
    sys.stdout.write(json.dumps(p))
except Exception:
    pass
" 2>>"$ERR_LOG")
if [ -n "$_REDACTED" ]; then
  PAYLOAD="$_REDACTED"
fi

# --- Synchronous injection (must complete before Claude responds) ---------
/root/.local/token-savior-venv/bin/python3 -c "
import sys, json, re
sys.path.insert(0, '/root/token-savior/src')
from token_savior import memory_db

try:
    payload = json.loads('''$PAYLOAD''')
    text = (payload.get('prompt') or '').strip()
    if len(text) < 20:
        sys.exit(0)

    # Skip if prompt is itself a trigger phrase — user is recording, not asking
    TRIGGER_STARTS = (
        'rappelle-toi', 'rappelle toi', 'note que', 'à retenir',
        'règle', 'regle', 'ne jamais', 'toujours',
        'remember that', 'note that', 'important', 'rule:', 'never ', 'always ',
        'ruled out', 'ruled-out', 'ruled_out',
        'ne pas ', 'do not ',
        'essayé ', 'essaye ', 'tried ', 'tested ',
        'on a déjà', 'on a deja', 'already tried',
    )
    low = text.lower()
    if any(low.startswith(s) for s in TRIGGER_STARTS):
        sys.exit(0)

    db = memory_db.get_db()
    row = db.execute(
        'SELECT project_root FROM observations GROUP BY project_root ORDER BY COUNT(*) DESC LIMIT 1'
    ).fetchone()
    db.close()
    if not row:
        sys.exit(0)

    # Build FTS5-safe query: alphanumeric tokens >=3 chars, OR-joined, quoted
    tokens = re.findall(r'[A-Za-zÀ-ÿ0-9_]{3,}', text[:300])
    stop = {'que','qui','les','des','une','aux','pour','avec','dans','sur','par','est','sont','the','and','for','with','this','that','you','are','how','what','can','will','from'}
    tokens = [t for t in tokens if t.lower() not in stop][:12]
    if not tokens:
        sys.exit(0)
    query = ' OR '.join(f'\"{t}\"' for t in tokens)

    results = memory_db.observation_search(project_root=row[0], query=query, limit=10)
    if not results:
        sys.exit(0)

    priority_types = ('guardrail', 'convention', 'warning')
    priority = [r for r in results if r.get('type') in priority_types]
    others = [r for r in results if r.get('type') not in priority_types]
    top3 = (priority + others)[:3]
    if not top3:
        sys.exit(0)

    print('📌 Relevant memory:')
    for r in top3:
        sym = f\" ({r['symbol']})\" if r.get('symbol') else ''
        print(f\"  #{r['id']}  [{r['type']}]  {r['title']}{sym}\")
except Exception:
    pass
" 2>>"$ERR_LOG"

# --- Reasoning Trace injection (synchronous) -----------------------------
/root/.local/token-savior-venv/bin/python3 -c "
import sys, json
sys.path.insert(0, '/root/token-savior/src')
from token_savior import memory_db

try:
    payload = json.loads('''$PAYLOAD''')
    text = (payload.get('prompt') or '').strip()
    if len(text) < 20:
        sys.exit(0)
    db = memory_db.get_db()
    row = db.execute(
        'SELECT project_root FROM observations GROUP BY project_root ORDER BY COUNT(*) DESC LIMIT 1'
    ).fetchone()
    db.close()
    if not row:
        sys.exit(0)
    hint = memory_db.reasoning_inject(row[0], text)
    if hint:
        print(hint)
except Exception:
    pass
" 2>>"$ERR_LOG"

# --- Session mode auto-detection (synchronous, write override file) ------
/root/.local/token-savior-venv/bin/python3 -c "
import sys, json, re
sys.path.insert(0, '/root/token-savior/src')
from token_savior import memory_db

try:
    payload = json.loads('''$PAYLOAD''')
    text = (payload.get('prompt') or '').strip()
    if len(text) < 3:
        sys.exit(0)

    explicit = re.match(r'(?i)^mode\s*:\s*(debug|review|code|silent|infra)\b', text)
    if explicit:
        memory_db.set_session_override(explicit.group(1).lower())
        sys.exit(0)

    KEYWORDS = [
        (r'(?i)^(debug|fix this|wtf|pourquoi|why is)\b', 'debug'),
        (r'(?i)^(review|audit|check|v[eé]rifie|analyse)\b', 'review'),
    ]
    for pattern, target in KEYWORDS:
        if re.match(pattern, text):
            memory_db.set_session_override(target)
            break
except Exception:
    pass
" 2>>"$ERR_LOG"

# --- Background: archive + trigger phrases --------------------------------
/root/.local/token-savior-venv/bin/python3 -c "
import sys, json, re
sys.path.insert(0, '/root/token-savior/src')
from token_savior import memory_db

payload = json.loads('''$PAYLOAD''')
text = payload.get('prompt', '')
if len(text) < 10:
    sys.exit(0)

try:
    mode = memory_db.get_current_mode()
    archive_enabled = mode.get('prompt_archive', True)
except Exception:
    archive_enabled = True

db = memory_db.get_db()
row = db.execute(
    'SELECT project_root FROM observations GROUP BY project_root ORDER BY COUNT(*) DESC LIMIT 1'
).fetchone()
db.close()
project = row[0] if row else None

TRIGGER_PATTERNS = [
    (r'(?i)^rappelle[- ]toi que (.+)$', 'note'),
    (r'(?i)^note que (.+)$', 'note'),
    (r'(?i)^à retenir\s*:\s*(.+)$', 'convention'),
    (r'(?i)^r[eè]gle\s*:\s*(.+)$', 'guardrail'),
    (r'(?i)^ne jamais (.+)$', 'guardrail'),
    (r'(?i)^toujours (.+)$', 'convention'),
    (r'(?i)^remember that (.+)$', 'note'),
    (r'(?i)^note that (.+)$', 'note'),
    (r'(?i)^important\s*:\s*(.+)$', 'warning'),
    (r'(?i)^rule\s*:\s*(.+)$', 'guardrail'),
    (r'(?i)^never (.+)$', 'guardrail'),
    (r'(?i)^always (.+)$', 'convention'),
    (r'(?i)^commande\s*:\s*(.+)$', 'command'),
    (r'(?i)^infra\s*:\s*(.+)$', 'infra'),
    (r'(?i)^config\s*:\s*(.+)$', 'config'),
    (r'(?i)^id[ée]e\s*:\s*(.+)$', 'idea'),
    (r'(?i)^research\s*:\s*(.+)$', 'research'),
    # --- Negative memory (ruled_out) ---
    (r'(?i)^(?:ruled[- ]out|écart[ée]?|ecart[ée]?)\s*:\s*(.+)$', 'ruled_out'),
    (r'(?i)^(?:ne pas|do not)\s+(.+?)\s+(?:car|because|parce que)\s+(.+)$', 'ruled_out'),
    (r'(?i)^(?:essay[ée]|tried|tested)\s+(.+?),?\s+(?:[éa] (?:échou[ée]|fail)|fails?|ne marche pas|does not work)\b.*$', 'ruled_out'),
    (r'(?i)^(?:on a (?:déjà|deja) essay[ée]|already tried)\s+(.+)$', 'ruled_out'),
]

if project:
    for pattern, obs_type in TRIGGER_PATTERNS:
        m = re.match(pattern, text.strip())
        if m:
            content = m.group(1).strip()
            title = content[:60] + ('...' if len(content) > 60 else '')
            try:
                memory_db.observation_save(
                    session_id=None, project_root=project,
                    type=obs_type, title=title, content=content,
                    tags=['trigger-phrase'],
                )
            except Exception:
                pass
            break

if archive_enabled and project:
    memory_db.prompt_save(None, project, text)
" 2>>"$ERR_LOG" &

exit 0
