"""Event log: build/test/deploy markers.

Lifted from memory_db.py during the memory/ subpackage split.
"""

from __future__ import annotations

import sqlite3
import sys
from typing import Any

from token_savior import memory_db
from token_savior.db_core import _json_dumps, _now_epoch, _now_iso


def event_save(
    session_id: int | None,
    type: str,
    *,
    severity: str = "info",
    data: dict[str, Any] | None = None,
    symbol: str | None = None,
    file_path: str | None = None,
) -> int | None:
    """Log a significant event (build fail, test fail, deploy, etc.)."""
    now = _now_iso()
    epoch = _now_epoch()
    try:
        conn = memory_db.get_db()
        cur = conn.execute(
            "INSERT INTO events "
            "(session_id, type, severity, data, symbol, file_path, created_at, created_at_epoch) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (session_id, type, severity, _json_dumps(data), symbol, file_path, now, epoch),
        )
        conn.commit()
        eid = cur.lastrowid
        conn.close()
        return eid
    except sqlite3.Error as exc:
        print(f"[token-savior:memory] event_save error: {exc}", file=sys.stderr)
        return None
