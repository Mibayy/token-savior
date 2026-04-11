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

import hashlib
import json
import os
import sys
import time
import traceback
import uuid

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import Tool, TextContent
import mcp.types as types

from token_savior.git_tracker import get_git_status
from token_savior.compact_ops import get_changed_symbols
from token_savior.checkpoint_ops import (
    compare_checkpoint_by_symbol,
    create_checkpoint,
    delete_checkpoint,
    list_checkpoints,
    prune_checkpoints,
    restore_checkpoint,
)
from token_savior.edit_ops import insert_near_symbol, replace_symbol_source
from token_savior.git_ops import (
    build_commit_summary,
    summarize_patch_by_symbol,
)
from token_savior.impacted_tests import find_impacted_test_files, run_impacted_tests
from token_savior.models import ProjectIndex
from token_savior.project_actions import discover_project_actions, run_project_action
from token_savior.workflow_ops import (
    apply_symbol_change_and_validate,
)
from token_savior.breaking_changes import detect_breaking_changes as run_breaking_changes
from token_savior.complexity import find_hotspots as run_hotspots
from token_savior.config_analyzer import analyze_config as run_config_analysis
from token_savior.cross_project import find_cross_project_deps as run_cross_project
from token_savior.dead_code import find_dead_code as run_dead_code
from token_savior.docker_analyzer import analyze_docker as run_docker_analysis
from token_savior.slot_manager import SlotManager, _ProjectSlot

# ---------------------------------------------------------------------------
# Module-level state
# ---------------------------------------------------------------------------

server = Server("token-savior")

# Persistent cache
_CACHE_VERSION = 2  # Bumped: switched from pickle to JSON

# Slot manager encapsulates _projects dict and _active_root
_slot_mgr = SlotManager(_CACHE_VERSION)

# Session usage stats (aggregated across all projects in this session)
_session_start: float = time.time()
_session_id: str = uuid.uuid4().hex[:12]
_tool_call_counts: dict[str, int] = {}
_total_chars_returned: int = 0
_total_naive_chars: int = 0

# Persistent stats
_STATS_DIR = os.path.expanduser("~/.local/share/token-savior")
_MAX_SESSION_HISTORY = 200


def _detect_client_name() -> str:
    """Best-effort client attribution for persisted stats."""
    explicit = os.environ.get("TOKEN_SAVIOR_CLIENT", "").strip()
    if explicit:
        return explicit
    if os.environ.get("HERMES_GATEWAY_URL") or os.environ.get("HERMES_SESSION_ID"):
        return "hermes"
    if os.environ.get("CODEX_HOME") or os.environ.get("CODEX_SANDBOX"):
        return "codex"
    if os.environ.get("CLAUDECODE") or os.environ.get("CLAUDE_CODE_ENTRYPOINT"):
        return "claude-code"
    return "unknown"


_CLIENT_NAME = _detect_client_name()
_SESSION_LABEL = os.environ.get("TOKEN_SAVIOR_SESSION_LABEL", "").strip()


# ---------------------------------------------------------------------------
# Startup: parse env vars and register roots
# ---------------------------------------------------------------------------


def _parse_workspace_roots() -> list[str]:
    """Parse WORKSPACE_ROOTS (comma-separated) or fall back to PROJECT_ROOT."""
    workspace_raw = os.environ.get("WORKSPACE_ROOTS", "").strip()
    if workspace_raw:
        roots = [r.strip() for r in workspace_raw.split(",") if r.strip()]
        return [os.path.abspath(r) for r in roots if os.path.isdir(r)]

    single = os.environ.get("PROJECT_ROOT", "").strip()
    if single and os.path.isdir(single):
        return [os.path.abspath(single)]

    return []


def _register_roots(roots: list[str]) -> None:
    """Create slots for each root. Index is built lazily on first use."""
    _slot_mgr.register_roots(roots)


# Called once at module import so slots exist before any tool call.
_register_roots(_parse_workspace_roots())


# ---------------------------------------------------------------------------
# Stats helpers
# ---------------------------------------------------------------------------


def _get_stats_file(project_root: str) -> str:
    """Return path to the stats JSON file for this project."""
    slug = hashlib.md5(project_root.encode()).hexdigest()[:8]
    name = os.path.basename(project_root.rstrip("/"))
    return os.path.join(_STATS_DIR, f"{name}-{slug}.json")


