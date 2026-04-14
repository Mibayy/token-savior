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


def check_symbol_staleness(project_root: str, symbol: str, obs_created_epoch: int) -> bool:
    """True if the git log shows `symbol` was modified after the obs was created.

    Strictly best-effort: 3s timeout, silent failure → returns False.
    """
    try:
        import subprocess

        if not project_root or not os.path.isdir(os.path.join(project_root, ".git")):
            return False
        result = subprocess.run(
            ["git", "log", "-1", "--format=%ct", "-S", symbol, "--", "."],
            cwd=project_root,
            capture_output=True,
            text=True,
            timeout=3,
        )
        if result.returncode == 0 and result.stdout.strip():
            return int(result.stdout.strip()) > int(obs_created_epoch)
    except Exception:
        pass
    return False


def compute_continuity_score(project_root: str) -> dict[str, Any]:
    """Memory continuity score: share of obs not yet stale by the decay heuristic."""
    try:
        conn = get_db()
        total = conn.execute(
            "SELECT COUNT(*) FROM observations WHERE project_root=? AND archived=0",
            [project_root],
        ).fetchone()[0]
        if total == 0:
            conn.close()
            return {"score": 0, "valid": 0, "total": 0, "recent": 0,
                    "potentially_stale": 0, "label": "No memory"}

        now = int(time.time())
        recent_cutoff = now - 7 * 86400
        stale_cutoff = now - 30 * 86400

        recent = conn.execute(
            "SELECT COUNT(*) FROM observations "
            "WHERE project_root=? AND archived=0 AND created_at_epoch > ?",
            [project_root, recent_cutoff],
        ).fetchone()[0]
        potentially_stale = conn.execute(
            "SELECT COUNT(*) FROM observations "
            "WHERE project_root=? AND archived=0 "
            "  AND created_at_epoch < ? "
            "  AND (last_accessed_epoch IS NULL OR last_accessed_epoch < ?) "
            "  AND decay_immune=0",
            [project_root, stale_cutoff, stale_cutoff],
        ).fetchone()[0]
        conn.close()

        valid = max(0, total - potentially_stale)
        score = int((valid / total) * 100) if total > 0 else 0
        if score >= 80:
            label = "Strong"
        elif score >= 60:
            label = "Good"
        elif score >= 40:
            label = "Degraded"
        else:
            label = "Weak"

        return {
            "score": score, "valid": valid, "total": total,
            "recent": recent, "potentially_stale": potentially_stale, "label": label,
        }
    except sqlite3.Error as exc:
        print(f"[token-savior:memory] compute_continuity_score error: {exc}", file=sys.stderr)
        return {"score": 0, "valid": 0, "total": 0, "recent": 0,
                "potentially_stale": 0, "label": "Error"}


# ---------------------------------------------------------------------------
# Self-consistency (Bayesian validity) — Step C
# ---------------------------------------------------------------------------

#: Validity below this threshold quarantines the observation.
CONSISTENCY_QUARANTINE_THRESHOLD = 0.40
#: Validity below this threshold flags the observation as stale-suspected (⚠️).
CONSISTENCY_STALE_THRESHOLD = 0.60


def get_validity_score(obs_id: int) -> dict[str, Any]:
    """Return current Bayesian validity for an observation.

    Validity = α / (α + β). New observations default to (α=2.0, β=1.0) which
    biases the prior toward "valid" — only repeated negative checks flip it.
    """
    try:
        conn = get_db()
        row = conn.execute(
            "SELECT validity_alpha, validity_beta, last_checked_epoch, "
            "stale_suspected, quarantine FROM consistency_scores WHERE obs_id=?",
            [obs_id],
        ).fetchone()
        conn.close()
    except sqlite3.Error as exc:
        print(f"[token-savior:memory] get_validity_score error: {exc}", file=sys.stderr)
        return {"obs_id": obs_id, "validity": 1.0, "alpha": 2.0, "beta": 1.0,
                "last_checked_epoch": None, "stale_suspected": False,
                "quarantine": False, "exists": False}
    if row is None:
        return {"obs_id": obs_id, "validity": 2.0 / 3.0, "alpha": 2.0, "beta": 1.0,
                "last_checked_epoch": None, "stale_suspected": False,
                "quarantine": False, "exists": False}
    a, b = row["validity_alpha"], row["validity_beta"]
    return {
        "obs_id": obs_id,
        "validity": a / (a + b) if (a + b) > 0 else 1.0,
        "alpha": a, "beta": b,
        "last_checked_epoch": row["last_checked_epoch"],
        "stale_suspected": bool(row["stale_suspected"]),
        "quarantine": bool(row["quarantine"]),
        "exists": True,
    }


