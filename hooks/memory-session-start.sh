#!/bin/bash
# Memory Engine — SessionStart hook

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
import sys, os, json
sys.path.insert(0, '/root/token-savior/src')
from token_savior import memory_db

project = os.environ.get('CLAUDE_PROJECT_ROOT', '')

# Adaptive budget based on remaining context
try:
    ctx_pct = int(os.environ.get('CLAUDE_CONTEXT_REMAINING_PCT', '100'))
except ValueError:
    ctx_pct = 100

if ctx_pct >= 70:
    limit, type_filter = 30, None
elif ctx_pct >= 40:
    limit, type_filter = 15, None
elif ctx_pct >= 20:
    limit, type_filter = 5, None  # filter done below
else:
    limit, type_filter = 3, 'guardrail'

# Mode-aware filter: only when context healthy
mode_name = None
if ctx_pct >= 40 and type_filter is None:
    try:
        mode = memory_db.get_current_mode(project_root=project or None)
        mode_name = mode.get('name', 'code')
        mtypes = mode.get('auto_capture_types') or []
        if mtypes:
            type_filter = list(mtypes)
    except Exception:
        pass

rows = memory_db.get_recent_index(project, limit=limit, type_filter=type_filter, mode=mode_name) if project else []

if not rows:
    db = memory_db.get_db()
    row = db.execute(
        'SELECT project_root FROM observations GROUP BY project_root ORDER BY COUNT(*) DESC LIMIT 1'
    ).fetchone()
    db.close()
    if row:
        project = row[0]
        rows = memory_db.get_recent_index(project, limit=limit, type_filter=type_filter, mode=mode_name)

# Tight budget: keep only high-signal types
if 20 <= ctx_pct < 40:
    keep = {'guardrail', 'convention', 'warning'}
    rows = [r for r in rows if r['type'] in keep][:limit]

def _fmt_row(r):
    age = r.get('age') or '?'
    glob = '🌐 ' if r.get('is_global') else ''
    stale = '⚠️ ' if r.get('stale_suspected') else ''
    sym = f\" [{r['symbol']}]\" if r.get('symbol') else ''
    sc = r.get('score')
    sc_str = f\" ★{sc:.2f}\" if sc is not None else ''
    exp = r.get('expires_at_epoch')
    ttl_str = ''
    if exp:
        import time as _t
        secs = exp - int(_t.time())
        if secs < 7 * 86400:
            if secs <= 0:
                ttl_str = ' ⏰ expired'
            else:
                ttl_str = f' ⏰ {max(1, secs // 86400)}d'
    return f\"  #{r['id']}  [{r['type']}]  {stale}{glob}{r['title']}{sym}  {age}{sc_str}{ttl_str}\"

if rows:
    import hashlib
    from pathlib import Path as _P
    state_file = _P('/root/.local/share/token-savior/last_injected_state.json')
    state_file.parent.mkdir(parents=True, exist_ok=True)
    last_state = {}
    if state_file.exists():
        try:
            last_state = json.loads(state_file.read_text())
        except Exception:
            last_state = {}

    current_ids = [r['id'] for r in rows]
    payload = json.dumps({'ids': current_ids, 'mode': mode_name})
    current_hash = hashlib.md5(payload.encode()).hexdigest()
    last_ids = set(last_state.get('injected_ids', []))
    last_mode = last_state.get('mode')
    mode_tag = f' · mode:{mode_name}' if mode_name else ''

    if current_hash == last_state.get('hash'):
        print(f'### 📌 Memory Index [{mode_tag.lstrip(\" ·\")} · unchanged]')
    else:
        new_ids = set(current_ids) - last_ids
        removed_ids = last_ids - set(current_ids)
        delta_size = len(new_ids) + len(removed_ids)
        if last_ids and last_mode == mode_name and 0 < delta_size <= 5:
            print(f'### 📌 Memory Index [{mode_tag.lstrip(\" ·\")} · delta +{len(new_ids)}/-{len(removed_ids)}]')
            rows_by_id = {r['id']: r for r in rows}
            for oid in new_ids:
                r = rows_by_id.get(oid, {})
                print('  ✚ ' + _fmt_row(r).lstrip())
            for oid in removed_ids:
                print(f'  ✖ #{oid} removed from index')
        else:
            print(f'### 📌 Memory Index (context: {ctx_pct}% · {len(rows)} obs{mode_tag})')
            for r in rows:
                print(_fmt_row(r))
        state_file.write_text(json.dumps({
            'hash': current_hash,
            'injected_ids': current_ids,
            'injected_at_epoch': int(__import__('time').time()),
            'mode': mode_name,
        }))
    print('')

