"""Effectiveness ledger — records every action the memory system takes.

Twin of tool_capture.py: same DB (db_core.get_db), same put/query/aggregate
shape. Rows let the reflection loop score each rule/mechanism on two
unit-consistent axes (see ledger_net_value) and flag anything
counterproductive (negative friction_net, or pure token waste).
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


# --- Miss classification (unité A) -----------------------------------------
# observation_search exposes no reliable relevance score (FTS-only mode has
# none), so confidence is token OVERLAP between the correction's content tokens
# and the top obs (title + excerpt), never an opaque score.
OVERLAP_HIGH = 0.5          # >= this: confident the obs is the intended one
MIN_CONTENT_TOKENS = 2      # < this: query too thin to trust → uncertain

_CLASSIFY_STOP = {
    "que", "qui", "les", "des", "une", "aux", "pour", "avec", "dans", "sur",
    "par", "est", "sont", "the", "and", "for", "with", "this", "that", "you",
    "are", "how", "what", "can", "will", "from", "deja", "déjà", "dit", "dois",
    "fois", "rappelle", "toujours", "jamais",
}
_TOKEN_RE = re.compile(r"[A-Za-zÀ-ÿ0-9_]{3,}")


def _content_tokens(text: str) -> list[str]:
    """Content tokens of a correction: strip the matched trigger phrase and
    stopwords, keep lowercased word tokens >= 3 chars."""
    stripped = _CORRECTION_RE.sub(" ", text or "")
    toks = _TOKEN_RE.findall(stripped.lower())
    return [t for t in toks if t not in _CLASSIFY_STOP]


def classify_miss(
    correction_text: str,
    injected_obs_ids: list[int] | None,
    project_root: str | None,
    *,
    search_fn: Any = None,
) -> dict[str, Any]:
    """Classify a miss into unrecorded / invisible / ignored / uncertain.

    - too few content tokens             → uncertain (query too thin)
    - search returns nothing             → unrecorded (nothing to surface)
    - top obs overlaps >= OVERLAP_HIGH:
        obs id in injected set           → ignored   (surfaced but not acted on)
        else                             → invisible (existed, not surfaced)
    - overlap below threshold            → uncertain (found, but too weak to trust)

    ``search_fn(project_root, query, *, limit)`` defaults to observation_search;
    injected for testability.
    """
    tokens = _content_tokens(correction_text)
    base = {"miss_class": "uncertain", "expected_obs": None,
            "overlap": 0.0, "content_tokens": len(tokens)}
    if len(tokens) < MIN_CONTENT_TOKENS:
        return base
    if not project_root:
        return {**base, "miss_class": "uncertain"}

    if search_fn is None:
        # Resolve via the fully-formed memory_db module (its observation_search
        # attribute) rather than importing observations directly, which would
        # trip the observations<->memory_db import cycle.
        from token_savior import memory_db
        search_fn = memory_db.observation_search

    query = " OR ".join(f'"{t}"' for t in tokens)
    try:
        results = search_fn(project_root, query, limit=5) or []
    except Exception:
        return {**base, "miss_class": "uncertain"}
    if not results:
        return {**base, "miss_class": "unrecorded"}

    top = results[0]
    hay = f"{top.get('title', '')} {top.get('excerpt', '')}".lower()
    hay_toks = set(_TOKEN_RE.findall(hay))
    overlap = sum(1 for t in tokens if t in hay_toks) / len(tokens)
    injected = set(injected_obs_ids or [])
    if overlap >= OVERLAP_HIGH:
        cls = "ignored" if top.get("id") in injected else "invisible"
    else:
        cls = "uncertain"
    return {"miss_class": cls, "expected_obs": top.get("id"),
            "overlap": round(overlap, 3), "content_tokens": len(tokens)}


def record_injection(
    session_id: str | None,
    project_root: str | None,
    obs_ids: list[int],
    injected_text: str = "",
) -> dict[str, Any]:
    """Log what the memory injection surfaced this prompt: an 'injection' event
    carrying the surfaced obs ids and an approximate token cost."""
    cost = len(injected_text) // 4  # rough tokens estimate
    return ledger_put(
        "injection",
        session_id=session_id,
        project_root=project_root,
        cost_tokens=cost,
        meta={"obs_ids": list(obs_ids)},
    )


def _recent_injected_obs(session_id: str | None) -> list[int]:
    """Union of obs ids surfaced by injection events in this session."""
    if not session_id:
        return []
    ids: list[int] = []
    for ev in ledger_query(event_type="injection", session_id=session_id, limit=100):
        meta = ev.get("meta") or {}
        ids.extend(meta.get("obs_ids") or [])
    return ids


def _active_project_root() -> str | None:
    """The most-active project root, resolved the SAME way the injection block
    does — so classification searches the exact corpus the injection drew from,
    not a possibly-mismatched payload cwd."""
    try:
        conn = db_core.get_db()
        row = conn.execute(
            "SELECT project_root FROM observations "
            "GROUP BY project_root ORDER BY COUNT(*) DESC LIMIT 1"
        ).fetchone()
        conn.close()
        return row[0] if row else None
    except Exception:
        return None


def record_from_userprompt(
    payload: dict[str, Any],
    *,
    session_id: str | None = None,
    project_root: str | None = None,
) -> dict[str, Any] | None:
    """If the user text is a correction, log a classified 'miss' event. Else None."""
    text = (payload.get("prompt") or payload.get("user_message") or "")
    phrase = detect_correction(text)
    if not phrase:
        return None
    injected = _recent_injected_obs(session_id)
    # Search the same corpus the injection drew from; payload cwd may be absent
    # or not match the obs project_root.
    search_root = project_root or _active_project_root()
    cls = classify_miss(text, injected, search_root)
    mc = cls["miss_class"]
    was_visible = 1 if mc == "ignored" else (0 if mc == "invisible" else None)
    return ledger_put(
        "miss",
        session_id=session_id,
        project_root=project_root,
        outcome={"was_visible": was_visible},
        meta={"phrase": phrase, "text": text[:500],
              "miss_class": mc, "expected_obs": cls["expected_obs"],
              "overlap": cls["overlap"]},
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
