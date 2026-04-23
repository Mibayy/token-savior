#!/bin/bash
# Memory Engine — PreCompact hook
# Injects recent summaries + memory index before Claude Code compacts the conversation.
# Goal: prevent loss of memory context when the conversation gets compacted.


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
RESULT=$(/root/.local/token-savior-venv/bin/python3 -c "
import sys, os
sys.path.insert(0, '/root/token-savior/src')
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
exit 0
