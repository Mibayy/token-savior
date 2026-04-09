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

import dataclasses
import fnmatch
import hashlib
import json
import os
import sys
import time
import traceback
import uuid
from typing import Optional

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import Tool, TextContent
import mcp.types as types

from token_savior.git_tracker import is_git_repo, get_head_commit, get_changed_files, get_git_status
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
from token_savior.git_ops import build_commit_summary, get_changed_symbols_since_ref, summarize_patch_by_symbol
from token_savior.impacted_tests import find_impacted_test_files, run_impacted_tests
from token_savior.models import ProjectIndex
from token_savior.project_indexer import ProjectIndexer
from token_savior.project_actions import discover_project_actions, run_project_action
from token_savior.query_api import create_project_query_functions
from token_savior.workflow_ops import apply_symbol_change_and_validate, apply_symbol_change_validate_with_rollback
from token_savior.breaking_changes import detect_breaking_changes as run_breaking_changes
from token_savior.complexity import find_hotspots as run_hotspots
from token_savior.config_analyzer import analyze_config as run_config_analysis
from token_savior.cross_project import find_cross_project_deps as run_cross_project
from token_savior.dead_code import find_dead_code as run_dead_code
from token_savior.docker_analyzer import analyze_docker as run_docker_analysis

# ---------------------------------------------------------------------------
# Per-project slot — one per workspace root, fully isolated
# ---------------------------------------------------------------------------

@dataclasses.dataclass
class _ProjectSlot:
    root: str
    indexer: Optional[ProjectIndexer] = None
    query_fns: Optional[dict] = None
    is_git: bool = False
    stats_file: str = ""
    # Incremental update tracking
    _last_update_check: float = 0.0


# ---------------------------------------------------------------------------
# Module-level state
# ---------------------------------------------------------------------------

server = Server("token-savior")

# Dict of abs_path -> slot. Populated from WORKSPACE_ROOTS or PROJECT_ROOT.
_projects: dict[str, _ProjectSlot] = {}

# Currently active project root (used by tools that don't specify a project).
_active_root: str = ""

