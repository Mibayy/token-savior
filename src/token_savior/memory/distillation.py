"""MDL Memory Distillation: crystallize similar obs into abstractions.

Lifted from memory_db.py during the memory/ subpackage split.
"""

from __future__ import annotations

import json
import sqlite3
import sys
from typing import Any

from token_savior import memory_db
from token_savior.db_core import _json_dumps, _now_epoch, _now_iso, observation_hash


def run_mdl_distillation(
    project_root: str,
    dry_run: bool = True,
    min_cluster_size: int = 3,
    compression_required: float = 0.2,
    jaccard_threshold: float = 0.4,
) -> dict[str, Any]:
    """Detect MDL-compressible clusters and (optionally) crystallize them."""
    from token_savior.mdl_distiller import find_distillation_candidates

    try:
        # Include decay_immune types (guardrail/convention) — they are exactly
        # the repeated rules MDL is supposed to consolidate. Skip rows that
        # were already distilled so we don't loop.
        with memory_db.db_session() as conn:
            rows = [dict(r) for r in conn.execute(
                "SELECT id, type, title, content, symbol, file_path, tags "
                "FROM observations WHERE project_root=? AND archived=0 "
                "  AND (tags IS NULL OR "
                "       (tags NOT LIKE '%mdl-distilled%' "
                "        AND tags NOT LIKE '%mdl-abstraction%'))",
                [project_root],
            ).fetchall()]
    except sqlite3.Error as exc:
        print(f"[token-savior:memory] mdl_distillation load error: {exc}", file=sys.stderr)
        return {"clusters_found": 0, "clusters_applied": 0, "obs_distilled": 0,
                "abstractions_created": 0, "tokens_freed_estimate": 0,
                "dry_run": dry_run, "preview": []}

    clusters = find_distillation_candidates(
        rows,
        jaccard_threshold=jaccard_threshold,
        min_cluster_size=min_cluster_size,
        compression_required=compression_required,
    )

    preview: list[dict] = []
    for c in clusters[:10]:
        preview.append({
            "obs_ids": c.obs_ids,
            "size": len(c.obs_ids),
            "dominant_type": c.dominant_type,
            "mdl_before": c.mdl_before,
            "mdl_after": c.mdl_after,
            "compression_ratio": c.compression_ratio,
            "shared_tokens": c.shared_tokens,
            "abstraction": c.proposed_abstraction,
        })

    tokens_freed = int(sum(c.mdl_before - c.mdl_after for c in clusters))
    if dry_run or not clusters:
        return {
            "clusters_found": len(clusters),
            "clusters_applied": 0,
            "obs_distilled": 0,
            "abstractions_created": 0,
            "tokens_freed_estimate": tokens_freed,
            "dry_run": dry_run,
            "preview": preview,
        }

    # ---- Apply: create abstraction obs + delta-encode members + link ----
    applied = 0
    distilled = 0
    abstractions_created = 0
    try:
      with memory_db.db_session() as conn:
        now_iso = _now_iso()
        epoch = _now_epoch()
        for c in clusters:
            title = f"[MDL] {c.dominant_type} × {len(c.obs_ids)} — " + " / ".join(c.shared_tokens[:3])
            title = title[:200]
            content = c.proposed_abstraction
            chash = observation_hash(project_root, title, content)

            tags_json = _json_dumps(["mdl-abstraction", f"distilled-from-{len(c.obs_ids)}"])
            try:
                cur = conn.execute(
                    "INSERT INTO observations "
                    "(session_id, project_root, type, title, content, tags, "
                    " importance, content_hash, decay_immune, is_global, "
                    " created_at, created_at_epoch, updated_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        None, project_root, "convention", title, content,
                        tags_json, 8, chash, 1, 0, now_iso, epoch, now_iso,
                    ),
                )
            except sqlite3.Error as exc:
                print(f"[token-savior:memory] mdl abstraction insert error: {exc}", file=sys.stderr)
                continue
            abs_id = cur.lastrowid
            abstractions_created += 1

            for obs_id, delta in zip(c.obs_ids, c.deltas):
                new_content = f"[delta] {delta}\n[abstraction_id: {abs_id}]"
                try:
                    existing_tags = conn.execute(
                        "SELECT tags FROM observations WHERE id=?", [obs_id]
                    ).fetchone()
                    tag_list: list[str] = []
                    if existing_tags and existing_tags[0]:
                        try:
                            tag_list = json.loads(existing_tags[0]) or []
                        except Exception:
                            tag_list = []
                    if "mdl-distilled" not in tag_list:
                        tag_list.append("mdl-distilled")
                    conn.execute(
                        "UPDATE observations SET content=?, tags=?, updated_at=? WHERE id=?",
                        (new_content, _json_dumps(tag_list), now_iso, obs_id),
                    )
                    # supersedes link (abstraction → member)
                    conn.execute(
                        "INSERT OR IGNORE INTO observation_links "
                        "(source_id, target_id, link_type, auto_detected, created_at) "
                        "VALUES (?, ?, 'supersedes', 1, ?)",
                        (abs_id, obs_id, now_iso),
                    )
                    distilled += 1
                except sqlite3.Error as exc:
                    print(f"[token-savior:memory] mdl delta update error: {exc}", file=sys.stderr)
                    continue
            applied += 1
        conn.commit()
    except sqlite3.Error as exc:
        print(f"[token-savior:memory] mdl apply error: {exc}", file=sys.stderr)

    return {
        "clusters_found": len(clusters),
        "clusters_applied": applied,
        "obs_distilled": distilled,
        "abstractions_created": abstractions_created,
        "tokens_freed_estimate": tokens_freed,
        "dry_run": dry_run,
        "preview": preview,
    }


def get_mdl_stats(project_root: str | None = None) -> dict[str, Any]:
    """Counts of abstractions and distilled observations (tag-based)."""
    try:
        conn = memory_db.get_db()
        base = "SELECT id, tags, project_root FROM observations WHERE archived=0"
        params: list[Any] = []
        if project_root:
            base += " AND project_root=?"
            params.append(project_root)
        abstractions = 0
        distilled = 0
        for r in conn.execute(base, params).fetchall():
            raw = r[1] or "[]"
            try:
                tags = json.loads(raw)
            except Exception:
                tags = []
            if "mdl-abstraction" in tags:
                abstractions += 1
            if "mdl-distilled" in tags:
                distilled += 1
        conn.close()
        return {"abstractions": abstractions, "distilled": distilled}
    except sqlite3.Error:
        return {"abstractions": 0, "distilled": 0}
