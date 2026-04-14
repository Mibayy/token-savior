"""Summaries: save consolidation summaries + parse structured ones.

Lifted from memory_db.py during the memory/ subpackage split.
"""

from __future__ import annotations

import sqlite3
import sys
from typing import Any

from token_savior import memory_db
from token_savior.db_core import _json_dumps, _now_epoch, _now_iso


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
            conn = memory_db.get_db()
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
        conn = memory_db.get_db()
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