# Persistent cache
_CACHE_FILENAME = ".token-savior-cache.json"
_LEGACY_CACHE_FILENAME = ".codebase-index-cache.json"  # auto-migrate
_CACHE_VERSION = 2  # Bumped: switched from pickle to JSON

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
    global _active_root
    for root in roots:
        if root not in _projects:
            _projects[root] = _ProjectSlot(root=root)
    if roots and not _active_root:
        _active_root = roots[0]


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
        session_calls = sum(_tool_call_counts.values()) - _tool_call_counts.get("get_usage_stats", 0)
        cum["project"] = slot.root
        cum["last_session"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        cum["last_client"] = _CLIENT_NAME
        history = [entry for entry in cum.get("history", []) if entry.get("session_id") != _session_id]
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


def _count_and_wrap_result(slot: _ProjectSlot, name: str, arguments: dict, result: object) -> list[types.TextContent]:
    """Update usage counters for a tool result and return it as text content."""
    global _total_chars_returned, _total_naive_chars

    formatted = _format_result(result)
    _total_chars_returned += len(formatted)
    _total_naive_chars += _estimate_naive_chars_for_call(slot, name, arguments, result)

    if slot.stats_file:
        _flush_stats(slot, _total_naive_chars)

    return [TextContent(type="text", text=formatted)]


def _estimate_naive_chars_for_call(slot: _ProjectSlot, tool_name: str, arguments: dict, result: object) -> int:
    """Estimate the naive character cost of one tool call."""
    index = slot.indexer._project_index if slot.indexer else None
    if index is None:
        return 0

    source_chars = sum(meta.total_chars for meta in index.files.values())
    file_sizes = {path: meta.total_chars for path, meta in index.files.items()}

    def size_for(paths: list[str]) -> int:
        total = 0
        for path in paths:
            resolved = path if path in file_sizes else next((p for p in file_sizes if p.endswith(path) or path.endswith(p)), None)
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

    if tool_name in {"apply_symbol_change_and_validate", "apply_symbol_change_validate_with_rollback"} and isinstance(result, dict):
        edit = result.get("edit", {})
        file_path = edit.get("file")
        validation = result.get("validation", {})
        impacted = validation.get("selection", {}).get("impacted_tests", [])
        return max(size_for(([file_path] if file_path else []) + impacted) * 2, len(_format_result(result)))

    if tool_name in {"get_changed_symbols", "get_changed_symbols_since_ref", "compare_checkpoint_by_symbol"} and isinstance(result, dict):
        files = [entry.get("file") for entry in result.get("files", []) if entry.get("file")]
        return max(size_for(files), len(_format_result(result)))

    multiplier = _TOOL_COST_MULTIPLIERS.get(tool_name, 0.10)
    return max(int(source_chars * multiplier), len(_format_result(result)))


def _format_usage_stats(include_cumulative: bool = False) -> str:
    """Format session usage statistics, optionally with cumulative history."""
    elapsed = time.time() - _session_start
    total_calls = sum(_tool_call_counts.values())
    query_calls = total_calls - _tool_call_counts.get("get_usage_stats", 0)

    # Aggregate source size across all loaded projects
    source_chars = 0
    for slot in _projects.values():
        if slot.indexer and slot.indexer._project_index:
            source_chars += sum(m.total_chars for m in slot.indexer._project_index.files.values())

    lines = [
        f"Session duration: {_format_duration(elapsed)}",
        f"Total queries: {query_calls}",
    ]

    if len(_projects) > 1:
        loaded = [s.root for s in _projects.values() if s.indexer is not None]
        lines.append(f"Projects loaded: {len(loaded)}/{len(_projects)}")
        for root in loaded:
            lines.append(f"  • {os.path.basename(root)} ({root})")
        if _active_root:
            lines.append(f"Active project: {os.path.basename(_active_root)}")

    if _tool_call_counts:
        lines.append("")
        lines.append("Queries by tool:")
        for tool_name, count in sorted(_tool_call_counts.items(), key=lambda x: -x[1]):
            if tool_name == "get_usage_stats":
                continue
            lines.append(f"  {tool_name}: {count}")

    lines.append("")
    lines.append(f"Total chars returned: {_total_chars_returned:,}")

    if source_chars > 0:
        lines.append(f"Total source in index: {source_chars:,} chars")
        if query_calls > 0 and _total_naive_chars > _total_chars_returned:
            naive_chars = _total_naive_chars
            reduction = (1 - _total_chars_returned / naive_chars) * 100 if naive_chars > 0 else 0
            lines.append(
                f"Estimated without indexer: {naive_chars:,} chars "
                f"({naive_chars // 4:,} tokens) over {query_calls} queries"
            )
            lines.append(
                f"Estimated with indexer: {_total_chars_returned:,} chars "
                f"({_total_chars_returned // 4:,} tokens)"
            )
            lines.append(f"Estimated token savings: {reduction:.1f}%")

    if include_cumulative:
        # Collect cumulative stats for ALL projects with data
        all_project_stats = []
        for root, slot in _projects.items():
            sf = slot.stats_file or _get_stats_file(root)
            cum = _load_cumulative_stats(sf)
            if cum.get("total_calls", 0) > 0:
                all_project_stats.append((os.path.basename(root.rstrip("/")), cum))

        if all_project_stats:
            lines.append("")
            lines.append("─── Cumulative token savings per project ───")

            # Header
            lines.append(f"  {'Project':<26} {'Sessions':>8} {'Queries':>8} {'Tokens used':>12} {'Tokens naive':>13} {'Savings':>8}")
            lines.append(f"  {'─'*26} {'─'*8} {'─'*8} {'─'*12} {'─'*13} {'─'*8}")

            total_chars = 0
            total_naive = 0
            total_calls = 0
            total_sessions = 0

            for name, cum in sorted(all_project_stats, key=lambda x: -x[1].get("total_naive_chars", 0)):
                cum_chars = cum.get("total_chars_returned", 0)
                cum_naive = cum.get("total_naive_chars", 0)
                cum_calls = cum.get("total_calls", 0)
                cum_sessions = cum.get("sessions", 0)
                savings = (1 - cum_chars / cum_naive) * 100 if cum_naive > cum_chars > 0 else 0
                tokens_used = cum_chars // 4
                tokens_naive = cum_naive // 4
                savings_str = f"{savings:.0f}%" if cum_naive > 0 else "—"
                lines.append(
                    f"  {name:<26} {cum_sessions:>8} {cum_calls:>8} {tokens_used:>12,} {tokens_naive:>13,} {savings_str:>8}"
                )
                total_chars += cum_chars
                total_naive += cum_naive
                total_calls += cum_calls
                total_sessions += cum_sessions

            # Total row
            total_savings = (1 - total_chars / total_naive) * 100 if total_naive > total_chars > 0 else 0
            lines.append(f"  {'─'*26} {'─'*8} {'─'*8} {'─'*12} {'─'*13} {'─'*8}")
            lines.append(
                f"  {'TOTAL':<26} {total_sessions:>8} {total_calls:>8} {total_chars//4:>12,} {total_naive//4:>13,} {total_savings:.0f}%"
            )

            latest_project_name, latest_project_stats = max(
                all_project_stats,
                key=lambda item: item[1].get("last_session", ""),
            )
            history = latest_project_stats.get("history", [])[-5:]
            if history:
                lines.append("")
                lines.append(f"─── Recent session log ({latest_project_name}) ───")
                lines.append(
                    f"  {'When':<20} {'Queries':>8} {'Used':>10} {'Naive':>10} {'Savings':>8}"
                )
                lines.append(f"  {'─'*20} {'─'*8} {'─'*10} {'─'*10} {'─'*8}")
                for entry in history:
                    when = entry.get("timestamp", "")[5:19].replace("T", " ")
                    lines.append(
                        f"  {when:<20} "
                        f"{entry.get('query_calls', 0):>8} "
                        f"{entry.get('tokens_used', 0):>10,} "
                        f"{entry.get('tokens_naive', 0):>10,} "
                        f"{entry.get('savings_pct', 0):>7.1f}%"
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
# Cache helpers
# ---------------------------------------------------------------------------

def _cache_path(project_root: str) -> str:
    """Return the path to the JSON cache file for this project.
    Auto-migrates legacy .codebase-index-cache.json → .token-savior-cache.json."""
    new_path = os.path.join(project_root, _CACHE_FILENAME)
    if not os.path.exists(new_path):
        legacy = os.path.join(project_root, _LEGACY_CACHE_FILENAME)
        if os.path.exists(legacy):
            try:
                os.rename(legacy, new_path)
                print(f"[token-savior] Migrated cache {_LEGACY_CACHE_FILENAME} → {_CACHE_FILENAME}", file=sys.stderr)
            except OSError:
                return legacy  # fallback to old name if rename fails
    return new_path


def _index_to_dict(index: "ProjectIndex") -> dict:
    """Serialize a ProjectIndex to a JSON-compatible dict (sets become sorted lists)."""
    from dataclasses import asdict

    def _convert(obj):
        if isinstance(obj, set):
            return sorted(obj)
        if isinstance(obj, dict):
            return {k: _convert(v) for k, v in obj.items()}
        if isinstance(obj, list):
            return [_convert(i) for i in obj]
        return obj

    return _convert(asdict(index))


def _index_from_dict(data: dict) -> "ProjectIndex":
    """Deserialize a ProjectIndex from JSON dict, restoring sets where needed."""
    from token_savior.models import (
        ProjectIndex, StructuralMetadata, FunctionInfo, ClassInfo,
        ImportInfo, SectionInfo, LineRange
    )

    def _lr(d: dict) -> LineRange:
        return LineRange(start=d["start"], end=d["end"])

    def _fi(d: dict) -> FunctionInfo:
        return FunctionInfo(
            name=d["name"], qualified_name=d["qualified_name"],
            line_range=_lr(d["line_range"]), parameters=d["parameters"],
            decorators=d["decorators"], docstring=d.get("docstring"),
            is_method=d["is_method"], parent_class=d.get("parent_class"),
        )

    def _ci(d: dict) -> ClassInfo:
        return ClassInfo(
            name=d["name"], line_range=_lr(d["line_range"]),
            base_classes=d["base_classes"], methods=[_fi(m) for m in d["methods"]],
            decorators=d["decorators"], docstring=d.get("docstring"),
        )

    def _ii(d: dict) -> ImportInfo:
        return ImportInfo(
            module=d["module"], names=d["names"], alias=d.get("alias"),
            line_number=d["line_number"], is_from_import=d["is_from_import"],
        )

    def _si(d: dict) -> SectionInfo:
        return SectionInfo(title=d["title"], level=d["level"], line_range=_lr(d["line_range"]))

    def _sm(d: dict) -> StructuralMetadata:
        return StructuralMetadata(
            source_name=d["source_name"], total_lines=d["total_lines"],
            total_chars=d["total_chars"], lines=d["lines"],
            line_char_offsets=d["line_char_offsets"],
            functions=[_fi(f) for f in d.get("functions", [])],
            classes=[_ci(c) for c in d.get("classes", [])],
            imports=[_ii(i) for i in d.get("imports", [])],
            sections=[_si(s) for s in d.get("sections", [])],
            dependency_graph=d.get("dependency_graph", {}),
        )

    def _sets(d: dict) -> dict[str, set[str]]:
        return {k: set(v) for k, v in d.items()}

    return ProjectIndex(
        root_path=data["root_path"],
        files={k: _sm(v) for k, v in data["files"].items()},
        global_dependency_graph=_sets(data.get("global_dependency_graph", {})),
        reverse_dependency_graph=_sets(data.get("reverse_dependency_graph", {})),
        import_graph=_sets(data.get("import_graph", {})),
        reverse_import_graph=_sets(data.get("reverse_import_graph", {})),
        symbol_table=data.get("symbol_table", {}),
        total_files=data.get("total_files", 0),
        total_lines=data.get("total_lines", 0),
        total_functions=data.get("total_functions", 0),
        total_classes=data.get("total_classes", 0),
        index_build_time_seconds=data.get("index_build_time_seconds", 0.0),
        index_memory_bytes=data.get("index_memory_bytes", 0),
        last_indexed_git_ref=data.get("last_indexed_git_ref"),
    )


def _save_cache(index: "ProjectIndex") -> None:
    """Persist the project index to a JSON cache file."""
    try:
        root = index.root_path
        path = _cache_path(root)
        payload = {"version": _CACHE_VERSION, "index": _index_to_dict(index)}
        with open(path, "w", encoding="utf-8") as f:
            json.dump(payload, f, separators=(",", ":"))
        print(f"[token-savior] Cache saved → {path}", file=sys.stderr)
    except Exception as exc:
        print(f"[token-savior] Cache save failed: {exc}", file=sys.stderr)


def _load_cache(project_root: str) -> "ProjectIndex | None":
    """Load a cached project index from JSON if it exists and is compatible."""
    path = _cache_path(project_root)
    if not os.path.exists(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            payload = json.load(f)
        if not isinstance(payload, dict) or payload.get("version") != _CACHE_VERSION:
            print("[token-savior] Cache version mismatch, ignoring", file=sys.stderr)
            return None
        return _index_from_dict(payload["index"])
    except Exception as exc:
        print(f"[token-savior] Cache load failed: {exc}", file=sys.stderr)
        return None


# ---------------------------------------------------------------------------
# Per-slot index management
# ---------------------------------------------------------------------------

def _ensure_slot(slot: _ProjectSlot) -> None:
    """Lazily initialize a project slot if not yet indexed."""
    if slot.indexer is not None:
        return

    root = slot.root
    slot.is_git = is_git_repo(root)
    if not slot.stats_file:
        slot.stats_file = _get_stats_file(root)

    cached_index = _load_cache(root)
    if cached_index is not None and slot.is_git and cached_index.last_indexed_git_ref:
        current_head = get_head_commit(root)
        if current_head == cached_index.last_indexed_git_ref:
            print(f"[token-savior] Cache hit (git ref matches) — {root}", file=sys.stderr)
            slot.indexer = ProjectIndexer(root)
            slot.indexer._project_index = cached_index
            slot.query_fns = create_project_query_functions(cached_index)
            return

        changeset = get_changed_files(root, cached_index.last_indexed_git_ref)
        total_changes = len(changeset.modified) + len(changeset.added) + len(changeset.deleted)
        if not changeset.is_empty and total_changes <= 20:
            print(
                f"[token-savior] Cache hit with {total_changes} changed files, "
                f"applying incremental update — {root}",
                file=sys.stderr,
            )
            slot.indexer = ProjectIndexer(root)
            slot.indexer._project_index = cached_index
            slot.query_fns = create_project_query_functions(cached_index)
            return

        print(
            f"[token-savior] Cache stale ({total_changes} changes), full rebuild — {root}",
            file=sys.stderr,
        )

    _build_slot(slot)


def _build_slot(slot: _ProjectSlot) -> None:
    """Full index build for a project slot."""
    root = slot.root
    if not slot.stats_file:
        slot.stats_file = _get_stats_file(root)

    print(f"[token-savior] Indexing project: {root}", file=sys.stderr)

    extra_excludes_raw = os.environ.get("EXCLUDE_EXTRA", "")
    exclude_override_raw = os.environ.get("EXCLUDE_PATTERNS", "")
    include_override_raw = os.environ.get("INCLUDE_PATTERNS", "")

    exclude_patterns = None
    include_patterns = None

    if exclude_override_raw:
        exclude_patterns = [p.strip() for p in exclude_override_raw.split(":") if p.strip()]
    elif extra_excludes_raw:
        tmp = ProjectIndexer(root)
        exclude_patterns = tmp.exclude_patterns + [p.strip() for p in extra_excludes_raw.split(":") if p.strip()]

    if include_override_raw:
        include_patterns = [p.strip() for p in include_override_raw.split(":") if p.strip()]

    slot.indexer = ProjectIndexer(root, include_patterns=include_patterns, exclude_patterns=exclude_patterns)
    index = slot.indexer.index()
    slot.query_fns = create_project_query_functions(index)

    if not slot.is_git:
        slot.is_git = is_git_repo(root)
    if slot.is_git:
        index.last_indexed_git_ref = get_head_commit(root)
        _save_cache(index)

    print(
        f"[token-savior] Indexed {index.total_files} files, "
        f"{index.total_lines} lines, "
        f"{index.total_functions} functions, "
        f"{index.total_classes} classes "
        f"in {index.index_build_time_seconds:.2f}s — {root}",
        file=sys.stderr,
    )


def _matches_include_patterns(rel_path: str, patterns: list[str]) -> bool:
    normalized = rel_path.replace(os.sep, "/")
    for pattern in patterns:
        if fnmatch.fnmatch(normalized, pattern):
            return True
    return False


def _maybe_incremental_update(slot: _ProjectSlot) -> None:
    """Check git for changes and incrementally update the slot index if needed."""
    if not slot.is_git or slot.indexer is None or slot.indexer._project_index is None:
        return

    # Throttle: check at most once every 30s per slot
    now = time.time()
    if now - slot._last_update_check < 30:
        return
    slot._last_update_check = now

    idx = slot.indexer._project_index
    if idx.last_indexed_git_ref is None:
        # No git ref was recorded at index time (e.g. initial index ran before
        # the first commit, or the cache was written by an older version).
        # Stamp HEAD now so future incremental checks have a baseline.
        # If HEAD is also None (empty repo, no commits yet) do nothing —
        # avoids a full-rebuild loop that would fire every 30 seconds.
        head = get_head_commit(slot.root)
        if head is not None:
            idx.last_indexed_git_ref = head
            _save_cache(idx)
        return

    changeset = get_changed_files(slot.root, idx.last_indexed_git_ref)
    if changeset.is_empty:
        return

    total_changes = len(changeset.modified) + len(changeset.added) + len(changeset.deleted)

    if total_changes > 20 and total_changes > idx.total_files * 0.5:
        print(
            f"[token-savior] Large changeset ({total_changes} files), "
            f"doing full rebuild — {slot.root}",
            file=sys.stderr,
        )
        _build_slot(slot)
        return

    for path in changeset.deleted:
        if path in idx.files:
            slot.indexer.remove_file(path)

    for path in changeset.modified + changeset.added:
        if slot.indexer._is_excluded(path):
            continue
        if not _matches_include_patterns(path, slot.indexer.include_patterns):
            continue
        abs_path = os.path.join(slot.root, path)
        if not os.path.isfile(abs_path):
            continue
        slot.indexer.reindex_file(path, skip_graph_rebuild=True)

    slot.indexer.rebuild_graphs()
    idx.last_indexed_git_ref = get_head_commit(slot.root)

    n_mod = len(changeset.modified)
    n_add = len(changeset.added)
    n_del = len(changeset.deleted)
    print(
        f"[token-savior] Incremental update: "
        f"{n_mod} modified, {n_add} added, {n_del} deleted — {slot.root}",
        file=sys.stderr,
    )
    _save_cache(idx)


# ---------------------------------------------------------------------------
# Resolve which slot to use for a given tool call
# ---------------------------------------------------------------------------

def _resolve_slot(project_hint: Optional[str] = None) -> tuple[Optional[_ProjectSlot], str]:
    """
    Return (slot, error_message). error_message is empty on success.

    Resolution order:
    1. explicit project_hint (basename or full path)
    2. _active_root
    3. only registered project (if exactly one)
    4. error
    """
    global _active_root

    if project_hint:
        # Try exact match first
        hint_abs = os.path.abspath(project_hint)
        if hint_abs in _projects:
            return _projects[hint_abs], ""
        # Try basename match
        for root, slot in _projects.items():
            if os.path.basename(root) == project_hint:
                return slot, ""
        return None, (
            f"Project '{project_hint}' not found. "
            f"Known projects: {', '.join(os.path.basename(r) for r in _projects)}"
        )

    if _active_root and _active_root in _projects:
        return _projects[_active_root], ""

    if len(_projects) == 1:
        root = next(iter(_projects))
        _active_root = root
        return _projects[root], ""

    if not _projects:
        return None, "No projects registered. Call set_project_root('/path') first."

    return None, (
        "Multiple projects loaded but no active project set. "
        f"Call switch_project(name) with one of: {', '.join(os.path.basename(r) for r in _projects)}"
    )


# ---------------------------------------------------------------------------
# Tool definitions
# ---------------------------------------------------------------------------

# Shared project parameter injected into multi-project tools
_PROJECT_PARAM = {
    "project": {
        "type": "string",
        "description": (
            "Optional project name or path to target a specific project. "
            "Omit to use the active project."
        ),
    }
}

TOOLS = [
    Tool(
        name="list_projects",
        description="List all registered workspace projects with their index status.",
        inputSchema={"type": "object", "properties": {}},
    ),
    Tool(
        name="switch_project",
        description="Switch the active project. Subsequent tool calls without explicit project target this project.",
        inputSchema={
            "type": "object",
            "properties": {
                "name": {
                    "type": "string",
                    "description": "Project name (basename of path) or full path.",
                },
            },
            "required": ["name"],
        },
    ),
    Tool(
        name="get_git_status",
        description="Return a structured git status summary for the active project: branch, ahead/behind, staged, unstaged, and untracked files.",
        inputSchema={"type": "object", "properties": {**_PROJECT_PARAM}},
    ),
    Tool(
        name="get_changed_symbols",
        description="Return a compact symbol-oriented summary of current worktree changes, avoiding large textual diffs.",
        inputSchema={
            "type": "object",
            "properties": {
                "max_files": {
                    "type": "integer",
                    "description": "Maximum changed files to report (default 20).",
                },
                "max_symbols_per_file": {
                    "type": "integer",
                    "description": "Maximum symbols to report per file (default 20).",
                },
                **_PROJECT_PARAM,
            },
        },
    ),
    Tool(
        name="get_changed_symbols_since_ref",
        description="Return a compact symbol-oriented summary of git changes since a given ref, avoiding large textual diffs.",
        inputSchema={
            "type": "object",
            "properties": {
                "since_ref": {
                    "type": "string",
                    "description": "Git ref to compare against HEAD and current worktree.",
                },
                "max_files": {
                    "type": "integer",
                    "description": "Maximum changed files to report (default 20).",
                },
                "max_symbols_per_file": {
                    "type": "integer",
                    "description": "Maximum symbols to report per file (default 20).",
                },
                **_PROJECT_PARAM,
            },
            "required": ["since_ref"],
        },
    ),
    Tool(
        name="summarize_patch_by_symbol",
        description="Summarize a set of changed files as symbol-level entries for compact review instead of textual diffs.",
        inputSchema={
            "type": "object",
            "properties": {
                "changed_files": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Changed files to summarize. Omit to summarize indexed files currently passed in by caller logic.",
                },
                "max_files": {
                    "type": "integer",
                    "description": "Maximum files to report (default 20).",
                },
                "max_symbols_per_file": {
                    "type": "integer",
                    "description": "Maximum symbols to report per file (default 20).",
                },
                **_PROJECT_PARAM,
            },
        },
    ),
    Tool(
        name="build_commit_summary",
        description="Build a compact commit/review summary from changed files using symbol-level structure instead of textual diffs.",
        inputSchema={
            "type": "object",
            "properties": {
                "changed_files": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Changed files to summarize.",
                },
                "max_files": {
                    "type": "integer",
                    "description": "Maximum files to report (default 20).",
                },
                "max_symbols_per_file": {
                    "type": "integer",
                    "description": "Maximum symbols to report per file (default 20).",
                },
                **_PROJECT_PARAM,
            },
            "required": ["changed_files"],
        },
    ),
    Tool(
        name="create_checkpoint",
        description="Create a compact checkpoint for a bounded set of files before a workflow mutation.",
        inputSchema={
            "type": "object",
            "properties": {
                "file_paths": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Project files to save into the checkpoint.",
                },
                **_PROJECT_PARAM,
            },
            "required": ["file_paths"],
        },
    ),
    Tool(
        name="list_checkpoints",
        description="List available checkpoints for the active project.",
        inputSchema={"type": "object", "properties": {**_PROJECT_PARAM}},
    ),
    Tool(
        name="delete_checkpoint",
        description="Delete a specific checkpoint.",
        inputSchema={
            "type": "object",
            "properties": {
                "checkpoint_id": {
                    "type": "string",
                    "description": "Checkpoint identifier to delete.",
                },
                **_PROJECT_PARAM,
            },
            "required": ["checkpoint_id"],
        },
    ),
    Tool(
        name="prune_checkpoints",
        description="Keep only the newest N checkpoints and delete older ones.",
        inputSchema={
            "type": "object",
            "properties": {
                "keep_last": {
                    "type": "integer",
                    "description": "How many recent checkpoints to keep (default 10).",
                },
                **_PROJECT_PARAM,
            },
        },
    ),
    Tool(
        name="restore_checkpoint",
        description="Restore files from a previously created checkpoint.",
        inputSchema={
            "type": "object",
            "properties": {
                "checkpoint_id": {
                    "type": "string",
                    "description": "Checkpoint identifier returned by create_checkpoint.",
                },
                **_PROJECT_PARAM,
            },
            "required": ["checkpoint_id"],
        },
    ),
    Tool(
        name="compare_checkpoint_by_symbol",
        description="Compare a checkpoint against current files at symbol level, returning added/removed/changed symbols without a textual diff.",
        inputSchema={
            "type": "object",
            "properties": {
                "checkpoint_id": {
                    "type": "string",
                    "description": "Checkpoint identifier returned by create_checkpoint.",
                },
                "max_files": {
                    "type": "integer",
                    "description": "Maximum files to compare (default 20).",
                },
                **_PROJECT_PARAM,
            },
            "required": ["checkpoint_id"],
        },
    ),
    Tool(
        name="replace_symbol_source",
        description="Replace an indexed symbol's full source block directly, without sending a file-wide patch.",
        inputSchema={
            "type": "object",
            "properties": {
                "symbol_name": {
                    "type": "string",
                    "description": "Function, method, class, or section name to replace.",
                },
                "new_source": {
                    "type": "string",
                    "description": "Replacement source for the symbol.",
                },
                "file_path": {
                    "type": "string",
                    "description": "Optional file path to disambiguate symbols.",
                },
                **_PROJECT_PARAM,
            },
            "required": ["symbol_name", "new_source"],
        },
    ),
    Tool(
        name="insert_near_symbol",
        description="Insert content immediately before or after an indexed symbol, avoiding a file-wide edit payload.",
        inputSchema={
            "type": "object",
            "properties": {
                "symbol_name": {
                    "type": "string",
                    "description": "Function, method, class, or section name near which to insert.",
                },
                "content": {
                    "type": "string",
                    "description": "Content to insert.",
                },
                "position": {
                    "type": "string",
                    "description": "Insertion position: 'before' or 'after' (default 'after').",
                },
                "file_path": {
                    "type": "string",
                    "description": "Optional file path to disambiguate symbols.",
                },
                **_PROJECT_PARAM,
            },
            "required": ["symbol_name", "content"],
        },
    ),
    Tool(
        name="find_impacted_test_files",
        description="Infer a compact set of likely impacted pytest files from changed files or symbols.",
        inputSchema={
            "type": "object",
            "properties": {
                "changed_files": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Changed project files to map to likely impacted tests.",
                },
                "symbol_names": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Changed symbols to map to likely impacted tests.",
                },
                "max_tests": {
                    "type": "integer",
                    "description": "Maximum impacted test files to return (default 20).",
                },
                **_PROJECT_PARAM,
            },
        },
    ),
    Tool(
        name="run_impacted_tests",
        description="Run only the inferred impacted pytest files and return a compact summary instead of full logs.",
        inputSchema={
            "type": "object",
            "properties": {
                "changed_files": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Changed project files to map to likely impacted tests.",
                },
                "symbol_names": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Changed symbols to map to likely impacted tests.",
                },
                "max_tests": {
                    "type": "integer",
                    "description": "Maximum impacted test files to run (default 20).",
                },
                "timeout_sec": {
                    "type": "integer",
                    "description": "Maximum runtime in seconds (default 120).",
                },
                "max_output_chars": {
                    "type": "integer",
                    "description": "Maximum stdout/stderr characters to keep when included (default 12000).",
                },
                "include_output": {
                    "type": "boolean",
                    "description": "Include bounded raw stdout/stderr in the response. Default false for token efficiency.",
                },
                "compact": {
                    "type": "boolean",
                    "description": "Return only the minimum useful fields for agent loops.",
                },
                **_PROJECT_PARAM,
            },
        },
    ),
    Tool(
        name="apply_symbol_change_and_validate",
        description="Replace a symbol, reindex the file, and run only the inferred impacted tests as one compact workflow.",
        inputSchema={
            "type": "object",
            "properties": {
                "symbol_name": {
                    "type": "string",
                    "description": "Function, method, class, or section name to replace.",
                },
                "new_source": {
                    "type": "string",
                    "description": "Replacement source for the symbol.",
                },
                "file_path": {
                    "type": "string",
                    "description": "Optional file path to disambiguate symbols.",
                },
                "max_tests": {
                    "type": "integer",
                    "description": "Maximum impacted test files to run (default 20).",
                },
                "timeout_sec": {
                    "type": "integer",
                    "description": "Maximum runtime in seconds (default 120).",
                },
                "max_output_chars": {
                    "type": "integer",
                    "description": "Maximum stdout/stderr characters to keep when included (default 12000).",
                },
                "include_output": {
                    "type": "boolean",
                    "description": "Include bounded raw stdout/stderr in the response. Default false for token efficiency.",
                },
                "compact": {
                    "type": "boolean",
                    "description": "Return only the minimum useful fields for agent loops.",
                },
                **_PROJECT_PARAM,
            },
            "required": ["symbol_name", "new_source"],
        },
    ),
    Tool(
        name="apply_symbol_change_validate_with_rollback",
        description="Replace a symbol, validate impacted tests, and restore the previous file automatically if validation fails.",
        inputSchema={
            "type": "object",
            "properties": {
                "symbol_name": {
                    "type": "string",
                    "description": "Function, method, class, or section name to replace.",
                },
                "new_source": {
                    "type": "string",
                    "description": "Replacement source for the symbol.",
                },
                "file_path": {
                    "type": "string",
                    "description": "Optional file path to disambiguate symbols.",
                },
                "max_tests": {
                    "type": "integer",
                    "description": "Maximum impacted test files to run (default 20).",
                },
                "timeout_sec": {
                    "type": "integer",
                    "description": "Maximum runtime in seconds (default 120).",
                },
                "max_output_chars": {
                    "type": "integer",
                    "description": "Maximum stdout/stderr characters to keep when included (default 12000).",
                },
                "include_output": {
                    "type": "boolean",
                    "description": "Include bounded raw stdout/stderr in the response. Default false for token efficiency.",
                },
                "compact": {
                    "type": "boolean",
                    "description": "Return only the minimum useful fields for agent loops.",
                },
                **_PROJECT_PARAM,
            },
            "required": ["symbol_name", "new_source"],
        },
    ),
    Tool(
        name="discover_project_actions",
        description="Detect conventional project actions from build files (tests, lint, build, run) without executing them.",
        inputSchema={"type": "object", "properties": {**_PROJECT_PARAM}},
    ),
    Tool(
        name="run_project_action",
        description="Run a previously discovered project action by id with bounded output and timeout.",
        inputSchema={
            "type": "object",
            "properties": {
                "action_id": {
                    "type": "string",
                    "description": "Action id returned by discover_project_actions (e.g. 'python:test', 'npm:test').",
                },
                "timeout_sec": {
                    "type": "integer",
                    "description": "Maximum runtime in seconds (default 120).",
                },
                "max_output_chars": {
                    "type": "integer",
                    "description": "Maximum stdout/stderr characters to keep (default 12000).",
                },
                "include_output": {
                    "type": "boolean",
                    "description": "Include bounded raw stdout/stderr in the response. Default false for token efficiency.",
                },
                **_PROJECT_PARAM,
            },
            "required": ["action_id"],
        },
    ),
    Tool(
        name="get_project_summary",
        description="High-level overview of the project: file count, packages, top classes/functions.",
        inputSchema={"type": "object", "properties": {**_PROJECT_PARAM}},
    ),
    Tool(
        name="list_files",
        description="List indexed files. Optional glob pattern to filter (e.g. '*.py', 'src/**/*.ts').",
        inputSchema={
            "type": "object",
            "properties": {
                "pattern": {
                    "type": "string",
                    "description": "Glob pattern to filter files (uses fnmatch).",
                },
                "max_results": {
                    "type": "integer",
                    "description": "Maximum number of results to return (0 = unlimited, default 0).",
                },
                **_PROJECT_PARAM,
            },
        },
    ),
    Tool(
        name="get_structure_summary",
        description="Structure summary for a file (functions, classes, imports, line counts) or the whole project if no file specified.",
        inputSchema={
            "type": "object",
            "properties": {
                "file_path": {
                    "type": "string",
                    "description": "Relative path to a file in the project. Omit for project-level summary.",
                },
                **_PROJECT_PARAM,
            },
        },
    ),
    Tool(
        name="get_function_source",
        description="Get the full source code of a function or method by name. Uses the symbol table to locate the file automatically.",
        inputSchema={
            "type": "object",
            "properties": {
                "name": {
                    "type": "string",
                    "description": "Function or method name (e.g. 'my_func' or 'MyClass.my_method').",
                },
                "file_path": {
                    "type": "string",
                    "description": "Optional file path to narrow the search.",
                },
                "max_lines": {
                    "type": "integer",
                    "description": "Maximum number of source lines to return (0 = unlimited, default 0).",
                },
                **_PROJECT_PARAM,
            },
            "required": ["name"],
        },
    ),
    Tool(
        name="get_class_source",
        description="Get the full source code of a class by name. Uses the symbol table to locate the file automatically.",
        inputSchema={
            "type": "object",
            "properties": {
                "name": {
                    "type": "string",
                    "description": "Class name.",
                },
                "file_path": {
                    "type": "string",
                    "description": "Optional file path to narrow the search.",
                },
                "max_lines": {
                    "type": "integer",
                    "description": "Maximum number of source lines to return (0 = unlimited, default 0).",
                },
                **_PROJECT_PARAM,
            },
            "required": ["name"],
        },
    ),
    Tool(
        name="get_functions",
        description="List all functions (with name, lines, params, file). Filter to a specific file or get all project functions.",
        inputSchema={
            "type": "object",
            "properties": {
                "file_path": {
                    "type": "string",
                    "description": "Relative path to filter to a single file. Omit for all project functions.",
                },
                "max_results": {
                    "type": "integer",
                    "description": "Maximum number of results to return (0 = unlimited, default 0).",
                },
                **_PROJECT_PARAM,
            },
        },
    ),
    Tool(
        name="get_classes",
        description="List all classes (with name, lines, methods, bases, file). Filter to a specific file or get all project classes.",
        inputSchema={
            "type": "object",
            "properties": {
                "file_path": {
                    "type": "string",
                    "description": "Relative path to filter to a single file. Omit for all project classes.",
                },
                "max_results": {
                    "type": "integer",
                    "description": "Maximum number of results to return (0 = unlimited, default 0).",
                },
                **_PROJECT_PARAM,
            },
        },
    ),
    Tool(
        name="get_imports",
        description="List all imports (with module, names, line). Filter to a specific file or get all project imports.",
        inputSchema={
            "type": "object",
            "properties": {
                "file_path": {
                    "type": "string",
                    "description": "Relative path to filter to a single file. Omit for all project imports.",
                },
                "max_results": {
                    "type": "integer",
                    "description": "Maximum number of results to return (0 = unlimited, default 0).",
                },
                **_PROJECT_PARAM,
            },
        },
    ),
    Tool(
        name="find_symbol",
        description="Find where a symbol (function, method, class) is defined. Returns file path, line range, type, signature, and a source preview (~20 lines).",
        inputSchema={
            "type": "object",
            "properties": {
                "name": {
                    "type": "string",
                    "description": "Symbol name to find (e.g. 'ProjectIndexer', 'annotate', 'MyClass.run').",
                },
                **_PROJECT_PARAM,
            },
            "required": ["name"],
        },
    ),
    Tool(
        name="get_dependencies",
        description="What does this symbol call/use? Returns list of symbols referenced by the named function or class.",
        inputSchema={
            "type": "object",
            "properties": {
                "name": {
                    "type": "string",
                    "description": "Symbol name to query.",
                },
                "max_results": {
                    "type": "integer",
                    "description": "Maximum number of results to return (0 = unlimited, default 0).",
                },
                **_PROJECT_PARAM,
            },
            "required": ["name"],
        },
    ),
    Tool(
        name="get_dependents",
        description="What calls/uses this symbol? Returns list of symbols that reference the named function or class.",
        inputSchema={
            "type": "object",
            "properties": {
                "name": {
                    "type": "string",
                    "description": "Symbol name to query.",
                },
                "max_results": {
                    "type": "integer",
                    "description": "Maximum number of results to return (0 = unlimited, default 0).",
                },
                **_PROJECT_PARAM,
            },
            "required": ["name"],
        },
    ),
    Tool(
        name="get_change_impact",
        description="Analyze the impact of changing a symbol. Returns direct and transitive dependents, each scored with a confidence value (1.0 = direct caller, 0.6 = 2 hops, etc.) and depth.",
        inputSchema={
            "type": "object",
            "properties": {
                "name": {
                    "type": "string",
                    "description": "Symbol name to analyze.",
                },
                "max_direct": {
                    "type": "integer",
                    "description": "Maximum number of direct dependents to return (0 = unlimited, default 0).",
                },
                "max_transitive": {
                    "type": "integer",
                    "description": "Maximum number of transitive dependents to return (0 = unlimited, default 0).",
                },
                **_PROJECT_PARAM,
            },
            "required": ["name"],
        },
    ),
    Tool(
        name="get_call_chain",
        description="Find the shortest dependency path between two symbols (BFS through the dependency graph).",
        inputSchema={
            "type": "object",
            "properties": {
                "from_name": {
                    "type": "string",
                    "description": "Starting symbol name.",
                },
                "to_name": {
                    "type": "string",
                    "description": "Target symbol name.",
                },
                **_PROJECT_PARAM,
            },
            "required": ["from_name", "to_name"],
        },
    ),
    Tool(
        name="get_edit_context",
        description=(
            "All-in-one context for editing a symbol. Returns the symbol source, "
            "its direct dependencies (what it calls), and its callers (who uses it) "
            "in a single response. Saves 3 separate tool calls."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "name": {
                    "type": "string",
                    "description": "Symbol name to get full edit context for.",
                },
                "max_deps": {
                    "type": "integer",
                    "description": "Max dependencies to return (default 10).",
                },
                "max_callers": {
                    "type": "integer",
                    "description": "Max callers to return (default 10).",
                },
                **_PROJECT_PARAM,
            },
            "required": ["name"],
        },
    ),
    Tool(
        name="get_file_dependencies",
        description="List files that this file imports from (file-level import graph).",
        inputSchema={
            "type": "object",
            "properties": {
                "file_path": {
                    "type": "string",
                    "description": "Relative path to the file.",
                },
                "max_results": {
                    "type": "integer",
                    "description": "Maximum number of results to return (0 = unlimited, default 0).",
                },
                **_PROJECT_PARAM,
            },
            "required": ["file_path"],
        },
    ),
    Tool(
        name="get_file_dependents",
        description="List files that import from this file (reverse import graph).",
        inputSchema={
            "type": "object",
            "properties": {
                "file_path": {
                    "type": "string",
                    "description": "Relative path to the file.",
                },
                "max_results": {
                    "type": "integer",
                    "description": "Maximum number of results to return (0 = unlimited, default 0).",
                },
                **_PROJECT_PARAM,
            },
            "required": ["file_path"],
        },
    ),
    Tool(
        name="search_codebase",
        description="Regex search across all indexed files. Returns up to 100 matches with file, line number, and content.",
        inputSchema={
            "type": "object",
            "properties": {
                "pattern": {
                    "type": "string",
                    "description": "Regular expression pattern to search for.",
                },
                "max_results": {
                    "type": "integer",
                    "description": "Maximum number of results to return (default 100, 0 = unlimited).",
                },
                **_PROJECT_PARAM,
            },
            "required": ["pattern"],
        },
    ),
    Tool(
        name="reindex",
        description="Re-index the entire project. Use after making significant file changes to refresh the structural index.",
        inputSchema={"type": "object", "properties": {**_PROJECT_PARAM}},
    ),
    Tool(
        name="set_project_root",
        description=(
            "Add a new project root to the workspace and switch to it. "
            "Triggers a full reindex of the new root. "
            "After calling this, all other tools operate on the new project by default."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Absolute path to the project root directory.",
                },
            },
            "required": ["path"],
        },
    ),
    Tool(
        name="get_feature_files",
        description=(
            "Find all files related to a feature keyword, then trace imports to build the "
            "complete feature map. Example: get_feature_files('contrat') returns all routes, "
            "components, lib, types connected to contracts. Each file is classified by role."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "keyword": {"type": "string", "description": "Feature keyword (e.g. 'contrat', 'paiement', 'auth')."},
                "max_results": {"type": "integer", "description": "Max files to return (0 = all, default 0)."},
                **_PROJECT_PARAM,
            },
            "required": ["keyword"],
        },
    ),
    Tool(
        name="get_usage_stats",
        description="Session efficiency stats: tool calls, characters returned vs total source, estimated token savings.",
        inputSchema={"type": "object", "properties": {}},
    ),
    # v3: Route Map, Env Usage, Components
    Tool(
        name="get_routes",
        description="Detect all API routes and pages in a Next.js App Router project. Returns route path, file, HTTP methods, and type (api/page/layout).",
        inputSchema={
            "type": "object",
            "properties": {
                "max_results": {"type": "integer", "description": "Max routes to return (0 = all, default 0)."},
                **_PROJECT_PARAM,
            },
        },
    ),
    Tool(
        name="get_env_usage",
        description="Cross-reference an environment variable across all code, .env files, and workflow configs. Shows where it's defined, read, and written.",
        inputSchema={
            "type": "object",
            "properties": {
                "var_name": {"type": "string", "description": "Environment variable name (e.g. HELLOASSO_CLIENT_ID)."},
                "max_results": {"type": "integer", "description": "Max results (0 = all, default 0)."},
                **_PROJECT_PARAM,
            },
            "required": ["var_name"],
        },
    ),
    Tool(
        name="get_components",
        description="Detect React components in .tsx/.jsx files. Identifies pages, layouts, and named components by convention (uppercase name or default export).",
        inputSchema={
            "type": "object",
            "properties": {
                "file_path": {"type": "string", "description": "Optional file to scan (default: all .tsx/.jsx)."},
                "max_results": {"type": "integer", "description": "Max results (0 = all, default 0)."},
                **_PROJECT_PARAM,
            },
        },
    ),
    Tool(
        name="analyze_config",
        description=(
            "Analyze config files for issues and insights: duplicate keys, hardcoded secrets, orphan entries, "
            "config file loaders (which code loads which config), and schema (what keys code expects with defaults). "
            "Checks can be filtered via the 'checks' parameter."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "checks": {
                    "type": "array",
                    "items": {"type": "string", "enum": ["duplicates", "secrets", "orphans", "loaders", "schema"]},
                    "description": 'Checks to run (default: duplicates,secrets,orphans). Options: "duplicates", "secrets", "orphans", "loaders", "schema".',
                },
                "file_path": {
                    "type": "string",
                    "description": "Specific config file to analyze. Omit to analyze all config files.",
                },
                "severity": {
                    "type": "string",
                    "enum": ["all", "error", "warning"],
                    "description": 'Filter by severity (default: "all").',
                },
                **_PROJECT_PARAM,
            },
        },
    ),
    Tool(
        name="find_dead_code",
        description=(
            "Find unreferenced functions and classes in the codebase. "
            "Detects symbols with zero callers, excluding entry points (main, tests, route handlers, etc.)."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "max_results": {
                    "type": "integer",
                    "description": "Maximum number of dead symbols to report (default: 50).",
                },
                **_PROJECT_PARAM,
            },
        },
    ),
    Tool(
        name="find_hotspots",
        description=(
            "Rank functions by complexity score (line count, branching, nesting depth, parameter count). "
            "Helps identify code that needs refactoring."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "max_results": {
                    "type": "integer",
                    "description": "Maximum number of functions to report (default: 20).",
                },
                "min_score": {
                    "type": "number",
                    "description": "Minimum complexity score to include (default: 0).",
                },
                **_PROJECT_PARAM,
            },
        },
    ),
    Tool(
        name="detect_breaking_changes",
        description=(
            "Detect breaking API changes between the current code and a git ref. "
            "Finds removed functions, removed parameters, added required parameters, and signature changes."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "since_ref": {
                    "type": "string",
                    "description": 'Git ref to compare against (default: "HEAD~1"). Can be a commit SHA, branch, or tag.',
                },
                **_PROJECT_PARAM,
            },
        },
    ),
    Tool(
        name="find_cross_project_deps",
        description=(
            "Detect dependencies between indexed projects. "
            "Shows which projects import packages from other indexed projects and shared external dependencies."
        ),
        inputSchema={
            "type": "object",
            "properties": {},
        },
    ),
    Tool(
        name="analyze_docker",
        description=(
            "Analyze Dockerfiles in the project: base images, stages, exposed ports, ENV/ARG vars, "
            "and cross-reference with config files. Flags issues like 'latest' tags and missing env vars."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                **_PROJECT_PARAM,
            },
        },
    ),
    Tool(
        name="get_entry_points",
        description="Score functions by likelihood of being execution entry points (routes, handlers, main functions, exported APIs). Returns functions with score and reasons, sorted by likelihood desc.",
        inputSchema={
            "type": "object",
            "properties": {
                "max_results": {
                    "type": "integer",
                    "description": "Maximum number of entry points to return (default 20).",
                },
                **_PROJECT_PARAM,
            },
        },
    ),
    Tool(
        name="get_symbol_cluster",
        description=(
            "Get the functional cluster for a symbol — all closely related symbols "
            "grouped by community detection on the dependency graph. Useful for "
            "understanding which symbols belong to the same functional area without "
            "chaining multiple dependency queries."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "name": {
                    "type": "string",
                    "description": "Symbol name to find the cluster for.",
                },
                "max_members": {
                    "type": "integer",
                    "description": "Maximum cluster members to return (default 30).",
                },
                **_PROJECT_PARAM,
            },
            "required": ["name"],
        },
    ),
]


