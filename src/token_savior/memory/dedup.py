"""Cross-project / semantic dedup checks + injection stats.

Lifted from memory_db.py during the memory/ subpackage split.
"""

from __future__ import annotations

import sqlite3
import sys
from typing import Any

from token_savior import memory_db
from token_savior.memory._text_utils import _jaccard


def global_dedup_check(
    title: str, content: str, obs_type: str, threshold: float = 0.85
) -> dict[str, Any] | None:
    """Cross-project dedup for globals. Returns best global match (content_hash or Jaccard)."""
    try:
        db = memory_db.get_db()
        rows = db.execute(
            "SELECT id, title, content, type, project_root, content_hash "
            "FROM observations WHERE archived=0 AND is_global=1 AND type=?",
            [obs_type],
        ).fetchall()
        db.close()
    except sqlite3.Error:
        return None
    chash = memory_db.content_hash(content)
    best = None
    best_score = 0.0
    for r in rows:
        if chash and r["content_hash"] and r["content_hash"] == chash:
            return {
                "id": r["id"], "title": r["title"], "type": r["type"],
                "project_root": r["project_root"], "score": 1.0, "reason": "content_hash",
            }
        score = _jaccard(title, r["title"])
        if score >= threshold and score > best_score:
            best_score = score
            best = {
                "id": r["id"], "title": r["title"], "type": r["type"],
                "project_root": r["project_root"], "score": round(score, 2),
                "reason": "jaccard",
            }
    return best


def semantic_dedup_check(
    project_root: str, title: str, obs_type: str, threshold: float = 0.85
) -> dict[str, Any] | None:
    """Return best near-duplicate (same type) if Jaccard(title) >= threshold."""
    try:
        db = memory_db.get_db()
        rows = db.execute(
            "SELECT id, title, type FROM observations "
            "WHERE project_root=? AND archived=0 AND type=?",
            [project_root, obs_type],
        ).fetchall()
        db.close()
    except sqlite3.Error:
        return None
    best = None
    best_score = 0.0
    for r in rows:
        score = _jaccard(title, r["title"])
        if score >= threshold and score > best_score:
            best_score = score
            best = {
                "id": r["id"], "title": r["title"], "type": r["type"],
                "score": round(score, 2),
            }
    return best


def get_injection_stats(project_root: str) -> dict[str, Any]:
    try:
        db = memory_db.get_db()
        row = db.execute(
            "SELECT COUNT(*) AS sessions, "
            "  COALESCE(SUM(tokens_injected), 0) AS total_injected, "
            "  COALESCE(SUM(tokens_saved_est), 0) AS total_saved_est, "
            "  COALESCE(AVG(tokens_injected), 0) AS avg_injected, "
            "  COALESCE(AVG(tokens_saved_est), 0) AS avg_saved "
            "FROM sessions WHERE project_root=? AND tokens_injected > 0",
            [project_root],
        ).fetchone()
        db.close()
        d = dict(row) if row else {
            "sessions": 0, "total_injected": 0, "total_saved_est": 0,
            "avg_injected": 0, "avg_saved": 0,
        }
        ratio = (d["total_saved_est"] / d["total_injected"]) if d["total_injected"] else 0
        d["roi_ratio"] = round(ratio, 2)
        d["avg_injected"] = int(d["avg_injected"] or 0)
        d["avg_saved"] = int(d["avg_saved"] or 0)
        return d
    except sqlite3.Error as exc:
        print(f"[token-savior:memory] get_injection_stats error: {exc}", file=sys.stderr)
        return {"sessions": 0, "total_injected": 0, "total_saved_est": 0,
                "avg_injected": 0, "avg_saved": 0, "roi_ratio": 0}


def dedup_sweep(
    project_root: str | None = None,
    recompute: bool = False,
    batch_size: int = 500,
) -> dict[str, Any]:
    """Backfill ``observations.content_hash`` for rows missing the dedup key.

    Parameters
    ----------
    project_root : str | None
        If provided, only sweep rows belonging to that project.
    recompute : bool
        If True, rehash every row (not just NULL). Use after a hash-formula
        change invalidated existing hashes — the default keeps it a cheap,
        idempotent backfill.
    batch_size : int
        Commit cadence; keeps the transaction bounded on large DBs.

    Returns a summary dict: ``{scanned, updated, collisions_merged, archived}``.
    ``collisions_merged`` counts rows whose recomputed hash collides with an
    already-hashed row in the same project — those are archived (not deleted)
    so the user can inspect them via ``memory_archived``.
    """
    stats = {"scanned": 0, "updated": 0, "collisions_merged": 0, "archived": 0}
    try:
        conn = memory_db.get_db()
    except sqlite3.Error as exc:
        print(f"[token-savior:memory] dedup_sweep connect error: {exc}", file=sys.stderr)
        return stats
    try:
        where = ["archived = 0"]
        params: list[Any] = []
        if not recompute:
            where.append("(content_hash IS NULL OR content_hash = '')")
        if project_root:
            where.append("project_root = ?")
            params.append(project_root)
        sql = (
            "SELECT id, project_root, content, content_hash "
            "FROM observations WHERE " + " AND ".join(where)
        )
        rows = conn.execute(sql, params).fetchall()
        stats["scanned"] = len(rows)

        pending = 0
        for r in rows:
            new_hash = memory_db.content_hash(r["content"] or "")
            if new_hash is None:
                continue
            if r["content_hash"] == new_hash:
                continue
            existing = conn.execute(
                "SELECT id FROM observations "
                "WHERE content_hash = ? AND project_root = ? AND archived = 0 AND id != ?",
                (new_hash, r["project_root"], r["id"]),
            ).fetchone()
            if existing is not None:
                conn.execute(
                    "UPDATE observations SET archived = 1, updated_at = ? WHERE id = ?",
                    (memory_db._now_iso(), r["id"]),
                )
                stats["collisions_merged"] += 1
                stats["archived"] += 1
            else:
                conn.execute(
                    "UPDATE observations SET content_hash = ?, updated_at = ? WHERE id = ?",
                    (new_hash, memory_db._now_iso(), r["id"]),
                )
                stats["updated"] += 1
            pending += 1
            if pending >= batch_size:
                conn.commit()
                pending = 0
        conn.commit()
    except sqlite3.Error as exc:
        print(f"[token-savior:memory] dedup_sweep error: {exc}", file=sys.stderr)
    finally:
        conn.close()
    return stats
