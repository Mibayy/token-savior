"""Observation links: auto-link, promotions, explain, related lookups.

Lifted from memory_db.py during the memory/ subpackage split.
"""

from __future__ import annotations

import json
import sqlite3
import sys
import time
from typing import Any

from token_savior import memory_db
from token_savior.db_core import _now_iso

_PROMOTION_TYPE_RANK = {
    "note": 1, "bugfix": 2, "decision": 2,
    "warning": 3, "convention": 4, "guardrail": 5,
}
_PROMOTION_RULES = [
    ("note", 5, "convention"),
    ("note", 10, "guardrail"),
    ("bugfix", 5, "convention"),
    ("warning", 5, "guardrail"),
    ("decision", 3, "convention"),
]

_TYPE_PRIORITY = {
    "guardrail": "critical", "convention": "high", "warning": "high",
    "command": "medium", "decision": "medium", "infra": "medium",
    "config": "medium", "bugfix": "low", "note": "low",
    "research": "low", "idea": "low", "error_pattern": "high",
}


def _ensure_links_index(conn) -> None:
    try:
        conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_links_unique "
            "ON observation_links(source_id, target_id, link_type)"
        )
        conn.commit()
    except sqlite3.Error:
        pass


def auto_link_observation(
    new_obs_id: int,
    project_root: str,
    contradict_ids: list[int] | None = None,
) -> int:
    """Create 'related' links with obs sharing symbol/context/tags, and
    'contradicts' links for any ids in contradict_ids."""
    linked = 0
    try:
        with memory_db.db_session() as db:
            _ensure_links_index(db)
            new_obs = db.execute(
                "SELECT symbol, context, tags FROM observations WHERE id=?",
                [new_obs_id],
            ).fetchone()
            if not new_obs:
                return 0

            candidates: set[int] = set()
            if new_obs["symbol"]:
                rows = db.execute(
                    "SELECT id FROM observations "
                    "WHERE symbol=? AND id!=? AND project_root=? AND archived=0",
                    [new_obs["symbol"], new_obs_id, project_root],
                ).fetchall()
                candidates.update(r["id"] for r in rows)

            if new_obs["context"]:
                ctx_keyword = new_obs["context"][:20]
                if ctx_keyword:
                    rows = db.execute(
                        "SELECT id FROM observations "
                        "WHERE context LIKE ? AND id!=? AND project_root=? AND archived=0",
                        [f"%{ctx_keyword}%", new_obs_id, project_root],
                    ).fetchall()
                    candidates.update(r["id"] for r in rows)

            if new_obs["tags"]:
                try:
                    new_tags = set(json.loads(new_obs["tags"]))
                    if new_tags:
                        rows = db.execute(
                            "SELECT id, tags FROM observations "
                            "WHERE id!=? AND project_root=? AND archived=0 AND tags IS NOT NULL",
                            [new_obs_id, project_root],
                        ).fetchall()
                        for r in rows:
                            try:
                                existing = set(json.loads(r["tags"]))
                                if new_tags & existing:
                                    candidates.add(r["id"])
                            except Exception:
                                pass
                except Exception:
                    pass

            now_iso = _now_iso()

            for other_id in candidates:
                a, b = min(new_obs_id, other_id), max(new_obs_id, other_id)
                try:
                    cur = db.execute(
                        "INSERT OR IGNORE INTO observation_links "
                        "(source_id, target_id, link_type, auto_detected, created_at) "
                        "VALUES (?, ?, 'related', 1, ?)",
                        (a, b, now_iso),
                    )
                    if cur.rowcount > 0:
                        linked += 1
                except sqlite3.Error:
                    pass

            for cid in (contradict_ids or []):
                if cid == new_obs_id:
                    continue
                a, b = min(new_obs_id, cid), max(new_obs_id, cid)
                try:
                    cur = db.execute(
                        "INSERT OR IGNORE INTO observation_links "
                        "(source_id, target_id, link_type, auto_detected, created_at) "
                        "VALUES (?, ?, 'contradicts', 1, ?)",
                        (a, b, now_iso),
                    )
                    if cur.rowcount > 0:
                        linked += 1
                except sqlite3.Error:
                    pass

            db.commit()
    except sqlite3.Error as exc:
        print(f"[token-savior:memory] auto_link_observation error: {exc}", file=sys.stderr)
    return linked