# ---------------------------------------------------------------------------
# MCP handlers
# ---------------------------------------------------------------------------


@server.list_tools()
async def list_tools() -> list[Tool]:
    return TOOLS


# ---------------------------------------------------------------------------
# Tool handler functions — each returns a raw result (not wrapped)
# ---------------------------------------------------------------------------


def _prep(slot: _ProjectSlot) -> None:
    """Ensure slot is indexed and incrementally updated."""
    _ensure_slot(slot)
    _maybe_incremental_update(slot)


# ── Index-level handlers (slot + ensure + update → result) ────────────────

def _h_get_git_status(slot, args):
    return get_git_status(slot.root)

def _h_get_changed_symbols(slot, args):
    _prep(slot)
    return get_changed_symbols(
        slot.indexer._project_index,
        max_files=args.get("max_files", 20),
        max_symbols_per_file=args.get("max_symbols_per_file", 20),
    )

def _h_get_changed_symbols_since_ref(slot, args):
    _prep(slot)
    return get_changed_symbols_since_ref(
        slot.indexer._project_index, args["since_ref"],
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
        slot.indexer._project_index, args["checkpoint_id"],
        max_files=args.get("max_files", 20),
    )

def _h_replace_symbol_source(slot, args):
    _prep(slot)
    result = replace_symbol_source(
        slot.indexer._project_index, args["symbol_name"], args["new_source"],
        file_path=args.get("file_path"),
    )
    if result.get("ok"):
        slot.indexer.reindex_file(result["file"])
    return result

