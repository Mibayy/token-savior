"""Memory engine stats: counts by type/project/freshness.

Lifted from memory_db.py during the memory/ subpackage split.
"""

from __future__ import annotations

import sqlite3
import sys
from typing import Any

from token_savior import memory_db


def get_stats(project_root: str | None = None) -> dict[str, Any]:
    """Memory stats: counts by type, project, freshness."""
    try:
        conn = memory_db.get_db()
        where = ""
        params: list[Any] = []
        if project_root:
            where = "WHERE project_root=? AND archived=0"
            params = [project_root]
        else:
            where = "WHERE archived=0"

        total = conn.execute(f"SELECT COUNT(*) FROM observations {where}", params).fetchone()[0]

        type_rows = conn.execute(
            f"SELECT type, COUNT(*) AS cnt FROM observations {where} GROUP BY type ORDER BY cnt DESC",
            params,
        ).fetchall()

        project_rows = conn.execute(
            "SELECT project_root, COUNT(*) AS cnt FROM observations WHERE archived=0 "
            "GROUP BY project_root ORDER BY cnt DESC",
        ).fetchall()

        session_count = conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0]
        summary_count = conn.execute("SELECT COUNT(*) FROM summaries").fetchone()[0]
        event_count = conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]

        conn.close()
        return {
            "total_observations": total,
            "by_type": {r["type"]: r["cnt"] for r in type_rows},
            "by_project": {r["project_root"]: r["cnt"] for r in project_rows},
            "sessions": session_count,
            "summaries": summary_count,
            "events": event_count,
        }
    except sqlite3.Error as exc:
        print(f"[token-savior:memory] get_stats error: {exc}", file=sys.stderr)
        return {}
