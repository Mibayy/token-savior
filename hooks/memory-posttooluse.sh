#!/bin/bash
# Memory Engine — PostToolUse hook
# - Auto-capture des commandes Bash significatives réussies
# - Hint de capture pour les WebFetch (research)


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

/root/.local/token-savior-venv/bin/python3 -c "
import sys, json, re
sys.path.insert(0, '/root/token-savior/src')
from token_savior import memory_db

try:
    payload = json.loads(sys.stdin.read())
except Exception:
    sys.exit(0)

tool = payload.get('tool_name', '')

# === BASH AUTO-CAPTURE ===
if tool == 'Bash':
    tin = payload.get('tool_input', {}) or {}
    command = tin.get('command', '')
    tres = payload.get('tool_response', {}) or {}
    exit_code = tres.get('exit_code', tres.get('exitCode', 0))
    try:
        exit_code = int(exit_code) if exit_code is not None else 0
    except Exception:
        exit_code = 0
    if exit_code != 0 or not command:
        sys.exit(0)

    CAPTURE_PATTERNS = [
        (r'systemctl\s+(start|restart|enable|reload)\s+(\S+)',
         lambda m: ('command', f'{m.group(1)} {m.group(2)}', command, m.group(2))),
        (r'\bcrontab\s+-[el]',
         lambda m: ('infra', 'Crontab modifié', command, 'cron')),
        (r'(chmod|chown)\s+[^\s]+\s+(\S+)',
         lambda m: ('infra', f'{m.group(1)} sur {m.group(2)}', command, m.group(2))),
    ]

    for pattern, builder in CAPTURE_PATTERNS:
        m = re.search(pattern, command, re.IGNORECASE)
        if not m:
            continue
        obs_type, title, content, ctx = builder(m)
        db = memory_db.get_db()
        row = db.execute(
            'SELECT project_root FROM observations GROUP BY project_root ORDER BY COUNT(*) DESC LIMIT 1'
        ).fetchone()
        db.close()
        if not row:
            break
        try:
            obs_id = memory_db.observation_save(
                session_id=None,
                project_root=row[0],
                type=obs_type,
                title=title,
                content=content,
                context=ctx,
                tags=['auto-capture', 'bash'],
            )
            if obs_id:
                print(f'AUTO-SAVED: #{obs_id} [{obs_type}] {title}', file=sys.stderr)
        except Exception as exc:
            print(f'[posttooluse] save error: {exc}', file=sys.stderr)
        break

# === WEBFETCH RESEARCH HINT ===
elif tool in ('WebFetch', 'web_fetch'):
    tin = payload.get('tool_input', {}) or {}
    url = tin.get('url', '') or ''
    if not url or len(url) < 10:
        sys.exit(0)
    import urllib.parse
    try:
        parsed = urllib.parse.urlparse(url)
        domain = parsed.netloc.replace('www.', '')
        path = parsed.path.strip('/').replace('-', ' ').replace('_', ' ')
        suggested = f'{domain}: {path[:50]}' if path else domain
    except Exception:
        suggested = url[:60]
    print(
        f'💡 Research hint: memory_save type=\"research\" '
        f'title=\"{suggested}\" context=\"{url}\" content=\"[finding]\"'
    )
" <<< "$PAYLOAD" 2>>"$ERR_LOG" &

# === ACTIVITY TRACKER (mode auto-detection) ===
/root/.local/token-savior-venv/bin/python3 -c "
import sys, json, os, time, re
from pathlib import Path

try:
    payload = json.loads(sys.stdin.read())
except Exception:
    sys.exit(0)

tool = payload.get('tool_name', '')
short = tool.split('__')[-1]