def _h_insert_near_symbol(slot, args):
    _prep(slot)
    result = insert_near_symbol(
        slot.indexer._project_index, args["symbol_name"], args["content"],
        position=args.get("position", "after"), file_path=args.get("file_path"),
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
        slot.indexer, args["symbol_name"], args["new_source"],
        file_path=args.get("file_path"),
        max_tests=args.get("max_tests", 20),
        timeout_sec=args.get("timeout_sec", 120),
        max_output_chars=args.get("max_output_chars", 12000),
        include_output=args.get("include_output", False),
        compact=args.get("compact", False),
    )

def _h_apply_symbol_change_validate_with_rollback(slot, args):
    _prep(slot)
    return apply_symbol_change_validate_with_rollback(
        slot.indexer, args["symbol_name"], args["new_source"],
        file_path=args.get("file_path"),
        max_tests=args.get("max_tests", 20),
        timeout_sec=args.get("timeout_sec", 120),
        max_output_chars=args.get("max_output_chars", 12000),
        include_output=args.get("include_output", False),
        compact=args.get("compact", False),
    )

def _h_discover_project_actions(slot, args):
    return discover_project_actions(slot.root)

def _h_run_project_action(slot, args):
    return run_project_action(
        slot.root, args["action_id"],
        timeout_sec=args.get("timeout_sec", 120),
        max_output_chars=args.get("max_output_chars", 12000),
        include_output=args.get("include_output", False),
    )