# Continuity score
if project:
    try:
        cs = memory_db.compute_continuity_score(project)
        if cs.get('total', 0) > 0:
            print(f\"### 🧭 Continuity: {cs['score']}% ({cs['label']}) — {cs['valid']}/{cs['total']} valid, {cs['recent']} recent, {cs['potentially_stale']} stale\")
            print('')
    except Exception:
        pass
" 2>>"$ERR_LOG")

if [ -n "$RESULT" ]; then
    echo "$RESULT"
fi

# Track tokens_injected on active session
INJECTED_CHARS=$(printf '%s' "$RESULT" | wc -c)
INJECTED_TOKENS=$((INJECTED_CHARS / 4))
if [ "$INJECTED_TOKENS" -gt 0 ]; then
    /root/.local/token-savior-venv/bin/python3 -c "
import sys, os
sys.path.insert(0, '/root/token-savior/src')
from token_savior import memory_db
project = os.environ.get('CLAUDE_PROJECT_ROOT', '')
if not project:
    db = memory_db.get_db()
    row = db.execute('SELECT project_root FROM observations GROUP BY project_root ORDER BY COUNT(*) DESC LIMIT 1').fetchone()
    db.close()
    project = row[0] if row else ''
if project:
    db = memory_db.get_db()
    row = db.execute('SELECT id FROM sessions WHERE project_root=? AND status=? ORDER BY created_at_epoch DESC LIMIT 1', [project, 'active']).fetchone()
    if row:
        db.execute('UPDATE sessions SET tokens_injected=? WHERE id=?', [$INJECTED_TOKENS, row[0]])
        db.commit()
    db.close()
" 2>>"$ERR_LOG"
fi

# Warm start: find similar historical sessions by signature and pre-warm PPM
WARMSTART=$(/root/.local/token-savior-venv/bin/python3 -c "
import sys, os
sys.path.insert(0, '/root/token-savior/src')
from pathlib import Path
from token_savior.session_warmstart import SessionWarmStart, compute_signature
from token_savior.markov_prefetcher import PPMPrefetcher
from token_savior import memory_db

project = os.environ.get('CLAUDE_PROJECT_ROOT', '')
if not project:
    db = memory_db.get_db()
    row = db.execute('SELECT project_root FROM observations GROUP BY project_root ORDER BY COUNT(*) DESC LIMIT 1').fetchone()
    db.close()
    project = row[0] if row else ''

try:
    mode = memory_db.get_current_mode(project_root=project or None)
    mode_name = mode.get('name', 'code')
except Exception:
    mode_name = 'code'

stats_dir = Path(os.path.expanduser('~/.local/share/token-savior'))
ws = SessionWarmStart(stats_dir)

# Embryo signature — only mode + project known, no tools yet.
embryo = compute_signature({'tool_counts': {}, 'duration_min': 0, 'turns': 0, 'obs_accessed': 0, 'symbols': [], 'mode': mode_name})
similar = ws.find_similar_sessions(embryo, project_root=project or None, top_k=3, min_sim=0.3)

if similar:
    best_sim = similar[0][1]
    prefetcher = PPMPrefetcher(stats_dir)
    seeded_pairs = 0
    for entry, _ in similar:
        tc = entry.get('tool_counts') or {}
        top_tools = sorted(tc.items(), key=lambda x: -x[1])[:3]
        for i in range(len(top_tools) - 1):
            prefetcher.record_call(top_tools[i][0], '')
            seeded_pairs += 1
        if top_tools:
            prefetcher.record_call(top_tools[-1][0], '')
    prefetcher.save()
    print(f'🔥 Warm start: {len(similar)} similar sessions (best: {best_sim:.0%}) — {seeded_pairs} PPM seeds')
" 2>>"$ERR_LOG")

if [ -n "$WARMSTART" ]; then
    echo "$WARMSTART"
fi

# Statusline: [mem:N obs · mode:X]
STATUSLINE=$(/root/.local/token-savior-venv/bin/python3 -c "
import sys, os
sys.path.insert(0, '/root/token-savior/src')
from token_savior import memory_db

project = os.environ.get('CLAUDE_PROJECT_ROOT', '')
db = memory_db.get_db()
if not project:
    row = db.execute(
        'SELECT project_root FROM observations GROUP BY project_root ORDER BY COUNT(*) DESC LIMIT 1'
    ).fetchone()
    project = row[0] if row else ''
count = 0
if project:
    count = db.execute(
        'SELECT COUNT(*) FROM observations WHERE project_root=? AND archived=0',
        [project],
    ).fetchone()[0]
db.close()

mode = memory_db.get_current_mode(project_root=project or None)
mode_name = mode.get('name', 'code')
origin = mode.get('origin', 'global')
try:
    tracker = memory_db._read_activity_tracker()
    src = tracker.get('current_mode_source', 'auto')
    label = 'auto-detected' if src == 'auto' else src
except Exception:
    label = origin
print(f'[mem:{count} obs · mode:{mode_name} · {label}]')
" 2>>"$ERR_LOG")

if [ -n "$STATUSLINE" ]; then
    echo "$STATUSLINE"
fi

# Weekly auto-decay (fire-and-forget, background)
(
    FLAG=/root/.local/share/token-savior/last_decay
    mkdir -p "$(dirname "$FLAG")"
    NOW=$(date +%s)
    LAST=0
    [ -f "$FLAG" ] && LAST=$(cat "$FLAG" 2>/dev/null || echo 0)
    AGE=$((NOW - LAST))
    if [ "$AGE" -ge 604800 ]; then
        /root/.local/token-savior-venv/bin/python3 -c "
import sys, os
sys.path.insert(0, '/root/token-savior/src')
from token_savior import memory_db

project = os.environ.get('CLAUDE_PROJECT_ROOT', '')
if not project:
    db = memory_db.get_db()
    row = db.execute(
        'SELECT project_root FROM observations GROUP BY project_root ORDER BY COUNT(*) DESC LIMIT 1'
    ).fetchone()
    db.close()
    if row:
        project = row[0]

if project:
    res = memory_db.run_decay(project, dry_run=False)
    print(f'[decay] archived={res.get(\"archived\",0)} kept={res.get(\"kept\",0)} immune={res.get(\"immune\",0)}', file=sys.stderr)
    promo = memory_db.run_promotions(project_root='', dry_run=False)
    if promo.get('count', 0) > 0:
        print(f'[promote] {promo[\"count\"]} obs elevated', file=sys.stderr)
" 2>>"$ERR_LOG"
        echo "$NOW" > "$FLAG"
    fi

    # Monthly Token Economy ROI garbage collection (30-day interval)
    ROI_FLAG=/root/.local/share/token-savior/last_roi_gc
    LAST_ROI=0
    [ -f "$ROI_FLAG" ] && LAST_ROI=$(cat "$ROI_FLAG" 2>>"$ERR_LOG" || echo 0)
    AGE_ROI=$((NOW - LAST_ROI))
    if [ "$AGE_ROI" -ge 2592000 ]; then
        /root/.local/token-savior-venv/bin/python3 -c "
import sys
sys.path.insert(0, '/root/token-savior/src')
from token_savior import memory_db
res = memory_db.run_roi_gc(dry_run=False)
print(f'[roi-gc] archived={res.get(\"archived\",0)} kept={res.get(\"kept\",0)} threshold={res.get(\"threshold\",0)}', file=sys.stderr)
" 2>>"$ERR_LOG"
        echo "$NOW" > "$ROI_FLAG"
    fi

    # Weekly markdown export (fire-and-forget)
    EXPORT_FLAG=/root/.local/share/token-savior/last_md_export
    LAST_EXP=0
    [ -f "$EXPORT_FLAG" ] && LAST_EXP=$(cat "$EXPORT_FLAG" 2>>"$ERR_LOG" || echo 0)
    AGE_EXP=$((NOW - LAST_EXP))
    if [ "$AGE_EXP" -ge 604800 ]; then
        /root/.local/token-savior-venv/bin/python3 \
            /root/token-savior/scripts/export_markdown.py \
            --output-dir /root/memory-backup >/dev/null 2>&1
        echo "$NOW" > "$EXPORT_FLAG"
    fi
) &

exit 0
