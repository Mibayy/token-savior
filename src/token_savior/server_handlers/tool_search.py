"""ts_search — embedding-based tool routing for defer_loading clients.

Returns the top-K Token Savior tools most relevant to a natural-language
query. Lets a thin client manifest (5-7 hot tools + ts_search) defer the
remaining ~60 schemas off the system prompt and pull only what's relevant
to the current turn.

Usage from the agent side:
    ts_search(query="find dependents of update_user", top_k=5)
    -> {"matched_tools": [
         {"name": "get_dependents",      "description": "...", "inputSchema": {...}},
         {"name": "get_change_impact",   "description": "...", "inputSchema": {...}},
         {"name": "get_full_context",    "description": "...", "inputSchema": {...}},
         ...
       ]}

Mirrors the Tool Attention paper (arxiv 2604.21816) and Anthropic's
deferred-loading recipe for tool_search_20250919. Uses the existing Nomic
768d embedding stack (memory.embeddings). Falls back to substring scoring
if VECTOR_SEARCH_AVAILABLE is False so the tool stays usable on minimal
installs.

Design notes:
  * Tool description embeddings are computed lazily on first call and
    cached in process memory. ~66 tools x 768 floats x 4 bytes = ~200 KB.
  * Query is embedded with as_query=True so the Nomic task router prepends
    "search_query: " (matches how memory/search.py operates).
  * If the bench-disabled tools (capture_*, memory_*) have been gated by
    env vars, they're already missing from TOOL_SCHEMAS-derived embeddings
    via the server's list_tools filter — ts_search only sees what's live.
"""
from __future__ import annotations

import math
from typing import Any

# Cached at first call; module-level to survive across tool invocations.
_TOOL_EMBED_CACHE: dict[str, list[float]] | None = None
_TOOL_DESCRIPTIONS: dict[str, str] | None = None


def _cosine(a: list[float], b: list[float]) -> float:
    """Cosine similarity between two non-empty equal-length vectors."""
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    if na == 0.0 or nb == 0.0:
        return 0.0
    return dot / (na * nb)


def _build_embedding_cache() -> tuple[dict[str, list[float]], dict[str, str]]:
    """Embed every TOOL_SCHEMAS entry once. Best-effort: failures yield no
    embedding for that tool, which downgrades it to substring fallback."""
    from token_savior.tool_schemas import TOOL_SCHEMAS
    try:
        from token_savior.memory.embeddings import embed
    except Exception:
        embed = None  # type: ignore[assignment]

    embeds: dict[str, list[float]] = {}
    descs: dict[str, str] = {}
    for name, schema in TOOL_SCHEMAS.items():
        desc = schema.get("description") or ""
        if isinstance(desc, tuple):
            desc = "".join(desc)
        text = f"{name}: {desc}".strip()
        descs[name] = text
        if embed is not None:
            try:
                v = embed(text, as_query=False)
                if v:
                    embeds[name] = v
            except Exception:
                pass  # Tool stays substring-only.
    return embeds, descs


def _ensure_cache() -> None:
    global _TOOL_EMBED_CACHE, _TOOL_DESCRIPTIONS
    if _TOOL_EMBED_CACHE is None:
        _TOOL_EMBED_CACHE, _TOOL_DESCRIPTIONS = _build_embedding_cache()


def _substring_score(query: str, text: str) -> float:
    """Cheap fallback when no embeddings: count overlapping word stems."""
    ql = {tok.lower() for tok in query.split() if len(tok) > 2}
    tl = text.lower()
    if not ql:
        return 0.0
    hits = sum(1 for tok in ql if tok in tl)
    return hits / len(ql)


def ts_search(
    query: str,
    top_k: int = 5,
    *,
    include_schema: bool = True,
    visible_tools: set[str] | None = None,
) -> dict[str, Any]:
    """Return the top-K Token Savior tools most relevant to the query.

    Args:
        query: Natural-language description of the task.
        top_k: Maximum number of tools to return (default 5, max 12).
        include_schema: If True, returns each tool's full inputSchema.
            Set False to halve the payload when the agent only needs
            the names + descriptions for routing.
        visible_tools: Optional whitelist (tool names). When set, scoring
            is restricted to that subset — used by server.py to honor the
            current TOKEN_SAVIOR_PROFILE / TS_*_DISABLE filters.

    Returns:
        {
          "query": "<echoed>",
          "method": "embedding" | "substring" | "mixed",
          "matched_tools": [
            {"name": ..., "score": 0.83, "description": ..., "inputSchema": {...}},
            ...
          ],
        }
    """
    from token_savior.tool_schemas import TOOL_SCHEMAS

    _ensure_cache()
    assert _TOOL_EMBED_CACHE is not None and _TOOL_DESCRIPTIONS is not None

    top_k = max(1, min(int(top_k or 5), 12))

    try:
        from token_savior.memory.embeddings import embed
        qv = embed(query, as_query=True) if query else None
    except Exception:
        qv = None

    method_used = "substring" if qv is None else "embedding"
    has_partial_substring = False

    pool = visible_tools if visible_tools else set(TOOL_SCHEMAS.keys())
    pool.discard("ts_search")  # Don't recommend ourselves.

    scored: list[tuple[str, float]] = []
    for name in pool:
        if name not in TOOL_SCHEMAS:
            continue
        text = _TOOL_DESCRIPTIONS.get(name, "")
        v = _TOOL_EMBED_CACHE.get(name)
        if qv is not None and v is not None:
            s = _cosine(qv, v)
        else:
            s = _substring_score(query, text)
            has_partial_substring = True
        scored.append((name, s))

    if method_used == "embedding" and has_partial_substring:
        method_used = "mixed"

    scored.sort(key=lambda t: -t[1])
    top = scored[:top_k]

    matched = []
    for name, score in top:
        schema = TOOL_SCHEMAS[name]
        entry: dict[str, Any] = {
            "name": name,
            "score": round(score, 3),
            "description": schema.get("description"),
        }
        if include_schema:
            entry["inputSchema"] = schema.get("inputSchema")
        matched.append(entry)

    return {
        "query": query,
        "method": method_used,
        "matched_tools": matched,
    }
