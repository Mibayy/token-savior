"""Token Savior Memory Engine — SQLite persistence layer.

Core DB primitives + shared utils live in `db_core`; this module re-exports
them for backward compatibility and owns the higher-level memory operations.
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
import sys
import time
from contextlib import AbstractContextManager
from pathlib import Path
from typing import Any

from . import db_core
from .db_core import (
    MEMORY_DB_PATH,
    _SCHEMA_PATH,
    _fts5_safe_query,
    _json_dumps,
    _migrated_paths,
    _now_epoch,
    _now_iso,
    observation_hash,
    relative_age,
    strip_private,
)

__all__ = [
    "MEMORY_DB_PATH", "_SCHEMA_PATH", "_migrated_paths",
    "run_migrations", "get_db", "db_session",
    "_now_iso", "_now_epoch", "_json_dumps",
    "observation_hash", "strip_private", "relative_age", "_fts5_safe_query",
]


# Thin wrappers so tests can patch `memory_db.MEMORY_DB_PATH` and affect
# connections opened via `memory_db.get_db()` / `memory_db.db_session()`.
def get_db(db_path: Path | str | None = None) -> sqlite3.Connection:
    return db_core.get_db(db_path or MEMORY_DB_PATH)


def db_session(
    db_path: Path | str | None = None,
) -> AbstractContextManager[sqlite3.Connection]:
    return db_core.db_session(db_path or MEMORY_DB_PATH)


def run_migrations(db_path: Path | str | None = None) -> None:
    return db_core.run_migrations(db_path or MEMORY_DB_PATH)


from token_savior.memory.consistency import (  # noqa: E402,F401  re-exports
    CONSISTENCY_QUARANTINE_THRESHOLD,
    CONSISTENCY_STALE_THRESHOLD,
    check_symbol_staleness,
    compute_continuity_score,
    get_consistency_stats,
    get_validity_score,
    list_quarantined_observations,
    run_consistency_check,
    update_consistency_score,
)


# ---------------------------------------------------------------------------
# Sessions
# ---------------------------------------------------------------------------


from token_savior.memory.sessions import session_end, session_start  # noqa: E402,F401  re-exports


# ---------------------------------------------------------------------------
# Observations
# ---------------------------------------------------------------------------

from token_savior.memory.decay import (  # noqa: E402,F401  re-exports (constants)
    _DECAY_IMMUNE_TYPES,
    _DECAY_MAX_AGE_SEC,
    _DECAY_MIN_ACCESS,
    _DECAY_UNREAD_SEC,
    _DEFAULT_TTL_DAYS,
)


from token_savior.memory.consistency import (  # noqa: E402,F401  re-exports
    _CONTRADICTION_OPPOSITES,
    _RULE_TYPES_FOR_CONTRADICTION,
    detect_contradictions,
)


_CORRUPTION_MARKERS = (
    "tool_response", "exit_code", "tool_input",
    '"type":"tool"', "ToolResult", "tool_use_id",
)


def _is_corrupted_content(title: str, content: str) -> bool:
    text = f"{title or ''} {content or ''}"
    if any(m in text for m in _CORRUPTION_MARKERS):
        return True
    t = (title or "").strip()
    if t.endswith(("',", '",', "}}", "}},")):
        return True
    return False


def observation_save(
    session_id: int | None,
    project_root: str,
    type: str,
    title: str,
    content: str,
    *,
    why: str | None = None,
    how_to_apply: str | None = None,
    symbol: str | None = None,
    file_path: str | None = None,
    context: str | None = None,
    tags: list[str] | None = None,
    importance: int = 5,
    private: bool = False,
    is_global: bool = False,
    ttl_days: int | None = None,
    expires_at_epoch: int | None = None,
) -> int | None:
    """Save an observation. Returns id, or None if duplicate detected."""
    title = strip_private(title) or ""
    content = strip_private(content) or ""
    why = strip_private(why)
    how_to_apply = strip_private(how_to_apply)
    if not title or title == "[PRIVATE]":
        return None
    if _is_corrupted_content(title, content):
        print(
            f"[token-savior:memory] refused corrupted obs: {title[:60]!r}",
            file=sys.stderr,
        )
        return None
    chash = observation_hash(project_root, title, content)
    now = _now_iso()
    epoch = _now_epoch()
    try:
        with db_session() as conn:
            row = conn.execute(
                "SELECT id FROM observations WHERE content_hash=? AND project_root=? AND archived=0",
                (chash, project_root),
            ).fetchone()
            if row is not None:
                return None

        if is_global:
            gdup = global_dedup_check(title, content, type, threshold=0.85)
            if gdup:
                if gdup["score"] >= 0.95:
                    print(
                        f"[token-savior:memory] global dup skip → #{gdup['id']} "
                        f"({gdup['reason']} {gdup['score']}) in {gdup['project_root']}",
                        file=sys.stderr,
                    )
                    return None
                if tags is None:
                    tags = []
                if "near-duplicate-global" not in tags:
                    tags = list(tags) + ["near-duplicate-global"]
                print(
                    f"[token-savior:memory] near-duplicate-global tag → #{gdup['id']} "
                    f"(score {gdup['score']})",
                    file=sys.stderr,
                )
        semantic = semantic_dedup_check(project_root, title, type, threshold=0.85)
        if semantic:
            if semantic["score"] >= 0.95:
                print(
                    f"[token-savior:memory] near-duplicate skip #{semantic['id']} "
                    f"(score {semantic['score']})",
                    file=sys.stderr,
                )
                return None
            if tags is None:
                tags = []
            if "near-duplicate" not in tags:
                tags = list(tags) + ["near-duplicate"]
            print(
                f"[token-savior:memory] near-duplicate tag → existing #{semantic['id']} "
                f"(score {semantic['score']})",
                file=sys.stderr,
            )
        immune = 1 if type in _DECAY_IMMUNE_TYPES else 0
        if expires_at_epoch is None:
            if ttl_days is not None:
                expires_at_epoch = epoch + int(ttl_days) * 86400
            elif type in _DEFAULT_TTL_DAYS and not immune:
                expires_at_epoch = epoch + _DEFAULT_TTL_DAYS[type] * 86400
        with db_session() as conn:
            try:
                conn.execute("DELETE FROM memory_cache WHERE cache_key LIKE ?", [f"{project_root}:%"])
            except sqlite3.Error:
                pass
            cur = conn.execute(
                "INSERT INTO observations "
                "(session_id, project_root, type, title, content, why, how_to_apply, "
                " symbol, file_path, context, tags, private, importance, content_hash, decay_immune, "
                " is_global, expires_at_epoch, created_at, created_at_epoch, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    session_id,
                    project_root,
                    type,
                    title,
                    content,
                    why,
                    how_to_apply,
                    symbol,
                    file_path,
                    context,
                    _json_dumps(tags),
                    1 if private else 0,
                    importance,
                    chash,
                    immune,
                    1 if is_global else 0,
                    expires_at_epoch,
                    now,
                    epoch,
                    now,
                ),
            )
            conn.commit()
            obs_id = cur.lastrowid
        try:
            notify_telegram(
                {"type": type, "title": title, "content": content, "symbol": symbol}
            )
        except Exception:
            pass
        return obs_id
    except sqlite3.Error as exc:
        print(f"[token-savior:memory] observation_save error: {exc}", file=sys.stderr)
        return None


def observation_save_ruled_out(
    project_root: str,
    title: str,
    content: str,
    *,
    why: str | None = None,
    symbol: str | None = None,
    file_path: str | None = None,
    tags: list[str] | None = None,
    ttl_days: int = 180,
    session_id: int | None = None,
) -> int | None:
    """Save a `ruled_out` observation: an approach explicitly rejected.

    Negative memory — what NOT to try, with optional explanation.
    Default TTL 180d (same as bugfix). Higher type_score (0.95) than
    convention so it surfaces aggressively when an edit-sensitive tool
    is about to operate on the same area.
    """
    merged_tags = list(tags or [])
    if "ruled-out" not in merged_tags:
        merged_tags.append("ruled-out")
    return observation_save(
        session_id=session_id,
        project_root=project_root,
        type="ruled_out",
        title=title,
        content=content,
        why=why,
        symbol=symbol,
        file_path=file_path,
        tags=merged_tags,
        importance=7,
        ttl_days=ttl_days,
    )


# ---------------------------------------------------------------------------
# Step C: inter-agent memory bus
# ---------------------------------------------------------------------------

# Volatile observations are short-lived signals between subagents (or between
# a subagent and the parent). They expire fast (default 1 day) so the bus
# never accumulates stale chatter.
from token_savior.memory.bus import DEFAULT_VOLATILE_TTL_DAYS  # noqa: E402,F401  re-export


def observation_save_volatile(
    project_root: str,
    agent_id: str,
    title: str,
    content: str,
    *,
    obs_type: str = "note",
    symbol: str | None = None,
    file_path: str | None = None,
    tags: list[str] | None = None,
    ttl_days: int = DEFAULT_VOLATILE_TTL_DAYS,
    session_id: int | None = None,
) -> int | None:
    """Push a volatile, agent-tagged observation onto the bus.

    `agent_id` is required (a free-form subagent identifier such as
    "Explore", "code-reviewer", or a worktree name). The row is tagged
    `bus` + `volatile` for filtering and gets a short TTL so the bus
    self-cleans without explicit retention work.
    """
    if not agent_id:
        return None
    merged_tags = list(tags or [])
    for t in ("bus", "volatile"):
        if t not in merged_tags:
            merged_tags.append(t)

    obs_id = observation_save(
        session_id=session_id,
        project_root=project_root,
        type=obs_type,
        title=title,
        content=content,
        symbol=symbol,
        file_path=file_path,
        tags=merged_tags,
        importance=4,
        ttl_days=ttl_days,
    )
    if obs_id is None:
        return None
    try:
        conn = get_db()
        conn.execute(
            "UPDATE observations SET agent_id=? WHERE id=?",
            (agent_id, obs_id),
        )
        conn.commit()
        conn.close()
    except sqlite3.Error as exc:
        print(f"[token-savior:memory] observation_save_volatile agent tag error: {exc}", file=sys.stderr)
    return obs_id


from token_savior.memory.bus import memory_bus_list  # noqa: E402,F401  re-export


# ---------------------------------------------------------------------------
# Reasoning Trace Compression (v2.2 Step A)
# ---------------------------------------------------------------------------


from token_savior.memory.reasoning import (  # noqa: E402,F401  re-exports
    dcp_stats,
    optimize_output_order,
    reasoning_inject,
    reasoning_list,
    reasoning_save,
    reasoning_search,
    register_chunks,
)



# ---------------------------------------------------------------------------
# Step D: Adaptive Lattice (Beta-Binomial Thompson sampling on granularity)
# ---------------------------------------------------------------------------

# Granularity levels for source-fetching tools:
#   0 = full source (no compression)
#   1 = signature + docstring + first/last lines
#   2 = signature only
#   3 = name + line range only
from token_savior.memory.lattice import (  # noqa: E402,F401  re-exports
    LATTICE_CONTEXTS,
    LATTICE_LEVELS,
    _detect_context_type,
    _ensure_lattice_row,
    get_lattice_stats,
    record_lattice_feedback,
    thompson_sample_level,
)





def observation_search(
    project_root: str,
    query: str,
    *,
    type_filter: str | None = None,
    limit: int = 20,
    include_quarantine: bool = False,
) -> list[dict]:
    """FTS5 search across observations. Returns compact index dicts.

    Quarantined observations (Bayesian validity < 40%) are filtered out by
    default; pass ``include_quarantine=True`` to see them. Stale-suspected
    obs are returned but flagged via the ``stale_suspected`` key — callers
    can prepend ⚠️ to the title in formatted output.
    """
    try:
        conn = get_db()
        params: list[Any] = []
        sql = (
            "SELECT o.id, o.type, o.title, o.importance, o.symbol, o.file_path, "
            "  snippet(observations_fts, 1, '»', '«', '...', 40) AS excerpt, "
            "  o.created_at, o.created_at_epoch, o.is_global, o.agent_id, "
            "  c.quarantine, c.stale_suspected "
            "FROM observations_fts AS f "
            "JOIN observations AS o ON o.id = f.rowid "
            "LEFT JOIN consistency_scores AS c ON c.obs_id = o.id "
            "WHERE observations_fts MATCH ? AND o.archived = 0 "
            "  AND (o.project_root = ? OR o.is_global = 1) "
        )
        params.extend([query, project_root])

        if not include_quarantine:
            sql += "AND (c.quarantine IS NULL OR c.quarantine = 0) "

        if type_filter:
            sql += "AND o.type = ? "
            params.append(type_filter)

        sql += "ORDER BY rank LIMIT ?"
        params.append(limit)

        rows = conn.execute(sql, params).fetchall()
        result = [dict(r) for r in rows]
        for r in result:
            r["age"] = relative_age(r.get("created_at_epoch"))
            r["stale_suspected"] = bool(r.get("stale_suspected"))
            r["quarantine"] = bool(r.get("quarantine"))
        conn.close()

        if result:
            _bump_access([r["id"] for r in result])

        return result
    except sqlite3.Error as exc:
        print(f"[token-savior:memory] observation_search error: {exc}", file=sys.stderr)
        return []


def observation_get(ids: list[int]) -> list[dict]:
    """Fetch full observation details by IDs (batch)."""
    if not ids:
        return []
    try:
        conn = get_db()
        placeholders = ",".join("?" for _ in ids)
        rows = conn.execute(
            f"SELECT * FROM observations WHERE id IN ({placeholders})",
            ids,
        ).fetchall()
        result = [dict(r) for r in rows]
        conn.close()

        if result:
            _bump_access([r["id"] for r in result])

        return result
    except sqlite3.Error as exc:
        print(f"[token-savior:memory] observation_get error: {exc}", file=sys.stderr)
        return []


def observation_get_by_session(session_id: int) -> list[dict]:
    """Return observations attached to a session (chronological)."""
    try:
        conn = get_db()
        rows = conn.execute(
            "SELECT id, type, title, content, symbol, file_path, created_at "
            "FROM observations WHERE session_id=? AND archived=0 "
            "ORDER BY created_at_epoch ASC",
            (session_id,),
        ).fetchall()
        result = [dict(r) for r in rows]
        conn.close()
        return result
    except sqlite3.Error as exc:
        print(f"[token-savior:memory] observation_get_by_session error: {exc}", file=sys.stderr)
        return []


def observation_get_by_symbol(
    project_root: str,
    symbol: str,
    *,
    file_path: str | None = None,
    limit: int = 5,
) -> list[dict]:
    """Get compact observation list linked to a symbol (for footer injection)."""
    try:
        conn = get_db()
        params: list[Any] = [project_root]

        ctx_like = f"%{symbol}%"
        if file_path:
            sql = (
                "SELECT id, type, title, symbol, context, created_at, created_at_epoch, is_global "
                "FROM observations "
                "WHERE archived=0 AND (project_root=? OR is_global=1) "
                "  AND (symbol=? OR file_path=? OR context LIKE ?) "
                "ORDER BY created_at_epoch DESC LIMIT ?"
            )
            params.extend([symbol, file_path, ctx_like, limit])
        else:
            sql = (
                "SELECT id, type, title, symbol, context, created_at, created_at_epoch, is_global "
                "FROM observations "
                "WHERE archived=0 AND (project_root=? OR is_global=1) "
                "  AND (symbol=? OR context LIKE ?) "
                "ORDER BY created_at_epoch DESC LIMIT ?"
            )
            params.extend([symbol, ctx_like, limit])

        rows = conn.execute(sql, params).fetchall()
        result = [dict(r) for r in rows]
        conn.close()
        for r in result:
            r["age"] = relative_age(r.get("created_at_epoch"))
            r["stale"] = check_symbol_staleness(
                project_root, r.get("symbol") or symbol, r.get("created_at_epoch") or 0
            ) if r.get("symbol") or symbol else False
        return result
    except sqlite3.Error as exc:
        print(f"[token-savior:memory] observation_get_by_symbol error: {exc}", file=sys.stderr)
        return []


def observation_update(
    obs_id: int,
    *,
    title: str | None = None,
    content: str | None = None,
    why: str | None = None,
    how_to_apply: str | None = None,
    tags: list[str] | None = None,
    importance: int | None = None,
    archived: bool | None = None,
) -> bool:
    """Update fields on an existing observation. Returns True on success."""
    sets: list[str] = []
    params: list[Any] = []

    if title is not None:
        sets.append("title=?")
        params.append(title)
    if content is not None:
        sets.append("content=?")
        params.append(content)
    if why is not None:
        sets.append("why=?")
        params.append(why)
    if how_to_apply is not None:
        sets.append("how_to_apply=?")
        params.append(how_to_apply)
    if tags is not None:
        sets.append("tags=?")
        params.append(_json_dumps(tags))
    if importance is not None:
        sets.append("importance=?")
        params.append(importance)
    if archived is not None:
        sets.append("archived=?")
        params.append(1 if archived else 0)

    if not sets:
        return False

    sets.append("updated_at=?")
    params.append(_now_iso())
    params.append(obs_id)

    try:
        conn = get_db()
        cur = conn.execute(
            f"UPDATE observations SET {', '.join(sets)} WHERE id=?",
            params,
        )
        conn.commit()
        changed = cur.rowcount > 0
        conn.close()
        return changed
    except sqlite3.Error as exc:
        print(f"[token-savior:memory] observation_update error: {exc}", file=sys.stderr)
        return False


def observation_delete(obs_id: int) -> bool:
    """Soft-delete (archive) an observation. Returns True if found."""
    ok = observation_update(obs_id, archived=True)
    if ok:
        try:
            invalidate_memory_cache()
        except Exception:
            pass
    return ok


# ---------------------------------------------------------------------------
# Summaries
# ---------------------------------------------------------------------------


def summary_save(
    session_id: int,
    project_root: str,
    content: str,
    observation_ids: list[int],
) -> int:
    """Save a consolidation summary covering a set of observations."""
    now = _now_iso()
    epoch = _now_epoch()

    covers_until: int | None = None
    if observation_ids:
        try:
            conn = get_db()
            placeholders = ",".join("?" for _ in observation_ids)
            row = conn.execute(
                f"SELECT MAX(created_at_epoch) FROM observations WHERE id IN ({placeholders})",
                observation_ids,
            ).fetchone()
            if row and row[0]:
                covers_until = row[0]
            conn.close()
        except sqlite3.Error:
            pass

    try:
        conn = get_db()
        cur = conn.execute(
            "INSERT INTO summaries "
            "(session_id, project_root, content, observation_ids, covers_until_epoch, "
            " created_at, created_at_epoch) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (session_id, project_root, content, _json_dumps(observation_ids), covers_until, now, epoch),
        )
        conn.commit()
        sid = cur.lastrowid
        conn.close()
        return sid  # type: ignore[return-value]
    except sqlite3.Error as exc:
        print(f"[token-savior:memory] summary_save error: {exc}", file=sys.stderr)
        raise


# ---------------------------------------------------------------------------
# Index & Timeline (progressive disclosure)
# ---------------------------------------------------------------------------


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
        db = get_db()
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
        conn = get_db()
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
        conn = get_db()
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
        conn = get_db()
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


# ---------------------------------------------------------------------------
# Events
# ---------------------------------------------------------------------------


from token_savior.memory.events import event_save  # noqa: E402,F401  re-export


# ---------------------------------------------------------------------------
# User prompts
# ---------------------------------------------------------------------------


from token_savior.memory.prompts import prompt_save, prompt_search  # noqa: E402,F401  re-exports


# ---------------------------------------------------------------------------
# Stats
# ---------------------------------------------------------------------------


from token_savior.memory.stats import get_stats  # noqa: E402,F401  re-export


# ---------------------------------------------------------------------------
# Decay
# ---------------------------------------------------------------------------




# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------




# ---------------------------------------------------------------------------
# Decay
# ---------------------------------------------------------------------------


from token_savior.memory.decay import (  # noqa: E402,F401  re-exports
    _ZERO_ACCESS_RULES,
    _bump_access,
    _decay_candidates_sql,
    _recalculate_relevance_scores,
    run_decay,
)


# ---------------------------------------------------------------------------
# Token Economy ROI — Garbage Collection based on expected value of retention.
# ---------------------------------------------------------------------------
# ROI(o) = tokens_saved_per_hit × P(hit) × horizon_days × TYPE_MULTIPLIER − tokens_stored
# P(hit) = exp(−λ × days_since_access) × (1 + 0.1 × access_count)
# An observation with ROI below ROI_THRESHOLD is a candidate for archival.

from token_savior.memory.roi import (  # noqa: E402,F401  re-exports
    _ROI_HORIZON_DAYS,
    _ROI_LAMBDA,
    _ROI_THRESHOLD,
    _ROI_TOKENS_PER_HIT,
    _ROI_TYPE_MULTIPLIER,
    compute_observation_roi,
    get_roi_stats,
    run_roi_gc,
)


# ---------------------------------------------------------------------------
# MDL Memory Distillation — crystallize similar obs into abstractions.
# ---------------------------------------------------------------------------

from token_savior.memory.distillation import get_mdl_stats, run_mdl_distillation  # noqa: E402,F401  re-exports


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
        with db_session() as db:
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


_TYPE_PRIORITY = {
    "guardrail": "critical", "convention": "high", "warning": "high",
    "command": "medium", "decision": "medium", "infra": "medium",
    "config": "medium", "bugfix": "low", "note": "low",
    "research": "low", "idea": "low", "error_pattern": "high",
}


def explain_observation(obs_id: int, query: str | None = None) -> dict[str, Any]:
    """Trace why an observation would appear in results."""
    try:
        db = get_db()
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


from token_savior.memory.dedup import (  # noqa: E402,F401  re-exports
    get_injection_stats,
    global_dedup_check,
    semantic_dedup_check,
)


# ---------------------------------------------------------------------------
# Closed-loop budget (Step B)
# ---------------------------------------------------------------------------

# Claude Max effective context window. Treat as a soft ceiling for budgeting;
# we measure observable consumption only (tokens we injected via hooks).
from token_savior.memory.budget import (  # noqa: E402,F401  re-exports
    DEFAULT_SESSION_BUDGET_TOKENS,
    format_session_budget_box,
    get_session_budget_stats,
)


from token_savior.memory._text_utils import _jaccard  # noqa: E402,F401  re-export


from token_savior.memory.health import run_health_check  # noqa: E402,F401  re-export


def relink_all(project_root: str, dry_run: bool = False) -> dict[str, Any]:
    """Replay auto_link_observation() over all active obs to backfill links."""
    db = get_db()
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

    db = get_db()
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
        db = get_db()
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


from token_savior.memory._text_utils import _STOPWORDS, _TOKEN_RE  # noqa: E402,F401  re-export


from token_savior.memory.prompts import analyze_prompt_patterns  # noqa: E402,F401  re-export


def run_promotions(project_root: str = "", dry_run: bool = False) -> dict[str, Any]:
    """Promote frequently-accessed observations to stronger types.

    Empty project_root = scan all projects.
    """
    now = int(time.time())
    recent_cutoff = now - 30 * 86400
    promoted: list[dict] = []
    try:
        db = get_db()
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


def observation_restore(obs_id: int) -> bool:
    """Un-archive an observation."""
    try:
        conn = get_db()
        cur = conn.execute("UPDATE observations SET archived=0 WHERE id=?", (obs_id,))
        conn.commit()
        ok = cur.rowcount > 0
        conn.close()
        return ok
    except sqlite3.Error:
        return False


def observation_list_archived(project_root: str | None = None, limit: int = 50) -> list[dict]:
    """List currently-archived observations."""
    try:
        conn = get_db()
        if project_root:
            rows = conn.execute(
                "SELECT id, type, title, created_at, project_root "
                "FROM observations WHERE archived=1 AND project_root=? "
                "ORDER BY created_at_epoch DESC LIMIT ?",
                (project_root, limit),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT id, type, title, created_at, project_root "
                "FROM observations WHERE archived=1 "
                "ORDER BY created_at_epoch DESC LIMIT ?",
                (limit,),
            ).fetchall()
        out = [dict(r) for r in rows]
        conn.close()
        return out
    except sqlite3.Error:
        return []


def summary_parse(content: str) -> dict[str, Any]:
    """Parse a structured summary into {changes:[...], memory:[...]}."""
    sections = {"changes": [], "memory": []}
    if not content:
        return sections
    current: str | None = None
    for raw in content.splitlines():
        line = raw.strip()
        if not line:
            continue
        low = line.lower().lstrip("#").strip()
        if low.startswith("changements") or low.startswith("changes") or low.startswith("changement"):
            current = "changes"
            continue
        if low.startswith("mémoire") or low.startswith("memoire") or low.startswith("memory"):
            current = "memory"
            continue
        if line.startswith(("- ", "* ", "• ")):
            item = line[2:].strip()
            if current and item:
                sections[current].append(item)
    return sections


# ---------------------------------------------------------------------------
# Corpora (thematic bundles)
# ---------------------------------------------------------------------------


from token_savior.memory.corpora import corpus_build, corpus_get  # noqa: E402,F401  re-exports


# ---------------------------------------------------------------------------
# Capture modes (split into memory/modes.py)
# ---------------------------------------------------------------------------

from token_savior.memory.modes import (  # noqa: E402,F401  re-exports
    ACTIVITY_TRACKER_PATH,
    DEFAULT_MODES,
    MODE_CONFIG_PATH,
    SESSION_OVERRIDE_PATH,
    _load_mode_file,
    _read_activity_tracker,
    _read_session_override,
    _write_activity_tracker,
    clear_session_override,
    get_current_mode,
    list_modes,
    set_mode,
    set_project_mode,
    set_session_override,
)


# ---------------------------------------------------------------------------
# Telegram notifications
# ---------------------------------------------------------------------------


from token_savior.memory.notifications import notify_telegram  # noqa: E402,F401  re-export
