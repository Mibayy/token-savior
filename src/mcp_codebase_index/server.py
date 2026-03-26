# mcp-codebase-index - Structural codebase indexer with MCP server
# Copyright (C) 2026 Michael Doyle
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the
# GNU Affero General Public License for more details.
#
# You should have received a copy of the GNU Affero General Public License
# along with this program. If not, see <https://www.gnu.org/licenses/>.
#
# Commercial licensing available. See COMMERCIAL-LICENSE.md for details.

"""MCP server for the structural codebase indexer.

Exposes project-wide structural query functions as MCP tools,
enabling Claude Code to navigate codebases efficiently without
reading entire files into context.

Single-project usage (original):
    PROJECT_ROOT=/path/to/project mcp-codebase-index

Multi-project workspace usage:
    WORKSPACE_ROOTS=/root/improvence,/root/sirius,/root/sirius-5min mcp-codebase-index

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
from typing import Optional

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import Tool, TextContent
import mcp.types as types

from mcp_codebase_index.git_tracker import is_git_repo, get_head_commit, get_changed_files
from mcp_codebase_index.models import ProjectIndex
from mcp_codebase_index.project_indexer import ProjectIndexer
from mcp_codebase_index.query_api import create_project_query_functions

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

server = Server("mcp-codebase-index")

# Dict of abs_path -> slot. Populated from WORKSPACE_ROOTS or PROJECT_ROOT.
_projects: dict[str, _ProjectSlot] = {}

# Currently active project root (used by tools that don't specify a project).
_active_root: str = ""

# Persistent cache
_CACHE_FILENAME = ".codebase-index-cache.json"
_CACHE_VERSION = 2  # Bumped: switched from pickle to JSON

# Session usage stats (aggregated across all projects in this session)
_session_start: float = time.time()
_tool_call_counts: dict[str, int] = {}
_total_chars_returned: int = 0

# Persistent stats
_STATS_DIR = os.path.expanduser("~/.local/share/mcp-codebase-index")


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
        return {"total_calls": 0, "total_chars_returned": 0, "total_naive_chars": 0, "sessions": 0, "tool_counts": {}}
    try:
        with open(stats_file) as f:
            return json.load(f)
    except Exception:
        return {"total_calls": 0, "total_chars_returned": 0, "total_naive_chars": 0, "sessions": 0, "tool_counts": {}}


def _flush_stats(slot: _ProjectSlot, naive_chars: int) -> None:
    """Append current session stats to the persistent JSON file for this slot."""
    if not slot.stats_file:
        return
    try:
        os.makedirs(_STATS_DIR, exist_ok=True)
        cum = _load_cumulative_stats(slot.stats_file)
        session_calls = sum(_tool_call_counts.values()) - _tool_call_counts.get("get_usage_stats", 0)
        cum["sessions"] = cum.get("sessions", 0) + 1
        cum["total_calls"] = cum.get("total_calls", 0) + session_calls
        cum["total_chars_returned"] = cum.get("total_chars_returned", 0) + _total_chars_returned
        cum["total_naive_chars"] = cum.get("total_naive_chars", 0) + naive_chars
        cum["project"] = slot.root
        cum["last_session"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        for tool, count in _tool_call_counts.items():
            if tool == "get_usage_stats":
                continue
            cum["tool_counts"][tool] = cum["tool_counts"].get(tool, 0) + count
        with open(slot.stats_file, "w") as f:
            json.dump(cum, f, indent=2)
    except Exception as e:
        print(f"[mcp-codebase-index] Failed to flush stats: {e}", file=sys.stderr)


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
    "get_file_dependencies": 0.02,
    "get_file_dependents": 0.10,
    "search_codebase": 0.15,
    "reindex": 0.0,
    "set_project_root": 0.0,
    "switch_project": 0.0,
    "list_projects": 0.0,
}


# ---------------------------------------------------------------------------
# Formatting helpers
# ---------------------------------------------------------------------------

def _format_result(value: object) -> str:
    """Format a query result as readable text."""
    if isinstance(value, str):
        return value
    if isinstance(value, (dict, list)):
        return json.dumps(value, indent=2, default=str)
    return str(value)


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
        if query_calls > 0 and source_chars > _total_chars_returned:
            naive_chars = 0
            for tool_name, count in _tool_call_counts.items():
                if tool_name == "get_usage_stats":
                    continue
                multiplier = _TOOL_COST_MULTIPLIERS.get(tool_name, 0.10)
                naive_chars += int(source_chars * multiplier * count)
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
        # Show cumulative stats for the active project
        slot = _projects.get(_active_root)
        if slot and slot.stats_file:
            cum = _load_cumulative_stats(slot.stats_file)
            cum_calls = cum.get("total_calls", 0)
            if cum_calls > 0:
                lines.append("")
                lines.append("─── Cumulative (all sessions) ───")
                lines.append(f"Sessions: {cum.get('sessions', 0)}")
                lines.append(f"Total queries: {cum_calls:,}")
                cum_chars = cum.get("total_chars_returned", 0)
                cum_naive = cum.get("total_naive_chars", 0)
                lines.append(f"Chars returned: {cum_chars:,} ({cum_chars // 4:,} tokens)")
                if cum_naive > 0:
                    cum_reduction = (1 - cum_chars / cum_naive) * 100 if cum_naive > cum_chars else 0
                    lines.append(f"Naive estimate: {cum_naive:,} ({cum_naive // 4:,} tokens)")
                    lines.append(f"Token savings: {cum_reduction:.1f}%")
                if cum.get("tool_counts"):
                    lines.append("Top tools:")
                    for t, c in sorted(cum["tool_counts"].items(), key=lambda x: -x[1])[:5]:
                        lines.append(f"  {t}: {c:,}")
                if cum.get("last_session"):
                    lines.append(f"Last session: {cum['last_session']}")

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
    """Return the path to the JSON cache file for this project."""
    return os.path.join(project_root, _CACHE_FILENAME)


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
    from mcp_codebase_index.models import (
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
        print(f"[mcp-codebase-index] Cache saved → {path}", file=sys.stderr)
    except Exception as exc:
        print(f"[mcp-codebase-index] Cache save failed: {exc}", file=sys.stderr)


def _load_cache(project_root: str) -> "ProjectIndex | None":
    """Load a cached project index from JSON if it exists and is compatible."""
    path = _cache_path(project_root)
    if not os.path.exists(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            payload = json.load(f)
        if not isinstance(payload, dict) or payload.get("version") != _CACHE_VERSION:
            print("[mcp-codebase-index] Cache version mismatch, ignoring", file=sys.stderr)
            return None
        return _index_from_dict(payload["index"])
    except Exception as exc:
        print(f"[mcp-codebase-index] Cache load failed: {exc}", file=sys.stderr)
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
            print(f"[mcp-codebase-index] Cache hit (git ref matches) — {root}", file=sys.stderr)
            slot.indexer = ProjectIndexer(root)
            slot.indexer._project_index = cached_index
            slot.query_fns = create_project_query_functions(cached_index)
            return

        changeset = get_changed_files(root, cached_index.last_indexed_git_ref)
        total_changes = len(changeset.modified) + len(changeset.added) + len(changeset.deleted)
        if not changeset.is_empty and total_changes <= 20:
            print(
                f"[mcp-codebase-index] Cache hit with {total_changes} changed files, "
                f"applying incremental update — {root}",
                file=sys.stderr,
            )
            slot.indexer = ProjectIndexer(root)
            slot.indexer._project_index = cached_index
            slot.query_fns = create_project_query_functions(cached_index)
            return

        print(
            f"[mcp-codebase-index] Cache stale ({total_changes} changes), full rebuild — {root}",
            file=sys.stderr,
        )

    _build_slot(slot)


def _build_slot(slot: _ProjectSlot) -> None:
    """Full index build for a project slot."""
    root = slot.root
    if not slot.stats_file:
        slot.stats_file = _get_stats_file(root)

    print(f"[mcp-codebase-index] Indexing project: {root}", file=sys.stderr)

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
        f"[mcp-codebase-index] Indexed {index.total_files} files, "
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
    changeset = get_changed_files(slot.root, idx.last_indexed_git_ref)
    if changeset.is_empty:
        return

    total_changes = len(changeset.modified) + len(changeset.added) + len(changeset.deleted)

    if total_changes > 20 and total_changes > idx.total_files * 0.5:
        print(
            f"[mcp-codebase-index] Large changeset ({total_changes} files), "
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
        f"[mcp-codebase-index] Incremental update: "
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
        description="Analyze the impact of changing a symbol. Returns direct dependents and transitive (cascading) dependents.",
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
        name="get_usage_stats",
        description="Session efficiency stats: tool calls, characters returned vs total source, estimated token savings.",
        inputSchema={"type": "object", "properties": {}},
    ),
]


# ---------------------------------------------------------------------------
# MCP handlers
# ---------------------------------------------------------------------------


@server.list_tools()
async def list_tools() -> list[Tool]:
    return TOOLS


@server.call_tool()
async def call_tool(name: str, arguments: dict) -> list[types.TextContent]:
    global _total_chars_returned, _active_root

    _tool_call_counts[name] = _tool_call_counts.get(name, 0) + 1

    try:
        # ── Meta tools ────────────────────────────────────────────────────────

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
                    lines.append(f"  • {name_part}{active} — {idx.total_files} files, {idx.total_functions} functions ({root})")
                else:
                    lines.append(f"  • {name_part}{active} — {status} ({root})")
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
            return [TextContent(type="text", text=f"Switched to '{os.path.basename(slot.root)}' ({slot.root}) — {info}.")]

        if name == "set_project_root":
            new_root = os.path.abspath(arguments["path"])
            if not os.path.isdir(new_root):
                return [TextContent(type="text", text=f"Error: '{new_root}' is not a directory.")]
            if new_root not in _projects:
                _projects[new_root] = _ProjectSlot(root=new_root)
            _active_root = new_root
            slot = _projects[new_root]
            # Force full rebuild
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

        # ── Query tools — resolve slot, lazy-init, run ─────────────────────

        project_hint = arguments.get("project")
        slot, err = _resolve_slot(project_hint)
        if err:
            return [TextContent(type="text", text=f"Error: {err}")]

        _ensure_slot(slot)
        _maybe_incremental_update(slot)

        if slot.query_fns is None:
            return [TextContent(type="text", text=f"Error: index not built for '{slot.root}'. Call reindex first.")]

        qfns = slot.query_fns

        if name == "get_project_summary":
            result = qfns["get_project_summary"]()

        elif name == "list_files":
            pattern = arguments.get("pattern")
            max_results = arguments.get("max_results", 0)
            result = qfns["list_files"](pattern, max_results=max_results)

        elif name == "get_structure_summary":
            result = qfns["get_structure_summary"](arguments.get("file_path"))

        elif name == "get_function_source":
            result = qfns["get_function_source"](
                arguments["name"],
                arguments.get("file_path"),
                max_lines=arguments.get("max_lines", 0),
            )

        elif name == "get_class_source":
            result = qfns["get_class_source"](
                arguments["name"],
                arguments.get("file_path"),
                max_lines=arguments.get("max_lines", 0),
            )

        elif name == "get_functions":
            result = qfns["get_functions"](arguments.get("file_path"), max_results=arguments.get("max_results", 0))

        elif name == "get_classes":
            result = qfns["get_classes"](arguments.get("file_path"), max_results=arguments.get("max_results", 0))

        elif name == "get_imports":
            result = qfns["get_imports"](arguments.get("file_path"), max_results=arguments.get("max_results", 0))

        elif name == "find_symbol":
            result = qfns["find_symbol"](arguments["name"])

        elif name == "get_dependencies":
            result = qfns["get_dependencies"](arguments["name"], max_results=arguments.get("max_results", 0))

        elif name == "get_dependents":
            result = qfns["get_dependents"](arguments["name"], max_results=arguments.get("max_results", 0))

        elif name == "get_change_impact":
            result = qfns["get_change_impact"](
                arguments["name"],
                max_direct=arguments.get("max_direct", 0),
                max_transitive=arguments.get("max_transitive", 0),
            )

        elif name == "get_call_chain":
            result = qfns["get_call_chain"](arguments["from_name"], arguments["to_name"])

        elif name == "get_file_dependencies":
            result = qfns["get_file_dependencies"](arguments["file_path"], max_results=arguments.get("max_results", 0))

        elif name == "get_file_dependents":
            result = qfns["get_file_dependents"](arguments["file_path"], max_results=arguments.get("max_results", 0))

        elif name == "search_codebase":
            result = qfns["search_codebase"](arguments["pattern"], max_results=arguments.get("max_results", 100))

        else:
            return [TextContent(type="text", text=f"Error: unknown tool '{name}'")]

        formatted = _format_result(result)
        _total_chars_returned += len(formatted)

        # Flush stats
        source_chars = 0
        for s in _projects.values():
            if s.indexer and s.indexer._project_index:
                source_chars += sum(m.total_chars for m in s.indexer._project_index.files.values())
        naive_chars = 0
        for t, c in _tool_call_counts.items():
            if t == "get_usage_stats":
                continue
            naive_chars += int(source_chars * _TOOL_COST_MULTIPLIERS.get(t, 0.10) * c)
        if slot.stats_file:
            _flush_stats(slot, naive_chars)

        return [TextContent(type="text", text=formatted)]

    except Exception as e:
        tb = traceback.format_exc()
        print(f"[mcp-codebase-index] Error in {name}: {tb}", file=sys.stderr)
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