def update_consistency_score(obs_id: int, success: bool) -> dict[str, Any]:
    """Record one Bayesian check outcome — bumps α on success, β on failure.

    Recomputes ``stale_suspected`` and ``quarantine`` flags from the new
    posterior validity. Returns the updated record.
    """
    try:
        with db_session() as conn:
            now = int(time.time())
            row = conn.execute(
                "SELECT validity_alpha, validity_beta FROM consistency_scores WHERE obs_id=?",
                [obs_id],
            ).fetchone()
            if row is None:
                alpha, beta = 2.0, 1.0
            else:
                alpha, beta = row["validity_alpha"], row["validity_beta"]
            if success:
                alpha += 1.0
            else:
                beta += 1.0
            validity = alpha / (alpha + beta)
            quarantine = 1 if validity < CONSISTENCY_QUARANTINE_THRESHOLD else 0
            stale = 1 if (not quarantine and validity < CONSISTENCY_STALE_THRESHOLD) else 0
            conn.execute(
                "INSERT INTO consistency_scores "
                "(obs_id, validity_alpha, validity_beta, last_checked_epoch, "
                "stale_suspected, quarantine) VALUES (?,?,?,?,?,?) "
                "ON CONFLICT(obs_id) DO UPDATE SET "
                "validity_alpha=excluded.validity_alpha, "
                "validity_beta=excluded.validity_beta, "
                "last_checked_epoch=excluded.last_checked_epoch, "
                "stale_suspected=excluded.stale_suspected, "
                "quarantine=excluded.quarantine",
                [obs_id, alpha, beta, now, stale, quarantine],
            )
            conn.commit()
    except sqlite3.Error as exc:
        print(f"[token-savior:memory] update_consistency_score error: {exc}", file=sys.stderr)
        return {"obs_id": obs_id, "validity": 0.0, "alpha": 0.0, "beta": 0.0,
                "stale_suspected": False, "quarantine": False}
    return {
        "obs_id": obs_id, "validity": validity, "alpha": alpha, "beta": beta,
        "stale_suspected": bool(stale), "quarantine": bool(quarantine),
    }


