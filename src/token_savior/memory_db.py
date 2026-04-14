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


def _recalculate_relevance_scores() -> int:
    """Recalculate relevance scores based on decay config. Returns updated count."""
    try:
        conn = get_db()
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


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _bump_access(ids: list[int]) -> None:
    """Increment access_count and update last_accessed_at/epoch for given IDs."""
    if not ids:
        return
    now = _now_iso()
    epoch = _now_epoch()
    try:
        conn = get_db()
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


# ---------------------------------------------------------------------------
# Decay
# ---------------------------------------------------------------------------


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


_ZERO_ACCESS_RULES = [
    ("note", 30),
    ("research", 45),
    ("idea", 60),
    ("bugfix", 90),
]


def run_decay(project_root: str | None = None, dry_run: bool = True) -> dict[str, Any]:
    """Archive observations eligible for decay. Returns counts + preview."""
    sql, params = _decay_candidates_sql()
    if project_root:
        sql += "AND project_root = ? "
        params.append(project_root)
    sql += "ORDER BY created_at_epoch ASC"

    try:
        with db_session() as conn:
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


# ---------------------------------------------------------------------------
# Token Economy ROI — Garbage Collection based on expected value of retention.
# ---------------------------------------------------------------------------
# ROI(o) = tokens_saved_per_hit × P(hit) × horizon_days × TYPE_MULTIPLIER − tokens_stored
# P(hit) = exp(−λ × days_since_access) × (1 + 0.1 × access_count)
# An observation with ROI below ROI_THRESHOLD is a candidate for archival.

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
    # decay_immune observations always get a floor boost so they're never GC'd
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
        with db_session() as conn:
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
        conn = get_db()
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


# ---------------------------------------------------------------------------
# MDL Memory Distillation — crystallize similar obs into abstractions.
# ---------------------------------------------------------------------------

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
        with db_session() as conn:
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
      with db_session() as conn:
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
        conn = get_db()
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


def global_dedup_check(
    title: str, content: str, obs_type: str, threshold: float = 0.85
) -> dict[str, Any] | None:
    """Cross-project dedup for globals. Returns best global match (content_hash or Jaccard)."""
    try:
        db = get_db()
        rows = db.execute(
            "SELECT id, title, content, type, project_root, content_hash "
            "FROM observations WHERE archived=0 AND is_global=1 AND type=?",
            [obs_type],
        ).fetchall()
        db.close()
    except sqlite3.Error:
        return None
    import hashlib as _h
    norm = (content or "").strip().lower()
    chash = _h.sha256(norm.encode("utf-8")).hexdigest() if norm else None
    best = None
    best_score = 0.0
    for r in rows:
        if chash and r["content_hash"] and r["content_hash"].endswith(chash[:16]):
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
        db = get_db()
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
        db = get_db()
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


# ---------------------------------------------------------------------------
# Closed-loop budget (Step B)
# ---------------------------------------------------------------------------

# Claude Max effective context window. Treat as a soft ceiling for budgeting;
# we measure observable consumption only (tokens we injected via hooks).
DEFAULT_SESSION_BUDGET_TOKENS = 200_000


