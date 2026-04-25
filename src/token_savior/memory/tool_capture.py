"""Tool output capture — sandbox verbose tool outputs in SQLite + FTS5.

The agent doesn't have to hold a 56KB Playwright snapshot or a 45KB access log
in its context window. The hook (or any caller) routes the full output to
``capture_put`` which persists it and returns a handle plus a small preview.
The agent then uses ``capture_search`` / ``capture_get`` / ``capture_aggregate``
to retrieve only the slice it actually needs.

This module owns its own helper SQL but reuses ``token_savior.db_core.get_db``
so captures live in the same memory.sqlite as observations / corpora.
"""
from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import sys
import time
from typing import Any

from token_savior import db_core


_DEFAULT_PREVIEW_BYTES = 800
_DEFAULT_PREVIEW_LINES = 8
_MAX_OUTPUT_BYTES = 8 * 1024 * 1024  # 8 MiB hard cap; truncate beyond


def _hash_args(args: Any) -> str:
    try:
        s = json.dumps(args, sort_keys=True, default=str)
    except Exception:
        s = str(args)
    return hashlib.sha1(s.encode("utf-8", errors="replace")).hexdigest()[:16]


def _make_preview(output: str) -> str:
    """Return a trimmed head+tail preview suitable for the model context.

    Strategy: first ``_DEFAULT_PREVIEW_LINES`` non-empty lines, capped at
    ``_DEFAULT_PREVIEW_BYTES`` chars. If the output is short, we just return
    it whole.
    """
    if not output:
        return ""
    if len(output) <= _DEFAULT_PREVIEW_BYTES:
        return output
    lines = output.splitlines()
    if len(lines) <= _DEFAULT_PREVIEW_LINES * 2:
        head = "\n".join(lines[:_DEFAULT_PREVIEW_LINES])
        tail = "\n".join(lines[-_DEFAULT_PREVIEW_LINES:])
    else:
        head = "\n".join(lines[:_DEFAULT_PREVIEW_LINES])
        tail = "\n".join(lines[-_DEFAULT_PREVIEW_LINES:])
    omitted = len(lines) - 2 * _DEFAULT_PREVIEW_LINES
    sep = f"\n... [{omitted} lines omitted, full output stored — use capture_get] ...\n"
    preview = head + sep + tail
    if len(preview) > _DEFAULT_PREVIEW_BYTES:
        preview = preview[: _DEFAULT_PREVIEW_BYTES] + "…"
    return preview