def run_consistency_check(
    project_root: str | None = None,
    *,
    limit: int = 100,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Sweep symbol-linked observations and update Bayesian validity.

    Failure = ``check_symbol_staleness`` says the symbol moved after the obs
    was created. We pick the ``limit`` candidates with the oldest
    ``last_checked_epoch`` (NULL first) so freshly-added obs get vetted.
    """
    try:
        params: list[Any] = []
        sql = (
            "SELECT o.id, o.project_root, o.symbol, o.created_at_epoch "
            "FROM observations AS o "
            "LEFT JOIN consistency_scores AS c ON c.obs_id = o.id "
            "WHERE o.archived = 0 AND o.symbol IS NOT NULL AND o.symbol != '' "
        )
        if project_root:
            sql += "AND o.project_root = ? "
            params.append(project_root)
        sql += "ORDER BY (c.last_checked_epoch IS NULL) DESC, c.last_checked_epoch ASC LIMIT ?"
        params.append(limit)
        with db_session() as conn:
            rows = conn.execute(sql, params).fetchall()
    except sqlite3.Error as exc:
        print(f"[token-savior:memory] run_consistency_check error: {exc}", file=sys.stderr)
        return {"checked": 0, "failed": 0, "quarantined": 0, "stale_suspected": 0}

    checked = 0
    failed = 0
    quarantined_now = 0
    stale_now = 0
    for r in rows:
        moved = check_symbol_staleness(r["project_root"], r["symbol"], r["created_at_epoch"] or 0)
        success = not moved
        checked += 1
        if not success:
            failed += 1
        if dry_run:
            continue
        res = update_consistency_score(r["id"], success)
        if res.get("quarantine"):
            quarantined_now += 1
        elif res.get("stale_suspected"):
            stale_now += 1

    return {
        "checked": checked,
        "failed": failed,
        "quarantined": quarantined_now,
        "stale_suspected": stale_now,
        "dry_run": dry_run,
    }


def get_consistency_stats(project_root: str | None = None) -> dict[str, Any]:
    """Aggregate quarantine / stale counts across observations."""
    try:
        conn = get_db()
        params: list[Any] = []
        join = (
            "FROM consistency_scores AS c "
            "JOIN observations AS o ON o.id = c.obs_id "
            "WHERE o.archived = 0 "
        )
        if project_root:
            join += "AND o.project_root = ? "
            params.append(project_root)
        scored = conn.execute("SELECT COUNT(*) " + join, params).fetchone()[0]
        quarantined = conn.execute(
            "SELECT COUNT(*) " + join + "AND c.quarantine = 1", params,
        ).fetchone()[0]
        stale = conn.execute(
            "SELECT COUNT(*) " + join + "AND c.stale_suspected = 1", params,
        ).fetchone()[0]
        avg_row = conn.execute(
            "SELECT AVG(c.validity_alpha / (c.validity_alpha + c.validity_beta)) " + join,
            params,
        ).fetchone()
        conn.close()
    except sqlite3.Error as exc:
        print(f"[token-savior:memory] get_consistency_stats error: {exc}", file=sys.stderr)
        return {"scored": 0, "quarantined": 0, "stale_suspected": 0, "avg_validity": 0.0}
    avg = float(avg_row[0]) if avg_row and avg_row[0] is not None else 0.0
    return {
        "scored": scored,
        "quarantined": quarantined,
        "stale_suspected": stale,
        "avg_validity": avg,
    }


def list_quarantined_observations(
    project_root: str | None = None, *, limit: int = 50,
) -> list[dict]:
    """List quarantined observations with their validity score."""
    try:
        conn = get_db()
        params: list[Any] = []
        sql = (
            "SELECT o.id, o.type, o.title, o.symbol, o.project_root, "
            "  o.created_at_epoch, c.validity_alpha, c.validity_beta, "
            "  c.last_checked_epoch "
            "FROM consistency_scores AS c "
            "JOIN observations AS o ON o.id = c.obs_id "
            "WHERE c.quarantine = 1 AND o.archived = 0 "
        )
        if project_root:
            sql += "AND o.project_root = ? "
            params.append(project_root)
        sql += "ORDER BY (c.validity_alpha / (c.validity_alpha + c.validity_beta)) ASC LIMIT ?"
        params.append(limit)
        rows = conn.execute(sql, params).fetchall()
        conn.close()
    except sqlite3.Error as exc:
        print(f"[token-savior:memory] list_quarantined_observations error: {exc}", file=sys.stderr)
        return []
    out = []
    for r in rows:
        a, b = r["validity_alpha"], r["validity_beta"]
        d = dict(r)
        d["validity"] = a / (a + b) if (a + b) > 0 else 0.0
        d["age"] = relative_age(r["created_at_epoch"])
        out.append(d)
    return out


# ---------------------------------------------------------------------------
# Sessions
# ---------------------------------------------------------------------------


def session_start(project_root: str) -> int:
    """Create a new active session. Returns the session id."""
    now = _now_iso()
    epoch = _now_epoch()
    try:
        with db_session() as conn:
            cur = conn.execute(
                "INSERT INTO sessions (project_root, status, created_at, created_at_epoch) "
                "VALUES (?, 'active', ?, ?)",
                (project_root, now, epoch),
            )
            conn.commit()
            return cur.lastrowid  # type: ignore[return-value]
    except sqlite3.Error as exc:
        print(f"[token-savior:memory] session_start error: {exc}", file=sys.stderr)
        raise


def session_end(
    session_id: int,
    summary: str | None = None,
    symbols_changed: list[str] | None = None,
    files_changed: list[str] | None = None,
    end_type: str = "completed",
) -> None:
    """Mark a session as closed with optional summary data.

    end_type: "completed" (clean SessionEnd) or "interrupted" (Stop hook).
    The status column stays "completed" (satisfies existing CHECK constraint);
    end_type carries the distinction.
    """
    now = _now_iso()
    epoch = _now_epoch()
    try:
        with db_session() as conn:
            conn.execute(
                "UPDATE sessions SET status='completed', end_type=?, completed_at=?, completed_at_epoch=?, "
                "summary=?, symbols_changed=?, files_changed=? WHERE id=?",
                (end_type, now, epoch, summary, _json_dumps(symbols_changed), _json_dumps(files_changed), session_id),
            )
            conn.commit()
    except sqlite3.Error as exc:
        print(f"[token-savior:memory] session_end error: {exc}", file=sys.stderr)


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


_RULE_TYPES_FOR_CONTRADICTION = frozenset(
    {"guardrail", "convention", "warning", "command", "config"}
)
_CONTRADICTION_OPPOSITES = [
    (r"\bjamais\b",  r"\btoujours\b"),
    (r"\bnever\b",   r"\balways\b"),
    (r"\bdisable\b", r"\benable\b"),
    (r"\bne pas\b",  r"\butiliser\b"),
    (r"\bavoid\b",   r"\buse\b"),
    (r"\boff\b",     r"\bon\b"),
]


def detect_contradictions(
    project_root: str, title: str, content: str, obs_type: str
) -> list[dict]:
    """Find existing rule-type obs that may contradict a new one."""
    if obs_type not in _RULE_TYPES_FOR_CONTRADICTION:
        return []
    import re as _re
    text = f"{title or ''} {content or ''}".lower()
    targets: list[str] = []
    for pos_a, pos_b in _CONTRADICTION_OPPOSITES:
        if _re.search(pos_a, text):
            targets.append(pos_b)
        if _re.search(pos_b, text):
            targets.append(pos_a)
    if not targets:
        return []

    conflicts: list[dict] = []
    seen: set[int] = set()
    try:
        db = get_db()
        for raw in targets:
            token = _re.sub(r"\\b|\\", "", raw).strip()
            if not token:
                continue
            try:
                rows = db.execute(
                    "SELECT o.id, o.type, o.title, o.content, o.symbol, o.context "
                    "FROM observations_fts f "
                    "JOIN observations o ON o.id = f.rowid "
                    "WHERE observations_fts MATCH ? "
                    "  AND o.project_root = ? "
                    "  AND o.archived = 0 "
                    "  AND o.type IN ('guardrail','convention','warning','command','config') "
                    "LIMIT 5",
                    (f'"{token}"', project_root),
                ).fetchall()
            except sqlite3.Error:
                rows = []
            for r in rows:
                if r["id"] in seen:
                    continue
                seen.add(r["id"])
                conflicts.append(dict(r))
        db.close()
    except sqlite3.Error as exc:
        print(f"[token-savior:memory] detect_contradictions error: {exc}", file=sys.stderr)
    return conflicts


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
DEFAULT_VOLATILE_TTL_DAYS = 1


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


def memory_bus_list(
    project_root: str,
    *,
    agent_id: str | None = None,
    limit: int = 20,
    include_expired: bool = False,
) -> list[dict]:
    """Return live bus messages for *project_root*, newest first.

    Filters on agent_id when provided. Skips expired rows by default.
    """
    try:
        conn = get_db()
        sql = (
            "SELECT id, type, title, content, symbol, file_path, agent_id, "
            "       created_at, created_at_epoch, expires_at_epoch "
            "FROM observations "
            "WHERE archived=0 AND agent_id IS NOT NULL "
            "  AND project_root=? "
        )
        params: list[Any] = [project_root]
        if agent_id:
            sql += "AND agent_id=? "
            params.append(agent_id)
        if not include_expired:
            sql += "AND (expires_at_epoch IS NULL OR expires_at_epoch > ?) "
            params.append(int(time.time()))
        sql += "ORDER BY created_at_epoch DESC LIMIT ?"
        params.append(limit)
        rows = conn.execute(sql, params).fetchall()
        conn.close()
    except sqlite3.Error as exc:
        print(f"[token-savior:memory] memory_bus_list error: {exc}", file=sys.stderr)
        return []
    out = [dict(r) for r in rows]
    for r in out:
        r["age"] = relative_age(r.get("created_at_epoch"))
    return out


# ---------------------------------------------------------------------------
# Reasoning Trace Compression (v2.2 Step A)
# ---------------------------------------------------------------------------


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
        conn = get_db()
        # Dedup on goal_hash within a project.
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
        conn = get_db()
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
        # Fallback: if FTS yielded nothing, widen to a LIKE scan.
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
        conn = get_db()
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
        conn = get_db()
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
        conn = get_db()
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
        conn = get_db()
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



# ---------------------------------------------------------------------------
# Step D: Adaptive Lattice (Beta-Binomial Thompson sampling on granularity)
# ---------------------------------------------------------------------------

# Granularity levels for source-fetching tools:
#   0 = full source (no compression)
#   1 = signature + docstring + first/last lines
#   2 = signature only
#   3 = name + line range only
LATTICE_LEVELS = (0, 1, 2, 3)
LATTICE_CONTEXTS = ("navigation", "edit", "review", "unknown")


def _detect_context_type(call_sequence: list[str] | None, lookback: int = 5) -> str:
    """Classify the current context from the recent prefetcher call sequence.

    Heuristics:
      - 'edit'       → any of the last *lookback* states starts with an edit/mutate tool
      - 'review'     → any of the last states is a git/diff/changed-symbols tool
      - 'navigation' → the last states are read-only structural lookups
      - 'unknown'    → empty sequence
    """
    if not call_sequence:
        return "unknown"
    recent = call_sequence[-lookback:]
    edit_tools = {
        "replace_symbol_source", "insert_near_symbol",
        "apply_symbol_change_and_validate",
        "apply_symbol_change_validate_with_rollback",
        "Edit", "Write", "MultiEdit",
    }
    review_tools = {
        "get_git_status", "get_changed_symbols", "get_changed_symbols_since_ref",
        "summarize_patch_by_symbol", "build_commit_summary",
        "detect_breaking_changes", "compare_checkpoint_by_symbol",
    }
    nav_tools = {
        "get_function_source", "get_class_source", "find_symbol",
        "search_codebase", "get_dependencies", "get_dependents",
        "get_call_chain", "list_files", "get_structure_summary",
    }
    for state in reversed(recent):
        head = state.split(":", 1)[0]
        if head in edit_tools:
            return "edit"
        if head in review_tools:
            return "review"
        if head in nav_tools:
            return "navigation"
    return "unknown"


def _ensure_lattice_row(conn, context_type: str, level: int) -> None:
    epoch = _now_epoch()
    conn.execute(
        "INSERT OR IGNORE INTO adaptive_lattice "
        "(context_type, level, alpha, beta, updated_at_epoch) VALUES (?, ?, 1.0, 1.0, ?)",
        (context_type, level, epoch),
    )


def thompson_sample_level(context_type: str = "unknown") -> int:
    """Sample a granularity level via Beta-Binomial Thompson sampling.

    For each level draws from Beta(α, β) and returns the argmax. Cold-start
    rows have α=β=1 (uniform prior). Falls back to level 0 on any error.
    """
    if context_type not in LATTICE_CONTEXTS:
        context_type = "unknown"
    try:
        import random as _rnd
        conn = get_db()
        for lv in LATTICE_LEVELS:
            _ensure_lattice_row(conn, context_type, lv)
        conn.commit()
        rows = conn.execute(
            "SELECT level, alpha, beta FROM adaptive_lattice WHERE context_type=?",
            (context_type,),
        ).fetchall()
        conn.close()
    except sqlite3.Error as exc:
        print(f"[token-savior:memory] thompson_sample_level error: {exc}", file=sys.stderr)
        return 0

    samples: list[tuple[int, float]] = []
    for r in rows:
        d = dict(r)
        try:
            draw = _rnd.betavariate(max(d["alpha"], 0.01), max(d["beta"], 0.01))
        except ValueError:
            draw = 0.0
        samples.append((int(d["level"]), draw))
    if not samples:
        return 0
    return max(samples, key=lambda x: x[1])[0]


def record_lattice_feedback(context_type: str, level: int, success: bool) -> None:
    """Update the Beta posterior for (context_type, level): success → α+1, else β+1."""
    if context_type not in LATTICE_CONTEXTS:
        context_type = "unknown"
    if level not in LATTICE_LEVELS:
        return
    try:
        epoch = _now_epoch()
        conn = get_db()
        _ensure_lattice_row(conn, context_type, level)
        col = "alpha" if success else "beta"
        conn.execute(
            f"UPDATE adaptive_lattice SET {col}={col}+1.0, updated_at_epoch=? "
            "WHERE context_type=? AND level=?",
            (epoch, context_type, level),
        )
        conn.commit()
        conn.close()
    except sqlite3.Error as exc:
        print(f"[token-savior:memory] record_lattice_feedback error: {exc}", file=sys.stderr)


def get_lattice_stats(context_type: str | None = None) -> list[dict]:
    """Return the current Beta posteriors with derived mean and trial count.

    Filter by *context_type* when provided. Sorted by (context_type, level).
    """
    try:
        conn = get_db()
        if context_type:
            rows = conn.execute(
                "SELECT context_type, level, alpha, beta, updated_at_epoch "
                "FROM adaptive_lattice WHERE context_type=? "
                "ORDER BY level",
                (context_type,),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT context_type, level, alpha, beta, updated_at_epoch "
                "FROM adaptive_lattice ORDER BY context_type, level"
            ).fetchall()
        conn.close()
    except sqlite3.Error as exc:
        print(f"[token-savior:memory] get_lattice_stats error: {exc}", file=sys.stderr)
        return []
    out = []
    for r in rows:
        d = dict(r)
        a, b = float(d["alpha"]), float(d["beta"])
        trials = a + b - 2.0  # subtract the uniform prior counts
        mean = a / (a + b) if (a + b) > 0 else 0.0
        d["mean"] = round(mean, 3)
        d["trials"] = max(0, int(round(trials)))
        d["age"] = relative_age(d.get("updated_at_epoch"))
        out.append(d)
    return out





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


def event_save(
    session_id: int | None,
    type: str,
    *,
    severity: str = "info",
    data: dict[str, Any] | None = None,
    symbol: str | None = None,
    file_path: str | None = None,
) -> int | None:
    """Log a significant event (build fail, test fail, deploy, etc.)."""
    now = _now_iso()
    epoch = _now_epoch()
    try:
        conn = get_db()
        cur = conn.execute(
            "INSERT INTO events "
            "(session_id, type, severity, data, symbol, file_path, created_at, created_at_epoch) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (session_id, type, severity, _json_dumps(data), symbol, file_path, now, epoch),
        )
        conn.commit()
        eid = cur.lastrowid
        conn.close()
        return eid
    except sqlite3.Error as exc:
        print(f"[token-savior:memory] event_save error: {exc}", file=sys.stderr)
        return None


# ---------------------------------------------------------------------------
# User prompts
# ---------------------------------------------------------------------------


def prompt_save(
    session_id: int | None,
    project_root: str,
    prompt_text: str,
    prompt_number: int | None = None,
) -> int | None:
    """Store a user prompt for pattern analysis."""
    prompt_text = strip_private(prompt_text) or ""
    if not prompt_text or prompt_text == "[PRIVATE]":
        return None
    now = _now_iso()
    epoch = _now_epoch()
    try:
        conn = get_db()
        cur = conn.execute(
            "INSERT INTO user_prompts "
            "(session_id, project_root, prompt_text, prompt_number, created_at, created_at_epoch) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (session_id, project_root, prompt_text, prompt_number, now, epoch),
        )
        conn.commit()
        pid = cur.lastrowid
        conn.close()
        return pid
    except sqlite3.Error as exc:
        print(f"[token-savior:memory] prompt_save error: {exc}", file=sys.stderr)
        return None


def prompt_search(
    project_root: str,
    query: str,
    *,
    limit: int = 10,
) -> list[dict]:
    """FTS5 search across user prompts. Returns id, prompt_number, excerpt, created_at."""
    try:
        conn = get_db()
        rows = conn.execute(
            "SELECT p.id, p.prompt_number, p.created_at, "
            "snippet(user_prompts_fts, 0, '[', ']', '…', 12) AS excerpt "
            "FROM user_prompts_fts f "
            "JOIN user_prompts p ON p.id = f.rowid "
            "WHERE user_prompts_fts MATCH ? "
            "  AND (p.project_root = ? OR p.project_root IS NULL) "
            "ORDER BY p.created_at_epoch DESC LIMIT ?",
            (query, project_root, limit),
        ).fetchall()
        result = [dict(r) for r in rows]
        conn.close()
        return result
    except sqlite3.Error as exc:
        print(f"[token-savior:memory] prompt_search error: {exc}", file=sys.stderr)
        return []


# ---------------------------------------------------------------------------
# Stats
# ---------------------------------------------------------------------------


def get_stats(project_root: str | None = None) -> dict[str, Any]:
    """Memory stats: counts by type, project, freshness."""
    try:
        conn = get_db()
        where = ""
        params: list[Any] = []
        if project_root:
            where = "WHERE project_root=? AND archived=0"
            params = [project_root]
        else:
            where = "WHERE archived=0"

        total = conn.execute(f"SELECT COUNT(*) FROM observations {where}", params).fetchone()[0]

        type_rows = conn.execute(
            f"SELECT type, COUNT(*) AS cnt FROM observations {where} GROUP BY type ORDER BY cnt DESC",
            params,
        ).fetchall()

        project_rows = conn.execute(
            "SELECT project_root, COUNT(*) AS cnt FROM observations WHERE archived=0 "
            "GROUP BY project_root ORDER BY cnt DESC",
        ).fetchall()

        session_count = conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0]
        summary_count = conn.execute("SELECT COUNT(*) FROM summaries").fetchone()[0]
        event_count = conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]

        conn.close()
        return {
            "total_observations": total,
            "by_type": {r["type"]: r["cnt"] for r in type_rows},
            "by_project": {r["project_root"]: r["cnt"] for r in project_rows},
            "sessions": session_count,
            "summaries": summary_count,
            "events": event_count,
        }
    except sqlite3.Error as exc:
        print(f"[token-savior:memory] get_stats error: {exc}", file=sys.stderr)
        return {}


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


def analyze_prompt_patterns(
    project_root: str,
    *,
    window_days: int = 14,
    min_occurrences: int = 3,
    max_suggestions: int = 5,
) -> list[dict]:
    """Find recurring tokens in user prompts → suggest obs topics.

    Returns list of {token, count, sample_prompts, existing_obs_count}.
    """
    cutoff = int(time.time()) - window_days * 86400
    suggestions: list[dict] = []
    try:
        db = get_db()
        rows = db.execute(
            "SELECT id, prompt_text FROM user_prompts "
            "WHERE (project_root=? OR project_root IS NULL) AND created_at_epoch >= ? "
            "ORDER BY created_at_epoch DESC LIMIT 200",
            (project_root, cutoff),
        ).fetchall()

        from collections import Counter, defaultdict
        counter: Counter[str] = Counter()
        samples: dict[str, list[str]] = defaultdict(list)
        for r in rows:
            text = (r["prompt_text"] or "").lower()
            seen_in_prompt: set[str] = set()
            for tok in _TOKEN_RE.findall(text):
                tok_l = tok.lower()
                if tok_l in _STOPWORDS or len(tok_l) < 4:
                    continue
                if tok_l in seen_in_prompt:
                    continue
                seen_in_prompt.add(tok_l)
                counter[tok_l] += 1
                if len(samples[tok_l]) < 3:
                    samples[tok_l].append((r["prompt_text"] or "")[:80])

        for tok, cnt in counter.most_common(30):
            if cnt < min_occurrences:
                break
            existing = db.execute(
                "SELECT COUNT(*) FROM observations "
                "WHERE (project_root=? OR is_global=1) AND archived=0 "
                "  AND (title LIKE ? OR content LIKE ? OR context LIKE ?)",
                (project_root, f"%{tok}%", f"%{tok}%", f"%{tok}%"),
            ).fetchone()[0]
            if existing >= 2:
                continue
            suggestions.append({
                "token": tok,
                "count": cnt,
                "sample_prompts": samples[tok],
                "existing_obs_count": existing,
            })
            if len(suggestions) >= max_suggestions:
                break
        db.close()
    except sqlite3.Error as exc:
        print(f"[token-savior:memory] analyze_prompt_patterns error: {exc}", file=sys.stderr)
    return suggestions


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


def corpus_build(
    project_root: str,
    name: str,
    *,
    filter_type: str | None = None,
    filter_tags: list[str] | None = None,
    filter_symbol: str | None = None,
) -> dict[str, Any]:
    """Build a corpus from observations matching the filters. Stores IDs."""
    where = ["project_root = ?", "archived = 0"]
    params: list[Any] = [project_root]
    if filter_type:
        where.append("type = ?")
        params.append(filter_type)
    if filter_symbol:
        where.append("symbol = ?")
        params.append(filter_symbol)

    sql = (
        "SELECT id, type, title, tags FROM observations "
        f"WHERE {' AND '.join(where)} "
        "ORDER BY created_at_epoch DESC"
    )
    try:
        conn = get_db()
        rows = [dict(r) for r in conn.execute(sql, params).fetchall()]

        if filter_tags:
            wanted = set(filter_tags)
            filtered = []
            for r in rows:
                tags = []
                try:
                    tags = json.loads(r.get("tags") or "[]") or []
                except Exception:
                    tags = []
                if wanted & set(tags):
                    filtered.append(r)
            rows = filtered

        ids = [r["id"] for r in rows]
        type_counts: dict[str, int] = {}
        for r in rows:
            type_counts[r["type"]] = type_counts.get(r["type"], 0) + 1

        now = _now_iso()
        epoch = _now_epoch()
        conn.execute(
            "INSERT INTO corpora (project_root, name, filter_type, filter_tags, "
            "filter_symbol, observation_ids, created_at, created_at_epoch) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(project_root, name) DO UPDATE SET "
            "filter_type=excluded.filter_type, filter_tags=excluded.filter_tags, "
            "filter_symbol=excluded.filter_symbol, observation_ids=excluded.observation_ids, "
            "created_at=excluded.created_at, created_at_epoch=excluded.created_at_epoch",
            (
                project_root,
                name,
                filter_type,
                _json_dumps(filter_tags),
                filter_symbol,
                json.dumps(ids),
                now,
                epoch,
            ),
        )
        conn.commit()
        conn.close()
        return {
            "name": name,
            "count": len(ids),
            "observation_ids": ids,
            "type_counts": type_counts,
            "preview": [r["title"] for r in rows[:3]],
            "created_at": now,
        }
    except sqlite3.Error as exc:
        print(f"[token-savior:memory] corpus_build error: {exc}", file=sys.stderr)
        return {"name": name, "count": 0, "observation_ids": [], "type_counts": {}, "preview": []}


def corpus_get(project_root: str, name: str) -> dict[str, Any] | None:
    """Fetch corpus metadata + observation rows."""
    try:
        conn = get_db()
        row = conn.execute(
            "SELECT * FROM corpora WHERE project_root=? AND name=?",
            (project_root, name),
        ).fetchone()
        if not row:
            conn.close()
            return None
        ids = json.loads(row["observation_ids"] or "[]")
        if not ids:
            conn.close()
            return {"corpus": dict(row), "observations": []}
        placeholders = ",".join("?" * len(ids))
        obs = conn.execute(
            f"SELECT id, type, title, content, why, how_to_apply, symbol, file_path, "
            f"tags, importance, created_at FROM observations WHERE id IN ({placeholders}) "
            f"AND archived=0 ORDER BY created_at_epoch DESC",
            ids,
        ).fetchall()
        conn.close()
        return {"corpus": dict(row), "observations": [dict(o) for o in obs]}
    except sqlite3.Error as exc:
        print(f"[token-savior:memory] corpus_get error: {exc}", file=sys.stderr)
        return None


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
