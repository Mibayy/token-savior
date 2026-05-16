"""Per-tool latency instrumentation.

Records every dispatched MCP tool call into a ``tool_latency`` table in
the shared memory.db so we can compute real p50/p95 latencies per tool.

Design:
- One process-local sqlite connection, opened lazily on first ``record``.
- ``CREATE TABLE IF NOT EXISTS`` on first write — no migration needed.
- WAL + ``synchronous=NORMAL`` for cheap (<1ms) per-call inserts.
- Silent on every failure: telemetry must never break dispatch.
- ``check_same_thread=False`` because the MCP server runs asyncio tasks
  and may dispatch from a worker thread.
"""

from __future__ import annotations

import sqlite3
import threading
import time
from pathlib import Path
from typing import Optional

from token_savior.db_core import MEMORY_DB_PATH

_CREATE_SQL = """
CREATE TABLE IF NOT EXISTS tool_latency (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts INTEGER NOT NULL,
    tool TEXT NOT NULL,
    project TEXT,
    duration_ms INTEGER NOT NULL,
    status TEXT NOT NULL,
    error_type TEXT
)
"""

_INDEX_SQL = (
    "CREATE INDEX IF NOT EXISTS idx_tool_latency_tool_ts "
    "ON tool_latency(tool, ts)"
)

_INSERT_SQL = (
    "INSERT INTO tool_latency (ts, tool, project, duration_ms, status, error_type) "
    "VALUES (?, ?, ?, ?, ?, ?)"
)

_conn: Optional[sqlite3.Connection] = None
_lock = threading.Lock()
_disabled = False


def _open_conn(db_path: Optional[Path] = None) -> sqlite3.Connection:
    path = db_path or MEMORY_DB_PATH
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(
        str(path),
        check_same_thread=False,
        isolation_level=None,  # autocommit; one INSERT per call
        timeout=1.0,
    )
    # WAL is set globally by run_migrations(); idempotent here.
    try:
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA synchronous = NORMAL")
    except sqlite3.OperationalError:
        pass
    conn.execute(_CREATE_SQL)
    conn.execute(_INDEX_SQL)
    return conn


def _get_conn() -> Optional[sqlite3.Connection]:
    global _conn, _disabled
    if _disabled:
        return None
    if _conn is not None:
        return _conn
    with _lock:
        if _conn is not None:
            return _conn
        try:
            _conn = _open_conn()
        except Exception:
            _disabled = True
            return None
        return _conn


def record(
    tool: str,
    project: Optional[str],
    duration_ms: int,
    status: str,
    error_type: Optional[str] = None,
    ts: Optional[int] = None,
) -> None:
    """Persist a single latency sample. Never raises."""
    conn = _get_conn()
    if conn is None:
        return
    try:
        conn.execute(
            _INSERT_SQL,
            (
                int(ts if ts is not None else time.time()),
                tool,
                project,
                int(duration_ms),
                status,
                error_type,
            ),
        )
    except Exception:
        # Silent — telemetry must never break dispatch.
        pass


def reset_for_tests() -> None:
    """Close the cached connection. Used by tests that patch MEMORY_DB_PATH."""
    global _conn, _disabled
    with _lock:
        if _conn is not None:
            try:
                _conn.close()
            except Exception:
                pass
        _conn = None
        _disabled = False
