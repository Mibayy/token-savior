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


from token_savior.memory.summaries import summary_parse, summary_save  # noqa: E402,F401  re-exports


# ---------------------------------------------------------------------------
# Index & Timeline (progressive disclosure)
# ---------------------------------------------------------------------------


from token_savior.memory.index import (  # noqa: E402,F401  re-exports
    _TYPE_SCORES,
    _ensure_memory_cache,
    compute_obs_score,
    get_recent_index,
    get_timeline_around,
    get_top_observations,
    invalidate_memory_cache,
)


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


from token_savior.memory.links import (  # noqa: E402,F401  re-exports
    _PROMOTION_RULES,
    _PROMOTION_TYPE_RANK,
    _ensure_links_index,
    auto_link_observation,
)


from token_savior.memory.links import (  # noqa: E402,F401  re-exports
    _TYPE_PRIORITY,
    explain_observation,
)


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


from token_savior.memory.links import relink_all  # noqa: E402,F401  re-export


from token_savior.memory.links import get_linked_observations  # noqa: E402,F401  re-export


from token_savior.memory._text_utils import _STOPWORDS, _TOKEN_RE  # noqa: E402,F401  re-export


from token_savior.memory.prompts import analyze_prompt_patterns  # noqa: E402,F401  re-export


from token_savior.memory.links import run_promotions  # noqa: E402,F401  re-export


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
