"""Detect superseded / redundant tool results in a transcript (feature B).

Agent-agnostic: consumes the same Events ``transcript_scanner`` yields for any
harness. This module is PURE — it decides WHAT is stale, not how to act on it.
The findings feed two delivery paths: a callable MCP tool (works on every
agent) and, on Claude Code only, the PreCompact hook. Nothing here is
harness-specific.
"""
from __future__ import annotations

from collections.abc import Iterable

from token_savior.discover.transcript_scanner import Event

_READ_TOOLS = {"Read", "NotebookRead"}
_EDIT_TOOLS = {
    "Edit", "Write", "MultiEdit", "NotebookEdit",
    "replace_symbol_source", "edit_lines_in_symbol",
    "insert_near_symbol", "move_symbol", "add_field_to_model",
}


def _base(tool: str) -> str:
    """Strip an ``mcp__<server>__`` prefix so tool names match by their leaf."""
    return tool.rsplit("__", 1)[-1] if "__" in tool else tool


def find_superseded(events: Iterable[Event]) -> list[dict]:
    """References to tool results made obsolete LATER in the same session:

    - ``reread``          a file read, then read again (first read redundant)
    - ``read_then_edit``  a file read, then edited (the read is now stale)
    - ``rerun``           a Bash command run, then re-run (first output stale)

    Each finding names the stale event (``stale_index``) and the fresh one
    (``fresh_index``) so a caller can drop the stale content or replace it with
    a pointer. Order-preserving, deterministic, no model.
    """
    findings: list[dict] = []
    last_read: dict[tuple[str, str], int] = {}
    last_bash: dict[tuple[str, str], int] = {}
    for i, ev in enumerate(events):
        base = _base(ev.tool_name)
        sess = ev.session_id
        if base in _READ_TOOLS:
            fp = ev.args.get("file_path")
            if fp:
                key = (sess, fp)
                if key in last_read:
                    findings.append({"reason": "reread", "tool": base, "target": fp,
                                     "session": sess, "stale_index": last_read[key],
                                     "fresh_index": i})
                last_read[key] = i
        elif base in _EDIT_TOOLS:
            fp = ev.args.get("file_path")
            if fp:
                key = (sess, fp)
                if key in last_read:
                    findings.append({"reason": "read_then_edit", "tool": base, "target": fp,
                                     "session": sess, "stale_index": last_read.pop(key),
                                     "fresh_index": i})
        elif base == "Bash":
            cmd = ev.args.get("command")
            if cmd:
                key = (sess, cmd)
                if key in last_bash:
                    findings.append({"reason": "rerun", "tool": "Bash", "target": cmd[:80],
                                     "session": sess, "stale_index": last_bash[key],
                                     "fresh_index": i})
                last_bash[key] = i
    return findings
