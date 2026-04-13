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
import threading
import time
import traceback
import uuid
from pathlib import Path

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
from token_savior.markov_prefetcher import MarkovPrefetcher
from token_savior import memory_db

# ---------------------------------------------------------------------------
# Module-level state
# ---------------------------------------------------------------------------

server = Server("token-savior-recall")

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

# Compact Symbol Cache (CSC) — per-session, in-memory.
# Tracks symbols already sent this session so repeat reads return a compact
# stub (cache_token + signature) instead of the full body. Reset on restart.
# key = f"{kind}:{project_root}:{qualified_name}"
# value = {"cache_token": str, "body_hash": str, "view_count": int,
#          "full_source": str, "signature": str}
_session_symbol_cache: dict[str, dict] = {}
_csc_hits: int = 0
_csc_tokens_saved: int = 0  # naive_chars - actual_chars summed across hits

# Persistent stats
_STATS_DIR = os.path.expanduser("~/.local/share/token-savior")
_MAX_SESSION_HISTORY = 200

# Markov prefetcher (P8) — first-order model on tool-call sequences.
_prefetcher = MarkovPrefetcher(Path(_STATS_DIR))
# Pre-warm cache populated by the daemon thread; key = predicted state.
_prefetch_cache: dict[str, str] = {}
_prefetch_lock = threading.Lock()


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

    if tool_name == "apply_symbol_change_and_validate" and isinstance(result, dict):
        edit = result.get("edit", {})
        file_path = edit.get("file")
        validation = result.get("validation", {})
        impacted = validation.get("selection", {}).get("impacted_tests", [])
        return max(
            size_for(([file_path] if file_path else []) + impacted) * 2, len(_format_result(result))
        )

    if tool_name in {
        "get_changed_symbols",
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

    if _csc_hits > 0:
        lines.append(
            f"CSC hits this session: {_csc_hits} ({_csc_tokens_saved:,} tokens saved)"
        )

    # Markov prefetcher state.
    mstats = _prefetcher.get_stats()
    if mstats["transitions"] > 0:
        lines.append(
            f"Markov model: {mstats['states']} states, {mstats['transitions']} transitions"
        )
        lines.append(f"Top sequence: {mstats['top_sequence']}")
        with _prefetch_lock:
            lines.append(f"Prefetch cache: {len(_prefetch_cache)} warmed entries")

    # Symbol-level reindex counters from the last reindex_file call (per slot).
    for root, slot in _slot_mgr.projects.items():
        idx = getattr(slot.indexer, "_project_index", None)
        if idx is None:
            continue
        checked = getattr(idx, "last_reindex_symbols_checked", 0)
        if checked:
            reindexed = idx.last_reindex_symbols_reindexed
            lines.append(
                f"Symbol-level reindex ({os.path.basename(root)}): "
                f"{reindexed}/{checked} symbols reindexed (last file change)"
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

    # Memory Engine stats (non-fatal if DB unreachable)
    try:
        db = memory_db.get_db()
        obs_row = db.execute(
            "SELECT COUNT(*) AS total_obs, "
            "COUNT(DISTINCT project_root) AS projects, "
            "COUNT(DISTINCT session_id) AS sessions, "
            "COUNT(DISTINCT type) AS types, "
            "SUM(CASE WHEN symbol IS NOT NULL THEN 1 ELSE 0 END) AS linked_to_symbol "
            "FROM observations WHERE archived=0"
        ).fetchone()
        prompts_count = db.execute("SELECT COUNT(*) FROM user_prompts").fetchone()[0]
        summaries_count = db.execute("SELECT COUNT(*) FROM summaries").fetchone()[0]
        db.close()

        lines.append("")
        lines.append("──────────────────────────────")
        lines.append("MEMORY ENGINE")
        lines.append("──────────────────────────────")
        linked = obs_row["linked_to_symbol"] or 0
        lines.append(f"Observations  : {obs_row['total_obs']} ({linked} liées à un symbole)")
        lines.append(f"Sessions      : {obs_row['sessions'] or 0}")
        lines.append(f"Projets       : {obs_row['projects'] or 0}")
        lines.append(f"Summaries     : {summaries_count}")
        lines.append(f"Prompts       : {prompts_count}")
        lines.append(f"Types         : {obs_row['types'] or 0} types distincts")

        try:
            active_root = _slot_mgr.active_root or ""
            if active_root:
                roi = memory_db.get_injection_stats(active_root)
                if roi.get("sessions", 0) > 0:
                    lines.append("")
                    lines.append("──────────────────────────────")
                    lines.append("MEMORY ENGINE ROI")
                    lines.append("──────────────────────────────")
                    lines.append(f"Sessions tracked : {roi['sessions']}")
                    lines.append(
                        f"Tokens injected  : {roi['total_injected']} "
                        f"(avg {roi['avg_injected']}/session)"
                    )
                    lines.append(
                        f"Tokens saved est.: {roi['total_saved_est']} "
                        f"(avg {roi['avg_saved']}/session)"
                    )
                    lines.append(f"ROI ratio        : {roi['roi_ratio']}x")
        except Exception:
            pass
    except Exception:
        pass

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
    result = run_impacted_tests(
        slot.indexer._project_index,
        changed_files=args.get("changed_files"),
        symbol_names=args.get("symbol_names"),
        max_tests=args.get("max_tests", 20),
        timeout_sec=args.get("timeout_sec", 120),
        max_output_chars=args.get("max_output_chars", 12000),
        include_output=args.get("include_output", False),
        compact=args.get("compact", False),
    )
    try:
        if isinstance(result, dict) and not result.get("ok", True):
            symbols = args.get("symbol_names") or []
            headline = result.get("summary", {}).get("headline", "test failure")
            timed_out = result.get("timed_out", False)
            obs_type = "warning" if timed_out else "error_pattern"
            title = f"Test failure: {headline}"[:120]
            content_parts = [f"Exit code: {result.get('exit_code')}"]
            if timed_out:
                content_parts.append(f"Timed out after {args.get('timeout_sec', 120)}s")
            if result.get("command"):
                content_parts.append(f"Command: {' '.join(result['command'])}")
            tail = result.get("summary", {}).get("tail", [])
            if tail:
                content_parts.append("Last lines:\n" + "\n".join(tail[-5:]))
            obs_id = memory_db.observation_save(
                session_id=None,
                project_root=slot.root,
                type=obs_type,
                title=title,
                content="\n".join(content_parts),
                symbol=symbols[0] if symbols else None,
                tags=["test-failure", "auto"],
            )
            if obs_id is not None:
                if isinstance(result, dict):
                    result["_memory_saved"] = f"#{obs_id}"
    except Exception:
        pass
    return result


def _h_verify_edit(slot, args):
    """P9 — pure static EditSafety certificate, no mutation."""
    from token_savior.edit_ops import resolve_symbol_location
    from token_savior.edit_verifier import verify_edit

    _prep(slot)
    index = slot.indexer._project_index if slot.indexer else None
    if index is None:
        return "Error: index not built. Call reindex first."
    symbol_name = args["symbol_name"]
    new_source = args["new_source"]
    loc = resolve_symbol_location(
        index, symbol_name, file_path=args.get("file_path")
    )
    if "error" in loc:
        return f"Error: {loc['error']}"
    full_path = (
        loc["file"]
        if os.path.isabs(loc["file"])
        else os.path.join(index.root_path, loc["file"])
    )
    try:
        with open(full_path, "r", encoding="utf-8") as fh:
            source_lines = fh.read().splitlines()
    except OSError as exc:
        return f"Error: cannot read {full_path}: {exc}"
    old_source = "\n".join(source_lines[loc["line"] - 1 : loc["end_line"]])
    cert = verify_edit(old_source, new_source, symbol_name, index.root_path)
    return cert.format()


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
    result = run_breaking_changes(
        slot.indexer._project_index,
        since_ref=args.get("since_ref", "HEAD~1"),
    )
    try:
        if "no breaking changes" not in result:
            import re
            saved = 0
            # Only auto-save observations from the BREAKING: section.
            # WARNING: and NON-BREAKING: entries are informational and must
            # not trigger the guardrail flow.
            in_breaking_section = False
            for raw in result.splitlines():
                line = raw.strip()
                if not line:
                    continue
                if line == "BREAKING:":
                    in_breaking_section = True
                    continue
                if line in ("WARNING:", "NON-BREAKING:"):
                    in_breaking_section = False
                    continue
                if not in_breaking_section:
                    continue
                # Accept both the legacy em-dash separator and the new ASCII hyphen.
                m = re.match(r"(.+?):(\d+)\s+(?:-|\u2014)\s+(.+)", line)
                if not m:
                    continue
                file_path, _, message = m.group(1), m.group(2), m.group(3)
                sym_m = re.match(r"(?:function|class|method)\s+(\w+)", message)
                symbol_name = sym_m.group(1) if sym_m else None
                obs_id = memory_db.observation_save(
                    session_id=None,
                    project_root=slot.root,
                    type="guardrail",
                    title=f"Breaking change: {symbol_name or file_path}",
                    content=f"API change detected: {message}",
                    symbol=symbol_name,
                    file_path=file_path,
                    tags=["breaking-change", "api"],
                )
                if obs_id is not None:
                    saved += 1
            if saved:
                result += f"\n\n\u26a0\ufe0f Guardrail auto-saved to memory for {saved} symbol(s)"
    except Exception:
        pass
    return result


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


# ── Memory Engine handlers ────────────────────────────────────────────────


def _resolve_project_root(arguments: dict) -> str:
    project_hint = arguments.get("project")
    slot, err = _slot_mgr.resolve(project_hint)
    if slot:
        return slot.root
    roots = _parse_workspace_roots()
    if roots:
        return roots[0]
    return os.path.expanduser("~")


def _resolve_memory_project(arguments: dict) -> str:
    """Resolve the project_root for memory tools.

    Falls back from explicit hint → active slot (if it has observations) →
    project with the most observations → active slot → workspace default.
    This lets `memory_index`/`memory_search` work even when the active slot
    is a code project but observations live under a different project_root.
    """
    hint = arguments.get("project")
    if hint:
        slot, _ = _slot_mgr.resolve(hint)
        if slot:
            return slot.root
        return hint

    active_root = _slot_mgr.active_root
    if active_root:
        conn = memory_db.get_db()
        try:
            row = conn.execute(
                "SELECT COUNT(*) FROM observations WHERE project_root=?",
                (active_root,),
            ).fetchone()
        finally:
            conn.close()
        if row and row[0] > 0:
            return active_root

    conn = memory_db.get_db()
    try:
        row = conn.execute(
            "SELECT project_root FROM observations "
            "GROUP BY project_root ORDER BY COUNT(*) DESC LIMIT 1"
        ).fetchone()
    finally:
        conn.close()
    if row:
        return row[0]

    return active_root or _resolve_project_root(arguments)


def _mh_memory_save(args: dict) -> str:
    root = _resolve_memory_project(args)
    obs_type = args["type"]
    title = args["title"]
    content = args["content"]

    conflicts = memory_db.detect_contradictions(root, title, content, obs_type)
    tags = list(args.get("tags") or [])
    if conflicts and "potential-conflict" not in tags:
        tags.append("potential-conflict")

    semantic = memory_db.semantic_dedup_check(root, title, obs_type, threshold=0.85)
    near_dup_warning = None
    if semantic and semantic["score"] < 0.95:
        near_dup_warning = semantic

    obs_id = memory_db.observation_save(
        args.get("session_id"),
        root,
        obs_type,
        title,
        content,
        why=args.get("why"),
        how_to_apply=args.get("how_to_apply"),
        symbol=args.get("symbol"),
        file_path=args.get("file_path"),
        context=args.get("context"),
        tags=tags,
        importance=args.get("importance", 5),
        is_global=bool(args.get("is_global", False)),
        ttl_days=args.get("ttl_days"),
    )
    if obs_id is None:
        return "Duplicate observation (already exists with same content hash)."
    _invalidate_injection_hash()

    try:
        memory_db.auto_link_observation(
            obs_id, root, contradict_ids=[c["id"] for c in conflicts]
        )
    except Exception:
        pass

    scope = " 🌐 global" if args.get("is_global") else ""
    lines = [f"Observation #{obs_id} saved ({obs_type}: {title}){scope}."]
    if conflicts:
        lines.append("⚠️ Potential contradictions detected:")
        for c in conflicts[:5]:
            lines.append(f"  #{c['id']} [{c['type']}] {c['title']} — check if still valid")
    if near_dup_warning:
        lines.append(
            f"⚠️ Near-duplicate: #{near_dup_warning['id']} "
            f"'{near_dup_warning['title']}' (similarity: {near_dup_warning['score']})"
        )
    return "\n".join(lines)


def _mh_memory_promote(args: dict) -> str:
    dry = bool(args.get("dry_run", True))
    root = args.get("project")
    try:
        target = _resolve_memory_project({"project": root}) if root else ""
    except Exception:
        target = ""
    res = memory_db.run_promotions(project_root=target, dry_run=dry)
    lst = res.get("promoted", [])
    if not lst:
        return "No promotion candidates."
    verb = "Would promote" if dry else "Promoted"
    lines = [f"{verb} {len(lst)} observations:"]
    for p in lst:
        lines.append(
            f"  #{p['id']}  [{p['from_type']}→{p['to_type']}]  {p['title']}  ({p['access_count']} accesses)"
        )
    return "\n".join(lines)


def _mh_memory_export_md(args: dict) -> str:
    import subprocess
    out_dir = args.get("output_dir") or "/root/memory-backup"
    script = "/root/token-savior/scripts/export_markdown.py"
    try:
        proc = subprocess.run(
            ["/root/.local/token-savior-venv/bin/python3", script, "--output-dir", out_dir],
            capture_output=True, text=True, timeout=60,
        )
        out = (proc.stdout or "").strip()
        err = (proc.stderr or "").strip()
        if proc.returncode != 0:
            return f"Export failed: {err or out}"
        return out or f"Exported → {out_dir}"
    except Exception as exc:
        return f"Export error: {exc}"


def _mh_memory_relink(args: dict) -> str:
    root = _resolve_memory_project(args)
    dry = bool(args.get("dry_run", False))
    res = memory_db.relink_all(root, dry_run=dry)
    verb = "Would process" if dry else "Processed"
    return (
        f"{verb} {res['processed']} obs → {res['links_created']} links created "
        f"(total in DB: {res['total_links_in_db']}, delta: +{res['delta']})"
    )


def _mh_memory_top(args: dict) -> str:
    root = _resolve_memory_project(args)
    rows = memory_db.get_top_observations(
        root,
        limit=int(args.get("limit", 20)),
        sort_by=args.get("sort_by", "score"),
    )
    if not rows:
        return "No observations."
    lines = [
        f"{'#':>5}  {'TYPE':12}  {'SCORE':>5}  {'ACC':>4}  TITLE",
        "─" * 70,
    ]
    for r in rows:
        flags = ""
        if r.get("is_global"):
            flags += "🌐"
        if r.get("decay_immune"):
            flags += "🔒"
        title = (r["title"] or "")[:40]
        lines.append(
            f"#{r['id']:<4} [{r['type']:10s}] {r['score']:5.2f}  "
            f"{(r.get('access_count') or 0):4d}  {title} {flags}"
        )
    return "\n".join(lines)


def _mh_memory_why(args: dict) -> str:
    res = memory_db.explain_observation(int(args["id"]), args.get("query"))
    if "error" in res:
        return res["error"]
    lines = [
        "═" * 60,
        f"Why #{res['obs_id']} [{res['type']}] '{res['title']}' appears:",
        "─" * 60,
    ]
    for r in res["reasons"]:
        lines.append(f"  {r}")
    return "\n".join(lines)


def _mh_memory_doctor(args: dict) -> str:
    root = _resolve_memory_project(args)
    issues = memory_db.run_health_check(root)
    s = issues["summary"]
    lines = ["🏥 Memory Health Check", "══════════════════════"]
    if issues["orphan_symbols"]:
        lines.append(f"⚠️  {len(issues['orphan_symbols'])} orphan symbols:")
        for o in issues["orphan_symbols"][:10]:
            lines.append(f"  #{o['id']} {o['symbol']} ({o['file_path']}) — {o['title']}")
    else:
        lines.append("✅ No orphan symbols")
    if issues["near_duplicates"]:
        lines.append(f"⚠️  {len(issues['near_duplicates'])} near-duplicates:")
        for d in issues["near_duplicates"][:10]:
            lines.append(
                f"  #{d['id_a']} ≈ #{d['id_b']} ({d['score']}) "
                f"'{d['title_a']}' ≈ '{d['title_b']}'"
            )
    else:
        lines.append("✅ No near-duplicates")
    if issues["incomplete_obs"]:
        lines.append(f"⚠️  {len(issues['incomplete_obs'])} incomplete obs:")
        for o in issues["incomplete_obs"][:10]:
            lines.append(f"  #{o['id']} [{o['type']}] {o['title']}")
    else:
        lines.append("✅ No incomplete obs")
    lines.append(f"\nTotal issues: {s['total_issues']}")
    return "\n".join(lines)


def _mh_memory_patterns(args: dict) -> str:
    root = _resolve_memory_project(args)
    window = int(args.get("window_days", 14))
    min_occ = int(args.get("min_occurrences", 3))
    suggestions = memory_db.analyze_prompt_patterns(
        root, window_days=window, min_occurrences=min_occ
    )
    if not suggestions:
        return f"No recurring patterns (window={window}d, min={min_occ})."
    lines = [f"Found {len(suggestions)} recurring topics without strong memory:"]
    for s in suggestions:
        lines.append(
            f"  · '{s['token']}' mentioned {s['count']}x "
            f"({s['existing_obs_count']} existing obs)"
        )
        for sp in s["sample_prompts"][:2]:
            lines.append(f"      > {sp}")
    lines.append("\n💡 Consider memory_save for recurring topics above.")
    return "\n".join(lines)


def _mh_memory_from_bash(args: dict) -> str:
    root = _resolve_memory_project(args)
    command = (args.get("command") or "").strip()
    if not command:
        return "Empty command."
    obs_type = args.get("type") or "command"
    ctx = args.get("context")
    if not ctx:
        import re as _re
        m = _re.search(r"(systemctl|docker|nginx|crontab|hermes|sirius|python3?|pip|npm|apt)\s+(\S+)", command)
        ctx = m.group(0) if m else command[:60]
    title = command[:60] + ("..." if len(command) > 60 else "")
    obs_id = memory_db.observation_save(
        session_id=None,
        project_root=root,
        type=obs_type,
        title=title,
        content=command,
        context=ctx,
        tags=["bash", "command"],
    )
    if obs_id is None:
        return "Duplicate observation (already exists)."
    return f"Saved bash command #{obs_id}: {title}"


def _mh_memory_make_global(args: dict) -> str:
    obs_id = int(args["id"])
    conn = memory_db.get_db()
    cur = conn.execute(
        "UPDATE observations SET is_global=1, updated_at=? WHERE id=?",
        (memory_db._now_iso(), obs_id),
    )
    conn.commit()
    ok = cur.rowcount > 0
    conn.close()
    return f"Observation #{obs_id} is now 🌐 global." if ok else f"Observation #{obs_id} not found."


def _mh_memory_make_local(args: dict) -> str:
    obs_id = int(args["id"])
    conn = memory_db.get_db()
    cur = conn.execute(
        "UPDATE observations SET is_global=0, updated_at=? WHERE id=?",
        (memory_db._now_iso(), obs_id),
    )
    conn.commit()
    ok = cur.rowcount > 0
    conn.close()
    return f"Observation #{obs_id} scoped back to its project." if ok else f"Observation #{obs_id} not found."


def _mh_memory_search(args: dict) -> str:
    root = _resolve_memory_project(args)
    rows = memory_db.observation_search(
        project_root=root,
        query=args["query"],
        type_filter=args.get("type_filter"),
        limit=args.get("limit", 20),
    )
    if not rows:
        return "No observations match the query."
    lines = ["| ID | Type | Title | Importance | Age |", "|---|---|---|---|---|"]
    for r in rows:
        age = r.get("age") or "?"
        glob = "🌐 " if r.get("is_global") else ""
        lines.append(f"| {r['id']} | {r['type']} | {glob}{r['title']} | {r['importance']} | {age} |")
    lines.append(f"\n{len(rows)} results. Use `memory_get` with IDs for full details.")
    return "\n".join(lines)


def _mh_memory_get(args: dict) -> str:
    ids = args["ids"]
    full = bool(args.get("full", False))
    all_obs = memory_db.observation_get(ids)
    obs_map = {o["id"]: o for o in all_obs}
    blocks = []
    for obs_id in ids:
        obs = obs_map.get(obs_id)
        if obs is None:
            blocks.append(f"## #{obs_id}\nNot found.")
            continue
        b = [f"## #{obs['id']} — {obs['title']}"]
        b.append(f"**Type:** {obs['type']}  **Importance:** {obs['importance']}  **Created:** {obs['created_at'][:10]}")
        if obs.get("symbol"):
            b.append(f"**Symbol:** `{obs['symbol']}`")
        if obs.get("file_path"):
            b.append(f"**File:** {obs['file_path']}")
        content = obs['content'] or ''
        if not full and len(content) > 80:
            content = content[:80] + "... (use full=true for complete content)"
        b.append(f"\n{content}")
        if obs.get("why"):
            w = obs['why']
            if not full and len(w) > 80:
                w = w[:80] + "..."
            b.append(f"\n**Why:** {w}")
        if obs.get("how_to_apply"):
            h = obs['how_to_apply']
            if not full and len(h) > 80:
                h = h[:80] + "..."
            b.append(f"\n**How to apply:** {h}")
        if obs.get("tags"):
            b.append(f"\n**Tags:** {obs['tags']}")
        try:
            links = memory_db.get_linked_observations(obs["id"])
        except Exception:
            links = {"related": [], "contradicts": [], "supersedes": []}
        if links.get("related"):
            parts = [f"#{lk['id']} [{lk['type']}] {lk['title']}" for lk in links["related"][:5]]
            b.append("\n🔗 See also: " + " · ".join(parts))
        if links.get("contradicts"):
            parts = [f"#{lk['id']} [{lk['type']}] {lk['title']}" for lk in links["contradicts"][:5]]
            b.append("⚠️ Contradicts: " + " · ".join(parts))
        if links.get("supersedes"):
            parts = [f"#{lk['id']} {lk['title']}" for lk in links["supersedes"][:5]]
            b.append("↳ Supersedes: " + " · ".join(parts))
        blocks.append("\n".join(b))
    return "\n\n---\n\n".join(blocks)


def _invalidate_injection_hash() -> None:
    for p in (
        "/root/.local/share/token-savior/last_injected_hash",
        "/root/.local/share/token-savior/last_injected_state.json",
    ):
        try:
            if os.path.exists(p):
                os.remove(p)
        except Exception:
            pass


def _mh_memory_delete(args: dict) -> str:
    obs_list = memory_db.observation_get([args["id"]])
    if not obs_list:
        return f"Observation #{args['id']} not found."
    memory_db.observation_delete(args["id"])
    _invalidate_injection_hash()
    return f"Observation #{args['id']} archived."


def _mh_memory_index(args: dict) -> str:
    root = _resolve_memory_project(args)
    rows = memory_db.get_recent_index(
        project_root=root,
        limit=args.get("limit", 30),
        type_filter=args.get("type_filter"),
    )
    if not rows:
        return "No observations yet for this project."
    lines = ["| ID | Type | Title | Imp. | Score | Age |", "|---|---|---|---|---|---|"]
    for r in rows:
        age = r.get("age") or "?"
        score = f"{r.get('relevance_score', 1.0):.2f}"
        glob = "🌐 " if r.get("is_global") else ""
        lines.append(f"| {r['id']} | {r['type']} | {glob}{r['title']} | {r['importance']} | {score} | {age} |")
    lines.append(f"\n{len(rows)} observations. Use `memory_get` with IDs for full details.")
    return "\n".join(lines)


def _mh_memory_timeline(args: dict) -> str:
    root = _resolve_memory_project(args)
    obs_id = args["observation_id"]
    window_hours = args.get("window", 24)
    rows = memory_db.get_timeline_around(
        root,
        obs_id,
        window_hours=window_hours,
    )
    if not rows:
        return f"No timeline context for observation #{obs_id}."
    lines = []
    for r in rows:
        marker = " **⟵**" if r["id"] == obs_id else ""
        date = r["created_at"][:16] if r.get("created_at") else "?"
        lines.append(f"- #{r['id']} [{r['type']}] {r['title']} ({date}){marker}")
    return "\n".join(lines)


def _mh_prompt_save(args: dict) -> str:
    root = _resolve_memory_project(args)
    pid = memory_db.prompt_save(
        None,
        root,
        args["prompt_text"],
        prompt_number=args.get("prompt_number"),
    )
    if pid is None:
        return "Failed to save prompt."
    return f"Prompt #{pid} saved."


def _mh_prompt_search(args: dict) -> str:
    root = _resolve_memory_project(args)
    rows = memory_db.prompt_search(
        project_root=root,
        query=args["query"],
        limit=args.get("limit", 10),
    )
    if not rows:
        return "No prompts match the query."
    lines = ["| ID | # | Excerpt | Date |", "|---|---|---|---|"]
    for r in rows:
        date = r["created_at"][:10] if r.get("created_at") else "?"
        num = r.get("prompt_number") if r.get("prompt_number") is not None else "—"
        excerpt = (r.get("excerpt") or "").replace("|", "\\|").replace("\n", " ")
        lines.append(f"| {r['id']} | {num} | {excerpt} | {date} |")
    lines.append(f"\n{len(rows)} prompt(s) matched.")
    return "\n".join(lines)


def _mh_memory_get_mode(args: dict) -> str:
    try:
        project = _resolve_memory_project({})
    except Exception:
        project = None
    mode = memory_db.get_current_mode(project_root=project)
    lines = [
        f"**Active mode:** `{mode['name']}` · origin: _{mode.get('origin','global')}_ — {mode.get('description', '')}",
        "",
        f"- auto_capture_types   : {', '.join(mode.get('auto_capture_types') or []) or '(none)'}",
        f"- notify_telegram_types: {', '.join(mode.get('notify_telegram_types') or []) or '(none)'}",
        f"- session_summary      : {mode.get('session_summary')}",
        f"- prompt_archive       : {mode.get('prompt_archive')}",
    ]
    all_modes = [m["name"] for m in memory_db.list_modes()]
    lines.append("")
    lines.append(f"Available modes: {', '.join(all_modes)}")
    return "\n".join(lines)


def _mh_memory_set_mode(args: dict) -> str:
    mode = args["mode"]
    ok = memory_db.set_mode(mode, source="manual")
    if not ok:
        return f"Unknown mode: {mode}. Valid: code, review, debug, infra, silent."
    cfg = memory_db.get_current_mode()
    return (
        f"Mode set to `{mode}` (manual — auto-switch disabled until session end) — "
        f"{cfg.get('description', '')}."
    )


def _mh_corpus_build(args: dict) -> str:
    root = _resolve_memory_project(args)
    result = memory_db.corpus_build(
        root,
        args["name"],
        filter_type=args.get("filter_type"),
        filter_tags=args.get("filter_tags"),
        filter_symbol=args.get("filter_symbol"),
    )
    if result["count"] == 0:
        return f"Corpus '{args['name']}' built: 0 observations matched filters."
    tc = result.get("type_counts") or {}
    breakdown = ", ".join(f"{t} x{n}" for t, n in sorted(tc.items(), key=lambda x: -x[1]))
    lines = [f"Corpus '{result['name']}' built: {result['count']} observations ({breakdown})."]
    if result.get("preview"):
        lines.append("")
        lines.append("Preview:")
        for t in result["preview"]:
            lines.append(f"  - {t}")
    return "\n".join(lines)


def _mh_corpus_query(args: dict) -> str:
    root = _resolve_memory_project(args)
    data = memory_db.corpus_get(root, args["name"])
    if not data:
        return f"Corpus '{args['name']}' not found. Build it first with corpus_build."
    obs = data.get("observations") or []
    if not obs:
        return f"Corpus '{args['name']}' is empty."
    lines = [f"=== CORPUS: {args['name']} ({len(obs)} obs) ==="]
    for o in obs:
        lines.append("")
        lines.append(f"### #{o['id']} [{o['type']}] {o['title']}")
        if o.get("symbol"):
            lines.append(f"**Symbol:** `{o['symbol']}`")
        if o.get("file_path"):
            lines.append(f"**File:** {o['file_path']}")
        lines.append("")
        lines.append(o.get("content") or "")
        if o.get("why"):
            lines.append(f"\n**Why:** {o['why']}")
        if o.get("how_to_apply"):
            lines.append(f"\n**How to apply:** {o['how_to_apply']}")
    lines.append("")
    lines.append(f"QUESTION: {args['question']}")
    return "\n".join(lines)


def _mh_memory_decay(args: dict) -> str:
    root = _resolve_memory_project(args) if args.get("project") else None
    dry = args.get("dry_run", True)
    result = memory_db.run_decay(project_root=root, dry_run=dry)
    tag = "DRY RUN — " if dry else ""
    lines = [
        f"**{tag}Decay report** (project: {root or 'all'})",
        "",
        f"- candidates   : {result['candidates']}",
        f"- archived     : {result['archived']}",
        f"- kept active  : {result['kept']}",
        f"- immune       : {result['immune']}",
    ]
    if result.get("preview"):
        lines.append("")
        lines.append("Preview:")
        for p in result["preview"]:
            lines.append(
                f"  #{p['id']} [{p['type']}] {p['title']} "
                f"— accessed {p['access_count']}x — {p['created_at'][:10]}"
            )
    return "\n".join(lines)


def _mh_memory_archived(args: dict) -> str:
    root = _resolve_memory_project(args) if args.get("project") else None
    limit = args.get("limit", 50)
    rows = memory_db.observation_list_archived(project_root=root, limit=limit)
    if not rows:
        return "No archived observations."
    lines = ["| ID | Type | Title | Archived from | Date |", "|---|---|---|---|---|"]
    for r in rows:
        date = r["created_at"][:10] if r.get("created_at") else "?"
        proj = (r.get("project_root") or "").rsplit("/", 1)[-1]
        lines.append(f"| {r['id']} | {r['type']} | {r['title']} | {proj} | {date} |")
    lines.append(f"\n{len(rows)} archived observation(s).")
    return "\n".join(lines)


def _mh_memory_restore(args: dict) -> str:
    ok = memory_db.observation_restore(args["id"])
    return (
        f"Observation #{args['id']} restored."
        if ok
        else f"Observation #{args['id']} not found or already active."
    )


def _mh_memory_set_project_mode(args: dict) -> str:
    project = args["project"]
    mode_name = args["mode"]
    ok = memory_db.set_project_mode(project, mode_name)
    if not ok:
        return f"Unknown mode '{mode_name}'. Valid: code, review, debug, silent."
    return f"Project {project} → mode {mode_name}"


def _mh_memory_status(args: dict) -> str:
    project = _resolve_memory_project(args)
    db = memory_db.get_db()
    active = db.execute(
        "SELECT COUNT(*) FROM observations WHERE project_root=? AND archived=0",
        [project],
    ).fetchone()[0]
    archived = db.execute(
        "SELECT COUNT(*) FROM observations WHERE project_root=? AND archived=1",
        [project],
    ).fetchone()[0]
    sessions = db.execute(
        "SELECT COUNT(*) FROM sessions WHERE project_root=?", [project]
    ).fetchone()[0]
    last_session = db.execute(
        "SELECT created_at, end_type, status FROM sessions "
        "WHERE project_root=? ORDER BY id DESC LIMIT 1",
        [project],
    ).fetchone()
    last_summary = db.execute(
        "SELECT created_at FROM summaries WHERE project_root=? ORDER BY id DESC LIMIT 1",
        [project],
    ).fetchone()
    summary_count = db.execute(
        "SELECT COUNT(*) FROM summaries WHERE project_root=?", [project]
    ).fetchone()[0]
    prompts = db.execute(
        "SELECT COUNT(*) FROM user_prompts WHERE project_root=?", [project]
    ).fetchone()[0]
    db.close()

    mode = memory_db.get_current_mode()
    mode_name = mode.get("name", "code")

    sess_line = f"{sessions}"
    if last_session:
        day = (last_session["created_at"] or "")[:10]
        et = last_session["end_type"] or last_session["status"] or "?"
        sess_line = f"{sessions} (last: {day} end={et})"

    sum_line = f"{summary_count}"
    if last_summary:
        sum_line = f"{summary_count} (last: {(last_summary['created_at'] or '')[:10]})"

    rows = [
        ("Project", project),
        ("Mode", mode_name),
        ("Obs", f"{active} active · {archived} archived"),
        ("Sessions", sess_line),
        ("Summaries", sum_line),
        ("Prompts", f"{prompts} archived"),
    ]
    label_w = max(len(r[0]) for r in rows)
    val_w = max(len(r[1]) for r in rows)
    inner = label_w + val_w + 5
    top = "┌─ Memory Engine Status " + "─" * (inner - len(" Memory Engine Status ") - 1) + "┐"
    bot = "└" + "─" * (inner) + "┘"
    body = [f"│ {k.ljust(label_w)} : {v.ljust(val_w)} │" for k, v in rows]
    return "\n".join([top] + body + [bot])


def _mh_memory_set_global(args: dict) -> str:
    if args.get("is_global"):
        return _mh_memory_make_global({"id": args["id"]})
    return _mh_memory_make_local({"id": args["id"]})


def _mh_memory_mode(args: dict) -> str:
    action = args.get("action", "get")
    if action == "get":
        return _mh_memory_get_mode({})
    if action == "set":
        return _mh_memory_set_mode({"mode": args["mode"]})
    if action == "set_project":
        return _mh_memory_set_project_mode({"project": args["project"], "mode": args["mode"]})
    return f"Unknown action: {action}"


def _mh_memory_archive(args: dict) -> str:
    action = args.get("action", "list")
    if action == "run":
        return _mh_memory_decay({"dry_run": args.get("dry_run", True), "project": args.get("project")})
    if action == "list":
        return _mh_memory_archived({"limit": args.get("limit", 50), "project": args.get("project")})
    if action == "restore":
        return _mh_memory_restore({"id": args["id"]})
    return f"Unknown action: {action}"


def _mh_memory_maintain(args: dict) -> str:
    action = args.get("action")
    if action == "promote":
        return _mh_memory_promote({"dry_run": args.get("dry_run", True), "project": args.get("project")})
    if action == "relink":
        return _mh_memory_relink({"dry_run": args.get("dry_run", False)})
    if action == "export":
        return _mh_memory_export_md({"output_dir": args.get("output_dir", "/root/memory-backup")})
    if action == "patterns":
        return _mh_memory_patterns({
            "window_days": args.get("window_days", 14),
            "min_occurrences": args.get("min_occurrences", 3),
        })
    return f"Unknown action: {action}"


def _mh_memory_prompts(args: dict) -> str:
    action = args.get("action")
    if action == "save":
        return _mh_prompt_save({
            "prompt_text": args["prompt_text"],
            "prompt_number": args.get("prompt_number"),
            "project": args.get("project"),
        })
    if action == "search":
        return _mh_prompt_search({
            "query": args["query"],
            "limit": args.get("limit", 10),
            "project": args.get("project"),
        })
    return f"Unknown action: {action}"


_MEMORY_HANDLERS: dict[str, object] = {
    "memory_save": _mh_memory_save,
    "memory_search": _mh_memory_search,
    "memory_get": _mh_memory_get,
    "memory_delete": _mh_memory_delete,
    "memory_index": _mh_memory_index,
    "memory_timeline": _mh_memory_timeline,
    "memory_prompts": _mh_memory_prompts,
    "memory_mode": _mh_memory_mode,
    "corpus_build": _mh_corpus_build,
    "corpus_query": _mh_corpus_query,
    "memory_archive": _mh_memory_archive,
    "memory_status": _mh_memory_status,
    "memory_maintain": _mh_memory_maintain,
    "memory_from_bash": _mh_memory_from_bash,
    "memory_doctor": _mh_memory_doctor,
    "memory_why": _mh_memory_why,
    "memory_top": _mh_memory_top,
    "memory_set_global": _mh_memory_set_global,
}

# Dispatch table: tool name → handler(slot, arguments) → result
_SLOT_HANDLERS: dict[str, object] = {
    "get_git_status": _h_get_git_status,
    "get_changed_symbols": _h_get_changed_symbols,
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
    "verify_edit": _h_verify_edit,
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


def _lookup_symbol_meta(slot, args: dict) -> tuple[str, str, str] | None:
    """Return (kind, body_hash, signature) for a symbol, or None if unresolved."""
    try:
        idx = slot.indexer._project_index
    except AttributeError:
        return None
    name = args.get("name")
    if not name or idx is None:
        return None
    file_path = args.get("file_path")

    def _check(meta, rel_path):
        for func in meta.functions:
            if func.name == name or func.qualified_name == name:
                sig = f"def {func.name}({', '.join(func.parameters)})"
                return ("function", func.body_hash, sig)
        for cls in meta.classes:
            if cls.name == name:
                sig = f"class {cls.name}"
                if cls.base_classes:
                    sig += f"({', '.join(cls.base_classes)})"
                return ("class", cls.body_hash, sig)
            for method in cls.methods:
                if method.qualified_name == name:
                    sig = f"def {method.qualified_name}({', '.join(method.parameters)})"
                    return ("function", method.body_hash, sig)
        return None

    if file_path:
        for rel_path, meta in idx.files.items():
            if rel_path == file_path or rel_path.endswith(file_path):
                got = _check(meta, rel_path)
                if got:
                    return got
        return None
    if name in idx.symbol_table:
        rel_path = idx.symbol_table[name]
        meta = idx.files.get(rel_path)
        if meta is not None:
            got = _check(meta, rel_path)
            if got:
                return got
    for rel_path, meta in idx.files.items():
        got = _check(meta, rel_path)
        if got:
            return got
    return None


def _csc_compact_response(
    name: str,
    signature: str,
    cache_tok: str,
    view_count: int,
    modified: bool,
    diff_preview: str = "",
) -> str:
    """Format a compact CSC hit response."""
    tag = "[MODIFIED]" if modified else ""
    header = f"@sym:{cache_tok} [{name}] {tag}".rstrip()
    lines = [header]
    if signature:
        lines.append(f"Signature: {signature}")
    if modified:
        lines.append(f"Changed body, prior views: {view_count}")
        if diff_preview:
            lines.append("Diff (first lines):")
            lines.append(diff_preview)
    else:
        lines.append(
            f"(body unchanged since last view - {view_count} view{'s' if view_count != 1 else ''} this session)"
        )
    lines.append(
        "Use force_full=true to bypass the session cache and get the full body."
    )
    return "\n".join(lines)


def _csc_diff_preview(old_full: str, new_full: str, max_lines: int = 5) -> str:
    """Return the first `max_lines` diff hunks between two bodies (trivial line diff)."""
    import difflib

    diff = difflib.unified_diff(
        old_full.splitlines(),
        new_full.splitlines(),
        lineterm="",
        n=1,
    )
    out: list[str] = []
    for line in diff:
        if line.startswith("@@") or line.startswith("---") or line.startswith("+++"):
            continue
        out.append(line)
        if len(out) >= max_lines:
            break
    return "\n".join(out)


def _csc_maybe_serve(
    slot,
    kind: str,
    args: dict,
    produce_full,
) -> str:
    """Entry point for get_function_source / get_class_source with CSC.

    `produce_full` is a zero-arg callable returning the full formatted source.
    Returns either the compact stub (cache hit, body unchanged) or the full
    source (miss / force_full / modified).
    """
    global _csc_hits, _csc_tokens_saved

    force_full = bool(args.get("force_full", False))
    level = int(args.get("level", 0) or 0)

    full = produce_full()
    # Skip cache when:
    # - caller asked for non-L0 abstraction (already compact by design)
    # - symbol wasn't resolvable (error messages must pass through verbatim)
    # - force_full is set
    if level > 0 or force_full or full.startswith("Error:"):
        return full

    meta = _lookup_symbol_meta(slot, args)
    if meta is None:
        return full
    _kind, body_hash, signature = meta
    if not body_hash:
        return full

    project_root = getattr(slot, "root", "") or ""
    name = args["name"]
    key = f"{kind}:{project_root}:{name}"
    entry = _session_symbol_cache.get(key)

    from token_savior.symbol_hash import cache_token

    tok = cache_token(body_hash)

    if entry is None:
        # Miss — return full, record.
        _session_symbol_cache[key] = {
            "cache_token": tok,
            "body_hash": body_hash,
            "view_count": 1,
            "full_source": full,
            "signature": signature,
        }
        return full

    entry["view_count"] += 1
    prior_full = entry.get("full_source", "")
    if entry["body_hash"] == body_hash:
        compact = _csc_compact_response(
            name=name,
            signature=signature,
            cache_tok=tok,
            view_count=entry["view_count"],
            modified=False,
        )
        saved = max(0, len(full) - len(compact))
        _csc_hits += 1
        _csc_tokens_saved += saved // 4
        return compact

    # Modified — return compact with diff preview; refresh cache.
    diff_preview = _csc_diff_preview(prior_full, full)
    compact = _csc_compact_response(
        name=name,
        signature=signature,
        cache_tok=tok,
        view_count=entry["view_count"],
        modified=True,
        diff_preview=diff_preview,
    )
    saved = max(0, len(full) - len(compact))
    _csc_hits += 1
    _csc_tokens_saved += saved // 4
    entry.update(
        {
            "cache_token": tok,
            "body_hash": body_hash,
            "full_source": full,
            "signature": signature,
        }
    )
    return compact


def _q_get_class_source(qfns, args: dict) -> str:
    slot, _ = _slot_mgr.resolve(args.get("project"))
    return _csc_maybe_serve(
        slot,
        "class",
        args,
        lambda: qfns["get_class_source"](
            args["name"],
            args.get("file_path"),
            max_lines=args.get("max_lines", 0),
            level=args.get("level", 0),
        ),
    )


def _q_get_function_source(qfns, args: dict) -> str:
    slot, _ = _slot_mgr.resolve(args.get("project"))
    result = _csc_maybe_serve(
        slot,
        "function",
        args,
        lambda: qfns["get_function_source"](
            args["name"],
            args.get("file_path"),
            max_lines=args.get("max_lines", 0),
            level=args.get("level", 0),
        ),
    )
    try:
        project_root = _resolve_project_root(args)
        symbol_name = args["name"]
        file_path = args.get("file_path")
        rows = memory_db.observation_get_by_symbol(
            project_root, symbol_name, file_path=file_path, limit=3
        )
        if rows:
            lines = ["\n───", f"📌 Memory ({len(rows)}):"]
            for r in rows:
                age = r.get("age") or "?"
                stale = "⚠️ " if r.get("stale") else ""
                glob = "🌐 " if r.get("is_global") else ""
                lines.append(f"  #{r['id']}  [{r['type']}]  {stale}{glob}{r['title']}  — {age}")
            result += "\n".join(lines)
    except Exception:
        pass
    return result


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
    "get_function_source": _q_get_function_source,
    "get_class_source": _q_get_class_source,
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
    "get_backward_slice": lambda q, a: q["get_backward_slice"](
        a["name"], a["variable"], a["line"], file_path=a.get("file_path")
    ),
    "pack_context": lambda q, a: q["pack_context"](
        a["query"],
        budget_tokens=a.get("budget_tokens", 4000),
        max_symbols=a.get("max_symbols", 20),
    ),
    "get_relevance_cluster": lambda q, a: q["get_relevance_cluster"](
        a["name"],
        budget=a.get("budget", 10),
        include_reverse=a.get("include_reverse", True),
    ),
    "find_semantic_duplicates": lambda q, a: q["find_semantic_duplicates"](
        min_lines=a.get("min_lines", 4)
    ),
}


def _warm_cache_async(
    predictions: list[tuple[str, float]], slot, min_prob: float = 0.25
) -> None:
    """Spawn a daemon thread to pre-render likely next responses.

    daemon=True is critical: if the MCP server shuts down mid-prefetch, the
    thread is killed with the process instead of holding it open.
    """
    if not predictions:
        return

    def _worker() -> None:
        try:
            qfns = slot.query_fns if slot is not None else None
            for state, prob in predictions:
                if prob < min_prob:
                    continue
                if ":" not in state:
                    continue
                next_tool, next_symbol = state.split(":", 1)
                if not next_symbol or qfns is None:
                    continue
                produce = None
                if next_tool == "get_function_source":
                    produce = lambda s=next_symbol: qfns["get_function_source"](s)
                elif next_tool == "get_class_source":
                    produce = lambda s=next_symbol: qfns["get_class_source"](s)
                elif next_tool == "get_dependents":
                    produce = lambda s=next_symbol: qfns["get_dependents"](s)
                elif next_tool == "get_dependencies":
                    produce = lambda s=next_symbol: qfns["get_dependencies"](s)
                elif next_tool == "find_symbol":
                    produce = lambda s=next_symbol: qfns["find_symbol"](s)
                if produce is None:
                    continue
                try:
                    result = produce()
                except Exception:
                    continue
                with _prefetch_lock:
                    _prefetch_cache[state] = (
                        result if isinstance(result, str) else str(result)
                    )
                    if len(_prefetch_cache) > 64:
                        # Bound memory; drop oldest entries (insertion order).
                        for stale_key in list(_prefetch_cache)[:32]:
                            _prefetch_cache.pop(stale_key, None)
        except Exception:
            pass  # never crash the daemon

    t = threading.Thread(target=_worker, daemon=True)
    t.start()


@server.call_tool()
async def call_tool(name: str, arguments: dict) -> list[types.TextContent]:
    global _total_chars_returned, _total_naive_chars

    _tool_call_counts[name] = _tool_call_counts.get(name, 0) + 1
    _record_symbol = arguments.get("name") or arguments.get("symbol_name", "")
    try:
        _prefetcher.record_call(name, _record_symbol or "")
    except Exception:
        pass

    try:
        # ── Meta tools (no slot needed) ───────────────────────────────────

        if name == "get_usage_stats":
            return [TextContent(type="text", text=_format_usage_stats(include_cumulative=True))]

        if name == "get_call_predictions":
            tool_name = arguments.get("tool_name", "")
            symbol_name = arguments.get("symbol_name", "")
            top_k = int(arguments.get("top_k", 5))
            preds = _prefetcher.predict_next(tool_name, symbol_name, top_k=top_k)
            if not preds:
                return [
                    TextContent(
                        type="text",
                        text=(
                            f"No transitions recorded for state "
                            f"'{tool_name}:{symbol_name}' yet."
                        ),
                    )
                ]
            lines = [f"Markov predictions after {tool_name}({symbol_name}):"]
            for state, prob in preds:
                lines.append(f"  {prob*100:5.1f}%  {state}")
            return [TextContent(type="text", text="\n".join(lines))]

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

        # ── Memory tools (no slot required) ──────────────────────────────

        mem_handler = _MEMORY_HANDLERS.get(name)
        if mem_handler is not None:
            result = mem_handler(arguments)
            return [TextContent(type="text", text=result)]

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
            # Markov: predict next likely calls and pre-warm in a daemon thread
            try:
                preds = _prefetcher.predict_next(
                    name, _record_symbol or "", top_k=3
                )
                if preds:
                    _warm_cache_async(preds, slot)
            except Exception:
                pass
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
