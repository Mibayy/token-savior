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
    "Raw regex grep"                   -> search_codebase(pattern=Y)
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
import time
import traceback
from typing import Any

# MCP imports : tous deferred a `run()`. Le import `mcp.types` declenche
# `import mcp` qui charge tout le SDK (uvicorn, sse_starlette, fastmcp) ~800ms
# cold start. Inacceptable pour la CLI fork-mode et les scripts qui importent
# `_dispatch_tool`. On utilise les shims locaux (token_savior._compat) :
# `TextContent` / `Tool` / `types` duck-type. Le serveur MCP convertit aux
# vrais types `mcp.types.*` UNIQUEMENT a la frontiere protocole (list_tools,
# call_tool), une fois par appel, sans pollution du cold-start des handlers.
from token_savior._compat import TextContent, Tool, types  # type: ignore

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
from token_savior.server_handlers.tool_search import ts_search as _ts_search_impl
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
# `from token_savior.server_state import server` declenchait
# __getattr__('server') qui faisait lazy import de mcp.server (1.24s SDK).
# On retire l import au top et on accede a `s.server` UNIQUEMENT dans run()
# qui est le seul site de l acces. Les decorateurs sont appliques en runtime
# dans run() egalement.
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


def _thin_input_schema(schema: dict) -> dict:
    """Retire les `description` des sub-properties de l inputSchema.

    Sur le profile tiny_plus, mesure : 9915 chars -> 5563 chars (-44%, -1209
    tokens) sur le manifest. La description top-level du tool reste, ce qui
    suffit dans 95% des cas pour que le model invoque correctement -- les
    sub-prop descriptions sont du bruit JSON-Schema verbose.

    Opt-in via TS_THIN_SCHEMAS=1. Recommande sur les profiles bench et les
    setups Claude Code OAuth (compte Max) ou chaque token de manifest est
    re-cache a chaque turn.
    """
    import copy
    s = copy.deepcopy(schema)
    props = s.get("properties", {})
    for pdef in props.values():
        pdef.pop("description", None)
        if isinstance(pdef.get("items"), dict):
            pdef["items"].pop("description", None)
    return s


_PROFILE_RAW = os.environ.get("TOKEN_SAVIOR_PROFILE", "full").lower()
# `optimized` profile (v4.0+) implies thin schemas automatically — sinon
# user doit le set explicitement.
_THIN_SCHEMAS = (
    os.environ.get("TS_THIN_SCHEMAS") == "1"
    or _PROFILE_RAW == "optimized"
)


def _schema_for(s: dict) -> dict:
    return _thin_input_schema(s["inputSchema"]) if _THIN_SCHEMAS else s["inputSchema"]


