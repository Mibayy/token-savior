"""MCP resources: expose stored observations as ``ts://obs/{id}`` so clients
that support resource ``@``-mentions (Claude Code) can pull a specific memory
without a tool round-trip. Read-only, additive -- does not touch the tool path.

The ``ts://obs/{id}`` scheme is already the de-facto URI printed by
memory_index; this formalises it as a real MCP resource.
"""
from __future__ import annotations

from typing import Any

from token_savior import memory_db
from token_savior import server_state as state
from token_savior.memory.index import get_top_observations

_URI_PREFIX = "ts://obs/"


def list_observation_resources(limit: int = 50) -> list[Any]:
    """Return up to ``limit`` observation resources for the active project.

    Bounded on purpose: resources/list is a menu, not a data dump. Ranked by
    the same score memory_index uses (access_count / recency / importance).
    """
    import mcp.types as types

    root = state._slot_mgr.active_root or ""
    if not root:
        return []
    resources: list[Any] = []
    for o in get_top_observations(root, limit=limit):
        oid = o.get("id")
        if oid is None:
            continue
        title = (o.get("title") or f"observation {oid}").strip()
        resources.append(
            types.Resource(
                uri=f"{_URI_PREFIX}{oid}",
                name=title[:80],
                description=(o.get("type") or "note"),
                mimeType="text/markdown",
            )
        )
    return resources


def read_observation_resource(uri: Any) -> str:
    """Return the markdown body of the observation named by a ts://obs/{id} URI."""
    s = str(uri)
    if not s.startswith(_URI_PREFIX):
        raise ValueError(f"Not a {_URI_PREFIX} URI: {s}")
    tail = s[len(_URI_PREFIX):].strip("/")
    try:
        oid = int(tail)
    except ValueError as exc:
        raise ValueError(f"Bad observation id in {s}") from exc

    db = memory_db.get_db()
    try:
        row = db.execute(
            "SELECT type, title, content, why, how_to_apply, symbol, file_path "
            "FROM observations WHERE id=? AND archived=0",
            [oid],
        ).fetchone()
    finally:
        db.close()
    if row is None:
        raise ValueError(f"Observation {oid} not found")

    d = dict(row)
    parts: list[str] = [f"# {d.get('title') or f'observation {oid}'}"]
    if d.get("type"):
        parts.append(f"_type: {d['type']}_")
    if d.get("content"):
        parts.append("")
        parts.append(str(d["content"]))
    if d.get("why"):
        parts.append(f"\n**Why:** {d['why']}")
    if d.get("how_to_apply"):
        parts.append(f"\n**How to apply:** {d['how_to_apply']}")
    if d.get("symbol"):
        parts.append(f"\n`{d.get('file_path') or ''}::{d['symbol']}`")
    return "\n".join(parts)
