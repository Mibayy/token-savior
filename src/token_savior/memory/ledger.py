"""Effectiveness ledger — records every action the memory system takes.

Twin of tool_capture.py: same DB (db_core.get_db), same put/query/aggregate
shape. Rows let the reflection loop compute net value per rule/mechanism and
flag anything counterproductive (net < 0).
"""
from __future__ import annotations

import json
import re
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


def ledger_query(
    *,
    event_type: str | None = None,
    session_id: str | None = None,
    since_epoch: int | None = None,
    limit: int = 100,
) -> list[dict[str, Any]]:
    """Return ledger rows newest-first, filtered."""
    clauses: list[str] = []
    params: list[Any] = []
    if event_type is not None:
        clauses.append("event_type = ?")
        params.append(event_type)
    if session_id is not None:
        clauses.append("session_id = ?")
        params.append(session_id)
    if since_epoch is not None:
        clauses.append("ts_epoch >= ?")
        params.append(int(since_epoch))
    where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
    sql = (
        "SELECT id, ts_epoch, event_type, subject, session_id, project_root, "
        " cost_tokens, latency_ms, acted_on, prevented_error, ignored, "
        " block_justified, was_visible, meta_json "
        "FROM ledger_events" + where + " ORDER BY ts_epoch DESC, id DESC LIMIT ?"
    )
    params.append(int(limit))
    conn = db_core.get_db()
    rows = conn.execute(sql, params).fetchall()
    conn.close()
    out: list[dict[str, Any]] = []
    for r in rows:
        out.append({
            "id": r[0], "ts_epoch": r[1], "event_type": r[2], "subject": r[3],
            "session_id": r[4], "project_root": r[5], "cost_tokens": r[6],
            "latency_ms": r[7],
            "outcome": {
                "acted_on": r[8], "prevented_error": r[9], "ignored": r[10],
                "block_justified": r[11], "was_visible": r[12],
            },
            "meta": json.loads(r[13]) if r[13] else None,
        })
    return out


_CORRECTION_PATTERNS = [
    r"je t'?ai déjà dit",
    r"je t'?avais dit",
    r"tu devais\b",
    r"je te rappelle",
    r"combien de fois (?:je (?:dois|te)|faut-il te|dois-je te)",
    r"encore une fois,? tu\b",
    r"comme (?:je t'?ai dit|d'?habitude)",
    r"je te l'?ai dit",
]
_CORRECTION_RE = re.compile("|".join(_CORRECTION_PATTERNS), re.IGNORECASE)


def detect_correction(text: str) -> str | None:
    """Return the matched correction phrase (lowercased) or None."""
    if not text:
        return None
    m = _CORRECTION_RE.search(text)
    return m.group(0).lower() if m else None


def record_from_userprompt(
    payload: dict[str, Any],
    *,
    session_id: str | None = None,
    project_root: str | None = None,
) -> dict[str, Any] | None:
    """If the user text is a correction, log a 'miss' event. Else None."""
    text = (payload.get("prompt") or payload.get("user_message") or "")
    phrase = detect_correction(text)
    if not phrase:
        return None
    return ledger_put(
        "miss",
        session_id=session_id,
        project_root=project_root,
        meta={"phrase": phrase, "text": text[:500]},
    )
