"""Token Economy ROI: archival GC based on expected retention value.

Lifted from memory_db.py during the memory/ subpackage split.

ROI(o) = tokens_saved_per_hit × P(hit) × horizon_days × TYPE_MULTIPLIER − tokens_stored
P(hit) = exp(−λ × days_since_access) × (1 + 0.1 × access_count)
"""

from __future__ import annotations

import sqlite3
import sys
import time
from typing import Any

from token_savior import memory_db

_ROI_LAMBDA = 0.05  # exponential decay per day since last access
_ROI_HORIZON_DAYS = 30
_ROI_TOKENS_PER_HIT = 200  # estimated upstream token savings per recall
_ROI_THRESHOLD = 0.0  # below this → archival candidate

_ROI_TYPE_MULTIPLIER: dict[str, float] = {
    "guardrail": 3.0,
    "ruled_out": 2.5,
    "convention": 2.5,
    "warning": 2.0,
    "decision": 2.0,
    "error_pattern": 1.8,
    "command": 1.5,
    "infra": 1.5,
    "config": 1.5,
    "bugfix": 1.2,
    "research": 1.0,
    "note": 0.8,
    "idea": 0.7,
}


def compute_observation_roi(obs: dict[str, Any], now_epoch: int | None = None) -> dict[str, Any]:
    """Compute expected ROI of keeping an observation.

    Returns a dict with p_hit, tokens_saved_expected, tokens_stored, roi, multiplier.
    """
    import math
    now_epoch = now_epoch or int(time.time())
    last_acc = obs.get("last_accessed_epoch") or obs.get("created_at_epoch") or now_epoch
    days_since = max(0.0, (now_epoch - last_acc) / 86400.0)
    access_count = int(obs.get("access_count") or 0)
    p_hit = math.exp(-_ROI_LAMBDA * days_since) * (1.0 + 0.1 * access_count)
    p_hit = min(p_hit, 1.0)
    multiplier = _ROI_TYPE_MULTIPLIER.get(obs.get("type") or "note", 1.0)
    if obs.get("decay_immune"):
        multiplier = max(multiplier, 5.0)
    title = obs.get("title") or ""
    content = obs.get("content") or ""
    tokens_stored = max(1, (len(title) + len(content)) // 4)
    tokens_saved_expected = _ROI_TOKENS_PER_HIT * p_hit * _ROI_HORIZON_DAYS * multiplier
    roi = tokens_saved_expected - tokens_stored
    return {
        "p_hit": round(p_hit, 4),
        "tokens_saved_expected": round(tokens_saved_expected, 2),
        "tokens_stored": tokens_stored,
        "multiplier": multiplier,
        "roi": round(roi, 2),
    }


def run_roi_gc(
    project_root: str | None = None,
    dry_run: bool = True,
    threshold: float | None = None,
) -> dict[str, Any]:
    """Archive observations whose expected ROI falls below *threshold*.

    decay_immune observations are always kept.
    """
    th = _ROI_THRESHOLD if threshold is None else threshold
    try:
        with memory_db.db_session() as conn:
            sql = (
                "SELECT id, type, title, content, access_count, "
                "       created_at_epoch, last_accessed_epoch, decay_immune "
                "FROM observations WHERE archived=0 "
            )
            params: list[Any] = []
            if project_root:
                sql += "AND project_root=? "
                params.append(project_root)
            rows = [dict(r) for r in conn.execute(sql, params).fetchall()]

            now = int(time.time())
            candidates: list[dict] = []
            kept = 0
            for r in rows:
                if r.get("decay_immune"):
                    kept += 1
                    continue
                metrics = compute_observation_roi(r, now_epoch=now)
                if metrics["roi"] < th:
                    candidates.append({
                        "id": r["id"],
                        "type": r["type"],
                        "title": r["title"],
                        "access_count": r.get("access_count") or 0,
                        "roi": metrics["roi"],
                        "p_hit": metrics["p_hit"],
                        "tokens_stored": metrics["tokens_stored"],
                    })
                else:
                    kept += 1

            archived_ids: list[int] = []
            if not dry_run and candidates:
                ids = [c["id"] for c in candidates]
                placeholders = ",".join("?" for _ in ids)
                conn.execute(
                    f"UPDATE observations SET archived=1 WHERE id IN ({placeholders})",
                    ids,
                )
                conn.commit()
                archived_ids = ids

        candidates.sort(key=lambda c: c["roi"])
        return {
            "archived": len(archived_ids),
            "candidates": len(candidates),
            "kept": kept,
            "threshold": th,
            "dry_run": dry_run,
            "preview": candidates[:20],
            "archived_ids": archived_ids,
        }
    except sqlite3.Error as exc:
        print(f"[token-savior:memory] run_roi_gc error: {exc}", file=sys.stderr)
        return {
            "archived": 0, "candidates": 0, "kept": 0,
            "threshold": th, "dry_run": dry_run, "preview": [], "archived_ids": [],
        }


def get_roi_stats(project_root: str | None = None) -> dict[str, Any]:
    """Aggregate ROI statistics across the active corpus."""
    try:
        conn = memory_db.get_db()
        sql = (
            "SELECT id, type, title, content, access_count, "
            "       created_at_epoch, last_accessed_epoch, decay_immune "
            "FROM observations WHERE archived=0 "
        )
        params: list[Any] = []
        if project_root:
            sql += "AND project_root=? "
            params.append(project_root)
        rows = [dict(r) for r in conn.execute(sql, params).fetchall()]
        conn.close()

        if not rows:
            return {
                "total": 0, "total_tokens_stored": 0, "total_expected_savings": 0,
                "negative_roi_count": 0, "by_type": {},
                "threshold": _ROI_THRESHOLD, "lambda": _ROI_LAMBDA,
                "horizon_days": _ROI_HORIZON_DAYS,
            }

        now = int(time.time())
        total_tokens_stored = 0
        total_expected_savings = 0.0
        negative = 0
        by_type: dict[str, dict[str, Any]] = {}
        for r in rows:
            m = compute_observation_roi(r, now_epoch=now)
            total_tokens_stored += m["tokens_stored"]
            total_expected_savings += m["tokens_saved_expected"]
            if m["roi"] < _ROI_THRESHOLD and not r.get("decay_immune"):
                negative += 1
            t = r.get("type") or "unknown"
            bucket = by_type.setdefault(t, {"count": 0, "tokens": 0, "expected_savings": 0.0})
            bucket["count"] += 1
            bucket["tokens"] += m["tokens_stored"]
            bucket["expected_savings"] += m["tokens_saved_expected"]
        for bucket in by_type.values():
            bucket["expected_savings"] = round(bucket["expected_savings"], 2)
        return {
            "total": len(rows),
            "total_tokens_stored": total_tokens_stored,
            "total_expected_savings": round(total_expected_savings, 2),
            "net_roi": round(total_expected_savings - total_tokens_stored, 2),
            "negative_roi_count": negative,
            "by_type": by_type,
            "threshold": _ROI_THRESHOLD,
            "lambda": _ROI_LAMBDA,
            "horizon_days": _ROI_HORIZON_DAYS,
        }
    except sqlite3.Error as exc:
        print(f"[token-savior:memory] get_roi_stats error: {exc}", file=sys.stderr)
        return {"total": 0, "total_tokens_stored": 0, "total_expected_savings": 0,
                "negative_roi_count": 0, "by_type": {}}
