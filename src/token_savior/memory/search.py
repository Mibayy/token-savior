"""A1-3: hybrid search (FTS5 + sqlite-vec) with RRF fusion.

``hybrid_search`` is the search entry-point used by ``observation_search``.
Behaviour:

1. Always run an FTS5 query (existing keyword path).
2. If ``VECTOR_SEARCH_AVAILABLE`` is False, or embedding the query yields
   ``None``, or the ``obs_vectors`` table is missing, return FTS results
   untouched — full backwards compatibility.
3. Otherwise run a k-NN query against ``obs_vectors`` and fuse both rank
   lists with Reciprocal Rank Fusion (RRF, k=60, the reference constant
   from Cormack et al. 2009).

All helpers here are deliberately DB-agnostic with respect to how the
rows arrive — callers pass the SQL connection and the filter fragments,
which keeps the quarantine / type / project_root logic centralised at
one call-site in ``observation_search``.
"""

from __future__ import annotations

from typing import Any

RRF_K = 60  # standard Reciprocal Rank Fusion constant.


def rrf_merge(
    *ranked_lists: list[dict[str, Any]],
    limit: int = 20,
    k: int = RRF_K,
) -> list[dict[str, Any]]:
    """Fuse N rank-ordered result lists into a single list using RRF.

    Score per obs = Σ 1/(k + rank_i), where rank is 1-based within each
    individual list. The highest-scoring rows survive. Metadata is taken
    from the first list the id appears in (stable for debuggability).
    """
    scores: dict[int, float] = {}
    metadata: dict[int, dict[str, Any]] = {}
    for rows in ranked_lists:
        for rank, row in enumerate(rows, start=1):
            oid = row.get("id")
            if oid is None:
                continue
            scores[oid] = scores.get(oid, 0.0) + 1.0 / (k + rank)
            if oid not in metadata:
                metadata[oid] = row
    ranked = sorted(scores.items(), key=lambda kv: kv[1], reverse=True)
    out: list[dict[str, Any]] = []
    for oid, score in ranked[:limit]:
        row = dict(metadata[oid])
        row["_rrf_score"] = round(score, 6)
        out.append(row)
    return out


def vec_search_rows(
    conn: Any,
    query_vec: list[float],
    project_root: str,
    *,
    limit: int = 40,
    type_filter: str | None = None,
    include_quarantine: bool = False,
) -> list[dict[str, Any]]:
    """k-NN over obs_vectors → list of dicts matching observation_search shape.

    Returns [] on any failure (missing table, unloaded extension, bad vec
    serialization) so the hybrid path degrades gracefully.
    """
    try:
        import sqlite_vec  # type: ignore[import-not-found]
    except ImportError:
        return []
    try:
        blob = sqlite_vec.serialize_float32(query_vec)
    except Exception:
        return []

    sql = (
        "SELECT o.id, o.type, o.title, o.importance, o.symbol, o.file_path, "
        "  substr(COALESCE(o.narrative, o.content), 1, 160) AS excerpt, "
        "  o.created_at, o.created_at_epoch, o.is_global, o.agent_id, "
        "  c.quarantine, c.stale_suspected, v.distance "
        "FROM obs_vectors AS v "
        "JOIN observations AS o ON o.id = v.obs_id "
        "LEFT JOIN consistency_scores AS c ON c.obs_id = o.id "
        "WHERE v.embedding MATCH ? AND k = ? "
        "  AND o.archived = 0 "
        "  AND (o.project_root = ? OR o.is_global = 1) "
    )
    params: list[Any] = [blob, int(limit), project_root]
    if not include_quarantine:
        sql += "AND (c.quarantine IS NULL OR c.quarantine = 0) "
    if type_filter:
        sql += "AND o.type = ? "
        params.append(type_filter)
    sql += "ORDER BY v.distance"

    try:
        rows = conn.execute(sql, params).fetchall()
    except Exception:
        return []
    return [dict(r) for r in rows]


def hybrid_search(
    conn: Any,
    fts_rows: list[dict[str, Any]],
    query: str,
    project_root: str,
    *,
    limit: int = 20,
    type_filter: str | None = None,
    include_quarantine: bool = False,
) -> list[dict[str, Any]]:
    """Fuse FTS rows with a vector-search pass when available.

    ``fts_rows`` is computed by the caller (``observation_search``) using
    the existing SQL so quarantine/type/global filtering stays DRY. If the
    vector stack is unavailable, the FTS list is returned untouched
    (truncated to ``limit``) — fully backwards compatible.
    """
    from token_savior.db_core import VECTOR_SEARCH_AVAILABLE
    if not VECTOR_SEARCH_AVAILABLE:
        return fts_rows[:limit]
    try:
        from token_savior.memory.embeddings import embed
    except Exception:
        return fts_rows[:limit]
    vec = embed(query)
    if vec is None:
        return fts_rows[:limit]
    vec_rows = vec_search_rows(
        conn, vec, project_root,
        limit=limit * 2,
        type_filter=type_filter,
        include_quarantine=include_quarantine,
    )
    if not vec_rows:
        return fts_rows[:limit]
    return rrf_merge(fts_rows, vec_rows, limit=limit)
