"""Memory health check: orphans, near-duplicates, incomplete observations.

Lifted from memory_db.py during the memory/ subpackage split.
"""

from __future__ import annotations

import os
import sqlite3
import sys
from typing import Any

from token_savior import memory_db
from token_savior.memory._text_utils import _jaccard


def run_health_check(project_root: str) -> dict[str, Any]:
    """Report orphan symbols, stale obs, near-duplicates, incomplete obs."""
    issues: dict[str, Any] = {
        "orphan_symbols": [],
        "stale_obs": [],
        "near_duplicates": [],
        "incomplete_obs": [],
        "summary": {},
    }
    try:
        db = memory_db.get_db()
        incomplete = db.execute(
            "SELECT id, type, title FROM observations "
            "WHERE project_root=? AND archived=0 "
            "  AND symbol IS NULL AND file_path IS NULL AND context IS NULL "
            "  AND type NOT IN ('idea', 'research', 'note')",
            [project_root],
        ).fetchall()
        issues["incomplete_obs"] = [dict(r) for r in incomplete]

        all_obs = db.execute(
            "SELECT id, title FROM observations WHERE project_root=? AND archived=0",
            [project_root],
        ).fetchall()
        seen_pairs: set[tuple[int, int]] = set()
        for i, obs in enumerate(all_obs):
            for other in all_obs[:i]:
                score = _jaccard(obs["title"], other["title"])
                if score >= 0.7:
                    key = (min(obs["id"], other["id"]), max(obs["id"], other["id"]))
                    if key in seen_pairs:
                        continue
                    seen_pairs.add(key)
                    issues["near_duplicates"].append({
                        "id_a": obs["id"], "title_a": obs["title"],
                        "id_b": other["id"], "title_b": other["title"],
                        "score": round(score, 2),
                    })

        symbol_obs = db.execute(
            "SELECT id, title, symbol, file_path FROM observations "
            "WHERE project_root=? AND archived=0 AND symbol IS NOT NULL",
            [project_root],
        ).fetchall()
        for obs in symbol_obs:
            fp = obs["file_path"]
            if not fp:
                continue
            full = fp if os.path.isabs(fp) else os.path.join(project_root, fp)
            if not os.path.exists(full):
                issues["orphan_symbols"].append({
                    "id": obs["id"], "title": obs["title"],
                    "symbol": obs["symbol"], "file_path": fp,
                })
        db.close()
    except sqlite3.Error as exc:
        print(f"[token-savior:memory] run_health_check error: {exc}", file=sys.stderr)

    issues["summary"] = {
        "orphan_symbols": len(issues["orphan_symbols"]),
        "near_duplicates": len(issues["near_duplicates"]),
        "incomplete_obs": len(issues["incomplete_obs"]),
        "total_issues": (
            len(issues["orphan_symbols"])
            + len(issues["near_duplicates"])
            + len(issues["incomplete_obs"])
        ),
    }
    return issues
