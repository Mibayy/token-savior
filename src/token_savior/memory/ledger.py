"""Effectiveness ledger — records every action the memory system takes.

Twin of tool_capture.py: same DB (db_core.get_db), same put/query/aggregate
shape. Rows let the reflection loop compute net value per rule/mechanism and
flag anything counterproductive (net < 0).
"""
from __future__ import annotations

import json
import sqlite3
import sys
import time
from typing import Any

from token_savior import db_core

EVENT_TYPES = {
    "injection", "soft_remind", "hard_block", "silence", "miss", "false_positive",
}

_OUTCOME_KEYS = ("acted_on", "prevented_error", "ignored", "block_justified", "was_visible")


def _b(v: Any) -> int | None:
    if v is None:
        return None
    return 1 if v else 0


def ledger_put(
    event_type: str,
    *,
    subject: str | None = None,
    session_id: str | None = None,
    project_root: str | None = None,
    cost_tokens: int = 0,
    latency_ms: int = 0,
    outcome: dict[str, Any] | None = None,
    meta: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Persist one ledger event. Returns {id, uri}."""
    outcome = outcome or {}
    vals = {k: _b(outcome.get(k)) for k in _OUTCOME_KEYS}
    epoch = int(time.time())
    try:
        conn = db_core.get_db()
        cur = conn.execute(
            "INSERT INTO ledger_events "
            "(ts_epoch, event_type, subject, session_id, project_root, "
            " cost_tokens, latency_ms, acted_on, prevented_error, ignored, "
            " block_justified, was_visible, meta_json) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                epoch, event_type, subject, session_id, project_root,
                int(cost_tokens), int(latency_ms),
                vals["acted_on"], vals["prevented_error"], vals["ignored"],
                vals["block_justified"], vals["was_visible"],
                json.dumps(meta) if meta else None,
            ),
        )
        row_id = cur.lastrowid
        conn.commit()
        conn.close()
    except sqlite3.Error as exc:
        print(f"[token-savior:ledger] put error: {exc}", file=sys.stderr)
        return {"id": None, "uri": None, "error": str(exc)}
    return {"id": row_id, "uri": f"ts://ledger/{row_id}"}
