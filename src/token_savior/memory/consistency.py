"""Self-consistency: Bayesian validity scores + contradiction detection.

Lifted from memory_db.py during the memory/ subpackage split.
"""

from __future__ import annotations

import os
import sqlite3
import sys
import time
from typing import Any

from token_savior import memory_db
from token_savior.db_core import relative_age

#: Validity below this threshold quarantines the observation.
CONSISTENCY_QUARANTINE_THRESHOLD = 0.40
#: Validity below this threshold flags the observation as stale-suspected (⚠️).
CONSISTENCY_STALE_THRESHOLD = 0.60


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
        conn = memory_db.get_db()
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


def get_validity_score(obs_id: int) -> dict[str, Any]:
    """Return current Bayesian validity for an observation.

    Validity = α / (α + β). New observations default to (α=2.0, β=1.0) which
    biases the prior toward "valid" — only repeated negative checks flip it.
    """
    try:
        conn = memory_db.get_db()
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
        with memory_db.db_session() as conn:
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
        with memory_db.db_session() as conn:
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
        conn = memory_db.get_db()
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
        conn = memory_db.get_db()
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
        db = memory_db.get_db()
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
