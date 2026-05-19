"""Claude Code PreToolUse hook — rewrite Bash commands into compact variants.

Reads the PreToolUse JSON event from stdin. If the tool is ``Bash`` and the
command matches a known rewrite rule, emits ``hookSpecificOutput`` with
``permissionDecision: allow`` and an ``updatedInput`` carrying a more compact
form of the command (e.g. ``git status`` -> ``git status --porcelain=v2 --branch``).

The default is **off** — set ``TS_BASH_REWRITE=1`` in the Claude Code
environment to activate. When inactive, the hook returns an empty
``{"continue": true}`` payload so Claude proceeds with the original command.

Wire-up (one-time, in ``~/.claude/settings.json``):

    {
      "hooks": {
        "PreToolUse": [
          {
            "matcher": "Bash",
            "hooks": [{
              "type": "command",
              "command": "/usr/bin/python3 /root/token-savior/hooks/bash_rewriter_hook.py",
              "timeout": 2000
            }]
          }
        ]
      }
    }

Env vars:
  TS_BASH_REWRITE         -- set to "1" to enable rewrites (default off)
  TS_BASH_REWRITE_LOG     -- optional path; if set, append one JSON line per
                             rewrite for auditing

Implementation note (stdout flushing): Claude Code reads the hook's stdout
after the process exits. Python's print() with flush=True is enough to avoid
the buffer-drop scenario documented in the Node hook memory note.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

# Resolve the package even when invoked via /usr/bin/python3
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))


def _pass_through() -> None:
    """Emit a no-op payload so Claude proceeds with the original command."""
    print(json.dumps({"continue": True}), flush=True)


def _audit(entry: dict) -> None:
    log_path = os.environ.get("TS_BASH_REWRITE_LOG")
    if not log_path:
        return
    try:
        with open(log_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, default=str) + "\n")
    except Exception:
        # Auditing must never break the hook
        pass


def main() -> None:
    if os.environ.get("TS_BASH_REWRITE") != "1":
        _pass_through()
        return
    raw = sys.stdin.read()
    if not raw:
        _pass_through()
        return
    try:
        event = json.loads(raw)
    except Exception:
        _pass_through()
        return
    if event.get("tool_name") != "Bash":
        _pass_through()
        return
    tool_input = event.get("tool_input") or {}
    command = tool_input.get("command")
    if not isinstance(command, str) or not command.strip():
        _pass_through()
        return

    # Lazy import — keep hook startup cheap when env gate is off.
    try:
        from token_savior.bash_rewriter import rewrite
    except Exception as exc:
        sys.stderr.write(f"[ts-bash-rewriter] import failed: {exc}\n")
        _pass_through()
        return

    new_command, reason = rewrite(command)
    if reason is None or new_command == command:
        _pass_through()
        return

    payload = {
        "continue": True,
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "allow",
            "permissionDecisionReason": f"[token-savior:bash-rewriter] {reason}",
            "updatedInput": {**tool_input, "command": new_command},
        },
    }
    _audit({
        "session_id": event.get("session_id"),
        "cwd": event.get("cwd"),
        "original": command,
        "rewritten": new_command,
        "reason": reason,
    })
    print(json.dumps(payload), flush=True)


if __name__ == "__main__":
    main()