def explain_observation(obs_id: int, query: str | None = None) -> dict[str, Any]:
    """Trace why an observation would appear in results."""
    try:
        db = memory_db.get_db()
        obs = db.execute("SELECT * FROM observations WHERE id=?", [obs_id]).fetchone()
        if not obs:
            db.close()
            return {"error": f"Observation #{obs_id} not found"}
        obs = dict(obs)

        reasons: list[str] = []
        breakdown: dict[str, Any] = {}

        age_sec = int(time.time()) - int(obs.get("created_at_epoch") or 0)
        age_days = age_sec / 86400 if age_sec > 0 else 0
        if age_days < 1:
            reasons.append(f"📅 Very recent (created {int(age_days*24)}h ago)")
            breakdown["recency"] = "high"
        elif age_days < 7:
            reasons.append(f"📅 Recent ({int(age_days)}d ago)")
            breakdown["recency"] = "medium"
        else:
            reasons.append(f"📅 Age: {int(age_days)}d ago")
            breakdown["recency"] = "low"

        ac = obs.get("access_count") or 0
        if ac > 0:
            reasons.append(f"👁 Accessed {ac} times")
            if ac >= 5:
                reasons.append("⬆️ Promotion-eligible (high access count)")
            breakdown["access"] = ac

        if obs.get("symbol"):
            reasons.append(f"⚙️ Symbol link: {obs['symbol']}")
            breakdown["symbol"] = obs["symbol"]
        if obs.get("file_path"):
            reasons.append(f"📄 File: {obs['file_path']}")
            breakdown["file"] = obs["file_path"]
        if obs.get("context"):
            reasons.append(f"🔗 Context: {obs['context']}")
            breakdown["context"] = obs["context"]

        prio = _TYPE_PRIORITY.get(obs.get("type", ""), "low")
        reasons.append(f"🏷 Type [{obs['type']}] priority: {prio}")
        breakdown["type_priority"] = prio

        if obs.get("is_global"):
            reasons.append("🌐 Global observation")
            breakdown["global"] = True
        if obs.get("decay_immune"):
            reasons.append("🛡 Decay-immune")
            breakdown["decay_immune"] = True

        if obs.get("tags"):
            try:
                tg = json.loads(obs["tags"])
                if tg:
                    reasons.append(f"🏷 Tags: {', '.join(tg)}")
                    breakdown["tags"] = tg
            except Exception:
                pass

        try:
            links = get_linked_observations(obs_id)
            if links.get("related"):
                reasons.append(f"🔗 {len(links['related'])} related obs")
                breakdown["related_count"] = len(links["related"])
            if links.get("contradicts"):
                reasons.append(f"⚠️ Contradicts {len(links['contradicts'])} obs")
                breakdown["contradicts_count"] = len(links["contradicts"])
        except Exception:
            pass

        if query:
            try:
                row = db.execute(
                    "SELECT snippet(observations_fts, 1, '**', '**', '...', 10) "
                    "FROM observations_fts WHERE observations_fts MATCH ? AND rowid=?",
                    [query, obs_id],
                ).fetchone()
                if row and row[0]:
                    reasons.append(f"🔍 FTS5 match: {row[0]}")
                    breakdown["fts_match"] = True
            except sqlite3.Error:
                pass

        db.close()
        return {
            "obs_id": obs_id,
            "title": obs["title"],
            "type": obs["type"],
            "reasons": reasons,
            "score_breakdown": breakdown,
        }
    except sqlite3.Error as exc:
        return {"error": str(exc)}