# TOOLS = liste de ToolDef (shim local). Le serveur MCP convertit en
# mcp.types.Tool a la frontiere protocole, dans list_tools().
TOOLS = [Tool(name=name, description=s["description"], inputSchema=_schema_for(s))
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
#   lean (61 tools)  =  10 507 est. tokens  (-26 %, narrowly above
#                                              Claude Code's 10k
#                                              auto-defer threshold —
#                                              Spike 2 USE WHEN/NOT WHEN
#                                              rewrite should bring it
#                                              under on net)
#   ultra (17 + 1)   =   3 540 est. tokens  (-75 %)
#
# `lean` post-spike-1 keeps 3 tools that the pure call-volume cut would
# have dropped: `memory_save` (the user-facing "remember this across
# sessions" contract — dropping silently breaks README's "nothing
# forgotten" promise) and the atomic pair `discover_project_actions` +
# `run_project_action` (5/3330 calls on VPS, but the workflow needs
# both or none).
_LEAN_EXCLUDES: set[str] = {
    # Memory engine — opt-in only. memory_save / memory_index / memory_search
    # / memory_get / memory_delete are user-facing and stay visible.
    # memory_admin is a new fusion (Round 5) replacing 21 admin tools that
    # were previously listed here individually.
    "memory_search", "memory_get", "memory_index",
    "memory_delete", "memory_admin",
    # Reasoning — memory-adjacent, 0 calls in tsbench + VPS
    "reasoning_save", "reasoning_search", "reasoning_list",
    # Corpus — 0 calls in tsbench + VPS
    "corpus_build", "corpus_query",
    # search_in_symbols is a subset of search_codebase — kept registered
    # for backwards compatibility but excluded from lean.
    "search_in_symbols",
    # Tool capture — agent never invokes capture_put/purge directly
    # (hook handles that). capture_get + capture_search were initially
    # kept visible for post-compaction retrieval, but tsbench-26/04 showed
    # the agent invoking capture_get to re-fetch outputs > threshold,
    # injecting 5-30 KB back into context (cache_creation +40k on TASK-039).
    # The capture sandbox saves nothing if the agent re-pulls everything.
    # All capture_* tools are now lean-excluded; opt-in via TS_CAPTURE_VISIBLE=1.
    "capture_put", "capture_purge", "capture_aggregate", "capture_list",
    "capture_get", "capture_search",
    # (discover_project_actions + run_project_action kept atomically —
    #  low volume but paired workflow would break if split.)
}

# `ultra` = minimal manifest with lazy tool discovery. Curated list of
# tools that prod 30 d audit shows as ≥3 calls or strategically critical.
# LLM reaches the rest via ts_extended(mode="list" | "describe" | "call").
# Tradeoff: invoking a hidden tool costs an extra round trip.
#
# Manifest math measured 2026-04-25 (post Round 3 + Round 5):
#   full       (66) ~ 8 969 tokens
#   lean       (51) ~ 7 052
#   lean+memdis(50) ~ 6 740
#   ultra      (28) ~ 3 800     (-43 % vs lean+memdis, -57 % vs full)
#
# Expanded from the 17-tool baseline by ~11 tools that the 30 d production
# audit identified as moderately used (find_dead_code 18 calls,
# find_hotspots 17, get_imports 49, get_routes 15, etc.). Adding them
# preserves the mental model "main tools always reachable" while keeping
# the manifest under the 4k-token threshold where Claude Code stops
# auto-deferring.
_ULTRA_INCLUDES: set[str] = {
    # Project lifecycle (5)
    "switch_project", "set_project_root", "list_projects", "reindex",
    "get_project_summary",
    # Code navigation core (8)
    "search_codebase", "list_files",
    "get_function_source", "get_class_source", "find_symbol",
    "get_full_context", "get_structure_summary",
    "get_functions", "get_imports",
    # Dependency graph (3)
    "get_dependencies", "get_dependents", "get_file_dependents",
    # Edit primitives (4)
    "replace_symbol_source", "insert_near_symbol", "edit_lines_in_symbol",
    "add_field_to_model",
    # Analysis (5)
    "analyze_config", "analyze_docker", "find_dead_code",
    "find_hotspots", "find_semantic_duplicates", "detect_breaking_changes",
    # Git (2)
    "get_git_status", "get_changed_symbols",
    # Routes (1)
    "get_routes",
    # Memory user-facing (1)
    "memory_save",
    # Tool capture (2 — read-side only, hook does the writes)
    "capture_get", "capture_search",
}

# `tiny` = thin manifest with deferred-loading router. Exposes only 5 hot
# tools + ts_search. Other tools reachable via ts_search(query=...) which
# returns top-K matched schemas (Nomic embeddings on tool descriptions).
# Mirrors the Tool Attention paper (arxiv 2604.21816, -95% prefix on 120
# tools). One extra round-trip per turn for non-hot tools, but breaks
# even after ~3 cold-start agent turns. Manifest math 2026-04-26:
#   tiny  ( 6 tools)  ~  1 500 tokens  (-78 % vs lean post-cleanup)
_TINY_INCLUDES: set[str] = {
    "switch_project",
    "find_symbol",
    "get_function_source",
    "get_full_context",
    "search_codebase",
    "ts_search",
}

# `tiny_plus` = tiny + 9 tools that bench 26/04 showed agents abandon when
# missing or workaround poorly. Covers nav (entry points), audit (dead-code,
# semantic duplicates), graph (call chain), config (analyze_config), git
# (status + breaking changes), and edit primitives (replace_symbol_source,
# add_field_to_model). Manifest ~2.5 KT (vs tiny ~1.1 KT, lean ~7 KT).
_TINY_PLUS_INCLUDES: set[str] = _TINY_INCLUDES | {
    "find_dead_code",
    "find_semantic_duplicates",
    "get_call_chain",
    "get_entry_points",
    "analyze_config",
    "get_git_status",
    "detect_breaking_changes",
    "add_field_to_model",
    "replace_symbol_source",
}

# `code_mode` = single-shot multi-tool execution via ts_execute. Manifest
# is 4 tools (~1.5 KT) plus a per-call typed TS facade returned by
# ts_search. Model discovers tool signatures on demand; scripts run in
# one round-trip instead of N. Mirrors the Cloudflare Code Mode pattern.
_CODE_MODE_INCLUDES: set[str] = {
    "ts_execute",
    "ts_search",
    "switch_project",
    "list_projects",
}

# `auto` = adaptive profile built from telemetry. Three layers:
#   1. Hot core: top-K from persistent tool_call_counts (LinUCB feature
#      vector falls back to raw counts when the model is under-trained).
#   2. Always-on essentials: switch_project, list_projects, get_git_status.
#   3. Discovery: ts_search (defer-loading) + ts_execute (Code Mode bridge).
# Total manifest ~2-3 KT, converges to the user's actual usage after a
# handful of sessions. Defaults to TINY_PLUS_INCLUDES on cold start
# (no telemetry yet) to avoid a bad first-session experience.
_AUTO_HOT_K = int(os.environ.get("TS_AUTO_HOT_K", "10"))
_AUTO_ESSENTIALS: set[str] = {
    "switch_project",
    "list_projects",
    "get_git_status",
    "ts_search",
    "ts_execute",
}


def _auto_includes() -> set[str]:
    """Compute the auto-profile tool set from telemetry.

    Pure function so callers (tests, debug commands) can introspect what
    the runtime will expose without re-importing the server module.
    """
    try:
        from token_savior import telemetry as _t
        counts = _t.aggregate_counts()
    except Exception:
        counts = {}
    # Filter out tools that aren't in TOOL_SCHEMAS (renamed/removed in
    # earlier versions but still in old telemetry files).
    eligible = {t: n for t, n in counts.items() if t in TOOL_SCHEMAS}
    if not eligible:
        # Cold start: borrow tiny_plus as the warm baseline.
        return set(_AUTO_ESSENTIALS) | set(_TINY_PLUS_INCLUDES)
    # Top-K by call count, excluding tools already in essentials.
    ranked = sorted(eligible.items(), key=lambda kv: -kv[1])
    hot: list[str] = []
    for name, _n in ranked:
        if name in _AUTO_ESSENTIALS:
            continue
        hot.append(name)
        if len(hot) >= _AUTO_HOT_K:
            break
    return set(_AUTO_ESSENTIALS) | set(hot)


_PROFILE_EXCLUDES: dict[str, set[str]] = {
    "full": set(),
    "auto": set(TOOL_SCHEMAS) - _auto_includes(),
    "core": set(_MEMORY_HANDLERS) | set(_META_HANDLERS),
    "nav":  set(_MEMORY_HANDLERS) | set(_META_HANDLERS) | set(_SLOT_HANDLERS) | {"ts_execute"},
    "lean": _LEAN_EXCLUDES,
    "ultra": set(TOOL_SCHEMAS) - _ULTRA_INCLUDES,
    "tiny": set(TOOL_SCHEMAS) - _TINY_INCLUDES,
    "tiny_plus": set(TOOL_SCHEMAS) - _TINY_PLUS_INCLUDES,
    "code_mode": set(TOOL_SCHEMAS) - _CODE_MODE_INCLUDES,
    # `optimized` (v4.0+) — alias officiel pour le Pareto-optimum
    # `tiny_plus` couple a TS_THIN_SCHEMAS=1 + TS_CAPTURE_DISABLED=1
    # + TS_MEMORY_DISABLE=1. Reproduit 97.9% @ 3 395 tokens/task sur tsbench.
    # Les autres profiles restent dispo pour compat retro.
    "optimized": set(TOOL_SCHEMAS) - _TINY_PLUS_INCLUDES,
}

# Profiles slated for removal in 4.0.0 — superseded by the single adaptive
# `auto` profile that uses real telemetry instead of hand-tuned subsets.
_DEPRECATED_PROFILES: set[str] = {"core", "nav", "lean", "ultra", "tiny", "tiny_plus"}

_PROFILE = os.environ.get("TOKEN_SAVIOR_PROFILE", "full").lower()
if _PROFILE not in _PROFILE_EXCLUDES:
    print(
        f"[token-savior] unknown profile '{_PROFILE}', using full",
        file=sys.stderr,
    )
    _PROFILE = "full"

if _PROFILE in _DEPRECATED_PROFILES:
    print(
        f"[token-savior] DEPRECATED: profile '{_PROFILE}' is deprecated and "
        f"will be removed in v4.0.0. Use TOKEN_SAVIOR_PROFILE=auto for an "
        f"adaptive manifest sized from your actual usage, or "
        f"TOKEN_SAVIOR_PROFILE=full to keep every tool advertised.",
        file=sys.stderr,
    )

_HIDDEN_UNDER_ULTRA: set[str] = _PROFILE_EXCLUDES["ultra"]

if _PROFILE != "full":
    _excluded = _PROFILE_EXCLUDES[_PROFILE]
    TOOLS = [t for t in TOOLS if t.name not in _excluded]

# When memory is disabled at runtime (e.g. bench subprocess) hide the
# remaining memory entrypoints from the manifest — every advertised tool
# costs ~50-100 tokens whether it's used or not.
if os.environ.get("TS_MEMORY_DISABLE") == "1":
    _MEMORY_GATED = {
        "memory_save", "memory_index", "memory_search", "memory_get",
        "memory_delete", "memory_admin",
        "reasoning_save", "reasoning_search", "reasoning_list",
        "corpus_build", "corpus_query",
    }
    TOOLS = [t for t in TOOLS if t.name not in _MEMORY_GATED]

# When tool-capture sandboxing is disabled (TS_CAPTURE_DISABLED=1) the
# capture_* tools always return empty payloads but the agent still
# discovers them in the manifest and burns turns calling capture_search /
# capture_get on stale or empty rows. Drop the read-side capture tools
# from the manifest in that mode (the write-side ones — capture_put,
# capture_purge — are already lean-excluded; only capture_get and
# capture_search remain, and both become useless when nothing is captured).
if os.environ.get("TS_CAPTURE_DISABLED") == "1":
    _CAPTURE_GATED = {
        "capture_get", "capture_search",
        "capture_aggregate", "capture_list",
        "capture_put", "capture_purge",
    }
    TOOLS = [t for t in TOOLS if t.name not in _CAPTURE_GATED]

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

# Code Mode (ts_execute) is built from TOOL_SCHEMAS like every other tool;
# the only special handling is the env-gated removal below for sandboxed
# deployments that don't want a Node subprocess available.
if os.environ.get("TS_CODE_MODE_DISABLE") == "1":
    TOOLS = [t for t in TOOLS if t.name != "ts_execute"]

print(
    f"[token-savior] profile={_PROFILE} tools={len(TOOLS)}/{len(TOOL_SCHEMAS)}",
    file=sys.stderr,
)

# Default stays at 'full' (66 tools, ~9k tokens). Token-conscious users
# can opt down via TOKEN_SAVIOR_PROFILE=lean (51 tools), =ultra (33 hot
# tools + ts_extended proxy, ~5k tokens), =core, or =nav.
if "TOKEN_SAVIOR_PROFILE" not in os.environ and _PROFILE == "full":
    print(
        "[token-savior] profile=full (66 tools). Set TOKEN_SAVIOR_PROFILE=lean "
        "or =ultra to reduce manifest cost.",
        file=sys.stderr,
    )



# ---------------------------------------------------------------------------
# MCP handlers
# ---------------------------------------------------------------------------


async def list_tools() -> list:
    """MCP list_tools handler. Decorateur applique dans `run()` lazily."""
    # Convertit nos ToolDef locaux en mcp.types.Tool a la frontiere protocole.
    from mcp.types import Tool as McpTool
    return [
        McpTool(name=t.name, description=t.description, inputSchema=t.inputSchema)
        for t in TOOLS
    ]


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
    # A5: persistent scoped-by-client counter for profile tuning across
    # sessions. Silent on failure — telemetry must never break dispatch.
    try:
        from token_savior import telemetry
        telemetry.record_tool_call(name)
    except Exception:
        pass
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

    # Chain-nudge buffer (see server_state._chain_calls for rationale).
    # Push every call so the nudge detector can match on (prev_tool, prev_sym).
    if not s._CHAIN_NUDGE_DISABLED:
        s._chain_calls.append((time.monotonic(), name, record_symbol or ""))

    return record_symbol


# ---------------------------------------------------------------------------
# Chain-nudge: top-of-payload notice when a known wasteful pattern is detected.
# Data (2026-05-17..26): 42 find_symbol -> get_function_source same-symbol
# chains and 26 find_symbol -> get_full_context within 60s, all foldable.
# Trailing _hints are ignored in practice; this notice is prepended so it
# lands above any compressed payload.
# ---------------------------------------------------------------------------
_CHAIN_WINDOW_SEC = 60.0


_CHAIN_READ_AFTER_FIND = ("get_function_source", "get_class_source", "get_dependents", "get_dependencies")
_CHAIN_PREV_FIND = ("find_symbol",)
_CHAIN_PREV_READ = ("get_function_source", "get_class_source")

# Pattern 3: edit tools that should be preceded by get_edit_context.
_EDIT_TOOLS_NEEDING_CONTEXT = (
    "replace_symbol_source",
    "insert_near_symbol",
    "add_field_to_model",
    "move_symbol",
)

# Pattern 4: individual navigation calls foldable into one ts_execute script.
_NAV_CHAIN_TOOLS = (
    "find_symbol",
    "get_function_source",
    "get_class_source",
    "get_full_context",
    "get_dependents",
    "get_dependencies",
    "search_codebase",
    "get_structure_summary",
)
_TS_EXECUTE_NUDGE_THRESHOLD = 5


def _detect_chain_nudge(name: str, symbol: str) -> str | None:
    if s._CHAIN_NUDGE_DISABLED:
        return None
    now = time.monotonic()

    # Pattern 3: edit tool called without a preceding get_edit_context on the
    # same symbol. Audit 2026-07-04: 0 get_edit_context calls across ~199 edits
    # (replace_symbol_source 156 + add_field_to_model 28 + insert_near_symbol 15).
    # get_edit_context returns source + callers + deps + siblings + tests in one
    # call -- editing blind risks breaking a caller the agent never looked at.
    if symbol and name in _EDIT_TOOLS_NEEDING_CONTEXT:
        saw_context = False
        for ts, prev_tool, prev_sym in reversed(s._chain_calls):
            if now - ts > _CHAIN_WINDOW_SEC:
                break
            if prev_tool == "get_edit_context" and prev_sym == symbol:
                saw_context = True
                break
        if not saw_context:
            return (
                f"[NUDGE] You're editing '{symbol}' without calling "
                f"get_edit_context('{symbol}') first. It returns source + callers + "
                f"deps + siblings + impacted tests in one call, so you don't break a "
                f"caller you never saw."
            )

    if symbol:
        for ts, prev_tool, prev_sym in reversed(s._chain_calls):
            if now - ts > _CHAIN_WINDOW_SEC:
                break
            if prev_sym != symbol:
                continue
            # Pattern 1: find_symbol(X) -> get_function_source/etc(X) within 60s.
            # 9-day data: 42 occurrences. Both calls fold into one get_full_context.
            if name in _CHAIN_READ_AFTER_FIND and prev_tool in _CHAIN_PREV_FIND:
                return (
                    f"[NUDGE] You called find_symbol('{symbol}') then {name}('{symbol}') "
                    f"on the same symbol. Next time use get_full_context('{symbol}') "
                    f"-- one round-trip returns location + source + callers + deps."
                )
            # Pattern 2: get_function_source/get_class_source(X) -> get_full_context(X)
            # within 60s. 9-day data: 187 occurrences. Source is re-fetched as part
            # of get_full_context, so the first read was wasted.
            if name == "get_full_context" and prev_tool in _CHAIN_PREV_READ:
                return (
                    f"[NUDGE] You called {prev_tool}('{symbol}') then "
                    f"get_full_context('{symbol}'). The source was re-fetched. "
                    f"Start with get_full_context('{symbol}') next time -- it returns "
                    f"source + callers + deps in one call."
                )

    # Pattern 4: many individual nav calls in one window -> Code Mode.
    # Audit 2026-07-04: ts_execute used only 41x despite thousands of unitary
    # nav calls. Anthropic's "code execution with MCP" shows chained tool calls
    # fold into one script (up to -98.7% tokens). Fire once, when the count
    # crosses the threshold, to avoid nudging on every subsequent call.
    if name in _NAV_CHAIN_TOOLS:
        nav_in_window = sum(
            1 for ts, tool, _ in s._chain_calls
            if now - ts <= _CHAIN_WINDOW_SEC and tool in _NAV_CHAIN_TOOLS
        )
        if nav_in_window == _TS_EXECUTE_NUDGE_THRESHOLD:
            return (
                f"[NUDGE] {nav_in_window} separate navigation calls in the last minute. "
                f"ts_execute runs a JS script calling many tools in one round-trip "
                f"(await tools.get_full_context(...), etc.) -- collapses the chain and "
                f"cuts tokens. Consider it for multi-step exploration."
            )

    return None


def _prepend_nudge(result: list, nudge: str) -> list:
    if not nudge or not result:
        return result
    s._chain_nudges_emitted += 1
    notice = TextContent(type="text", text=nudge)
    return [notice, *result]


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


def _handle_ts_search(arguments: dict[str, Any]) -> list[types.TextContent]:
    """Defer-loading router: cosine-sim over Nomic tool description embeddings.

    Restricts scoring to currently-visible tools (honors profile + env gates)
    so a `tiny`-profile session sees `ts_search` reach back into the ~60
    hidden tools but cannot suggest something that's been intentionally
    excluded (e.g. capture_* under TS_CAPTURE_DISABLED=1).
    """
    import json as _json
    visible = {t.name for t in TOOLS}
    fmt = arguments.get("format")
    if fmt is None and _PROFILE == "code_mode":
        fmt = "ts"
    payload = _ts_search_impl(
        arguments.get("query") or "",
        top_k=arguments.get("top_k", 5),
        include_schema=arguments.get("include_schema", True),
        visible_tools=visible,
        format=fmt or "schema",
    )
    return [TextContent(type="text", text=_json.dumps(payload, indent=2))]


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


async def _handle_ts_execute(arguments: dict[str, Any]) -> list[types.TextContent]:
    """Run a user JS script in a Node sandbox.

    Each `tools.<name>(args)` call from the script is dispatched back through
    `_dispatch_tool` and JSON-serialized for the JS side. The script's return
    value becomes the tool result.
    """
    import json as _json
    from token_savior.code_mode import ALLOWED_TOOLS, run_script_async

    script = arguments.get("script") or ""
    if not script.strip():
        return [TextContent(type="text", text="Error: 'script' is required and non-empty")]
    timeout_ms = int(arguments.get("timeout_ms") or 30000)

    def _dispatch_from_sandbox(tool_name: str, tool_args: dict) -> Any:
        """Bridge: take a JS-side tool call, run Python dispatch, return JS-friendly value."""
        record_symbol = _track_call(tool_name, tool_args)
        result = _dispatch_tool(tool_name, tool_args, record_symbol)
        text = "".join(part.text for part in result if hasattr(part, "text"))
        stripped = text.strip()
        if stripped.startswith("{") or stripped.startswith("["):
            try:
                return _json.loads(stripped)
            except _json.JSONDecodeError:
                pass
        return text

    outcome = await run_script_async(
        script=script,
        allowed_tools=ALLOWED_TOOLS,
        dispatch=_dispatch_from_sandbox,
        timeout_ms=timeout_ms,
    )
    return [TextContent(type="text", text=_json.dumps(outcome, indent=2, default=str))]


# Request lifecycle logging is opt-in via TOKEN_SAVIOR_TRACE=1.
# Issue #27: gives operators (especially on Windows where MCP requests
# can hang or abort) a way to see start / dispatch / complete events
# without enabling the full debug logger.
_TRACE_REQUESTS = os.environ.get("TOKEN_SAVIOR_TRACE", "").lower() in ("1", "true", "yes")


def _to_mcp_content(items: list) -> list:
    # Convert shim TextContent (_compat) to real mcp.types.TextContent at the
    # protocol boundary. Handlers return shim instances for cold-start reasons;
    # the SDK's CallToolResult pydantic v2 model only accepts the real class.
    # Same-name-different-class -> ValidationError -> every call reports
    # isError=True. See issue #32 (broken 3.5.0..4.3.2).
    from mcp.types import TextContent as _McpText
    out = []
    for it in items:
        if isinstance(it, _McpText):
            out.append(it)
            continue
        out.append(_McpText(
            type=getattr(it, "type", "text"),
            text=getattr(it, "text", str(it)),
        ))
    return out


async def call_tool(name: str, arguments: dict[str, Any]) -> list:

    # Latency instrumentation: always record, regardless of TOKEN_SAVIOR_TRACE.
    # Wall-clock cost measured at <1ms per call (see tests/test_latency.py).
    _lat_start = time.monotonic()
    if _TRACE_REQUESTS:
        print(f"[token-savior] -> call {name}", file=sys.stderr, flush=True)

    record_symbol = _track_call(name, arguments)
    _lat_status = "ok"
    _lat_err: str | None = None
    try:
        if name == "ts_extended":
            result = _handle_ts_extended(arguments)
        elif name == "ts_search":
            result = _handle_ts_search(arguments)
        elif name == "ts_execute":
            result = await _handle_ts_execute(arguments)
        else:
            result = _dispatch_tool(name, arguments, record_symbol)
        nudge = _detect_chain_nudge(name, record_symbol)
        if nudge:
            result = _prepend_nudge(result, nudge)
        if _TRACE_REQUESTS:
            elapsed_ms = (time.monotonic() - _lat_start) * 1000.0
            print(
                f"[token-savior] <- ok   {name} ({elapsed_ms:.0f}ms)",
                file=sys.stderr,
                flush=True,
            )
        return _to_mcp_content(result)

    except Exception as e:
        _lat_status = "err"
        _lat_err = type(e).__name__
        if _TRACE_REQUESTS:
            elapsed_ms = (time.monotonic() - _lat_start) * 1000.0
            print(
                f"[token-savior] <- err  {name} ({elapsed_ms:.0f}ms) {type(e).__name__}: {e}",
                file=sys.stderr,
                flush=True,
            )
        print(f"[token-savior] Error in {name}: {traceback.format_exc()}", file=sys.stderr)
        return _to_mcp_content([TextContent(type="text", text=f"Error: {e}")])

    finally:
        # Fire-and-forget persistence. The latency module is silent on
        # every failure, but guard the import + active-project lookup too.
        try:
            from token_savior import latency as _latency
            _elapsed_ms = int((time.monotonic() - _lat_start) * 1000.0)
            try:
                _root = s._slot_mgr.active_root
                _project = os.path.basename(_root) if _root else None
            except Exception:
                _project = None
            _latency.record(name, _project, _elapsed_ms, _lat_status, _lat_err)
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


async def main():
    if _TRACE_REQUESTS:
        print("[token-savior] startup: running memory migrations", file=sys.stderr, flush=True)
    memory_db.run_migrations()
    # Warm the tool-description embedding cache in a background thread so the
    # first ts_search call from the client doesn't pay the Nomic cold start.
    # 9 days of usage measured avg 4.8s on ts_search; cold load dominates.
    # Skippable via TOKEN_SAVIOR_NO_WARMUP=1 for resource-constrained hosts.
    if os.environ.get("TOKEN_SAVIOR_NO_WARMUP", "").lower() not in ("1", "true", "yes"):
        try:
            from token_savior.server_handlers.tool_search import warm_up_async
            warm_up_async()
        except Exception:
            pass
    if _TRACE_REQUESTS:
        print("[token-savior] startup: opening stdio transport", file=sys.stderr, flush=True)
    # Lazy : Server() instancie ici, applique les decorateurs sur nos
    # handlers list_tools/call_tool, puis demarre. Tout le mcp.* import
    # est confine a ce point d entree -- les clients CLI (qui importent
    # _dispatch_tool depuis le module) ne payent jamais ce cout.
    from mcp.server.stdio import stdio_server
    server = s.get_server()
    server.list_tools()(list_tools)
    server.call_tool()(call_tool)
    async with stdio_server() as (read_stream, write_stream):
        if _TRACE_REQUESTS:
            print("[token-savior] startup: server.run loop entered", file=sys.stderr, flush=True)
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
