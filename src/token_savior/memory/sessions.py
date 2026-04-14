"""Session lifecycle: open/close rows in the sessions table.

Lifted from memory_db.py during the memory/ subpackage split.
"""

from __future__ import annotations

import sqlite3
import sys

from token_savior import memory_db
from token_savior.db_core import _json_dumps, _now_epoch, _now_iso


def session_start(project_root: str) -> int:
    """Create a new active session. Returns the session id."""
    now = _now_iso()
    epoch = _now_epoch()
    try:
        with memory_db.db_session() as conn:
            cur = conn.execute(
                "INSERT INTO sessions (project_root, status, created_at, created_at_epoch) "
                "VALUES (?, 'active', ?, ?)",
                (project_root, now, epoch),
            )
            conn.commit()
            return cur.lastrowid  # type: ignore[return-value]
    except sqlite3.Error as exc:
        print(f"[token-savior:memory] session_start error: {exc}", file=sys.stderr)
        raise


def session_end(
    session_id: int,
    summary: str | None = None,
    symbols_changed: list[str] | None = None,
    files_changed: list[str] | None = None,
    end_type: str = "completed",
) -> None:
    """Mark a session as closed with optional summary data.

    end_type: "completed" (clean SessionEnd) or "interrupted" (Stop hook).
    The status column stays "completed" (satisfies existing CHECK constraint);
    end_type carries the distinction.
    """
    now = _now_iso()
    epoch = _now_epoch()
    try:
        with memory_db.db_session() as conn:
            conn.execute(
                "UPDATE sessions SET status='completed', end_type=?, completed_at=?, completed_at_epoch=?, "
                "summary=?, symbols_changed=?, files_changed=? WHERE id=?",
                (end_type, now, epoch, summary, _json_dumps(symbols_changed), _json_dumps(files_changed), session_id),
            )
            conn.commit()
    except sqlite3.Error as exc:
        print(f"[token-savior:memory] session_end error: {exc}", file=sys.stderr)
