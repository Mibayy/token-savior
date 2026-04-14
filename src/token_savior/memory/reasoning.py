"""Reasoning Trace Compression (v2.2 Step A) + DCP chunk registry.

Lifted from memory_db.py during the memory/ subpackage split.
"""

from __future__ import annotations

import json
import sqlite3
import sys
from typing import Any

from token_savior import memory_db
from token_savior.db_core import (
    _fts5_safe_query,
    _json_dumps,
    _now_epoch,
    _now_iso,
    observation_hash,
    relative_age,
)
from token_savior.memory._text_utils import _jaccard


def reasoning_save(
    project_root: str,
    goal: str,
    steps: list[dict],
    conclusion: str,
    *,
    confidence: float = 0.8,
    evidence_obs_ids: list[int] | None = None,
    ttl_days: int | None = None,
) -> int | None:
    """Persist a reasoning chain (goal → steps → conclusion) for later recall."""
    if not goal or not conclusion:
        return None
    goal_norm = " ".join((goal or "").lower().split())
    ghash = observation_hash(project_root, goal_norm, conclusion)
    ehash = (
        observation_hash(project_root, ",".join(str(i) for i in evidence_obs_ids), "")
        if evidence_obs_ids
        else None
    )
    now = _now_iso()
    epoch = _now_epoch()
    expires = epoch + int(ttl_days) * 86400 if ttl_days else None
    try:
        conn = memory_db.get_db()
        existing = conn.execute(
            "SELECT id FROM reasoning_chains WHERE project_root=? AND goal_hash=?",
            (project_root, ghash),
        ).fetchone()
        if existing is not None:
            conn.close()
            return existing[0]
        cur = conn.execute(
            "INSERT INTO reasoning_chains "
            "(project_root, goal, goal_hash, steps, conclusion, confidence, "
            " evidence_hash, created_at, created_at_epoch, expires_at_epoch) "
            "VALUES (?,?,?,?,?,?,?,?,?,?)",
            (
                project_root, goal, ghash,
                _json_dumps(steps or []), conclusion,
                float(confidence), ehash, now, epoch, expires,
            ),
        )
        conn.commit()
        rid = cur.lastrowid
        conn.close()
        return rid
    except sqlite3.Error as exc:
        print(f"[token-savior:memory] reasoning_save error: {exc}", file=sys.stderr)
        return None


def reasoning_search(
    project_root: str,
    query: str,
    *,
    threshold: float = 0.3,
    limit: int = 5,
) -> list[dict]:
    """Return reasoning chains matching *query*, scored by Jaccard on the goal."""
    rows: list[Any] = []
    try:
        conn = memory_db.get_db()
        fts_q = _fts5_safe_query(query)
        if fts_q:
            try:
                rows = conn.execute(
                    "SELECT rc.id, rc.goal, rc.conclusion, rc.confidence, rc.steps, "
                    "       rc.created_at_epoch, rc.access_count "
                    "FROM reasoning_chains_fts f "
                    "JOIN reasoning_chains rc ON rc.id = f.rowid "
                    "WHERE reasoning_chains_fts MATCH ? AND rc.project_root=? "
                    "ORDER BY rank LIMIT ?",
                    (fts_q, project_root, limit),
                ).fetchall()
            except sqlite3.OperationalError:
                rows = []
        if not rows:
            like = f"%{(query or '')[:60]}%"
            rows = conn.execute(
                "SELECT id, goal, conclusion, confidence, steps, "
                "       created_at_epoch, access_count "
                "FROM reasoning_chains "
                "WHERE project_root=? AND (goal LIKE ? OR conclusion LIKE ?) "
                "ORDER BY created_at_epoch DESC LIMIT ?",
                (project_root, like, like, limit),
            ).fetchall()
        conn.close()
    except sqlite3.Error as exc:
        print(f"[token-savior:memory] reasoning_search error: {exc}", file=sys.stderr)
        return []

    results: list[dict] = []
    permissive = len(rows) <= 2
    for row in rows:
        d = dict(row)
        score = _jaccard(query or "", d.get("goal") or "")
        if score >= threshold or permissive:
            d["relevance"] = round(score, 3)
            d["age"] = relative_age(d.get("created_at_epoch"))
            results.append(d)
    results.sort(key=lambda x: x["relevance"], reverse=True)
    return results