def get_session_budget_stats(
    project_root: str,
    *,
    budget_tokens: int = DEFAULT_SESSION_BUDGET_TOKENS,
) -> dict[str, Any]:
    """Return the current/most-recent session's token budget consumption.

    Picks the active session for *project_root* if one exists, otherwise the
    most recent completed session. Returns a dict shaped for both the MCP tool
    and the CLI box renderer.

    Status thresholds:
      - 🟢 green   : pct_used < 50
      - 🟡 yellow  : 50 <= pct_used <= 75
      - 🔴 red     : pct_used > 75   (auto-injected during PreCompact)
    """
    out: dict[str, Any] = {
        "project_root": project_root,
        "session_id": None,
        "status_label": "active",
        "tokens_injected": 0,
        "tokens_saved_est": 0,
        "budget_tokens": budget_tokens,
        "pct_used": 0.0,
        "pct_saved": 0.0,
        "indicator": "🟢",
        "level": "green",
        "started_at": None,
    }
    try:
        db = get_db()
        # Prefer active session, else most recent.
        row = db.execute(
            "SELECT id, status, COALESCE(tokens_injected, 0) AS tokens_injected, "
            "       COALESCE(tokens_saved_est, 0) AS tokens_saved_est, "
            "       created_at, created_at_epoch "
            "FROM sessions "
            "WHERE project_root=? AND status='active' "
            "ORDER BY created_at_epoch DESC LIMIT 1",
            (project_root,),
        ).fetchone()
        if row is None:
            row = db.execute(
                "SELECT id, status, COALESCE(tokens_injected, 0) AS tokens_injected, "
                "       COALESCE(tokens_saved_est, 0) AS tokens_saved_est, "
                "       created_at, created_at_epoch "
                "FROM sessions "
                "WHERE project_root=? "
                "ORDER BY created_at_epoch DESC LIMIT 1",
                (project_root,),
            ).fetchone()
        db.close()
    except sqlite3.Error as exc:
        print(f"[token-savior:memory] get_session_budget_stats error: {exc}", file=sys.stderr)
        return out

    if row is None:
        return out

    d = dict(row)
    injected = int(d.get("tokens_injected") or 0)
    saved = int(d.get("tokens_saved_est") or 0)
    pct_used = (injected / budget_tokens * 100.0) if budget_tokens else 0.0
    pct_saved = (saved / budget_tokens * 100.0) if budget_tokens else 0.0
    if pct_used > 75:
        indicator, level = "🔴", "red"
    elif pct_used >= 50:
        indicator, level = "🟡", "yellow"
    else:
        indicator, level = "🟢", "green"

    out.update(
        session_id=d["id"],
        status_label=d.get("status") or "active",
        tokens_injected=injected,
        tokens_saved_est=saved,
        pct_used=round(pct_used, 1),
        pct_saved=round(pct_saved, 1),
        indicator=indicator,
        level=level,
        started_at=d.get("created_at"),
    )
    return out


def format_session_budget_box(stats: dict[str, Any]) -> str:
    """Render get_session_budget_stats() as a 60-char status box."""
    pct = stats.get("pct_used", 0.0)
    bar_w = 40
    filled = max(0, min(bar_w, int(round(pct / 100.0 * bar_w))))
    bar = "█" * filled + "·" * (bar_w - filled)
    sid = stats.get("session_id") or "—"
    project = stats.get("project_root") or "(none)"
    status = stats.get("status_label", "?")
    indicator = stats.get("indicator", "🟢")
    level = stats.get("level", "green")
    injected = stats.get("tokens_injected", 0)
    saved = stats.get("tokens_saved_est", 0)
    budget = stats.get("budget_tokens", DEFAULT_SESSION_BUDGET_TOKENS)
    pct_saved = stats.get("pct_saved", 0.0)
    started = (stats.get("started_at") or "")[:19]
    proj_name = project.rstrip("/").split("/")[-1] or project
    lines = [
        "┌─ Session Budget ─────────────────────────────────────────┐",
        f"│ Session #{sid}  · {status:<10} · started {started:<19} │",
        f"│ Project: {proj_name[:48]:<48}      │",
        f"│ Injected : {injected:>7,} tok  ({pct:>5.1f}% of {budget:>6,})        │",
        f"│ Saved est: {saved:>7,} tok  ({pct_saved:>5.1f}% of {budget:>6,})        │",
        f"│ {indicator}  {level.upper():<6}  [{bar}]  │",
        "└──────────────────────────────────────────────────────────┘",
    ]
    return "\n".join(lines)


from token_savior.memory._text_utils import _jaccard  # noqa: E402,F401  re-export


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
        db = get_db()
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
