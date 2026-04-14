"""Index & Timeline (progressive disclosure) — scoring + LRU cache.

Lifted from memory_db.py during the memory/ subpackage split.
"""

from __future__ import annotations

import json
import sqlite3
import sys
import time
from typing import Any

from token_savior import memory_db
from token_savior.db_core import relative_age

_TYPE_SCORES = {
    "guardrail": 1.0, "ruled_out": 0.95, "convention": 0.9, "warning": 0.8,
    "command": 0.7, "infra": 0.7, "config": 0.7,
    "decision": 0.6, "bugfix": 0.5, "error_pattern": 0.5,
    "research": 0.3, "note": 0.2, "idea": 0.2,
}


def compute_obs_score(obs: dict[str, Any]) -> float:
    now = time.time()
    age_days = (now - (obs.get("created_at_epoch") or now)) / 86400
    if age_days < 1:
        recency = 1.0
    elif age_days < 7:
        recency = 0.8
    elif age_days < 30:
        recency = 0.5
    elif age_days < 90:
        recency = 0.2
    else:
        recency = 0.1

    count = obs.get("access_count") or 0
    if count == 0:
        access = 0.0
    elif count == 1:
        access = 0.3
    elif count < 5:
        access = 0.6
    else:
        access = 1.0

    type_s = _TYPE_SCORES.get(obs.get("type") or "note", 0.2)
    return round(0.4 * recency + 0.3 * access + 0.3 * type_s, 3)


def get_top_observations(
    project_root: str, limit: int = 20, sort_by: str = "score"
) -> list[dict]:
    """Classement d'obs par score LRU / access_count / âge."""
    try:
        db = memory_db.get_db()
        rows = db.execute(
            "SELECT id, type, title, symbol, context, access_count, "
            "  created_at_epoch, last_accessed_epoch, decay_immune, is_global "
            "FROM observations "
            "WHERE (project_root=? OR is_global=1) AND archived=0 "
            "ORDER BY access_count DESC, created_at_epoch DESC "
            "LIMIT ?",
            [project_root, max(limit * 3, 60)],
        ).fetchall()
        db.close()
    except sqlite3.Error as exc:
        print(f"[token-savior:memory] get_top_observations error: {exc}", file=sys.stderr)
        return []

    items = [dict(r) for r in rows]
    for r in items:
        r["score"] = compute_obs_score(r)

    if sort_by == "score":
        items.sort(key=lambda x: x["score"], reverse=True)
    elif sort_by == "access_count":
        items.sort(key=lambda x: (x["access_count"] or 0), reverse=True)
    elif sort_by == "age":
        items.sort(key=lambda x: x.get("created_at_epoch") or 0, reverse=True)
    return items[:limit]


def _ensure_memory_cache(conn) -> None:
    conn.execute(
        "CREATE TABLE IF NOT EXISTS memory_cache ("
        "  id INTEGER PRIMARY KEY AUTOINCREMENT, "
        "  cache_key TEXT UNIQUE NOT NULL, "
        "  obs_ids_ordered TEXT NOT NULL, "
        "  scores TEXT NOT NULL, "
        "  created_at_epoch INTEGER NOT NULL)"
    )
    conn.commit()


def invalidate_memory_cache(project_root: str | None = None, mode: str | None = None) -> None:
    try:
        conn = memory_db.get_db()
        _ensure_memory_cache(conn)
        if project_root and mode:
            conn.execute(
                "DELETE FROM memory_cache WHERE cache_key=?",
                [f"{project_root}:{mode}"],
            )
        elif project_root:
            conn.execute(
                "DELETE FROM memory_cache WHERE cache_key LIKE ?",
                [f"{project_root}:%"],
            )
        else:
            conn.execute("DELETE FROM memory_cache")
        conn.commit()
        conn.close()
    except sqlite3.Error:
        pass


