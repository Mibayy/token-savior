"""Async Node subprocess sandbox for Code Mode ts_execute."""
from __future__ import annotations

import asyncio
import base64
import json
import os
from pathlib import Path
from typing import Any, Callable

_WORKER_PATH = Path(__file__).parent / "worker.mjs"
_NODE_BIN = os.environ.get("TS_CODE_MODE_NODE", "node")


async def run_script_async(
    script: str,
    allowed_tools: list[str],
    dispatch: Callable[[str, dict], Any],
    timeout_ms: int = 30000,
    max_log_chars: int = 8000,
) -> dict:
    """Run a user JS script in a Node sandbox.

    The script body is wrapped as `(async () => { <body> })()`. It can `await
    tools.<name>(args)` for any tool in `allowed_tools`. Each tool call is
    dispatched back to Python via `dispatch(tool_name, args)` synchronously.

    Returns: {"value": <any>, "logs": [...], "error": {"message", "stack"} | None,
              "tool_calls": int, "duration_ms": int}.
    """
    script_b64 = base64.b64encode(script.encode("utf-8")).decode("ascii")
    tools_json = json.dumps(allowed_tools)

    proc = await asyncio.create_subprocess_exec(
        _NODE_BIN,
        str(_WORKER_PATH),
        tools_json,
        script_b64,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env={**os.environ, "NODE_OPTIONS": "--no-warnings"},
    )

    logs: list[str] = []
    final_value: Any = None
    error: dict | None = None
    tool_calls = 0
    allowed_set = set(allowed_tools)
    started = asyncio.get_event_loop().time()

    def _trim_logs(new_entry: str) -> None:
        logs.append(new_entry)
        total = sum(len(s) for s in logs)
        if total > max_log_chars:
            while logs and total > max_log_chars:
                dropped = logs.pop(0)
                total -= len(dropped)
            logs.insert(0, "[...truncated...]")

    async def reader() -> None:
        nonlocal final_value, error, tool_calls
        while True:
            line_b = await proc.stdout.readline()
            if not line_b:
                return
            line = line_b.decode("utf-8", errors="replace").strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
            except json.JSONDecodeError:
                _trim_logs(f"[malformed]: {line[:200]}")
                continue
            t = msg.get("type")
            if t == "call":
                tool_calls += 1
                tool = msg.get("tool", "")
                args = msg.get("args", {}) or {}
                call_id = msg.get("id")
                if tool not in allowed_set:
                    resp = {
                        "type": "error",
                        "id": call_id,
                        "error": f"tool '{tool}' not in code-mode allowlist",
                    }
                else:
                    try:
                        result = await asyncio.to_thread(dispatch, tool, args)
                        resp = {"type": "result", "id": call_id, "value": result}
                    except Exception as e:
                        resp = {"type": "error", "id": call_id, "error": f"{type(e).__name__}: {e}"}
                proc.stdin.write((json.dumps(resp) + "\n").encode("utf-8"))
                await proc.stdin.drain()
            elif t == "log":
                level = msg.get("level", "info")
                _trim_logs(f"[{level}] {msg.get('value', '')}")
            elif t == "final":
                final_value = msg.get("value")
                return
            elif t == "error":
                error = {
                    "message": msg.get("message", ""),
                    "stack": msg.get("stack", ""),
                }
                return

    try:
        await asyncio.wait_for(reader(), timeout=timeout_ms / 1000.0)
    except asyncio.TimeoutError:
        error = {"message": f"script timeout after {timeout_ms}ms", "stack": ""}
    finally:
        if proc.returncode is None:
            try:
                proc.kill()
            except ProcessLookupError:
                pass
            try:
                await asyncio.wait_for(proc.wait(), timeout=2)
            except asyncio.TimeoutError:
                pass

    duration_ms = int((asyncio.get_event_loop().time() - started) * 1000)

    return {
        "value": final_value,
        "logs": logs,
        "error": error,
        "tool_calls": tool_calls,
        "duration_ms": duration_ms,
    }
