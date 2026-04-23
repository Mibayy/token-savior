#!/bin/bash
# Memory Engine — Stop / SessionEnd hook
#   Arg $1 = "stop" (default, interruption)
#          | "end"  (clean SessionEnd)
# Stop  → short 3-bullet summary, no Telegram, end_type=interrupted
# End   → 2-section structured summary (changes + memory), Telegram push, end_type=completed


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
HOOK_MODE="${1:-stop}"

# Anti-recursion: `claude -p` triggers its own Stop hook.
if [ -n "$TS_STOP_HOOK_RUNNING" ]; then
    exit 0
fi
export TS_STOP_HOOK_RUNNING=1

PY=/root/.local/token-savior-venv/bin/python3

# Always clear session-scoped mode override at the very start, regardless of
# whether the DB has any state or obs for this session. Mode is a session thing.
"$PY" -c "
import sys, json
sys.path.insert(0, '/root/token-savior/src')
from token_savior import memory_db
memory_db.clear_session_override()
# Reset activity-tracker source to 'auto' at session end
try:
    t = memory_db._read_activity_tracker()
    t['current_mode_source'] = 'auto'
    memory_db._write_activity_tracker(t)
except Exception:
    pass
" 2>>"$ERR_LOG"

# TCA — flush session co-activations into the persistent tensor.
"$PY" -c "
import os, sys
sys.path.insert(0, '/root/token-savior/src')
try:
    from pathlib import Path
    from token_savior.tca_engine import TCAEngine
    stats_dir = Path(os.path.expanduser('~/.local/share/token-savior'))
    engine = TCAEngine(stats_dir)
    pairs = engine.flush_session()
    if pairs:
        print(f'TCA: flushed {pairs} co-activation pairs.', file=sys.stderr)
except Exception:
    pass
" 2>>"$ERR_LOG"

