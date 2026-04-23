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

## Agent decision tree (pick the right tool first time)

    "Where is X defined?"              -> find_symbol(name=X)
    "Show me the source of X"          -> get_function_source / get_class_source
    "What calls X?"                    -> get_dependents(X)
    "What does X call?"                -> get_dependencies(X)
    "Impact of changing X"             -> get_change_impact(X)
    "Orient me on X (source+callers)"  -> get_full_context(X)
    "Find Y in code, want symbol ctx"  -> search_in_symbols(pattern=Y)
    "Raw regex grep"                   -> search_codebase(pattern=Y)
    "Audit this file"                  -> audit_file(file_path=F)
    "Dead / unused code"               -> find_dead_code
    "Complexity hotspots"              -> find_hotspots (T0=most actionable)
    "Breaking API changes"             -> detect_breaking_changes (T0=breaking)
    "Tests impacted by my change"      -> find_impacted_test_files
    "Config drift / secrets"           -> analyze_config
    "Routes / endpoints"               -> get_routes (stub flag = unimpl handler)

Rules of thumb:
  - Start with find_symbol or get_full_context, NOT search_codebase.
  - Edit code via replace_symbol_source / insert_near_symbol, NOT Edit/Write —
    these keep the index in sync automatically.
  - `_complete: true` in the result means the scan was exhaustive; no need
    to fall back to grep.
  - switch_project is idempotent: calling it with the current project is a
    cheap no-op.
