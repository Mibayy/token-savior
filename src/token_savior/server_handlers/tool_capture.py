"""Server handlers for tool capture (sandbox of verbose tool outputs).

These are META category handlers (no slot needed): they only read/write the
shared SQLite memory DB. Hooks call ``capture_put`` directly via the helper
script, so the dispatcher only exposes the *retrieval* surface to agents.
"""
from __future__ import annotations

import json
from typing import Any

import mcp.types as types

from token_savior.memory import tool_capture


def _text(s: Any) -> list[types.TextContent]:
    if not isinstance(s, str):
        s = json.dumps(s, indent=2, default=str)
    return [types.TextContent(type="text", text=s)]


def _ts_capture_put(arguments: dict[str, Any]) -> list[types.TextContent]:
    """Manual capture entrypoint — used by hooks and rare manual calls."""
    tool_name = arguments.get("tool_name") or "unknown"
    output = arguments.get("output") or ""
    res = tool_capture.capture_put(
        tool_name=tool_name,
        output=output,
        args_summary=arguments.get("args_summary"),
        session_id=arguments.get("session_id"),
        project_root=arguments.get("project_root"),
        meta=arguments.get("meta"),
    )
    return _text(res)


def _ts_capture_search(arguments: dict[str, Any]) -> list[types.TextContent]:
    rows = tool_capture.capture_search(
        query=arguments.get("query") or "",
        limit=int(arguments.get("limit", 20)),
        session_id=arguments.get("session_id"),
        project_root=arguments.get("project_root"),
        tool_name=arguments.get("tool_name"),
    )
    return _text({"count": len(rows), "results": rows})


def _ts_capture_get(arguments: dict[str, Any]) -> list[types.TextContent]:
    cap_id = arguments.get("id")
    if cap_id is None:
        return _text({"error": "id required"})
    res = tool_capture.capture_get(
        int(cap_id),
        range_spec=arguments.get("range"),
        max_bytes=arguments.get("max_bytes"),
    )
    if res is None:
        return _text({"error": f"capture {cap_id} not found"})
    return _text(res)


def _ts_capture_aggregate(arguments: dict[str, Any]) -> list[types.TextContent]:
    cap_id = arguments.get("id")
    if cap_id is None:
        return _text({"error": "id required"})
    res = tool_capture.capture_aggregate(
        int(cap_id),
        transform=arguments.get("transform", "stats"),
        pattern=arguments.get("pattern"),
    )
    if res is None:
        return _text({"error": f"capture {cap_id} not found"})
    return _text(res)


def _ts_capture_list(arguments: dict[str, Any]) -> list[types.TextContent]:
    rows = tool_capture.capture_list(
        session_id=arguments.get("session_id"),
        project_root=arguments.get("project_root"),
        tool_name=arguments.get("tool_name"),
        limit=int(arguments.get("limit", 50)),
    )
    return _text({"count": len(rows), "captures": rows})


def _ts_capture_purge(arguments: dict[str, Any]) -> list[types.TextContent]:
    n = tool_capture.capture_purge(
        older_than_sec=arguments.get("older_than_sec"),
        session_id=arguments.get("session_id"),
        project_root=arguments.get("project_root"),
    )
    return _text({"deleted": n})


HANDLERS: dict[str, Any] = {
    "capture_put": _ts_capture_put,
    "capture_search": _ts_capture_search,
    "capture_get": _ts_capture_get,
    "capture_aggregate": _ts_capture_aggregate,
    "capture_list": _ts_capture_list,
    "capture_purge": _ts_capture_purge,
}