def get_recent_index(
    project_root: str,
    *,
    limit: int = 30,
    type_filter: str | list | None = None,
    mode: str | None = None,
    include_quarantine: bool = False,
) -> list[dict]:
    """Layer 1: compact index for SessionStart injection, ordered by LRU score.

    Quarantined observations are filtered out by default; stale-suspected
    ones are annotated (``stale_suspected`` key) so the caller can prefix
    ⚠️ in the rendered index.
    """
    try:
        conn = memory_db.get_db()
        _ensure_memory_cache(conn)
        cache_key = f"{project_root}:{mode or 'default'}:{int(bool(include_quarantine))}"
        ttl = 3600

        cached = conn.execute(
            "SELECT obs_ids_ordered, scores, created_at_epoch "
            "FROM memory_cache WHERE cache_key=?",
            [cache_key],
        ).fetchone()
        cached_ids = None
        cached_scores: dict[str, Any] = {}
        if cached and (int(time.time()) - cached["created_at_epoch"] < ttl):
            try:
                cached_ids = json.loads(cached["obs_ids_ordered"])
                cached_scores = json.loads(cached["scores"])
            except Exception:
                cached_ids = None

        where = "o.archived=0 AND (o.project_root=? OR o.is_global=1)"
        params: list[Any] = [project_root]
        if type_filter:
            if isinstance(type_filter, str):
                where += " AND o.type=?"
                params.append(type_filter)
            else:
                types = list(type_filter)
                if "guardrail" not in types:
                    types.append("guardrail")
                placeholders = ",".join("?" * len(types))
                where += f" AND o.type IN ({placeholders})"
                params.extend(types)

        if not include_quarantine:
            where += " AND (c.quarantine IS NULL OR c.quarantine = 0)"

        rows = conn.execute(
            f"SELECT o.id, o.type, o.title, o.symbol, o.importance, o.relevance_score, "
            f"o.is_global, o.created_at, o.created_at_epoch, o.access_count, "
            f"o.expires_at_epoch, o.agent_id, "
            f"c.stale_suspected AS stale_suspected, c.quarantine AS quarantine "
            f"FROM observations AS o "
            f"LEFT JOIN consistency_scores AS c ON c.obs_id = o.id "
            f"WHERE {where}",
            params,
        ).fetchall()
        all_obs = [dict(r) for r in rows]
        for r in all_obs:
            r["score"] = cached_scores.get(str(r["id"])) or compute_obs_score(r)
            r["stale_suspected"] = bool(r.get("stale_suspected"))
            r["quarantine"] = bool(r.get("quarantine"))

        if cached_ids:
            order = {oid: i for i, oid in enumerate(cached_ids)}
            all_obs.sort(key=lambda o: order.get(o["id"], 10_000))
        else:
            all_obs.sort(key=lambda o: (-o["score"], -(o.get("created_at_epoch") or 0)))
            ids_ordered = [o["id"] for o in all_obs][: max(limit, 50)]
            scores_map = {str(o["id"]): o["score"] for o in all_obs}
            try:
                conn.execute(
                    "INSERT OR REPLACE INTO memory_cache "
                    "(cache_key, obs_ids_ordered, scores, created_at_epoch) "
                    "VALUES (?,?,?,?)",
                    (cache_key, json.dumps(ids_ordered),
                     json.dumps(scores_map), int(time.time())),
                )
                conn.commit()
            except sqlite3.Error:
                pass

        result = all_obs[:limit]
        conn.close()
        for r in result:
            r["age"] = relative_age(r.get("created_at_epoch"))
        return result
    except sqlite3.Error as exc:
        print(f"[token-savior:memory] get_recent_index error: {exc}", file=sys.stderr)
        return []


def get_timeline_around(
    project_root: str,
    obs_id: int,
    *,
    window_hours: int = 24,
) -> list[dict]:
    """Layer 2: chronological context around an observation."""
    try:
        conn = memory_db.get_db()
        anchor = conn.execute(
            "SELECT created_at_epoch FROM observations WHERE id=?",
            (obs_id,),
        ).fetchone()
        if anchor is None:
            conn.close()
            return []

        anchor_epoch = anchor[0]
        window_sec = window_hours * 3600
        lo = anchor_epoch - window_sec
        hi = anchor_epoch + window_sec

        obs_rows = conn.execute(
            "SELECT id, type, title, symbol, file_path, created_at, 'observation' AS kind "
            "FROM observations "
            "WHERE project_root=? AND archived=0 "
            "  AND created_at_epoch BETWEEN ? AND ? "
            "ORDER BY created_at_epoch",
            (project_root, lo, hi),
        ).fetchall()

        sum_rows = conn.execute(
            "SELECT id, 'summary' AS type, content AS title, NULL AS symbol, "
            "  NULL AS file_path, created_at, 'summary' AS kind "
            "FROM summaries "
            "WHERE project_root=? AND created_at_epoch BETWEEN ? AND ? "
            "ORDER BY created_at_epoch",
            (project_root, lo, hi),
        ).fetchall()

        combined = [dict(r) for r in obs_rows] + [dict(r) for r in sum_rows]
        combined.sort(key=lambda r: r.get("created_at", ""))
        conn.close()
        return combined
    except sqlite3.Error as exc:
        print(f"[token-savior:memory] get_timeline_around error: {exc}", file=sys.stderr)
        return []