def _h_analyze_config(slot, args):
    _prep(slot)
    return run_config_analysis(
        slot.indexer._project_index,
        checks=args.get("checks"), file_path=args.get("file_path"),
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
        slot.indexer._project_index, since_ref=args.get("since_ref", "HEAD~1"),
    )

def _h_find_cross_project_deps(slot, args):
    loaded: dict[str, ProjectIndex] = {}
    for root, s in _projects.items():
        _ensure_slot(s)
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
    "list_files": lambda q, a: q["list_files"](a.get("pattern"), max_results=a.get("max_results", 0)),
    "get_structure_summary": lambda q, a: q["get_structure_summary"](a.get("file_path")),
    "get_function_source": lambda q, a: q["get_function_source"](a["name"], a.get("file_path"), max_lines=a.get("max_lines", 0)),
    "get_class_source": lambda q, a: q["get_class_source"](a["name"], a.get("file_path"), max_lines=a.get("max_lines", 0)),
    "get_functions": lambda q, a: q["get_functions"](a.get("file_path"), max_results=a.get("max_results", 0)),
    "get_classes": lambda q, a: q["get_classes"](a.get("file_path"), max_results=a.get("max_results", 0)),
    "get_imports": lambda q, a: q["get_imports"](a.get("file_path"), max_results=a.get("max_results", 0)),
    "find_symbol": lambda q, a: q["find_symbol"](a["name"]),
    "get_dependencies": lambda q, a: q["get_dependencies"](a["name"], max_results=a.get("max_results", 0)),
    "get_dependents": lambda q, a: q["get_dependents"](a["name"], max_results=a.get("max_results", 0)),
    "get_change_impact": lambda q, a: q["get_change_impact"](a["name"], max_direct=a.get("max_direct", 0), max_transitive=a.get("max_transitive", 0)),
    "get_call_chain": lambda q, a: q["get_call_chain"](a["from_name"], a["to_name"]),
    "get_edit_context": _q_get_edit_context,
    "get_file_dependencies": lambda q, a: q["get_file_dependencies"](a["file_path"], max_results=a.get("max_results", 0)),
    "get_file_dependents": lambda q, a: q["get_file_dependents"](a["file_path"], max_results=a.get("max_results", 0)),
    "search_codebase": lambda q, a: q["search_codebase"](a["pattern"], max_results=a.get("max_results", 100)),
    "get_routes": lambda q, a: q["get_routes"](max_results=a.get("max_results", 0)),
    "get_env_usage": lambda q, a: q["get_env_usage"](a["var_name"], max_results=a.get("max_results", 0)),
    "get_components": lambda q, a: q["get_components"](file_path=a.get("file_path"), max_results=a.get("max_results", 0)),
    "get_feature_files": lambda q, a: q["get_feature_files"](a["keyword"], max_results=a.get("max_results", 0)),
    "get_entry_points": lambda q, a: q["get_entry_points"](max_results=a.get("max_results", 20)),
    "get_symbol_cluster": lambda q, a: q["get_symbol_cluster"](a["name"], max_members=a.get("max_members", 30)),
}


