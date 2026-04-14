"""Memory bus: list volatile, agent-tagged observations across subagents.

Lifted from memory_db.py during the memory/ subpackage split.
The volatile *write* path (observation_save_volatile) stays in
memory_db.py until the observations subpackage split (Step 22) — bus
is read-only here.
"""

from __future__ import annotations

import sqlite3
import sys
import time
from typing import Any

from token_savior import memory_db
from token_savior.db_core import relative_age

DEFAULT_VOLATILE_TTL_DAYS = 1


def memory_bus_list(
    project_root: str,
    *,
    agent_id: str | None = None,
    limit: int = 20,
    include_expired: bool = False,
) -> list[dict]:
    """Return live bus messages for *project_root*, newest first.

    Filters on agent_id when provided. Skips expired rows by default.
    """
    try:
        conn = memory_db.get_db()
        sql = (
            "SELECT id, type, title, content, symbol, file_path, agent_id, "
            "       created_at, created_at_epoch, expires_at_epoch "
            "FROM observations "
            "WHERE archived=0 AND agent_id IS NOT NULL "
            "  AND project_root=? "
        )
        params: list[Any] = [project_root]
        if agent_id:
            sql += "AND agent_id=? "
            params.append(agent_id)
        if not include_expired:
            sql += "AND (expires_at_epoch IS NULL OR expires_at_epoch > ?) "
            params.append(int(time.time()))
        sql += "ORDER BY created_at_epoch DESC LIMIT ?"
        params.append(limit)
        rows = conn.execute(sql, params).fetchall()
        conn.close()
    except sqlite3.Error as exc:
        print(f"[token-savior:memory] memory_bus_list error: {exc}", file=sys.stderr)
        return []
    out = [dict(r) for r in rows]
    for r in out:
        r["age"] = relative_age(r.get("created_at_epoch"))
    return out