def _load_cumulative_stats(stats_file: str) -> dict:
    """Load cumulative stats from disk, or return empty structure."""
    if not stats_file or not os.path.exists(stats_file):
        return {
            "total_calls": 0,
            "total_chars_returned": 0,
            "total_naive_chars": 0,
            "sessions": 0,
            "tool_counts": {},
            "client_counts": {},
            "history": [],
        }
    try:
        with open(stats_file) as f:
            payload = json.load(f)
            if "history" not in payload:
                payload["history"] = []
            if "client_counts" not in payload:
                payload["client_counts"] = {}
            return payload
    except Exception:
        return {
            "total_calls": 0,
            "total_chars_returned": 0,
            "total_naive_chars": 0,
            "sessions": 0,
            "tool_counts": {},
            "client_counts": {},
            "history": [],
        }


def _flush_stats(slot: _ProjectSlot, naive_chars: int) -> None:
    """Persist a per-session snapshot and recompute cumulative totals."""
    if not slot.stats_file:
        return
    try:
        os.makedirs(_STATS_DIR, exist_ok=True)
        cum = _load_cumulative_stats(slot.stats_file)
        session_calls = sum(_tool_call_counts.values()) - _tool_call_counts.get(
            "get_usage_stats", 0
        )
        cum["project"] = slot.root
        cum["last_session"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        cum["last_client"] = _CLIENT_NAME
        history = [
            entry for entry in cum.get("history", []) if entry.get("session_id") != _session_id
        ]
        savings_pct = (1 - _total_chars_returned / naive_chars) * 100 if naive_chars > 0 else 0.0
        session_entry = {
            "session_id": _session_id,
            "timestamp": cum["last_session"],
            "client_name": _CLIENT_NAME,
            "session_label": _SESSION_LABEL,
            "duration_sec": round(time.time() - _session_start, 3),
            "query_calls": session_calls,
            "chars_returned": _total_chars_returned,
            "naive_chars": naive_chars,
            "tokens_used": _total_chars_returned // 4,
            "tokens_naive": naive_chars // 4,
            "savings_pct": round(savings_pct, 2),
            "tool_counts": {
                tool: count
                for tool, count in _tool_call_counts.items()
                if tool != "get_usage_stats"
            },
        }
        history.append(session_entry)
        history = history[-_MAX_SESSION_HISTORY:]
        cum["history"] = history
        cum["sessions"] = len(history)
        cum["total_calls"] = sum(entry.get("query_calls", 0) for entry in history)
        cum["total_chars_returned"] = sum(entry.get("chars_returned", 0) for entry in history)
        cum["total_naive_chars"] = sum(entry.get("naive_chars", 0) for entry in history)
        aggregate_tool_counts: dict[str, int] = {}
        aggregate_client_counts: dict[str, int] = {}
        for entry in history:
            for tool, count in entry.get("tool_counts", {}).items():
                aggregate_tool_counts[tool] = aggregate_tool_counts.get(tool, 0) + count
            client_name = str(entry.get("client_name") or "unknown").strip() or "unknown"
            aggregate_client_counts[client_name] = aggregate_client_counts.get(client_name, 0) + 1
        cum["tool_counts"] = aggregate_tool_counts
        cum["client_counts"] = aggregate_client_counts
        with open(slot.stats_file, "w") as f:
            json.dump(cum, f, indent=2)
    except Exception as e:
        print(f"[token-savior] Failed to flush stats: {e}", file=sys.stderr)


# Realistic estimate of what % of codebase you'd need to read without the indexer
_TOOL_COST_MULTIPLIERS: dict[str, float] = {
    "get_project_summary": 0.10,
    "list_files": 0.01,
    "get_structure_summary": 0.05,
    "get_functions": 0.05,
    "get_classes": 0.05,
    "get_imports": 0.03,
    "get_function_source": 0.02,
    "get_class_source": 0.03,
    "find_symbol": 0.05,
    "get_dependencies": 0.10,
    "get_dependents": 0.15,
    "get_change_impact": 0.30,
    "get_call_chain": 0.20,
    "get_edit_context": 0.25,  # source + deps + callers in one call
    "get_file_dependencies": 0.02,
    "get_file_dependents": 0.10,
    "search_codebase": 0.15,
    "get_git_status": 0.03,
    "get_changed_symbols": 0.12,
    "get_changed_symbols_since_ref": 0.12,
    "summarize_patch_by_symbol": 0.15,
    "build_commit_summary": 0.18,
    "create_checkpoint": 0.05,
    "list_checkpoints": 0.02,
    "delete_checkpoint": 0.02,
    "prune_checkpoints": 0.03,
    "compare_checkpoint_by_symbol": 0.18,
    "restore_checkpoint": 0.08,
    "replace_symbol_source": 0.20,
    "insert_near_symbol": 0.10,
    "find_impacted_test_files": 0.08,
    "run_impacted_tests": 0.18,
    "apply_symbol_change_and_validate": 0.35,
    "apply_symbol_change_validate_with_rollback": 0.40,
    "discover_project_actions": 0.0,
    "run_project_action": 0.0,
    "reindex": 0.0,
    "set_project_root": 0.0,
    "switch_project": 0.0,
    "list_projects": 0.0,
    # v3
    "get_routes": 0.08,
    "get_env_usage": 0.12,
    "get_components": 0.06,
    "get_feature_files": 0.20,
    "get_entry_points": 0.10,
    "get_symbol_cluster": 0.15,
}


# ---------------------------------------------------------------------------
# Formatting helpers
# ---------------------------------------------------------------------------


def _format_result(value: object) -> str:
    """Format a query result as compact text."""
    if isinstance(value, str):
        return value
    if isinstance(value, (dict, list)):
        return json.dumps(value, separators=(",", ":"), default=str)
    return str(value)


def _count_and_wrap_result(
    slot: _ProjectSlot, name: str, arguments: dict, result: object
) -> list[types.TextContent]:
    """Update usage counters for a tool result and return it as text content."""
    global _total_chars_returned, _total_naive_chars

    formatted = _format_result(result)
    _total_chars_returned += len(formatted)
    _total_naive_chars += _estimate_naive_chars_for_call(slot, name, arguments, result)

    if slot.stats_file:
        _flush_stats(slot, _total_naive_chars)

    return [TextContent(type="text", text=formatted)]


def _estimate_naive_chars_for_call(
    slot: _ProjectSlot, tool_name: str, arguments: dict, result: object
) -> int:
    """Estimate the naive character cost of one tool call."""
    index = slot.indexer._project_index if slot.indexer else None
    if index is None:
        return 0

    source_chars = sum(meta.total_chars for meta in index.files.values())
    file_sizes = {path: meta.total_chars for path, meta in index.files.items()}

    def size_for(paths: list[str]) -> int:
        total = 0
        for path in paths:
            resolved = (
                path
                if path in file_sizes
                else next((p for p in file_sizes if p.endswith(path) or path.endswith(p)), None)
            )
            if resolved:
                total += file_sizes[resolved]
        return total

    if tool_name in {"summarize_patch_by_symbol", "build_commit_summary", "create_checkpoint"}:
        changed_files = arguments.get("changed_files") or arguments.get("file_paths") or []
        return max(size_for(changed_files), len(_format_result(result)))

    if tool_name in {"replace_symbol_source", "insert_near_symbol"} and isinstance(result, dict):
        target_file = result.get("file")
        return max(size_for([target_file]) * 2 if target_file else 0, len(_format_result(result)))

    if tool_name in {"run_impacted_tests", "find_impacted_test_files"} and isinstance(result, dict):
        selection = result.get("selection") or result
        impacted = selection.get("impacted_tests", [])
        changed = selection.get("changed_files", [])
        return max(size_for(impacted + changed), len(_format_result(result)))

    if tool_name in {
        "apply_symbol_change_and_validate",
        "apply_symbol_change_validate_with_rollback",
    } and isinstance(result, dict):
        edit = result.get("edit", {})
        file_path = edit.get("file")
        validation = result.get("validation", {})
        impacted = validation.get("selection", {}).get("impacted_tests", [])
        return max(
            size_for(([file_path] if file_path else []) + impacted) * 2, len(_format_result(result))
        )

    if tool_name in {
        "get_changed_symbols",
        "get_changed_symbols_since_ref",
        "compare_checkpoint_by_symbol",
    } and isinstance(result, dict):
        files = [entry.get("file") for entry in result.get("files", []) if entry.get("file")]
        return max(size_for(files), len(_format_result(result)))

    multiplier = _TOOL_COST_MULTIPLIERS.get(tool_name, 0.10)
    return max(int(source_chars * multiplier), len(_format_result(result)))


def _format_usage_stats(include_cumulative: bool = False) -> str:
    """Format session usage statistics, optionally with cumulative history."""
    elapsed = time.time() - _session_start
    total_calls = sum(_tool_call_counts.values())
    query_calls = total_calls - _tool_call_counts.get("get_usage_stats", 0)

    source_chars = 0
    for slot in _slot_mgr.projects.values():
        if slot.indexer and slot.indexer._project_index:
            source_chars += sum(m.total_chars for m in slot.indexer._project_index.files.values())

    lines = [f"Session: {_format_duration(elapsed)}, {query_calls} queries"]

    if len(_slot_mgr.projects) > 1:
        loaded = sum(1 for s in _slot_mgr.projects.values() if s.indexer is not None)
        lines.append(
            f"Projects: {loaded}/{len(_slot_mgr.projects)} loaded, active: {os.path.basename(_slot_mgr.active_root)}"
        )

    if _tool_call_counts:
        top_tools = sorted(
            ((t, c) for t, c in _tool_call_counts.items() if t != "get_usage_stats"),
            key=lambda x: -x[1],
        )
        tool_str = ", ".join(f"{t}:{c}" for t, c in top_tools[:8])
        if len(top_tools) > 8:
            tool_str += f" +{len(top_tools) - 8} more"
        lines.append(f"Tools: {tool_str}")

    lines.append(f"Chars returned: {_total_chars_returned:,}")
    if source_chars > 0 and query_calls > 0 and _total_naive_chars > _total_chars_returned:
        reduction = (1 - _total_chars_returned / _total_naive_chars) * 100
        lines.append(
            f"Savings: {reduction:.1f}% "
            f"({_total_chars_returned // 4:,} vs {_total_naive_chars // 4:,} tokens)"
        )

    if include_cumulative:
        all_project_stats = []
        for root, slot in _slot_mgr.projects.items():
            sf = slot.stats_file or _get_stats_file(root)
            cum = _load_cumulative_stats(sf)
            if cum.get("total_calls", 0) > 0:
                all_project_stats.append((os.path.basename(root.rstrip("/")), cum))

        if all_project_stats:
            lines.append("")
            lines.append("Project | Sessions | Queries | Used | Naive | Savings")
            total_chars = total_naive = total_calls_cum = total_sessions = 0

            for name, cum in sorted(
                all_project_stats, key=lambda x: -x[1].get("total_naive_chars", 0)
            ):
                c = cum.get("total_chars_returned", 0)
                n = cum.get("total_naive_chars", 0)
                s = cum.get("sessions", 0)
                q = cum.get("total_calls", 0)
                pct = f"{(1 - c / n) * 100:.0f}%" if n > c > 0 else "--"
                lines.append(f"{name} | {s} | {q} | {c // 4:,} | {n // 4:,} | {pct}")
                total_chars += c
                total_naive += n
                total_calls_cum += q
                total_sessions += s

            pct = (
                f"{(1 - total_chars / total_naive) * 100:.0f}%"
                if total_naive > total_chars > 0
                else "--"
            )
            lines.append(
                f"TOTAL | {total_sessions} | {total_calls_cum} | {total_chars // 4:,} | {total_naive // 4:,} | {pct}"
            )

            latest_name, latest_stats = max(
                all_project_stats, key=lambda x: x[1].get("last_session", "")
            )
            history = latest_stats.get("history", [])[-3:]
            if history:
                lines.append("")
                lines.append(f"Recent ({latest_name}):")
                for entry in history:
                    when = entry.get("timestamp", "")[5:19].replace("T", " ")
                    lines.append(
                        f"  {when} | {entry.get('query_calls', 0)} queries | "
                        f"{entry.get('tokens_used', 0):,} / {entry.get('tokens_naive', 0):,} | "
                        f"{entry.get('savings_pct', 0):.0f}%"
                    )

    return "\n".join(lines)


def _format_duration(seconds: float) -> str:
    """Format seconds into a human-readable duration."""
    if seconds < 60:
        return f"{seconds:.0f}s"
    minutes = int(seconds // 60)
    secs = int(seconds % 60)
    if minutes < 60:
        return f"{minutes}m {secs}s"
    hours = minutes // 60
    mins = minutes % 60
    return f"{hours}h {mins}m"


# ---------------------------------------------------------------------------
# Slot management — delegated to SlotManager (token_savior.slot_manager)
# ---------------------------------------------------------------------------


def _prep(slot: _ProjectSlot) -> None:
    """Ensure slot is indexed and incrementally updated."""
    _slot_mgr.ensure(slot)
    _slot_mgr.maybe_update(slot)


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


# ── Index-level handlers (slot + ensure + update → result) ────────────────


def _h_get_git_status(slot, args):
    return get_git_status(slot.root)


def _h_get_changed_symbols(slot, args):
    _prep(slot)
    return get_changed_symbols(
        slot.indexer._project_index,
        ref=args.get("ref") or args.get("since_ref"),
        max_files=args.get("max_files", 20),
        max_symbols_per_file=args.get("max_symbols_per_file", 20),
    )


def _h_get_changed_symbols_since_ref(slot, args):
    """Deprecated alias -- delegates to _h_get_changed_symbols."""
    result = _h_get_changed_symbols(slot, args)
    if isinstance(result, dict):
        result["_deprecated"] = (
            "[DEPRECATED] Use get_changed_symbols(ref=...) instead. "
            "This alias will be removed in v1.1.0."
        )
    return result


def _h_summarize_patch_by_symbol(slot, args):
    _prep(slot)
    return summarize_patch_by_symbol(
        slot.indexer._project_index,
        changed_files=args.get("changed_files"),
        max_files=args.get("max_files", 20),
        max_symbols_per_file=args.get("max_symbols_per_file", 20),
    )


def _h_build_commit_summary(slot, args):
    _prep(slot)
    return build_commit_summary(
        slot.indexer._project_index,
        changed_files=args["changed_files"],
        max_files=args.get("max_files", 20),
        max_symbols_per_file=args.get("max_symbols_per_file", 20),
    )


def _h_create_checkpoint(slot, args):
    _prep(slot)
    return create_checkpoint(slot.indexer._project_index, args["file_paths"])


def _h_list_checkpoints(slot, args):
    _prep(slot)
    return list_checkpoints(slot.indexer._project_index)


def _h_delete_checkpoint(slot, args):
    _prep(slot)
    return delete_checkpoint(slot.indexer._project_index, args["checkpoint_id"])


def _h_prune_checkpoints(slot, args):
    _prep(slot)
    return prune_checkpoints(slot.indexer._project_index, keep_last=args.get("keep_last", 10))


def _h_restore_checkpoint(slot, args):
    _prep(slot)
    result = restore_checkpoint(slot.indexer._project_index, args["checkpoint_id"])
    if result.get("ok"):
        for f in result.get("restored_files", []):
            slot.indexer.reindex_file(f)
    return result


def _h_compare_checkpoint_by_symbol(slot, args):
    _prep(slot)
    return compare_checkpoint_by_symbol(
        slot.indexer._project_index,
        args["checkpoint_id"],
        max_files=args.get("max_files", 20),
    )


def _h_replace_symbol_source(slot, args):
    _prep(slot)
    result = replace_symbol_source(
        slot.indexer._project_index,
        args["symbol_name"],
        args["new_source"],
        file_path=args.get("file_path"),
    )
    if result.get("ok"):
        slot.indexer.reindex_file(result["file"])
    return result


def _h_insert_near_symbol(slot, args):
    _prep(slot)
    result = insert_near_symbol(
        slot.indexer._project_index,
        args["symbol_name"],
        args["content"],
        position=args.get("position", "after"),
        file_path=args.get("file_path"),
    )
    if result.get("ok"):
        slot.indexer.reindex_file(result["file"])
    return result


def _h_find_impacted_test_files(slot, args):
    _prep(slot)
    return find_impacted_test_files(
        slot.indexer._project_index,
        changed_files=args.get("changed_files"),
        symbol_names=args.get("symbol_names"),
        max_tests=args.get("max_tests", 20),
    )


def _h_run_impacted_tests(slot, args):
    _prep(slot)
    return run_impacted_tests(
        slot.indexer._project_index,
        changed_files=args.get("changed_files"),
        symbol_names=args.get("symbol_names"),
        max_tests=args.get("max_tests", 20),
        timeout_sec=args.get("timeout_sec", 120),
        max_output_chars=args.get("max_output_chars", 12000),
        include_output=args.get("include_output", False),
        compact=args.get("compact", False),
    )


def _h_apply_symbol_change_and_validate(slot, args):
    _prep(slot)
    return apply_symbol_change_and_validate(
        slot.indexer,
        args["symbol_name"],
        args["new_source"],
        file_path=args.get("file_path"),
        max_tests=args.get("max_tests", 20),
        timeout_sec=args.get("timeout_sec", 120),
        max_output_chars=args.get("max_output_chars", 12000),
        include_output=args.get("include_output", False),
        compact=args.get("compact", False),
        rollback_on_failure=args.get("rollback_on_failure", False),
    )


def _h_apply_symbol_change_validate_with_rollback(slot, args):
    """Deprecated alias -- delegates with rollback_on_failure=True."""
    args_copy = {**args, "rollback_on_failure": True}
    result = _h_apply_symbol_change_and_validate(slot, args_copy)
    if isinstance(result, dict):
        result["_deprecated"] = (
            "[DEPRECATED] Use apply_symbol_change_and_validate(rollback_on_failure=true) instead. "
            "This alias will be removed in v1.1.0."
        )
    return result


def _h_discover_project_actions(slot, args):
    return discover_project_actions(slot.root)


def _h_run_project_action(slot, args):
    return run_project_action(
        slot.root,
        args["action_id"],
        timeout_sec=args.get("timeout_sec", 120),
        max_output_chars=args.get("max_output_chars", 12000),
        include_output=args.get("include_output", False),
    )


def _h_analyze_config(slot, args):
    _prep(slot)
    return run_config_analysis(
        slot.indexer._project_index,
        checks=args.get("checks"),
        file_path=args.get("file_path"),
        severity=args.get("severity", "all"),
    )


def _h_find_dead_code(slot, args):
    _prep(slot)
    return run_dead_code(slot.indexer._project_index, max_results=args.get("max_results", 50))


def _h_find_hotspots(slot, args):
    _prep(slot)
    return run_hotspots(
        slot.indexer._project_index,
        max_results=args.get("max_results", 20),
        min_score=args.get("min_score", 0.0),
    )


def _h_detect_breaking_changes(slot, args):
    _prep(slot)
    return run_breaking_changes(
        slot.indexer._project_index,
        since_ref=args.get("since_ref", "HEAD~1"),
    )


def _h_find_cross_project_deps(slot, args):
    loaded: dict[str, ProjectIndex] = {}
    for root, s in _slot_mgr.projects.items():
        _slot_mgr.ensure(s)
        if s.indexer and s.indexer._project_index:
            loaded[os.path.basename(root)] = s.indexer._project_index
    return run_cross_project(loaded)


def _h_analyze_docker(slot, args):
    _prep(slot)
    return run_docker_analysis(slot.indexer._project_index)


# Dispatch table: tool name → handler(slot, arguments) → result
_SLOT_HANDLERS: dict[str, object] = {
    "get_git_status": _h_get_git_status,
    "get_changed_symbols": _h_get_changed_symbols,
    "get_changed_symbols_since_ref": _h_get_changed_symbols_since_ref,
    "summarize_patch_by_symbol": _h_summarize_patch_by_symbol,
    "build_commit_summary": _h_build_commit_summary,
    "create_checkpoint": _h_create_checkpoint,
    "list_checkpoints": _h_list_checkpoints,
    "delete_checkpoint": _h_delete_checkpoint,
    "prune_checkpoints": _h_prune_checkpoints,
    "restore_checkpoint": _h_restore_checkpoint,
    "compare_checkpoint_by_symbol": _h_compare_checkpoint_by_symbol,
    "replace_symbol_source": _h_replace_symbol_source,
    "insert_near_symbol": _h_insert_near_symbol,
    "find_impacted_test_files": _h_find_impacted_test_files,
    "run_impacted_tests": _h_run_impacted_tests,
    "apply_symbol_change_and_validate": _h_apply_symbol_change_and_validate,
    "apply_symbol_change_validate_with_rollback": _h_apply_symbol_change_validate_with_rollback,
    "discover_project_actions": _h_discover_project_actions,
    "run_project_action": _h_run_project_action,
    "analyze_config": _h_analyze_config,
    "find_dead_code": _h_find_dead_code,
    "find_hotspots": _h_find_hotspots,
    "detect_breaking_changes": _h_detect_breaking_changes,
    "find_cross_project_deps": _h_find_cross_project_deps,
    "analyze_docker": _h_analyze_docker,
}


# ── Query-function handlers (qfns dict + arguments → result) ─────────────


def _q_get_edit_context(qfns, args):
    sym_name = args["name"]
    max_deps = args.get("max_deps", 10)
    max_callers = args.get("max_callers", 10)
    ctx: dict = {"symbol": sym_name}
    try:
        ctx["source"] = qfns["get_function_source"](sym_name, max_lines=200)
    except Exception:
        try:
            ctx["source"] = qfns["get_class_source"](sym_name, max_lines=200)
        except Exception:
            ctx["source"] = None
    try:
        ctx["location"] = qfns["find_symbol"](sym_name)
    except Exception:
        ctx["location"] = None
    try:
        ctx["dependencies"] = qfns["get_dependencies"](sym_name, max_results=max_deps)
    except Exception:
        ctx["dependencies"] = []
    try:
        ctx["callers"] = qfns["get_dependents"](sym_name, max_results=max_callers)
    except Exception:
        ctx["callers"] = []
    return ctx


# Dispatch table: tool name → handler(qfns, arguments) → result
_QFN_HANDLERS: dict[str, object] = {
    "get_project_summary": lambda q, a: q["get_project_summary"](),
    "list_files": lambda q, a: q["list_files"](
        a.get("pattern"), max_results=a.get("max_results", 0)
    ),
    "get_structure_summary": lambda q, a: q["get_structure_summary"](a.get("file_path")),
    "get_function_source": lambda q, a: q["get_function_source"](
        a["name"], a.get("file_path"), max_lines=a.get("max_lines", 0)
    ),
    "get_class_source": lambda q, a: q["get_class_source"](
        a["name"], a.get("file_path"), max_lines=a.get("max_lines", 0)
    ),
    "get_functions": lambda q, a: q["get_functions"](
        a.get("file_path"), max_results=a.get("max_results", 0)
    ),
    "get_classes": lambda q, a: q["get_classes"](
        a.get("file_path"), max_results=a.get("max_results", 0)
    ),
    "get_imports": lambda q, a: q["get_imports"](
        a.get("file_path"), max_results=a.get("max_results", 0)
    ),
    "find_symbol": lambda q, a: q["find_symbol"](a["name"]),
    "get_dependencies": lambda q, a: q["get_dependencies"](
        a["name"], max_results=a.get("max_results", 0)
    ),
    "get_dependents": lambda q, a: q["get_dependents"](
        a["name"], max_results=a.get("max_results", 0),
        max_total_chars=a.get("max_total_chars", 50_000),
    ),
    "get_change_impact": lambda q, a: q["get_change_impact"](
        a["name"], max_direct=a.get("max_direct", 0), max_transitive=a.get("max_transitive", 0),
        max_total_chars=a.get("max_total_chars", 50_000),
    ),
    "get_call_chain": lambda q, a: q["get_call_chain"](a["from_name"], a["to_name"]),
    "get_edit_context": _q_get_edit_context,
    "get_file_dependencies": lambda q, a: q["get_file_dependencies"](
        a["file_path"], max_results=a.get("max_results", 0)
    ),
    "get_file_dependents": lambda q, a: q["get_file_dependents"](
        a["file_path"], max_results=a.get("max_results", 0)
    ),
    "search_codebase": lambda q, a: q["search_codebase"](
        a["pattern"], max_results=a.get("max_results", 100)
    ),
    "get_routes": lambda q, a: q["get_routes"](max_results=a.get("max_results", 0)),
    "get_env_usage": lambda q, a: q["get_env_usage"](
        a["var_name"], max_results=a.get("max_results", 0)
    ),
    "get_components": lambda q, a: q["get_components"](
        file_path=a.get("file_path"), max_results=a.get("max_results", 0)
    ),
    "get_feature_files": lambda q, a: q["get_feature_files"](
        a["keyword"], max_results=a.get("max_results", 0)
    ),
    "get_entry_points": lambda q, a: q["get_entry_points"](max_results=a.get("max_results", 20)),
    "get_symbol_cluster": lambda q, a: q["get_symbol_cluster"](
        a["name"], max_members=a.get("max_members", 30)
    ),
}


@server.call_tool()
async def call_tool(name: str, arguments: dict) -> list[types.TextContent]:
    global _total_chars_returned, _total_naive_chars

    _tool_call_counts[name] = _tool_call_counts.get(name, 0) + 1

    try:
        # ── Meta tools (no slot needed) ───────────────────────────────────

        if name == "get_usage_stats":
            return [TextContent(type="text", text=_format_usage_stats(include_cumulative=True))]

        if name == "list_projects":
            if not _slot_mgr.projects:
                return [
                    TextContent(
                        type="text",
                        text="No projects registered. Call set_project_root('/path') first.",
                    )
                ]
            lines = [f"Workspace projects ({len(_slot_mgr.projects)}):"]
            for root, slot in _slot_mgr.projects.items():
                status = "indexed" if slot.indexer is not None else "not yet loaded"
                active = " [active]" if root == _slot_mgr.active_root else ""
                name_part = os.path.basename(root)
                if slot.indexer and slot.indexer._project_index:
                    idx = slot.indexer._project_index
                    lines.append(
                        f"  {name_part}{active} -- {idx.total_files} files, {idx.total_functions} functions ({root})"
                    )
                else:
                    lines.append(f"  {name_part}{active} -- {status} ({root})")
            return [TextContent(type="text", text="\n".join(lines))]

        if name == "switch_project":
            hint = arguments["name"]
            slot, err = _slot_mgr.resolve(hint)
            if err:
                return [TextContent(type="text", text=f"Error: {err}")]
            _slot_mgr.active_root = slot.root
            _slot_mgr.ensure(slot)
            idx = slot.indexer._project_index if slot.indexer else None
            info = f"{idx.total_files} files" if idx else "index not built"
            return [
                TextContent(
                    type="text",
                    text=f"Switched to '{os.path.basename(slot.root)}' ({slot.root}) -- {info}.",
                )
            ]

        if name == "set_project_root":
            new_root = os.path.abspath(arguments["path"])
            if not os.path.isdir(new_root):
                return [TextContent(type="text", text=f"Error: '{new_root}' is not a directory.")]
            if new_root not in _slot_mgr.projects:
                _slot_mgr.projects[new_root] = _ProjectSlot(root=new_root)
            _slot_mgr.active_root = new_root
            slot = _slot_mgr.projects[new_root]
            slot.indexer = None
            slot.query_fns = None
            _slot_mgr.build(slot)
            return [TextContent(type="text", text=f"Added and indexed '{new_root}' successfully.")]

        if name == "reindex":
            project_hint = arguments.get("project")
            slot, err = _slot_mgr.resolve(project_hint)
            if err:
                return [TextContent(type="text", text=f"Error: {err}")]
            slot.indexer = None
            slot.query_fns = None
            _slot_mgr.build(slot)
            return [
                TextContent(
                    type="text",
                    text=f"Project '{os.path.basename(slot.root)}' re-indexed successfully.",
                )
            ]

        # ── All other tools need a resolved slot ──────────────────────────

        project_hint = arguments.get("project")
        slot, err = _slot_mgr.resolve(project_hint)
        if err:
            return [TextContent(type="text", text=f"Error: {err}")]

        # Slot-level handlers (index operations, git, analysis)
        handler = _SLOT_HANDLERS.get(name)
        if handler is not None:
            result = handler(slot, arguments)
            return _count_and_wrap_result(slot, name, arguments, result)

        # Query-function handlers (require qfns)
        qfn_handler = _QFN_HANDLERS.get(name)
        if qfn_handler is not None:
            _prep(slot)
            if slot.query_fns is None:
                return [
                    TextContent(
                        type="text",
                        text=f"Error: index not built for '{slot.root}'. Call reindex first.",
                    )
                ]
            result = qfn_handler(slot.query_fns, arguments)
            return _count_and_wrap_result(slot, name, arguments, result)

        return [TextContent(type="text", text=f"Error: unknown tool '{name}'")]

    except Exception as e:
        tb = traceback.format_exc()
        print(f"[token-savior] Error in {name}: {tb}", file=sys.stderr)
        return [TextContent(type="text", text=f"Error: {e}")]


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


async def main():
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