def capture_put(
    tool_name: str,
    output: str,
    *,
    args_summary: str | None = None,
    session_id: str | None = None,
    project_root: str | None = None,
    meta: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Persist a tool output. Returns {id, uri, preview, bytes, lines}.

    Truncates outputs above _MAX_OUTPUT_BYTES (the leading slice is kept).
    """
    if output is None:
        output = ""
    if len(output) > _MAX_OUTPUT_BYTES:
        output = output[:_MAX_OUTPUT_BYTES] + "\n…[truncated to 8MiB cap]"
    preview = _make_preview(output)
    args_hash = _hash_args({"tool": tool_name, "args": args_summary})
    epoch = int(time.time())
    n_lines = output.count("\n") + (0 if output.endswith("\n") or not output else 1)
    try:
        conn = db_core.get_db()
        cur = conn.execute(
            "INSERT INTO tool_captures "
            "(session_id, project_root, tool_name, args_hash, args_summary, "
            " output_full, output_preview, output_bytes, output_lines, "
            " created_at_epoch, meta_json) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                session_id, project_root, tool_name, args_hash, args_summary,
                output, preview, len(output), n_lines, epoch,
                json.dumps(meta) if meta else None,
            ),
        )
        cap_id = cur.lastrowid
        conn.commit()
        conn.close()
    except sqlite3.Error as exc:
        print(f"[token-savior:tool_capture] put error: {exc}", file=sys.stderr)
        return {"id": None, "uri": None, "preview": preview, "error": str(exc)}
    return {
        "id": cap_id,
        "uri": f"ts://capture/{cap_id}",
        "preview": preview,
        "bytes": len(output),
        "lines": n_lines,
    }


def capture_search(
    query: str,
    *,
    limit: int = 20,
    session_id: str | None = None,
    project_root: str | None = None,
    tool_name: str | None = None,
) -> list[dict[str, Any]]:
    """BM25 search across captured outputs.

    Returns rows with id, tool_name, snippet (FTS5 highlighted), bytes, age.
    """
    safe = db_core._fts5_safe_query(query)
    if not safe:
        return []
    where = ["fts.tool_captures_fts MATCH ?"]
    params: list[Any] = [safe]
    if session_id:
        where.append("c.session_id = ?")
        params.append(session_id)
    if project_root:
        where.append("c.project_root = ?")
        params.append(project_root)
    if tool_name:
        where.append("c.tool_name = ?")
        params.append(tool_name)
    sql = (
        "SELECT c.id AS id, c.tool_name AS tool_name, c.args_summary AS args_summary, "
        "       c.output_bytes AS bytes, c.output_lines AS lines, "
        "       c.created_at_epoch AS epoch, "
        "       snippet(tool_captures_fts, 0, '«', '»', '…', 12) AS snippet, "
        "       bm25(tool_captures_fts) AS score "
        "FROM tool_captures_fts AS fts "
        "JOIN tool_captures AS c ON c.id = fts.rowid "
        f"WHERE {' AND '.join(where)} "
        "ORDER BY score ASC LIMIT ?"
    )
    params.append(limit)
    try:
        conn = db_core.get_db()
        rows = [dict(r) for r in conn.execute(sql, params).fetchall()]
        conn.close()
    except sqlite3.Error as exc:
        print(f"[token-savior:tool_capture] search error: {exc}", file=sys.stderr)
        return []
    now = int(time.time())
    for r in rows:
        r["uri"] = f"ts://capture/{r['id']}"
        r["age_sec"] = now - (r.pop("epoch") or now)
    return rows


def capture_get(
    cap_id: int,
    *,
    range_spec: str | None = None,
    max_bytes: int | None = None,
) -> dict[str, Any] | None:
    """Retrieve a captured output, optionally sliced.

    range_spec accepts: 'head', 'tail', 'all', 'preview', 'line:start-end'
    (1-indexed inclusive). Defaults to 'preview' for safety.
    max_bytes caps the returned content slice.
    """
    spec = (range_spec or "preview").strip().lower()
    try:
        conn = db_core.get_db()
        row = conn.execute(
            "SELECT id, tool_name, args_summary, output_full, output_preview, "
            "output_bytes, output_lines, created_at_epoch, meta_json, "
            "session_id, project_root "
            "FROM tool_captures WHERE id = ?",
            (cap_id,),
        ).fetchone()
        conn.close()
    except sqlite3.Error as exc:
        print(f"[token-savior:tool_capture] get error: {exc}", file=sys.stderr)
        return None
    if not row:
        return None
    full = row["output_full"] or ""
    if spec == "preview":
        content = row["output_preview"] or _make_preview(full)
    elif spec == "head":
        content = "\n".join(full.splitlines()[:50])
    elif spec == "tail":
        content = "\n".join(full.splitlines()[-50:])
    elif spec == "all":
        content = full
    elif spec.startswith("line:"):
        try:
            start_s, end_s = spec[len("line:"):].split("-", 1)
            start, end = int(start_s), int(end_s)
            lines = full.splitlines()
            content = "\n".join(lines[max(0, start - 1):end])
        except Exception:
            content = full[:_DEFAULT_PREVIEW_BYTES]
    else:
        content = full
    if max_bytes is not None and len(content) > max_bytes:
        content = content[:max_bytes] + "…[capped]"
    out = dict(row)
    out["uri"] = f"ts://capture/{cap_id}"
    out["content"] = content
    out["range"] = spec
    out.pop("output_full", None)
    out.pop("output_preview", None)
    return out


def capture_aggregate(
    cap_id: int,
    *,
    transform: str = "stats",
    pattern: str | None = None,
) -> dict[str, Any] | None:
    """Run a fast aggregation over a captured output without dumping it.

    transform:
      - 'stats' (default): line/byte/word counts + first/last timestamp-like tokens
      - 'count_lines'
      - 'extract:<regex>' or transform='extract' + pattern=<regex>: list distinct matches (cap 200)
      - 'count:<regex>' or transform='count' + pattern=<regex>: count matches
      - 'unique_lines': returns distinct line count + 5 sample lines
    """
    try:
        conn = db_core.get_db()
        row = conn.execute(
            "SELECT output_full, output_bytes, output_lines, tool_name "
            "FROM tool_captures WHERE id = ?",
            (cap_id,),
        ).fetchone()
        conn.close()
    except sqlite3.Error as exc:
        print(f"[token-savior:tool_capture] aggregate error: {exc}", file=sys.stderr)
        return None
    if not row:
        return None
    text = row["output_full"] or ""

    if transform.startswith("extract:"):
        pattern = transform[len("extract:"):]
        transform = "extract"
    elif transform.startswith("count:"):
        pattern = transform[len("count:"):]
        transform = "count"

    if transform == "stats":
        lines = text.splitlines()
        words = sum(len(l.split()) for l in lines)
        return {
            "id": cap_id,
            "uri": f"ts://capture/{cap_id}",
            "tool_name": row["tool_name"],
            "bytes": row["output_bytes"],
            "lines": row["output_lines"],
            "words": words,
            "first_line": lines[0][:200] if lines else None,
            "last_line": lines[-1][:200] if lines else None,
        }
    if transform == "count_lines":
        return {"id": cap_id, "lines": text.count("\n") + (0 if text.endswith("\n") or not text else 1)}
    if transform == "unique_lines":
        seen = list(dict.fromkeys(text.splitlines()))
        return {"id": cap_id, "unique_lines": len(seen), "sample": seen[:5]}
    if transform == "extract":
        if not pattern:
            return {"error": "extract transform needs a pattern"}
        try:
            rx = re.compile(pattern)
        except re.error as exc:
            return {"error": f"bad regex: {exc}"}
        matches = []
        seen: set[str] = set()
        for m in rx.finditer(text):
            v = m.group(0)
            if v in seen:
                continue
            seen.add(v)
            matches.append(v)
            if len(matches) >= 200:
                break
        return {"id": cap_id, "pattern": pattern, "distinct_matches": len(matches), "matches": matches}
    if transform == "count":
        if not pattern:
            return {"error": "count transform needs a pattern"}
        try:
            rx = re.compile(pattern)
        except re.error as exc:
            return {"error": f"bad regex: {exc}"}
        return {"id": cap_id, "pattern": pattern, "count": sum(1 for _ in rx.finditer(text))}
    return {"error": f"unknown transform: {transform}"}


def capture_list(
    *,
    session_id: str | None = None,
    project_root: str | None = None,
    tool_name: str | None = None,
    limit: int = 50,
) -> list[dict[str, Any]]:
    """List recent captures with metadata + preview.

    Newest first. Use to enumerate what's captured this session.
    """
    where = ["1=1"]
    params: list[Any] = []
    if session_id:
        where.append("session_id = ?")
        params.append(session_id)
    if project_root:
        where.append("project_root = ?")
        params.append(project_root)
    if tool_name:
        where.append("tool_name = ?")
        params.append(tool_name)
    sql = (
        "SELECT id, tool_name, args_summary, output_bytes, output_lines, "
        "       created_at_epoch, output_preview "
        "FROM tool_captures "
        f"WHERE {' AND '.join(where)} "
        "ORDER BY created_at_epoch DESC, id DESC LIMIT ?"
    )
    params.append(limit)
    try:
        conn = db_core.get_db()
        rows = [dict(r) for r in conn.execute(sql, params).fetchall()]
        conn.close()
    except sqlite3.Error as exc:
        print(f"[token-savior:tool_capture] list error: {exc}", file=sys.stderr)
        return []
    now = int(time.time())
    for r in rows:
        r["uri"] = f"ts://capture/{r['id']}"
        r["age_sec"] = now - (r.pop("created_at_epoch") or now)
        # truncate preview hard so list responses stay tiny
        prev = r.pop("output_preview", "") or ""
        r["preview"] = prev[:200] + ("…" if len(prev) > 200 else "")
    return rows


def capture_purge(
    *,
    older_than_sec: int | None = None,
    session_id: str | None = None,
    project_root: str | None = None,
) -> int:
    """Delete captures matching filters. Returns rows affected."""
    where = []
    params: list[Any] = []
    if older_than_sec is not None:
        where.append("created_at_epoch < ?")
        params.append(int(time.time()) - older_than_sec)
    if session_id:
        where.append("session_id = ?")
        params.append(session_id)
    if project_root:
        where.append("project_root = ?")
        params.append(project_root)
    if not where:
        return 0
    try:
        conn = db_core.get_db()
        cur = conn.execute(f"DELETE FROM tool_captures WHERE {' AND '.join(where)}", params)
        n = cur.rowcount
        conn.commit()
        conn.close()
        return n
    except sqlite3.Error as exc:
        print(f"[token-savior:tool_capture] purge error: {exc}", file=sys.stderr)
        return 0
