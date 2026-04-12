#!/bin/bash
# Memory Engine — PreCompact hook
# Injects recent summaries + memory index before Claude Code compacts the conversation.
# Goal: prevent loss of memory context when the conversation gets compacted.

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
" 2>/dev/null)

if [ -n "$RESULT" ]; then
    echo "$RESULT"
fi
exit 0
