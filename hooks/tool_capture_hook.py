"""Claude Code PostToolUse hook — sandbox large tool outputs.

Reads the PostToolUse JSON event from stdin. If the tool's output exceeds
``CAPTURE_THRESHOLD_BYTES`` (default 4096), persists the full output to
Token Savior's ``tool_captures`` table and emits an ``additionalContext``
note pointing at ``ts://capture/{id}``.

This hook does **not** replace the tool output the model just saw — it only
indexes it so the agent can retrieve the full content later (e.g. after
context compaction) via ``capture_search`` / ``capture_get``.

Wire-up (one-time, in ``~/.claude/settings.json``):

    {
      "hooks": {
        "PostToolUse": [
          {
            "matcher": "Bash|WebFetch|mcp__playwright|mcp__token-savior__search_codebase|Read",
            "hooks": [{
              "type": "command",
              "command": "/usr/bin/python3 /root/token-savior/hooks/tool_capture_hook.py"
            }]
          }
        ]
      }
    }

Env vars:
  TS_CAPTURE_THRESHOLD_BYTES  -- minimum response size to capture (default 4096)
  TS_CAPTURE_DISABLED         -- set to "1" to noop
  TS_CAPTURE_REPLACE          -- set to "1" for strong-replace: appends a directive
                                 instructing the agent to ignore the raw inline output
                                 and use capture_get on the URI instead. Useful in
                                 tight context budgets.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

# Resolve the package even when invoked via /usr/bin/python3
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

THRESHOLD = int(os.environ.get("TS_CAPTURE_THRESHOLD_BYTES", "4096"))


def _empty_pass() -> None:
    """Allow Claude Code to keep the original tool output untouched."""
    print(json.dumps({"continue": True}))


def main() -> None:
    if os.environ.get("TS_CAPTURE_DISABLED") == "1":
        _empty_pass()
        return
    raw = sys.stdin.read()
    if not raw:
        _empty_pass()
        return
    try:
        event = json.loads(raw)
    except Exception:
        _empty_pass()
        return

    tool_name = event.get("tool_name") or "unknown"
    response = event.get("tool_response") or {}
    # Claude Code PostToolUse exposes the textual response under .content
    # for native tools, .stdout/.stderr for Bash, or as a string fallback.
    content = (
        response.get("content")
        or response.get("stdout")
        or response.get("output")
        or ""
    )
    if not isinstance(content, str):
        try:
            content = json.dumps(content, default=str)
        except Exception:
            content = str(content)
    if len(content) < THRESHOLD:
        _empty_pass()
        return

    # Lazy import — keep hook startup cheap when the threshold short-circuits
    try:
        from token_savior.memory import tool_capture
    except Exception as exc:
        sys.stderr.write(f"[ts-capture-hook] import failed: {exc}\n")
        _empty_pass()
        return

    args_summary = json.dumps(event.get("tool_input") or {}, default=str)[:300]
    res = tool_capture.capture_put(
        tool_name=tool_name,
        output=content,
        args_summary=args_summary,
        session_id=event.get("session_id"),
        project_root=event.get("cwd"),
        meta={"hook": "PostToolUse"},
    )
    cap_id = res.get("id")
    if not cap_id:
        _empty_pass()
        return
    replace_mode = os.environ.get("TS_CAPTURE_REPLACE") == "1"
    if replace_mode:
        note = (
            f"[token-savior:capture] {tool_name} output {res['bytes']}B "
            f"sandboxed to ts://capture/{cap_id} ({res['lines']} lines). "
            f"REPLACE MODE: ignore the inline output above; treat it as truncated. "
            f"For any analysis, call capture_get(id={cap_id}, range='preview'|'head'|'tail'|'all'|'line:N-M') "
            f"or capture_search/capture_aggregate. The full content lives only in the sandbox."
        )
    else:
        note = (
            f"[token-savior:capture] {tool_name} output {res['bytes']}B "
            f"sandboxed to ts://capture/{cap_id} "
            f"({res['lines']} lines). "
            f"Use capture_search / capture_get / capture_aggregate to retrieve "
            f"this content later if the conversation is compacted."
        )
    print(json.dumps({
        "continue": True,
        "hookSpecificOutput": {
            "hookEventName": "PostToolUse",
            "additionalContext": note,
        },
    }))


if __name__ == "__main__":
    main()