def relink_all(project_root: str, dry_run: bool = False) -> dict[str, Any]:
    """Replay auto_link_observation() over all active obs to backfill links."""
    db = memory_db.get_db()
    obs_ids = [
        r["id"] for r in db.execute(
            "SELECT id FROM observations WHERE project_root=? AND archived=0 ORDER BY id",
            [project_root],
        ).fetchall()
    ]
    before = db.execute("SELECT COUNT(*) FROM observation_links").fetchone()[0]
    db.close()

    total_links = 0
    processed = 0
    for oid in obs_ids:
        processed += 1
        if dry_run:
            continue
        try:
            total_links += auto_link_observation(oid, project_root)
        except Exception:
            pass

    db = memory_db.get_db()
    after = db.execute("SELECT COUNT(*) FROM observation_links").fetchone()[0]
    db.close()
    return {
        "processed": processed,
        "links_created": total_links,
        "total_links_in_db": after,
        "delta": after - before,
        "dry_run": dry_run,
    }


def get_linked_observations(obs_id: int) -> dict[str, Any]:
    """Return related/contradicts/supersedes links for an obs."""
    out: dict[str, Any] = {"related": [], "contradicts": [], "supersedes": []}
    try:
        db = memory_db.get_db()
        rows = db.execute(
            "SELECT l.link_type, "
            "  CASE WHEN l.source_id=? THEN l.target_id ELSE l.source_id END AS linked_id, "
            "  o.type, o.title, o.symbol, o.context "
            "FROM observation_links l "
            "JOIN observations o ON o.id = "
            "  CASE WHEN l.source_id=? THEN l.target_id ELSE l.source_id END "
            "WHERE (l.source_id=? OR l.target_id=?) AND o.archived=0 "
            "ORDER BY l.link_type, l.created_at DESC",
            (obs_id, obs_id, obs_id, obs_id),
        ).fetchall()
        db.close()
        for r in rows:
            bucket = r["link_type"] if r["link_type"] in out else "related"
            out[bucket].append({
                "id": r["linked_id"],
                "type": r["type"],
                "title": r["title"],
                "symbol": r["symbol"],
                "context": r["context"],
            })
    except sqlite3.Error as exc:
        print(f"[token-savior:memory] get_linked_observations error: {exc}", file=sys.stderr)
    return out


def run_promotions(project_root: str = "", dry_run: bool = False) -> dict[str, Any]:
    """Promote frequently-accessed observations to stronger types.

    Empty project_root = scan all projects.
    """
    now = int(time.time())
    recent_cutoff = now - 30 * 86400
    promoted: list[dict] = []
    try:
        db = memory_db.get_db()
        for current_type, min_count, new_type in _PROMOTION_RULES:
            sql = (
                "SELECT id, title, type, access_count, project_root "
                "FROM observations "
                "WHERE type=? AND access_count >= ? AND archived=0 AND decay_immune=0 "
                "  AND last_accessed_epoch IS NOT NULL AND last_accessed_epoch > ? "
            )
            params: list[Any] = [current_type, min_count, recent_cutoff]
            if project_root:
                sql += "AND project_root=? "
                params.append(project_root)
            sql += "ORDER BY access_count DESC"
            rows = db.execute(sql, params).fetchall()
            for row in rows:
                if _PROMOTION_TYPE_RANK.get(new_type, 0) <= _PROMOTION_TYPE_RANK.get(row["type"], 0):
                    continue
                promoted.append({
                    "id": row["id"],
                    "title": row["title"],
                    "from_type": row["type"],
                    "to_type": new_type,
                    "access_count": row["access_count"],
                    "project_root": row["project_root"],
                })
                if not dry_run:
                    db.execute(
                        "UPDATE observations SET type=?, decay_immune=?, updated_at=? WHERE id=?",
                        (new_type, 1 if new_type == "guardrail" else 0, _now_iso(), row["id"]),
                    )
        if not dry_run:
            db.commit()
        db.close()
    except sqlite3.Error as exc:
        print(f"[token-savior:memory] run_promotions error: {exc}", file=sys.stderr)
    return {"promoted": promoted, "count": len(promoted), "dry_run": dry_run}