if tool == 'Bash':
    cmd = (payload.get('tool_input', {}) or {}).get('command', '')
    if re.search(r'systemctl|nginx|docker|service|daemon|journalctl|crontab', cmd, re.I):
        tool_key = 'Bash:infra'
    elif re.search(r'git\s+(push|pull|merge)', cmd, re.I):
        tool_key = 'Bash:git'
    elif re.search(r'(pip|npm|apt|pnpm)\s+install', cmd, re.I):
        tool_key = 'Bash:install'
    else:
        tool_key = 'Bash:other'
elif tool in ('WebFetch', 'web_fetch', 'WebSearch'):
    tool_key = 'WebFetch'
elif short in ('replace_symbol_source', 'insert_near_symbol',
               'apply_symbol_change_and_validate') or tool in ('Edit','Write','NotebookEdit'):
    tool_key = 'Edit'
elif short in ('get_function_source', 'get_class_source', 'find_symbol', 'get_edit_context'):
    tool_key = 'Read'
elif short in ('run_impacted_tests', 'find_dead_code', 'detect_breaking_changes',
               'find_hotspots', 'analyze_config', 'analyze_docker'):
    tool_key = 'Analyze'
else:
    sys.exit(0)

tracker_path = Path.home() / '.config' / 'token-savior' / 'activity_tracker.json'
tracker_path.parent.mkdir(parents=True, exist_ok=True)
try:
    tracker = json.loads(tracker_path.read_text())
except Exception:
    tracker = {'recent_tools': [], 'last_updated': 0,
               'suggested_mode': 'code', 'current_mode_source': 'auto'}

tracker['recent_tools'] = ([tool_key] + tracker.get('recent_tools', []))[:10]
tracker['last_updated'] = int(time.time())

recent = tracker['recent_tools']
infra_c   = sum(1 for t in recent if t == 'Bash:infra')
edit_c    = sum(1 for t in recent if t in ('Edit', 'Read'))
web_c     = sum(1 for t in recent if t == 'WebFetch')
analyze_c = sum(1 for t in recent if t == 'Analyze')

if infra_c >= 3:
    suggested = 'infra'
elif analyze_c >= 2:
    suggested = 'review'
elif edit_c >= 4:
    suggested = 'code'
elif web_c >= 3:
    suggested = 'review'
else:
    suggested = tracker.get('suggested_mode', 'code')

current_source = tracker.get('current_mode_source', 'auto')
if suggested != tracker.get('suggested_mode') and current_source != 'manual':
    sys.path.insert(0, '/root/token-savior/src')
    from token_savior import memory_db
    memory_db.set_mode(suggested, source='auto')
    tracker['suggested_mode'] = suggested
    tracker['current_mode_source'] = 'auto'
    print(f'Auto-mode: {suggested} (detected from activity)', file=sys.stderr)

tracker_path.write_text(json.dumps(tracker, indent=2))
" <<< "$PAYLOAD" 2>>"$ERR_LOG" &

# === A3: LLM AUTO-EXTRACT (opt-in via TS_AUTO_EXTRACT=1) ===
# Zero-cost when unset: the shell `if` short-circuits, Python is never spawned.
if [ "${TS_AUTO_EXTRACT:-}" = "1" ]; then
/root/.local/token-savior-venv/bin/python3 -c "
import sys, json
sys.path.insert(0, '/root/token-savior/src')

try:
    payload = json.loads(sys.stdin.read())
except Exception:
    sys.exit(0)

tool_name = payload.get('tool_name', '')
tool_input = payload.get('tool_input', {}) or {}
tool_output = payload.get('tool_response', {}) or {}
if isinstance(tool_output, (dict, list)):
    out_str = json.dumps(tool_output, default=str)[:4000]
else:
    out_str = str(tool_output)[:4000]

try:
    from token_savior.memory import auto_extract
    auto_extract.process_tool_use(tool_name, tool_input, out_str)
except Exception as exc:
    print(f'[posttooluse:auto-extract] {exc}', file=sys.stderr)
" <<< "$PAYLOAD" 2>>"$ERR_LOG" &
fi

exit 0