"""

from __future__ import annotations

import os
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

# A2-1: boot the optional web viewer thread when TS_VIEWER_PORT is set.
# Fully no-op (no imports beyond the module itself) when unset.
try:
    from token_savior.memory.viewer import start_if_configured as _viewer_start
    _viewer_start()
except Exception as _viewer_exc:  # pragma: no cover — defensive
    print(f"[token-savior] viewer boot skipped: {_viewer_exc}", file=sys.stderr)


# ---------------------------------------------------------------------------
# Tool definitions (schemas live in tool_schemas.py)
# ---------------------------------------------------------------------------

from token_savior.tool_schemas import TOOL_SCHEMAS  # noqa: E402

TOOLS = [Tool(name=name, description=s["description"], inputSchema=s["inputSchema"])
         for name, s in TOOL_SCHEMAS.items()]


# ---------------------------------------------------------------------------
# Profile filtering — TOKEN_SAVIOR_PROFILE env var
#
# Filters which tools are *advertised* via list_tools. Handlers remain
# registered in the dispatch tables, so a filtered-out tool still executes
# correctly if invoked directly by name.
# ---------------------------------------------------------------------------

# `lean` = aggressively trimmed profile for agent sessions that don't need
# the memory/reasoning/ML-stats machinery. Keeps the full surface of code
# navigation, editing, git, checkpoints, tests, and config/docker analysis.
# Manifest math measured 2026-04-23:
#   full (94 tools)  = 14 159 est. tokens
#   lean (58 tools)  =  9 907 est. tokens  (-30 %, just under Claude Code's
#                                             10k auto-defer threshold)
#   ultra (17 + 1)   =  3 540 est. tokens  (-75 %, aggressive)
_LEAN_EXCLUDES: set[str] = {
    # Memory engine (26 tools) — opt-in only
    "memory_save", "memory_search", "memory_get", "memory_index",
    "memory_delete", "memory_top", "memory_why", "memory_timeline",
    "memory_session_history", "memory_prompts", "memory_mode",
    "memory_archive", "memory_status", "memory_bus_push", "memory_bus_list",
    "memory_consistency", "memory_quarantine_list", "memory_maintain",
    "memory_doctor", "memory_vector_reindex", "memory_distill",
    "memory_dedup_sweep", "memory_roi_gc", "memory_roi_stats",
    "memory_from_bash", "memory_set_global",
    # Reasoning (3) — memory-adjacent
    "reasoning_save", "reasoning_search", "reasoning_list",
    # (stats fused into single get_stats tool — kept in lean since usage stats
    # are useful; ML subsystem categories are opt-in via category=)
    # Corpus / discover actions (4)
    "corpus_build", "corpus_query",
    "discover_project_actions", "run_project_action",
    # Niche analysis — edge cases, rare in practice
    "get_duplicate_classes", "get_call_predictions",
    "pack_context",
}

# `ultra` = minimal manifest with lazy tool discovery. Only 17 hot tools +
# a single `ts_extended` proxy exposed. LLM reaches the rest via
# ts_extended(mode="list" | "describe" | "call"). Cuts manifest from
# ~14 159 tokens (full 94) to ~3 540 tokens. Tradeoff: invoking a hidden
# tool costs an extra round trip unless the LLM already knows its name.
_ULTRA_INCLUDES: set[str] = {
    "switch_project", "list_projects", "reindex",
    "search_codebase", "list_files",
    "get_function_source", "get_class_source", "find_symbol",
    "get_full_context", "get_structure_summary",
    "get_functions", "get_dependencies", "get_dependents",
    "get_file_dependents", "analyze_config",
    "replace_symbol_source", "insert_near_symbol",
}

_PROFILE_EXCLUDES: dict[str, set[str]] = {
    "full": set(),
    "core": set(_MEMORY_HANDLERS) | set(_META_HANDLERS),
    "nav":  set(_MEMORY_HANDLERS) | set(_META_HANDLERS) | set(_SLOT_HANDLERS),
    "lean": _LEAN_EXCLUDES,
    "ultra": set(TOOL_SCHEMAS) - _ULTRA_INCLUDES,
}

_PROFILE = os.environ.get("TOKEN_SAVIOR_PROFILE", "full").lower()
if _PROFILE not in _PROFILE_EXCLUDES:
    print(
        f"[token-savior] unknown profile '{_PROFILE}', using full",
        file=sys.stderr,
    )
    _PROFILE = "full"

_HIDDEN_UNDER_ULTRA: set[str] = _PROFILE_EXCLUDES["ultra"]

if _PROFILE != "full":
    _excluded = _PROFILE_EXCLUDES[_PROFILE]
    TOOLS = [t for t in TOOLS if t.name not in _excluded]

if _PROFILE == "ultra":
    _hidden_catalog = ", ".join(sorted(_HIDDEN_UNDER_ULTRA))
    _TS_EXTENDED_DESC = (
        "Proxy for tools hidden under the ultra profile. Use mode='list' to "
        "see all hidden tool names + one-line descriptions, mode='describe' "
        "with name=<tool> to get its inputSchema, mode='call' with name=<tool> "
        "and args=<object> to invoke it. "
        f"Hidden tool names ({len(_HIDDEN_UNDER_ULTRA)}): {_hidden_catalog}"
    )
    TOOLS.append(Tool(
        name="ts_extended",
        description=_TS_EXTENDED_DESC,
        inputSchema={
            "type": "object",
            "properties": {
                "mode": {
                    "type": "string",
                    "enum": ["list", "describe", "call"],
                    "description": "'list' = catalog of hidden tools; 'describe' = inputSchema of one; 'call' = invoke one.",
                },
                "name": {
                    "type": "string",
                    "description": "Hidden tool name (required for describe/call).",
                },
                "args": {
                    "type": "object",
                    "description": "Arguments to pass when mode=call.",
                },
            },
            "required": ["mode"],
        },
    ))

print(
    f"[token-savior] profile={_PROFILE} tools={len(TOOLS)}/{len(TOOL_SCHEMAS)}",
    file=sys.stderr,
)



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

    if name == "switch_project":
        _maybe_auto_save_findings()
        s._auto_save_project = s._slot_mgr.active_root
        s._auto_save_symbols.clear()
        s._auto_save_tools.clear()
    elif s._auto_save_enabled:
        sym = arguments.get("name") or arguments.get("symbol_name", "")
        if sym:
            s._auto_save_symbols.append(sym)
        if name.startswith("get_") or name.startswith("find_") or name.startswith("search_"):
            s._auto_save_tools.append(name)

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


def _maybe_auto_save_findings():
    """If auto-save is enabled and we accumulated findings, save them."""
    if not s._auto_save_enabled:
        return
    if not s._auto_save_project or len(s._auto_save_symbols) < 2:
        return
    symbols = list(dict.fromkeys(s._auto_save_symbols))[:20]
    tools = list(dict.fromkeys(s._auto_save_tools))[:10]
    content = (
        f"Symbols accessed: {', '.join(symbols[:10])}"
        f"{f' (+{len(symbols)-10} more)' if len(symbols) > 10 else ''}. "
        f"Tools used: {', '.join(tools)}."
    )
    try:
        memory_db.observation_save(
            session_id=None,
            project=s._auto_save_project,
            obs_type="finding",
            title=f"Session findings ({len(symbols)} symbols)",
            content=content,
            tags=["auto-save"],
            importance=3,
            is_global=False,
        )
    except Exception as exc:
        print(f"[token-savior] auto-save error: {exc}", file=sys.stderr)
    s._auto_save_symbols.clear()
    s._auto_save_tools.clear()


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
        if os.environ.get("TOKEN_SAVIOR_DEBUG") == "1":
            return f"{compressed}\n[compressed: {before} → {after} chars, -{saved_pct:.1f}%]"
        return compressed
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


def _dispatch_tool(name: str, arguments: dict[str, Any], record_symbol: str) -> list[types.TextContent]:
    """Dispatch a tool by name, honoring the four handler categories.

    Shared by `call_tool` (normal entry) and the `ts_extended` proxy so that
    hidden tools in the `ultra` profile run through the exact same path.
    """
    meta_handler = _META_HANDLERS.get(name)
    if meta_handler is not None:
        return meta_handler(arguments)

    mem_handler = _MEMORY_HANDLERS.get(name)
    if mem_handler is not None:
        return [TextContent(type="text", text=mem_handler(arguments))]

    project_hint = arguments.get("project")
    slot, err = s._slot_mgr.resolve(project_hint)
    if err:
        return [TextContent(type="text", text=f"Error: {err}")]
    # Auto-promote explicit project hint to active. Previously the hint only
    # resolved for the current call, forcing agents to either repeat the
    # project= arg on every call or prefix a switch_project. This makes the
    # first real tool call implicitly set the session's active project.
    if project_hint and slot is not None and s._slot_mgr.active_root != slot.root:
        s._slot_mgr.active_root = slot.root

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
        src_key = None
        if name in s._SRC_CACHEABLE_TOOLS:
            args_repr = repr(sorted(
                (k, v) for k, v in arguments.items() if k != "project"
            ))
            src_key = f"{name}:{slot.root}:{slot.cache_gen}:{args_repr}"
            cached = s._session_result_cache.get(src_key)
            if cached is not None:
                s._src_hits += 1
                return _count_and_wrap_result(slot, name, arguments, cached)
            s._src_misses += 1
        result = qfn_handler(slot.query_fns, arguments)
        result = _maybe_compress(name, arguments, result)
        if src_key is not None:
            s._session_result_cache[src_key] = result
        _prefetch_next(name, record_symbol, slot)
        return _count_and_wrap_result(slot, name, arguments, result)

    return [TextContent(type="text", text=f"Error: unknown tool '{name}'")]


def _handle_ts_extended(arguments: dict[str, Any]) -> list[types.TextContent]:
    """Proxy for tools hidden under the `ultra` profile.

    Modes:
      - list: return a catalog (name -- one-line desc) of hidden tools
      - describe: return the inputSchema of one hidden tool
      - call: dispatch a hidden tool by name with provided args
    """
    from token_savior.tool_schemas import TOOL_SCHEMAS
    import json as _json

    mode = (arguments.get("mode") or "").lower()
    target = arguments.get("name")
    hidden = _HIDDEN_UNDER_ULTRA

    if mode == "list":
        lines = [f"Hidden tools under ultra profile ({len(hidden)}):"]
        for tool in sorted(hidden):
            desc = TOOL_SCHEMAS.get(tool, {}).get("description", "")
            lines.append(f"  {tool} -- {desc[:100]}")
        return [TextContent(type="text", text="\n".join(lines))]

    if mode == "describe":
        if not target or target not in TOOL_SCHEMAS:
            return [TextContent(type="text", text=f"Error: unknown tool '{target}'")]
        spec = TOOL_SCHEMAS[target]
        return [TextContent(type="text", text=_json.dumps(spec, indent=2))]

    if mode == "call":
        if not target:
            return [TextContent(type="text", text="Error: 'name' required for mode=call")]
        if target not in TOOL_SCHEMAS:
            return [TextContent(type="text", text=f"Error: unknown tool '{target}'")]
        inner_args = arguments.get("args") or {}
        if not isinstance(inner_args, dict):
            return [TextContent(type="text", text="Error: 'args' must be an object")]
        record_symbol = _track_call(target, inner_args)
        return _dispatch_tool(target, inner_args, record_symbol)

    return [TextContent(
        type="text",
        text="Error: mode must be one of 'list', 'describe', 'call'",
    )]


@server.call_tool()
async def call_tool(name: str, arguments: dict[str, Any]) -> list[types.TextContent]:

    record_symbol = _track_call(name, arguments)
    try:
        if name == "ts_extended":
            return _handle_ts_extended(arguments)
        return _dispatch_tool(name, arguments, record_symbol)

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
