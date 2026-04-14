"""User prompt archive + recurring-token analysis.

Lifted from memory_db.py during the memory/ subpackage split.
"""

from __future__ import annotations

import sqlite3
import sys
import time
from typing import Any

from token_savior import memory_db
from token_savior.db_core import _now_epoch, _now_iso, strip_private
from token_savior.memory._text_utils import _STOPWORDS, _TOKEN_RE


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
        conn = memory_db.get_db()
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
        conn = memory_db.get_db()
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
        db = memory_db.get_db()
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
