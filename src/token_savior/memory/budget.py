"""Closed-loop session budget (Step B): observe, score, render.

Lifted from memory_db.py during the memory/ subpackage split.
"""

from __future__ import annotations

import sqlite3
import sys
from typing import Any

from token_savior import memory_db

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
        db = memory_db.get_db()
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
