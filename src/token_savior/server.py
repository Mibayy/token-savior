"""Token Savior — MCP server.

Exposes project-wide structural query functions as MCP tools,
enabling Claude Code to navigate codebases efficiently without
reading entire files into context.

Single-project usage (original):
    PROJECT_ROOT=/path/to/project token-savior

Multi-project workspace usage:
    WORKSPACE_ROOTS=/root/hermes-agent,/root/token-savior,/root/improvence token-savior

Each root gets its own isolated index — no symbol collision, no dependency
graph pollution, no shared RAM between unrelated projects.
"""

from __future__ import annotations

import sys
import traceback
from typing import Any

from mcp.server.stdio import stdio_server
from mcp.types import Tool, TextContent
import mcp.types as types

from token_savior import memory_db
from token_savior import server_state as s
from token_savior.server_handlers import (
    META_HANDLERS as _META_HANDLERS,
    MEMORY_HANDLERS as _MEMORY_HANDLERS,
    QFN_HANDLERS as _QFN_HANDLERS,
    SLOT_HANDLERS as _SLOT_HANDLERS,
)
from token_savior.server_handlers.code_nav import (
    _q_get_edit_context,  # noqa: F401  -- re-export for tests/test_server.py
)
from token_savior.server_handlers.stats import (
    _format_duration,  # noqa: F401  -- re-export for tests/test_usage_stats.py
    _format_usage_stats,  # noqa: F401  -- re-export for tests/test_usage_stats.py
)
from token_savior.server_runtime import (
    _count_and_wrap_result,
    _flush_stats,  # noqa: F401  -- re-export for tests/test_usage_stats.py
    _format_result,
    _load_cumulative_stats,  # noqa: F401  -- re-export for tests/test_usage_stats.py
    _parse_workspace_roots,
    _prep,
    _register_roots,
    _warm_cache_async,
    compress_symbol_output,
)
from token_savior.server_state import server
from token_savior.slot_manager import _ProjectSlot  # noqa: F401  -- re-export for tests/test_usage_stats.py

# Called once at module import so slots exist before any tool call.
_register_roots(_parse_workspace_roots())


# ---------------------------------------------------------------------------
# Tool definitions (schemas live in tool_schemas.py)
# ---------------------------------------------------------------------------

from token_savior.tool_schemas import TOOL_SCHEMAS  # noqa: E402

TOOLS = [Tool(name=name, description=s["description"], inputSchema=s["inputSchema"])
         for name, s in TOOL_SCHEMAS.items()]



# ---------------------------------------------------------------------------
# MCP handlers
# ---------------------------------------------------------------------------


@server.list_tools()
async def list_tools() -> list[Tool]:
    return TOOLS


# ---------------------------------------------------------------------------
# Tool handler functions — each returns a raw result (not wrapped)
# ---------------------------------------------------------------------------


def _track_call(name: str, arguments: dict[str, Any]) -> str:
    """Tool-call telemetry: counts, PPM record, TCA activation, STTE hit."""

    s._tool_call_counts[name] = s._tool_call_counts.get(name, 0) + 1
    record_symbol = arguments.get("name") or arguments.get("symbol_name", "")
    try:
        s._prefetcher.record_call(name, record_symbol or "")
    except Exception:
        pass
    if record_symbol:
        try:
            s._tca_engine.record_activation(record_symbol)
        except Exception:
            pass
    if record_symbol and name in s._PREFETCHABLE_TOOLS:
        with s._prefetch_lock:
            cached = s._prefetch_cache.get(f"{name}:{record_symbol}")
        if cached is not None:
            s._spec_branches_hit += 1
            s._spec_tokens_saved += len(cached) // 4
    return record_symbol


def _maybe_compress(name: str, arguments: dict[str, Any], result):
    """Apply TCS structural compression if eligible."""
    if name not in s._COMPRESSIBLE_TOOLS or not arguments.get("compress", True):
        return result

    raw = _format_result(result)
    compressed = compress_symbol_output(name, result)
    before, after = len(raw), len(compressed)
    if after < before and compressed:
        saved_pct = (1 - after / before) * 100 if before else 0.0
        s._tcs_calls += 1
        s._tcs_chars_before += before
        s._tcs_chars_after += after
        return f"{compressed}\n[compressed: {before} → {after} chars, -{saved_pct:.1f}%]"
    return result


def _prefetch_next(name: str, record_symbol: str, slot) -> None:
    """Markov: predict next likely calls and pre-warm in a daemon thread."""
    try:
        preds = s._prefetcher.predict_next(name, record_symbol or "", top_k=3)
        if preds:
            _warm_cache_async(
                preds, slot, tool_name=name, symbol_name=record_symbol or "",
            )
    except Exception:
        pass


@server.call_tool()
async def call_tool(name: str, arguments: dict[str, Any]) -> list[types.TextContent]:

    record_symbol = _track_call(name, arguments)
    try:
        meta_handler = _META_HANDLERS.get(name)
        if meta_handler is not None:
            return meta_handler(arguments)

        mem_handler = _MEMORY_HANDLERS.get(name)
        if mem_handler is not None:
            return [TextContent(type="text", text=mem_handler(arguments))]

        slot, err = s._slot_mgr.resolve(arguments.get("project"))
        if err:
            return [TextContent(type="text", text=f"Error: {err}")]

        handler = _SLOT_HANDLERS.get(name)
        if handler is not None:
            return _count_and_wrap_result(slot, name, arguments, handler(slot, arguments))

        qfn_handler = _QFN_HANDLERS.get(name)
        if qfn_handler is not None:
            _prep(slot)
            if slot.query_fns is None:
                return [TextContent(
                    type="text",
                    text=f"Error: index not built for '{slot.root}'. Call reindex first.",
                )]
            result = qfn_handler(slot.query_fns, arguments)
            result = _maybe_compress(name, arguments, result)
            _prefetch_next(name, record_symbol, slot)
            return _count_and_wrap_result(slot, name, arguments, result)

        return [TextContent(type="text", text=f"Error: unknown tool '{name}'")]

    except Exception as e:
        print(f"[token-savior] Error in {name}: {traceback.format_exc()}", file=sys.stderr)
        return [TextContent(type="text", text=f"Error: {e}")]


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


async def main():
    memory_db.run_migrations()
    async with stdio_server() as (read_stream, write_stream):
        await server.run(
            read_stream,
            write_stream,
            server.create_initialization_options(),
        )


def main_sync():
    """Synchronous entry point for console_scripts."""
    import asyncio

    asyncio.run(main())


if __name__ == "__main__":
    main_sync()