@server.call_tool()
async def call_tool(name: str, arguments: dict) -> list[types.TextContent]:
    global _total_chars_returned, _total_naive_chars, _active_root

    _tool_call_counts[name] = _tool_call_counts.get(name, 0) + 1

    try:
        # ── Meta tools (no slot needed) ───────────────────────────────────

        if name == "get_usage_stats":
            return [TextContent(type="text", text=_format_usage_stats(include_cumulative=True))]

        if name == "list_projects":
            if not _projects:
                return [TextContent(type="text", text="No projects registered. Call set_project_root('/path') first.")]
            lines = [f"Workspace projects ({len(_projects)}):"]
            for root, slot in _projects.items():
                status = "indexed" if slot.indexer is not None else "not yet loaded"
                active = " [active]" if root == _active_root else ""
                name_part = os.path.basename(root)
                if slot.indexer and slot.indexer._project_index:
                    idx = slot.indexer._project_index
                    lines.append(f"  {name_part}{active} -- {idx.total_files} files, {idx.total_functions} functions ({root})")
                else:
                    lines.append(f"  {name_part}{active} -- {status} ({root})")
            return [TextContent(type="text", text="\n".join(lines))]

        if name == "switch_project":
            hint = arguments["name"]
            slot, err = _resolve_slot(hint)
            if err:
                return [TextContent(type="text", text=f"Error: {err}")]
            _active_root = slot.root
            _ensure_slot(slot)
            idx = slot.indexer._project_index if slot.indexer else None
            info = f"{idx.total_files} files" if idx else "index not built"
            return [TextContent(type="text", text=f"Switched to '{os.path.basename(slot.root)}' ({slot.root}) -- {info}.")]

        if name == "set_project_root":
            new_root = os.path.abspath(arguments["path"])
            if not os.path.isdir(new_root):
                return [TextContent(type="text", text=f"Error: '{new_root}' is not a directory.")]
            if new_root not in _projects:
                _projects[new_root] = _ProjectSlot(root=new_root)
            _active_root = new_root
            slot = _projects[new_root]
            slot.indexer = None
            slot.query_fns = None
            _build_slot(slot)
            return [TextContent(type="text", text=f"Added and indexed '{new_root}' successfully.")]

        if name == "reindex":
            project_hint = arguments.get("project")
            slot, err = _resolve_slot(project_hint)
            if err:
                return [TextContent(type="text", text=f"Error: {err}")]
            slot.indexer = None
            slot.query_fns = None
            _build_slot(slot)
            return [TextContent(type="text", text=f"Project '{os.path.basename(slot.root)}' re-indexed successfully.")]

        # ── All other tools need a resolved slot ──────────────────────────

        project_hint = arguments.get("project")
        slot, err = _resolve_slot(project_hint)
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
                return [TextContent(type="text", text=f"Error: index not built for '{slot.root}'. Call reindex first.")]
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
