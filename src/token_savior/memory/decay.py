"""Decay rules + relevance score recalculation.

Lifted from memory_db.py during the memory/ subpackage split.
"""

from __future__ import annotations

import sqlite3
import sys
import time
from typing import Any

from token_savior import memory_db
from token_savior.db_core import _now_epoch, _now_iso

_DECAY_IMMUNE_TYPES = frozenset({"guardrail", "convention", "decision", "user", "feedback"})

_DEFAULT_TTL_DAYS = {
    "command": 60,
    "research": 90,
    "note": 60,
    "idea": 120,
    "bugfix": 180,
    "ruled_out": 180,
}
_DECAY_MAX_AGE_SEC = 90 * 86400        # obs older than 90 days are candidates
_DECAY_UNREAD_SEC = 30 * 86400         # must also be unread for at least 30 days
_DECAY_MIN_ACCESS = 3                  # never decay obs accessed >= 3 times

_ZERO_ACCESS_RULES = [
    ("note", 30),
    ("research", 45),
    ("idea", 60),
    ("bugfix", 90),
]


def _recalculate_relevance_scores() -> int:
    """Recalculate relevance scores based on decay config. Returns updated count."""
    try:
        conn = memory_db.get_db()
        configs = conn.execute("SELECT * FROM decay_config").fetchall()
        config_map = {r["type"]: dict(r) for r in configs}

        now_epoch = _now_epoch()
        rows = conn.execute(
            "SELECT id, type, relevance_score, access_count, created_at_epoch "
            "FROM observations WHERE archived=0",
        ).fetchall()

        updated = 0
        for row in rows:
            cfg = config_map.get(row["type"])
            if cfg is None:
                continue

            days_old = (now_epoch - row["created_at_epoch"]) / 86400
            decay_rate = cfg["decay_rate"]
            min_score = cfg["min_score"]
            boost = cfg["boost_on_access"]

            base = decay_rate ** days_old
            boosted = base + (boost * row["access_count"])
            new_score = max(min_score, min(1.0, boosted))

            if abs(new_score - row["relevance_score"]) > 0.001:
                conn.execute(
                    "UPDATE observations SET relevance_score=? WHERE id=?",
                    (round(new_score, 4), row["id"]),
                )
                updated += 1

        conn.commit()
        conn.close()
        return updated
    except sqlite3.Error as exc:
        print(f"[token-savior:memory] _recalculate_relevance_scores error: {exc}", file=sys.stderr)
        return 0


def _bump_access(ids: list[int]) -> None:
    """Increment access_count and update last_accessed_at/epoch for given IDs."""
    if not ids:
        return
    now = _now_iso()
    epoch = _now_epoch()
    try:
        conn = memory_db.get_db()
        placeholders = ",".join("?" for _ in ids)
        conn.execute(
            f"UPDATE observations SET access_count = access_count + 1, "
            f"last_accessed_at = ?, last_accessed_epoch = ? WHERE id IN ({placeholders})",
            [now, epoch, *ids],
        )
        conn.commit()
        conn.close()
    except sqlite3.Error:
        pass


def _decay_candidates_sql() -> tuple[str, list]:
    now = _now_epoch()
    cutoff_age = now - _DECAY_MAX_AGE_SEC
    cutoff_unread = now - _DECAY_UNREAD_SEC
    sql = (
        "SELECT id, type, title, created_at, access_count, last_accessed_epoch, project_root "
        "FROM observations "
        "WHERE archived = 0 "
        "  AND decay_immune = 0 "
        "  AND created_at_epoch < ? "
        "  AND (last_accessed_epoch IS NULL OR last_accessed_epoch < ?) "
        "  AND access_count < ? "
    )
    return sql, [cutoff_age, cutoff_unread, _DECAY_MIN_ACCESS]


def run_decay(project_root: str | None = None, dry_run: bool = True) -> dict[str, Any]:
    """Archive observations eligible for decay. Returns counts + preview."""
    sql, params = _decay_candidates_sql()
    if project_root:
        sql += "AND project_root = ? "
        params.append(project_root)
    sql += "ORDER BY created_at_epoch ASC"

    try:
        with memory_db.db_session() as conn:
            rows = [dict(r) for r in conn.execute(sql, params).fetchall()]

            now = int(time.time())
            seen = {r["id"] for r in rows}

            ttl_rows: list[dict] = []
            tsql = (
                "SELECT id, type, title, created_at, access_count "
                "FROM observations "
                "WHERE archived=0 AND expires_at_epoch IS NOT NULL "
                "  AND expires_at_epoch < ? "
            )
            tparams: list[Any] = [now]
            if project_root:
                tsql += "AND project_root=? "
                tparams.append(project_root)
            for r in conn.execute(tsql, tparams).fetchall():
                d = dict(r)
                if d["id"] in seen:
                    continue
                d["reason"] = "ttl-expired"
                ttl_rows.append(d)
                seen.add(d["id"])

            zero_access_rows: list[dict] = []
            for obs_type, days in _ZERO_ACCESS_RULES:
                cutoff = now - days * 86400
                zsql = (
                    "SELECT id, type, title, created_at, access_count "
                    "FROM observations "
                    "WHERE archived=0 AND decay_immune=0 "
                    "  AND type=? AND access_count=0 AND created_at_epoch < ? "
                )
                zparams: list[Any] = [obs_type, cutoff]
                if project_root:
                    zsql += "AND project_root=? "
                    zparams.append(project_root)
                for r in conn.execute(zsql, zparams).fetchall():
                    d = dict(r)
                    if d["id"] in seen:
                        continue
                    d["reason"] = f"zero-access {obs_type} >{days}d"
                    zero_access_rows.append(d)
                    seen.add(d["id"])

            all_rows = ttl_rows + rows + zero_access_rows

            immune_count = conn.execute(
                "SELECT COUNT(*) FROM observations WHERE archived=0 AND decay_immune=1"
            ).fetchone()[0]
            kept_count = conn.execute(
                "SELECT COUNT(*) FROM observations WHERE archived=0"
            ).fetchone()[0] - len(all_rows)

            archived_ids: list[int] = []
            if not dry_run and all_rows:
                ids = [r["id"] for r in all_rows]
                placeholders = ",".join("?" for _ in ids)
                conn.execute(
                    f"UPDATE observations SET archived=1 WHERE id IN ({placeholders})",
                    ids,
                )
                conn.commit()
                archived_ids = ids

        return {
            "archived": len(all_rows) if not dry_run else 0,
            "candidates": len(all_rows),
            "zero_access_archived": len(zero_access_rows) if not dry_run else 0,
            "zero_access_candidates": len(zero_access_rows),
            "ttl_expired": len(ttl_rows) if not dry_run else 0,
            "ttl_candidates": len(ttl_rows),
            "kept": kept_count,
            "immune": immune_count,
            "preview": [
                {"id": r["id"], "type": r["type"], "title": r["title"],
                 "created_at": r["created_at"], "access_count": r.get("access_count", 0),
                 "reason": r.get("reason", "standard decay")}
                for r in all_rows[:20]
            ],
            "dry_run": dry_run,
            "archived_ids": archived_ids,
        }
    except sqlite3.Error as exc:
        print(f"[token-savior:memory] run_decay error: {exc}", file=sys.stderr)
        return {"archived": 0, "candidates": 0, "kept": 0, "immune": 0, "preview": [], "dry_run": dry_run}
