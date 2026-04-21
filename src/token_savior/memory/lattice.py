"""Adaptive Lattice (v2.2 Step D): Beta-Binomial Thompson sampling on granularity.

Lifted from memory_db.py during the memory/ subpackage split.
"""

from __future__ import annotations

import sqlite3
import sys

from token_savior import memory_db
from token_savior.db_core import _now_epoch, relative_age

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
        "detect_breaking_changes", "checkpoint",
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
        conn = memory_db.get_db()
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

    rows_d = [dict(r) for r in rows]
    if rows_d and all(
        abs(d.get("alpha", 1.0) - 1.0) < 1e-9 and abs(d.get("beta", 1.0) - 1.0) < 1e-9
        for d in rows_d
    ):
        return 0
    samples: list[tuple[int, float]] = []
    for d in rows_d:
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
        conn = memory_db.get_db()
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
        conn = memory_db.get_db()
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
