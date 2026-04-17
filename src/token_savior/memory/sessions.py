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
    *,
    request: str | None = None,
    investigated: str | None = None,
    learned: str | None = None,
    completed: str | None = None,
    next_steps: str | None = None,
    notes: str | None = None,
) -> None:
    """Mark a session as closed with optional summary data.

    end_type: "completed" (clean SessionEnd) or "interrupted" (Stop hook).
    The status column stays "completed" (satisfies existing CHECK constraint);
    end_type carries the distinction.

    If any of the structured rollup kwargs (``request``, ``investigated``,
    ``learned``, ``completed``, ``next_steps``, ``notes``) is non-empty, a
    row is inserted into ``session_summaries`` so memory_search / the
    history tool can surface past sessions.
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

            rollup_fields = (request, investigated, learned, completed, next_steps, notes)
            if any(f and f.strip() for f in rollup_fields):
                row = conn.execute(
                    "SELECT project_root FROM sessions WHERE id=?", (session_id,)
                ).fetchone()
                project_root = row["project_root"] if row else ""
                conn.execute(
                    "INSERT INTO session_summaries "
                    "(session_id, project_root, request, investigated, learned, "
                    " completed, next_steps, notes, created_at, created_at_epoch) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (session_id, project_root, request, investigated, learned,
                     completed, next_steps, notes, now, epoch),
                )

            conn.commit()
    except sqlite3.Error as exc:
        print(f"[token-savior:memory] session_end error: {exc}", file=sys.stderr)
_SUMMARY_TEXT_FIELDS = (
    "request", "investigated", "learned", "completed", "next_steps", "notes",
)


def session_summary_search(
    project_root: str,
    query: str,
    *,
    limit: int = 10,
) -> list[dict]:
    """FTS5 search over session_summaries. Returns rollup rows with age + excerpt."""
    if not (query or "").strip():
        return []
    try:
        with memory_db.db_session() as conn:
            rows = conn.execute(
                "SELECT s.id, s.session_id, s.project_root, "
                "  s.request, s.investigated, s.learned, s.completed, "
                "  s.next_steps, s.notes, s.created_at, s.created_at_epoch, "
                "  snippet(session_summaries_fts, -1, '»', '«', '...', 30) AS excerpt "
                "FROM session_summaries_fts AS f "
                "JOIN session_summaries AS s ON s.id = f.rowid "
                "WHERE session_summaries_fts MATCH ? "
                "  AND (s.project_root = ? OR s.project_root = '') "
                "ORDER BY rank LIMIT ?",
                [query, project_root, limit],
            ).fetchall()
    except sqlite3.Error as exc:
        print(f"[token-savior:memory] session_summary_search error: {exc}", file=sys.stderr)
        return []
    from token_savior.db_core import relative_age
    result = [dict(r) for r in rows]
    for r in result:
        r["age"] = relative_age(r.get("created_at_epoch"))
    return result


def session_summary_list(
    project_root: str | None = None,
    *,
    limit: int = 10,
) -> list[dict]:
    """Most recent session rollups. Omit ``project_root`` to span projects."""
    params: list = []
    where = ""
    if project_root:
        where = "WHERE project_root = ?"
        params.append(project_root)
    params.append(limit)
    try:
        with memory_db.db_session() as conn:
            rows = conn.execute(
                "SELECT id, session_id, project_root, request, investigated, "
                "  learned, completed, next_steps, notes, created_at, created_at_epoch "
                f"FROM session_summaries {where} "
                "ORDER BY created_at_epoch DESC LIMIT ?",
                params,
            ).fetchall()
    except sqlite3.Error as exc:
        print(f"[token-savior:memory] session_summary_list error: {exc}", file=sys.stderr)
        return []
    from token_savior.db_core import relative_age
    result = [dict(r) for r in rows]
    for r in result:
        r["age"] = relative_age(r.get("created_at_epoch"))
    return result
