"""Persistent per-tool call counter scoped by MCP client (A5).

Motivation — AUDIT.md Phase 2b and Phase 4 A5: we have no data to back
"which 14 tools are actually hot" when we flip the default profile in
v3.0. Session-scoped counters in ``get_stats`` evaporate when the
server stops, so decisions fall on author intuition (``_ULTRA_INCLUDES``).

This module adds a JSON-file counter at
``$TOKEN_SAVIOR_STATS_DIR/tool-calls.json``, bumped on every tool call.
The counter is scoped by ``TOKEN_SAVIOR_CLIENT`` (`claude-code`,
`cursor`, `cline`, …) so we can spot divergence between clients when
trimming the default profile.

Format (stable across versions; bump ``schema_version`` on breaking
changes):

    {
      "schema_version": 1,
      "last_updated_epoch": 1729701234,
      "counts": {
        "claude-code": {"find_symbol": 1234, "get_function_source": 567},
        "cursor":      {"find_symbol":   45}
      }
    }

The file is written with a temp-then-rename so the JSON on disk is
always valid even under crashes. Writes happen on every call; the cost
is a single ~5 KB rewrite per call (~1 ms on SSD) which is negligible
against tool-call latencies measured in hundreds of ms.

Failures (disk full, permission denied, JSON corruption from an older
version) are swallowed silently — telemetry must never break the
server. Errors surface via ``telemetry_health()`` for a future
``memory_doctor`` probe.
"""
from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path

_SCHEMA_VERSION = 1


def _stats_dir() -> Path:
    raw = os.environ.get("TOKEN_SAVIOR_STATS_DIR", "~/.local/share/token-savior")
    return Path(os.path.expanduser(raw))


def _counter_path() -> Path:
    return _stats_dir() / "tool-calls.json"


def _resolve_client() -> str:
    """Client id from env, defaulting to ``unknown``.

    The MCP server registry advertises ``TOKEN_SAVIOR_CLIENT`` as the
    canonical variable (see server.json). Empty strings count as
    unknown so a ``TOKEN_SAVIOR_CLIENT=`` in a broken .env doesn't
    silently pin every call to "".
    """
    raw = os.environ.get("TOKEN_SAVIOR_CLIENT", "").strip()
    return raw or "unknown"


# ── in-process cache ───────────────────────────────────────────────────────
#
# We do NOT re-read the JSON on every bump — that would make the counter
# N² over the session. The cache is loaded once lazily and flushed on
# every bump via atomic rename. Multi-process concurrent writes (two
# servers sharing a stats dir) would lose increments to each other; this
# is acceptable for the v2.8/v3.0 goal of "is this tool ever called".
#
_lock = threading.Lock()
_state: dict | None = None
_last_error: str | None = None


def _load() -> dict:
    global _last_error
    path = _counter_path()
    try:
        if not path.exists():
            return {
                "schema_version": _SCHEMA_VERSION,
                "last_updated_epoch": int(time.time()),
                "counts": {},
            }
        with path.open(encoding="utf-8") as f:
            data = json.load(f)
        if data.get("schema_version") != _SCHEMA_VERSION:
            # Incompatible schema: start fresh but keep the old file as
            # a .bak so a human can recover if needed.
            try:
                path.rename(path.with_suffix(path.suffix + ".bak"))
            except OSError:
                pass
            return {
                "schema_version": _SCHEMA_VERSION,
                "last_updated_epoch": int(time.time()),
                "counts": {},
            }
        counts = data.get("counts")
        if not isinstance(counts, dict):
            data["counts"] = {}
        return data
    except (OSError, json.JSONDecodeError) as e:
        _last_error = f"load: {type(e).__name__}: {e}"
        return {
            "schema_version": _SCHEMA_VERSION,
            "last_updated_epoch": int(time.time()),
            "counts": {},
        }


def _save(data: dict) -> None:
    global _last_error
    path = _counter_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        with tmp.open("w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, sort_keys=True)
        tmp.replace(path)
        _last_error = None
    except OSError as e:
        _last_error = f"save: {type(e).__name__}: {e}"


# ── public API ─────────────────────────────────────────────────────────────


def record_tool_call(tool_name: str) -> None:
    """Increment the counter for ``(tool_name, client)``.

    Never raises. Call on every successful tool invocation. Failure to
    persist is recorded in :func:`telemetry_health` but never propagates
    — we absolutely must not crash the tool-dispatch path for a
    telemetry write.
    """
    if not tool_name:
        return
    client = _resolve_client()
    global _state
    with _lock:
        if _state is None:
            _state = _load()
        bucket = _state["counts"].setdefault(client, {})
        bucket[tool_name] = bucket.get(tool_name, 0) + 1
        _state["last_updated_epoch"] = int(time.time())
        _save(_state)


def telemetry_health() -> dict:
    """Snapshot of the counter for a future ``memory_doctor`` section.

    Returns ``{"ok": bool, "path": str, "clients": int, "distinct_tools":
    int, "error": str | None}``.
    """
    global _state
    with _lock:
        if _state is None:
            _state = _load()
        counts = _state.get("counts", {})
        clients = list(counts.keys())
        distinct_tools = len({
            tool for bucket in counts.values() for tool in bucket
        })
        return {
            "ok": _last_error is None,
            "path": str(_counter_path()),
            "clients": len(clients),
            "distinct_tools": distinct_tools,
            "error": _last_error,
        }


def reset_for_tests() -> None:
    """Only for tests: clear in-process cache so _load() re-runs."""
    global _state, _last_error
    with _lock:
        _state = None
        _last_error = None
