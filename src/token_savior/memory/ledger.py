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


# Tokens a subject may burn with zero benefit before it is flagged as pure
# waste. Tunable; kept generous so only clear waste trips the flag.
TOKEN_WASTE_THRESHOLD = 500


def ledger_net_value(*, since_epoch: int | None = None) -> dict[str, Any]:
    """Aggregate per-subject effectiveness on TWO unit-consistent axes.

    Token counts and event counts are never subtracted from each other
    (that would compare tokens to events). Instead:
      - friction_net = benefit_events − friction_events  (both event counts)
      - token_cost   = sum of cost_tokens                (tokens, reported alone)

    benefit_events  = real errors prevented + acted-on reminders.
    friction_events = false positives + unjustified hard blocks.

    A subject is counterproductive when it hurts more than it helps
    behaviourally (friction_net < 0), OR it is pure waste (spent tokens above
    TOKEN_WASTE_THRESHOLD while never helping). An expensive-but-useful subject
    is left for the reflection loop to judge on the reported token_cost, not
    auto-killed here. Honest metric: counts real outcomes, not raw activity.
    """
    rows = ledger_query(since_epoch=since_epoch, limit=1_000_000)
    agg: dict[str, dict[str, int]] = {}

    def bucket(subj: str | None) -> dict[str, int]:
        key = subj if subj is not None else "(none)"
        return agg.setdefault(
            key,
            {"benefit_events": 0, "friction_events": 0,
             "friction_net": 0, "token_cost": 0},
        )

    for r in rows:
        b = bucket(r["subject"])
        o = r["outcome"]
        b["token_cost"] += int(r["cost_tokens"] or 0)
        if o.get("prevented_error") == 1:
            b["benefit_events"] += 1
        if r["event_type"] == "soft_remind" and o.get("acted_on") == 1:
            b["benefit_events"] += 1
        if r["event_type"] == "false_positive":
            b["friction_events"] += 1
        if r["event_type"] == "hard_block" and o.get("block_justified") == 0:
            b["friction_events"] += 1

    totals = {"benefit_events": 0, "friction_events": 0,
              "friction_net": 0, "token_cost": 0}
    counterproductive: list[str] = []
    for subj, b in agg.items():
        b["friction_net"] = b["benefit_events"] - b["friction_events"]
        totals["benefit_events"] += b["benefit_events"]
        totals["friction_events"] += b["friction_events"]
        totals["token_cost"] += b["token_cost"]
        pure_waste = b["benefit_events"] == 0 and b["token_cost"] > TOKEN_WASTE_THRESHOLD
        if b["friction_net"] < 0 or pure_waste:
            counterproductive.append(subj)
    totals["friction_net"] = totals["benefit_events"] - totals["friction_events"]
    return {"by_subject": agg, "totals": totals, "counterproductive": counterproductive}
