"""Streaming JSONL transcript scanner for Claude Code sessions.

Each ``.jsonl`` file under ``~/.claude/projects/<sanitized-path>/`` is one
session: a stream of ``user``/``assistant``/``tool_result`` events. We only
care about ``assistant`` events whose ``message.content`` carries one or
more ``tool_use`` blocks.

Streams line by line — never reads a full session into memory. Caller can
filter by ``since`` (datetime, UTC) and ``project`` (substring match on
the sanitized directory name, e.g. ``-root`` for ``/root``).
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator


@dataclass
class Event:
    """One tool-use event from a transcript.

    Attributes:
        ts: UTC datetime parsed from the event timestamp (may be None if
            the event lacks a parseable ``timestamp`` field).
        tool_name: Name of the tool the model called (``Read``, ``Bash``,
            ``mcp__token-savior__find_symbol``, ...).
        args: The ``input`` dict the model passed. Pruned to load-bearing
            keys only (Bash ``command``, Read ``file_path``, ...) — never
            includes full prompts.
        session_id: Session UUID (filename stem).
        project: Sanitized project directory name (e.g. ``-root``).
    """

    ts: datetime | None
    tool_name: str
    args: dict
    session_id: str
    project: str


def transcript_root() -> Path:
    """Return ``~/.claude/projects`` as a :class:`Path`."""
    return Path.home() / ".claude" / "projects"


def _parse_ts(raw: str | None) -> datetime | None:
    if not raw or not isinstance(raw, str):
        return None
    # ``2026-04-16T12:16:14.075Z`` — fromisoformat in 3.11+ accepts Z.
    try:
        if raw.endswith("Z"):
            raw = raw[:-1] + "+00:00"
        return datetime.fromisoformat(raw)
    except ValueError:
        return None


# Only the args we actually inspect downstream. Keeps memory bounded and
# guarantees no user prompt text leaks into Findings.
_KEEP_ARG_KEYS = {
    "command",        # Bash
    "file_path",      # Read / Edit / Write
    "pattern",        # Grep / search_codebase
    "name",           # find_symbol / get_function_source / ...
    "names",          # batch find_symbol
    "query",          # memory_search / ts_search / ToolSearch
}


def _prune_args(raw: dict | None) -> dict:
    if not isinstance(raw, dict):
        return {}
    out: dict = {}
    for k in _KEEP_ARG_KEYS:
        if k in raw:
            v = raw[k]
            # Truncate strings to avoid surprises; commands rarely exceed 500.
            if isinstance(v, str) and len(v) > 500:
                v = v[:500]
            out[k] = v
    return out


def _iter_session_events(
    path: Path,
    project: str,
    since: datetime | None,
) -> Iterator[Event]:
    session_id = path.stem
    try:
        fh = path.open("r", encoding="utf-8", errors="replace")
    except OSError:
        return
    try:
        for line in fh:
            # Cheap pre-filter: skip lines that obviously aren't assistant
            # tool_use events. Avoids JSON-parsing the ~70% of lines that
            # are user/queue/attachment events on a typical transcript.
            if '"type":"assistant"' not in line and '"type": "assistant"' not in line:
                continue
            if '"tool_use"' not in line:
                continue
            line = line.strip()
            if not line:
                continue
            try:
                d = json.loads(line)
            except json.JSONDecodeError:
                continue
            if d.get("type") != "assistant":
                continue
            ts = _parse_ts(d.get("timestamp"))
            if since is not None and ts is not None and ts < since:
                continue
            msg = d.get("message") or {}
            content = msg.get("content") or []
            if not isinstance(content, list):
                continue
            for block in content:
                if not isinstance(block, dict):
                    continue
                if block.get("type") != "tool_use":
                    continue
                name = block.get("name")
                if not isinstance(name, str):
                    continue
                yield Event(
                    ts=ts,
                    tool_name=name,
                    args=_prune_args(block.get("input")),
                    session_id=session_id,
                    project=project,
                )
    finally:
        fh.close()


def iter_events(
    root: Path,
    since: datetime | None = None,
    project: str | None = None,
) -> Iterator[Event]:
    """Yield tool-use Events across all sessions under ``root``.

    Streams one line at a time per file. Skips project dirs whose sanitized
    name does not contain ``project`` (if given). Files whose mtime is older
    than ``since`` are skipped entirely as a fast pre-filter; the per-event
    ``since`` check still applies for individual lines.
    """
    if not root.exists() or not root.is_dir():
        return

    since_epoch = since.timestamp() if since is not None else None

    for entry in sorted(root.iterdir()):
        if not entry.is_dir():
            continue
        proj_name = entry.name
        if project is not None and project not in proj_name:
            continue
        for jsonl in sorted(entry.glob("*.jsonl")):
            # Fast skip: if the whole file is older than `since`, skip it.
            if since_epoch is not None:
                try:
                    mtime = jsonl.stat().st_mtime
                except OSError:
                    continue
                if mtime < since_epoch:
                    continue
            yield from _iter_session_events(jsonl, proj_name, since)