def reasoning_inject(project_root: str, prompt: str) -> str | None:
    """Return a formatted hint if the prompt matches a past reasoning goal."""
    if not prompt or len(prompt.strip()) < 10:
        return None
    chains = reasoning_search(project_root, prompt, threshold=0.3, limit=3)
    if not chains:
        return None
    best = chains[0]
    if float(best.get("relevance", 0)) < 0.3:
        return None
    try:
        conn = memory_db.get_db()
        conn.execute(
            "UPDATE reasoning_chains SET access_count=access_count+1 WHERE id=?",
            (best["id"],),
        )
        conn.commit()
        conn.close()
    except sqlite3.Error:
        pass
    try:
        steps = json.loads(best.get("steps") or "[]")
    except Exception:
        steps = []
    lines = [
        f"🧠 Similar reasoning trace found (relevance: {best['relevance']:.2f}):",
        f"Goal: {best['goal']}",
        "─" * 40,
    ]
    for i, step in enumerate(steps[:5], 1):
        tool = step.get("tool", "")
        obs = (step.get("observation") or "")[:80]
        lines.append(f"  {i}. [{tool}] {obs}")
    if len(steps) > 5:
        lines.append(f"  ... ({len(steps) - 5} more steps)")
    lines.append(f"→ CONCLUSION: {best['conclusion']}")
    lines.append(
        f"  Confidence: {float(best.get('confidence', 0.8)):.0%} | "
        f"Used {int(best.get('access_count', 0)) + 1} times"
    )
    return "\n".join(lines)


def register_chunks(chunks: list[Any]) -> list[Any]:
    """Update dcp_chunk_registry with *chunks*; annotate each chunk in place.

    A chunk is *stable* if its fingerprint existed before this call. The
    ``seen_count`` and ``last_seen_epoch`` fields are bumped per fingerprint.
    Returns the input list (same objects) so callers can chain.
    """
    if not chunks:
        return chunks
    try:
        conn = memory_db.get_db()
        now = _now_epoch()
        for chunk in chunks:
            fp = chunk.fingerprint
            existing = conn.execute(
                "SELECT seen_count FROM dcp_chunk_registry WHERE fingerprint=?",
                (fp,),
            ).fetchone()
            if existing:
                chunk.is_stable = True
                chunk.cache_hit_count = int(existing["seen_count"])
                conn.execute(
                    "UPDATE dcp_chunk_registry "
                    "SET seen_count=seen_count+1, last_seen_epoch=? "
                    "WHERE fingerprint=?",
                    (now, fp),
                )
            else:
                preview = (chunk.content or "")[:50]
                conn.execute(
                    "INSERT INTO dcp_chunk_registry "
                    "(fingerprint, content_preview, seen_count, last_seen_epoch) "
                    "VALUES (?, ?, 1, ?)",
                    (fp, preview, now),
                )
        conn.commit()
        conn.close()
    except sqlite3.Error as exc:
        print(f"[token-savior:memory] register_chunks error: {exc}", file=sys.stderr)
    return chunks


def optimize_output_order(content: str) -> tuple[str, int, int]:
    """Reorder *content* so stable chunks (cache-hot) come first.

    Returns (optimized_content, stable_count, total_count). The footer
    ``[dcp: N/M chunks stable]`` is appended by the caller.
    """
    try:
        from token_savior.dcp_chunker import chunk_content
    except Exception:
        return content, 0, 0
    chunks = chunk_content(content)
    if not chunks:
        return content, 0, 0
    register_chunks(chunks)
    stable = [c for c in chunks if c.is_stable]
    unstable = [c for c in chunks if not c.is_stable]
    reordered = "".join(c.content for c in (stable + unstable))
    return reordered, len(stable), len(chunks)


def dcp_stats() -> dict[str, Any]:
    """Registry-level stats for DCP: total chunks, hit counts, top fingerprints."""
    try:
        conn = memory_db.get_db()
        row = conn.execute(
            "SELECT COUNT(*) AS total, "
            "       COALESCE(SUM(seen_count), 0) AS total_seen, "
            "       COALESCE(SUM(CASE WHEN seen_count > 1 THEN 1 ELSE 0 END), 0) AS stable "
            "FROM dcp_chunk_registry"
        ).fetchone()
        top = conn.execute(
            "SELECT fingerprint, content_preview, seen_count "
            "FROM dcp_chunk_registry ORDER BY seen_count DESC LIMIT 5"
        ).fetchall()
        conn.close()
    except sqlite3.Error as exc:
        print(f"[token-savior:memory] dcp_stats error: {exc}", file=sys.stderr)
        return {"total": 0, "stable": 0, "total_seen": 0, "top": []}
    return {
        "total": int(row["total"] or 0),
        "stable": int(row["stable"] or 0),
        "total_seen": int(row["total_seen"] or 0),
        "top": [dict(r) for r in top],
    }


def reasoning_list(project_root: str, limit: int = 50) -> list[dict]:
    """Return all reasoning chains for a project with basic stats."""
    try:
        conn = memory_db.get_db()
        rows = conn.execute(
            "SELECT id, goal, conclusion, confidence, access_count, "
            "       created_at, created_at_epoch "
            "FROM reasoning_chains WHERE project_root=? "
            "ORDER BY access_count DESC, created_at_epoch DESC LIMIT ?",
            (project_root, limit),
        ).fetchall()
        conn.close()
    except sqlite3.Error as exc:
        print(f"[token-savior:memory] reasoning_list error: {exc}", file=sys.stderr)
        return []
    out = []
    for r in rows:
        d = dict(r)
        d["age"] = relative_age(d.get("created_at_epoch"))
        out.append(d)
    return out