# 1. Resolve active session + attached observations (fallback: claim orphans <2h).
SESSION_JSON=$("$PY" -c "
import sys, os, json, time
sys.path.insert(0, '/root/token-savior/src')
from token_savior import memory_db

project = os.environ.get('CLAUDE_PROJECT_ROOT', '')
db = memory_db.get_db()
if not project:
    row = db.execute(
        'SELECT project_root FROM observations GROUP BY project_root ORDER BY COUNT(*) DESC LIMIT 1'
    ).fetchone()
    project = row[0] if row else ''

if not project:
    db.close()
    sys.exit(0)

row = db.execute(
    'SELECT id FROM sessions WHERE project_root=? AND status=? ORDER BY created_at_epoch DESC LIMIT 1',
    [project, 'active'],
).fetchone()
created = False
if row:
    session_id = row[0]
else:
    db.close()
    session_id = memory_db.session_start(project)
    created = True
    db = memory_db.get_db()
    cutoff = int(time.time()) - 7200
    db.execute(
        'UPDATE observations SET session_id=? '
        'WHERE session_id IS NULL AND project_root=? AND created_at_epoch >= ? AND archived=0',
        (session_id, project, cutoff),
    )
    db.commit()

db.close()
obs = memory_db.observation_get_by_session(session_id)
print(json.dumps({'session_id': session_id, 'project': project, 'obs': obs, 'created': created}))
" 2>>"$ERR_LOG")

if [ -z "$SESSION_JSON" ]; then
    exit 0
fi

SESSION_ID=$(echo "$SESSION_JSON" | "$PY" -c "import sys,json; print(json.load(sys.stdin)['session_id'])")
PROJECT=$(echo "$SESSION_JSON" | "$PY" -c "import sys,json; print(json.load(sys.stdin)['project'])")
OBS_COUNT=$(echo "$SESSION_JSON" | "$PY" -c "import sys,json; print(len(json.load(sys.stdin)['obs']))")

# 2. No observations → close silently
if [ "$OBS_COUNT" -eq 0 ]; then
    "$PY" -c "
import sys, os
sys.path.insert(0, '/root/token-savior/src')
from token_savior import memory_db
memory_db.session_end($SESSION_ID, end_type='$HOOK_MODE' == 'end' and 'completed' or 'interrupted')
memory_db.clear_session_override()
print(f'Session $SESSION_ID closed (no observations, mode=$HOOK_MODE).', file=sys.stderr)
" 2>>"$ERR_LOG"
    exit 0
fi

# 3. Build prompt: extract touched symbols from obs + add git-changed files for "end" mode
TMP_IN=$(mktemp)
TMP_OUT=$(mktemp)
trap "rm -f $TMP_IN $TMP_OUT" EXIT

CHANGED_SYMBOLS=$(echo "$SESSION_JSON" | "$PY" -c "
import sys, json
data = json.load(sys.stdin)
lines = []
seen = set()
for o in data['obs']:
    sym = o.get('symbol') or ''
    fp = o.get('file_path') or ''
    key = f'{sym}|{fp}'
    if not sym or key in seen:
        continue
    seen.add(key)
    label = f'{sym}' + (f' ({fp})' if fp else '')
    reason = (o.get('title') or '')[:80]
    lines.append(f'- {label}: {reason}')
print('\n'.join(lines) if lines else '(no symbol-linked obs)')
")

# Also include git-changed files in the project (end mode only)
GIT_CHANGES=""
if [ "$HOOK_MODE" = "end" ] && [ -d "$PROJECT/.git" ]; then
    GIT_CHANGES=$(cd "$PROJECT" && git diff --name-only HEAD 2>>"$ERR_LOG" | head -20)
fi

# Build the observations context
echo "$SESSION_JSON" | "$PY" -c "
import sys, json
data = json.load(sys.stdin)
for o in data['obs']:
    content = (o.get('content') or '')[:200]
    sym = f\" [{o.get('symbol')}]\" if o.get('symbol') else ''
    print(f\"[{o['type']}]{sym} {o['title']}: {content}\")
" > "$TMP_IN"

# Check mode gates session_summary
SUMMARY_ENABLED=$("$PY" -c "
import sys
sys.path.insert(0, '/root/token-savior/src')
from token_savior import memory_db
m = memory_db.get_current_mode()
print('1' if m.get('session_summary', True) else '0')
" 2>>"$ERR_LOG")

# 4. Generate summary via claude -p with mode-appropriate prompt
if [ "$SUMMARY_ENABLED" = "1" ] && command -v claude &>/dev/null; then
    if [ "$HOOK_MODE" = "end" ]; then
        PROMPT="Tu es un assistant de développement. Session de dev terminée.

Symboles modifiés pendant la session :
${CHANGED_SYMBOLS}

Fichiers changés (git) :
${GIT_CHANGES:-(aucun)}

Observations capturées :
$(cat "$TMP_IN")

Génère un summary structuré en 2 parties STRICTES :

## Changements
- symbol_name (file.py): description courte (1 ligne par symbole modifié)

## Mémoire
- bullet 1
- bullet 2
- bullet 3 (3 bullets max sur ce qui a été appris/décidé)

Réponds UNIQUEMENT avec ces 2 sections, rien d'autre."
        claude -p "$PROMPT" > "$TMP_OUT" 2>>"$ERR_LOG"
    else
        # Stop mode → short 3-bullet summary, no structured sections
        PROMPT="Session interrompue. Résume en 3 bullet points MAX ce qui a été fait avant l'interruption.
Réponds uniquement avec les bullets, rien d'autre.

Observations :
$(cat "$TMP_IN")"
        claude -p "$PROMPT" > "$TMP_OUT" 2>>"$ERR_LOG"
    fi
fi

# 5. Close the session + persist summary (safe: summary via stdin, IDs via env).
export SS_SID="$SESSION_ID"
export SS_PROJECT="$PROJECT"
export SS_MODE="$HOOK_MODE"
export SS_OBS_IDS=$(echo "$SESSION_JSON" | "$PY" -c "import sys,json; print(json.dumps([o['id'] for o in json.load(sys.stdin)['obs']]))")

"$PY" -c "
import sys, json, os
sys.path.insert(0, '/root/token-savior/src')
from token_savior import memory_db

summary = sys.stdin.read().strip() or None
session_id = int(os.environ['SS_SID'])
project = os.environ['SS_PROJECT']
mode = os.environ['SS_MODE']
obs_ids = json.loads(os.environ.get('SS_OBS_IDS', '[]'))

end_type = 'completed' if mode == 'end' else 'interrupted'
memory_db.session_end(session_id, summary=summary, end_type=end_type)
if summary and obs_ids:
    memory_db.summary_save(session_id, project, summary, obs_ids)
    print(f'Summary saved for session {session_id} (mode={mode}, {len(obs_ids)} obs).', file=sys.stderr)
else:
    print(f'Session {session_id} closed without summary (mode={mode}).', file=sys.stderr)

# Telegram push: only on 'end' mode + current mode allows it
if mode == 'end' and summary:
    try:
        cur = memory_db.get_current_mode()
        if cur.get('name') != 'silent':
            memory_db.notify_telegram({
                'type': 'note',
                'title': f'Session summary — {project.rsplit(chr(47),1)[-1]}',
                'content': summary,
                'symbol': None,
            })
    except Exception:
        pass

# Clear session mode override at the end of any session
try:
    memory_db.clear_session_override()
except Exception:
    pass
" < "$TMP_OUT" 2>>"$ERR_LOG"

# Weekly self-consistency check (7-day interval)
(
    CONS_FLAG=/root/.local/share/token-savior/last_consistency_check
    mkdir -p "$(dirname "$CONS_FLAG")"
    NOW_CONS=$(date +%s)
    LAST_CONS=0
    [ -f "$CONS_FLAG" ] && LAST_CONS=$(cat "$CONS_FLAG" 2>>"$ERR_LOG" || echo 0)
    AGE_CONS=$((NOW_CONS - LAST_CONS))
    if [ "$AGE_CONS" -ge 604800 ]; then
        "$PY" -c "
import sys
sys.path.insert(0, '/root/token-savior/src')
from token_savior import memory_db
res = memory_db.run_consistency_check(project_root='$PROJECT' or None, limit=200, dry_run=False)
print(f'[consistency] checked={res[\"checked\"]} failed={res[\"failed\"]} quarantined={res[\"quarantined\"]} stale={res[\"stale_suspected\"]}', file=sys.stderr)
" 2>>"$ERR_LOG"
        echo "$NOW_CONS" > "$CONS_FLAG"
    fi
) &

# Save session signature for cross-session warm start (all modes)
"$PY" -c "
import sys, os, time
sys.path.insert(0, '/root/token-savior/src')
from pathlib import Path
from token_savior.session_warmstart import SessionWarmStart
from token_savior import memory_db

try:
    sid = $SESSION_ID
    project = '$PROJECT'
    db = memory_db.get_db()
    row = db.execute(
        'SELECT created_at_epoch, ended_at_epoch, tokens_injected FROM sessions WHERE id=?',
        [sid],
    ).fetchone()
    if row:
        created, ended, tokens_injected = row[0], row[1] or int(time.time()), row[2] or 0
        duration_min = max(0.0, (ended - created) / 60.0)
    else:
        duration_min = 0.0

    try:
        mode = memory_db.get_current_mode(project_root=project or None)
        mode_name = mode.get('name', 'code')
    except Exception:
        mode_name = 'code'

    # Tool counts from access_count on observations (proxy — real tool call
    # sequences are tracked by PPMPrefetcher which doesn't persist per-session).
    obs_rows = memory_db.observation_get_by_session(sid)
    symbols = [o.get('symbol') for o in obs_rows if o.get('symbol')]

    # Derive tool_counts from PPMPrefetcher tail (recent call sequence).
    from token_savior.markov_prefetcher import PPMPrefetcher
    stats_dir = Path(os.path.expanduser('~/.local/share/token-savior'))
    prefetcher = PPMPrefetcher(stats_dir)
    tool_counts = {}
    for st in prefetcher.call_sequence[-200:]:
        tool_counts[st.tool] = tool_counts.get(st.tool, 0) + 1

    turns = sum(tool_counts.values())
    obs_accessed = sum(1 for o in obs_rows if (o.get('access_count') or 0) > 0)

    ws = SessionWarmStart(stats_dir)
    ws.save_session_signature(sid, project, {
        'tool_counts': tool_counts,
        'duration_min': duration_min,
        'turns': turns,
        'obs_accessed': obs_accessed,
        'symbols': symbols,
        'mode': mode_name,
    })
    db.close()
except Exception as e:
    print(f'[warmstart] save failed: {e}', file=sys.stderr)
" 2>>"$ERR_LOG"

# Compute tokens_saved_est for session (all modes)
"$PY" -c "
import sys
sys.path.insert(0, '/root/token-savior/src')
from token_savior import memory_db
sid = $SESSION_ID
db = memory_db.get_db()
n = db.execute(
    'SELECT COUNT(*) FROM observations WHERE session_id=? AND access_count > 0',
    [sid],
).fetchone()[0]
tokens_saved = n * 200
db.execute('UPDATE sessions SET tokens_saved_est=? WHERE id=?', [tokens_saved, sid])
db.commit()
db.close()
" 2>>"$ERR_LOG"

# End-of-session: prompt pattern suggestions (end mode only)
if [ "$HOOK_MODE" = "end" ]; then
    "$PY" -c "
import sys, os
sys.path.insert(0, '/root/token-savior/src')
from token_savior import memory_db
project = '$PROJECT'
try:
    sugg = memory_db.analyze_prompt_patterns(project, window_days=14, min_occurrences=3)
    if sugg:
        print('', file=sys.stderr)
        print(f'💡 Recurring topics in recent prompts ({len(sugg)}):', file=sys.stderr)
        for s in sugg[:5]:
            print(f\"  · '{s['token']}' ×{s['count']} — consider memory_save\", file=sys.stderr)
except Exception:
    pass
" 2>&1
fi

# End-of-session: MDL distillation suggestion (end mode only)
if [ "$HOOK_MODE" = "end" ]; then
    "$PY" -c "
import sys
sys.path.insert(0, '/root/token-savior/src')
from token_savior import memory_db
project = '$PROJECT'
try:
    res = memory_db.run_mdl_distillation(project, dry_run=True)
    undistilled = memory_db.get_db().execute(
        \"SELECT COUNT(*) FROM observations WHERE project_root=? AND archived=0 \"
        \"AND (tags IS NULL OR (tags NOT LIKE '%mdl-distilled%' AND tags NOT LIKE '%mdl-abstraction%'))\",
        [project],
    ).fetchone()[0]
    if undistilled > 20 and res.get('clusters_found', 0) > 0:
        print('', file=sys.stderr)
        print(f\"💡 MDL: {res['clusters_found']} distillation candidates \"
              f\"(~{res['tokens_freed_estimate']:,}t freed) — run memory_distill to compress\",
              file=sys.stderr)
except Exception:
    pass
" 2>&1
fi

# End-of-session: backup to markdown (end mode only)
if [ "$HOOK_MODE" = "end" ]; then
    (
        /root/.local/token-savior-venv/bin/python3 \
            /root/token-savior/scripts/export_markdown.py \
            --output-dir /root/memory-backup >/dev/null 2>&1
    ) &
fi

exit 0
