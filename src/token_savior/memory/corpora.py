"""Thematic observation bundles (corpora).

Lifted from memory_db.py during the memory/ subpackage split.
"""

from __future__ import annotations

import json
import sqlite3
import sys
from typing import Any

from token_savior import memory_db
from token_savior.db_core import _json_dumps, _now_epoch, _now_iso


def corpus_build(
    project_root: str,
    name: str,
    *,
    filter_type: str | None = None,
    filter_tags: list[str] | None = None,
    filter_symbol: str | None = None,
) -> dict[str, Any]:
    """Build a corpus from observations matching the filters. Stores IDs."""
    where = ["project_root = ?", "archived = 0"]
    params: list[Any] = [project_root]
    if filter_type:
        where.append("type = ?")
        params.append(filter_type)
    if filter_symbol:
        where.append("symbol = ?")
        params.append(filter_symbol)

    sql = (
        "SELECT id, type, title, tags FROM observations "
        f"WHERE {' AND '.join(where)} "
        "ORDER BY created_at_epoch DESC"
    )
    try:
        conn = memory_db.get_db()
        rows = [dict(r) for r in conn.execute(sql, params).fetchall()]

        if filter_tags:
            wanted = set(filter_tags)
            filtered = []
            for r in rows:
                tags = []
                try:
                    tags = json.loads(r.get("tags") or "[]") or []
                except Exception:
                    tags = []
                if wanted & set(tags):
                    filtered.append(r)
            rows = filtered

        ids = [r["id"] for r in rows]
        type_counts: dict[str, int] = {}
        for r in rows:
            type_counts[r["type"]] = type_counts.get(r["type"], 0) + 1

        now = _now_iso()
        epoch = _now_epoch()
        conn.execute(
            "INSERT INTO corpora (project_root, name, filter_type, filter_tags, "
            "filter_symbol, observation_ids, created_at, created_at_epoch) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(project_root, name) DO UPDATE SET "
            "filter_type=excluded.filter_type, filter_tags=excluded.filter_tags, "
            "filter_symbol=excluded.filter_symbol, observation_ids=excluded.observation_ids, "
            "created_at=excluded.created_at, created_at_epoch=excluded.created_at_epoch",
            (
                project_root,
                name,
                filter_type,
                _json_dumps(filter_tags),
                filter_symbol,
                json.dumps(ids),
                now,
                epoch,
            ),
        )
        conn.commit()
        conn.close()
        return {
            "name": name,
            "count": len(ids),
            "observation_ids": ids,
            "type_counts": type_counts,
            "preview": [r["title"] for r in rows[:3]],
            "created_at": now,
        }
    except sqlite3.Error as exc:
        print(f"[token-savior:memory] corpus_build error: {exc}", file=sys.stderr)
        return {"name": name, "count": 0, "observation_ids": [], "type_counts": {}, "preview": []}


def corpus_get(project_root: str, name: str) -> dict[str, Any] | None:
    """Fetch corpus metadata + observation rows."""
    try:
        conn = memory_db.get_db()
        row = conn.execute(
            "SELECT * FROM corpora WHERE project_root=? AND name=?",
            (project_root, name),
        ).fetchone()
        if not row:
            conn.close()
            return None
        ids = json.loads(row["observation_ids"] or "[]")
        if not ids:
            conn.close()
            return {"corpus": dict(row), "observations": []}
        placeholders = ",".join("?" * len(ids))
        obs = conn.execute(
            f"SELECT id, type, title, content, why, how_to_apply, symbol, file_path, "
            f"tags, importance, created_at FROM observations WHERE id IN ({placeholders}) "
            f"AND archived=0 ORDER BY created_at_epoch DESC",
            ids,
        ).fetchall()
        conn.close()
        return {"corpus": dict(row), "observations": [dict(o) for o in obs]}
    except sqlite3.Error as exc:
        print(f"[token-savior:memory] corpus_get error: {exc}", file=sys.stderr)
        return None
