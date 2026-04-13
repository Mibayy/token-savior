"""Structural query API for single-file and project-wide codebase navigation.

Provides factory functions that create dictionaries of query functions
bound to a StructuralMetadata (single file) or ProjectIndex (project-wide).
All functions return plain dicts/strings for easy use in a REPL.
"""

from __future__ import annotations

import fnmatch
import re
from collections import deque
from typing import Callable

from token_savior.community import compute_communities, get_cluster_for_symbol
from token_savior.entry_points import score_entry_points
from token_savior.models import (
    ClassInfo,
    FunctionInfo,
    ProjectIndex,
    StructuralMetadata,
)
from token_savior.symbol_hash import analyze_symbol_semantics


# ---------------------------------------------------------------------------
# Single-file query functions
# ---------------------------------------------------------------------------


def create_file_query_functions(metadata: StructuralMetadata) -> dict[str, Callable]:
    """Create query functions bound to a single file's structural metadata.

    Returns a dict mapping function names to callables. Each function returns
    plain dicts or strings suitable for printing in a REPL.
    """

    def get_structure_summary() -> str:
        """Overview of the file: functions, classes, imports, line count."""
        parts = [f"File: {metadata.source_name} ({metadata.total_lines} lines)"]

        if metadata.imports:
            modules = sorted({imp.module for imp in metadata.imports})
            parts.append(f"Imports: {', '.join(modules)}")

        if metadata.classes:
            for cls in metadata.classes:
                method_names = [m.name for m in cls.methods]
                bases = f"({', '.join(cls.base_classes)})" if cls.base_classes else ""
                parts.append(
                    f"Class {cls.name}{bases} (lines {cls.line_range.start}-{cls.line_range.end}): "
                    f"methods: {', '.join(method_names) if method_names else 'none'}"
                )

        top_level_funcs = [f for f in metadata.functions if not f.is_method]
        if top_level_funcs:
            for func in top_level_funcs:
                parts.append(
                    f"Function {func.name}({', '.join(func.parameters)}) "
                    f"(lines {func.line_range.start}-{func.line_range.end})"
                )

        if metadata.sections:
            for sec in metadata.sections:
                indent = "  " * (sec.level - 1)
                parts.append(
                    f"{indent}Section: {sec.title} "
                    f"(lines {sec.line_range.start}-{sec.line_range.end})"
                )

        return "\n".join(parts)

    def get_lines(start: int, end: int) -> str:
        """Get specific lines (1-indexed, inclusive)."""
        if start < 1:
            return "Error: start must be >= 1"
        if end > metadata.total_lines:
            end = metadata.total_lines
        if start > end:
            return f"Error: start ({start}) > end ({end})"
        # lines are 0-indexed internally
        return "\n".join(metadata.lines[start - 1 : end])

    def get_line_count() -> int:
        """Return the total number of lines."""
        return metadata.total_lines

    def get_functions() -> list[dict]:
        """All functions with name, qualified_name, lines, params."""
        return [
            {
                "name": f.name,
                "qualified_name": f.qualified_name,
                "lines": [f.line_range.start, f.line_range.end],
                "params": f.parameters,
                "is_method": f.is_method,
                "parent_class": f.parent_class,
            }
            for f in metadata.functions
        ]

    def get_classes() -> list[dict]:
        """All classes with name, lines, methods, bases."""
        return [
            {
                "name": cls.name,
                "lines": [cls.line_range.start, cls.line_range.end],
                "methods": [m.name for m in cls.methods],
                "bases": cls.base_classes,
            }
            for cls in metadata.classes
        ]

    def get_imports() -> list[dict]:
        """All imports with module, names, line."""
        return [
            {
                "module": imp.module,
                "names": imp.names,
                "line": imp.line_number,
                "is_from_import": imp.is_from_import,
            }
            for imp in metadata.imports
        ]

    def get_function_source(name: str) -> str:
        """Source of a function by name (searches top-level and methods)."""
        for f in metadata.functions:
            if f.name == name or f.qualified_name == name:
                return "\n".join(metadata.lines[f.line_range.start - 1 : f.line_range.end])
        return f"Error: function '{name}' not found"

    def get_class_source(name: str) -> str:
        """Source of a class by name."""
        for cls in metadata.classes:
            if cls.name == name:
                return "\n".join(metadata.lines[cls.line_range.start - 1 : cls.line_range.end])
        return f"Error: class '{name}' not found"

    def get_sections() -> list[dict]:
        """Sections for text files."""
        return [
            {
                "title": sec.title,
                "level": sec.level,
                "lines": [sec.line_range.start, sec.line_range.end],
            }
            for sec in metadata.sections
        ]

    def get_section_content(title: str) -> str:
        """Content of a section by title."""
        for sec in metadata.sections:
            if sec.title == title:
                return "\n".join(metadata.lines[sec.line_range.start - 1 : sec.line_range.end])
        return f"Error: section '{title}' not found"

    def _resolve_file_symbol(name: str) -> dict:
        """Resolve a symbol name to rich info from the file metadata."""
        for func in metadata.functions:
            if func.qualified_name == name or func.name == name:
                return {
                    "name": func.qualified_name,
                    "file": metadata.source_name,
                    "line": func.line_range.start,
                    "end_line": func.line_range.end,
                    "type": "method" if func.is_method else "function",
                }
        for cls in metadata.classes:
            if cls.name == name:
                return {
                    "name": cls.name,
                    "file": metadata.source_name,
                    "line": cls.line_range.start,
                    "end_line": cls.line_range.end,
                    "type": "class",
                }
        return {"name": name}

    def get_dependencies(name: str) -> list[dict]:
        """What this function/class references."""
        deps = metadata.dependency_graph.get(name)
        if deps is None:
            return [{"error": f"'{name}' not found in dependency graph"}]
        return [_resolve_file_symbol(dep) for dep in sorted(deps)]

    def get_dependents(name: str) -> list[dict]:
        """What references this function/class."""
        result = []
        for source, targets in metadata.dependency_graph.items():
            if name in targets:
                result.append(source)
        return [_resolve_file_symbol(dep) for dep in sorted(result)]

    def search_lines(pattern: str) -> list[dict]:
        """Regex search, returns [{line_number, content}], max 100 results."""
        try:
            regex = re.compile(pattern)
        except re.error as e:
            return [{"error": f"Invalid regex: {e}"}]
        results = []
        for i, line in enumerate(metadata.lines):
            if regex.search(line):
                results.append({"line_number": i + 1, "content": line})
                if len(results) >= 100:
                    break
        return results

    return {
        "get_structure_summary": get_structure_summary,
        "get_lines": get_lines,
        "get_line_count": get_line_count,
        "get_functions": get_functions,
        "get_classes": get_classes,
        "get_imports": get_imports,
        "get_function_source": get_function_source,
        "get_class_source": get_class_source,
        "get_sections": get_sections,
        "get_section_content": get_section_content,
        "get_dependencies": get_dependencies,
        "get_dependents": get_dependents,
        "search_lines": search_lines,
    }


# ---------------------------------------------------------------------------
# Project-wide query functions
# ---------------------------------------------------------------------------


def _resolve_file(index: ProjectIndex, file_path: str) -> StructuralMetadata | None:
    """Resolve a file path to its StructuralMetadata, trying exact and relative matches."""
    if file_path in index.files:
        return index.files[file_path]
    # Try matching against the end of stored paths
    for stored_path, meta in index.files.items():
        if stored_path.endswith(file_path) or file_path.endswith(stored_path):
            return meta
    return None


# ---------------------------------------------------------------------------
# Abstraction-level formatters (L1/L2/L3).
# ---------------------------------------------------------------------------


def _first_doc_line(doc: str | None) -> str:
    if not doc:
        return ""
    return doc.strip().splitlines()[0]


def _format_l1(sym: FunctionInfo | ClassInfo) -> str:
    """Signature + docstring. No body."""
    if isinstance(sym, ClassInfo):
        head = f"class {sym.name}"
        if sym.base_classes:
            head += f"({', '.join(sym.base_classes)})"
        head += ":"
        lines = [f"@{d}" for d in sym.decorators] + [head]
        if sym.docstring:
            lines.append(f'    """{sym.docstring.strip()}"""')
        return "\n".join(lines)
    # FunctionInfo
    params = ", ".join(sym.parameters)
    lines = [f"@{d}" for d in sym.decorators]
    lines.append(f"def {sym.name}({params}):")
    if sym.docstring:
        lines.append(f'    """{sym.docstring.strip()}"""')
    return "\n".join(lines)


def _format_l2(sym: FunctionInfo | ClassInfo, body: str) -> str:
    """Semantic summary: raises, side effects, return hints, first doc line."""
    if isinstance(sym, ClassInfo):
        header = f"[L2] class {sym.name}"
        if sym.base_classes:
            header += f"({', '.join(sym.base_classes)})"
    else:
        header = f"[L2] {sym.name}({', '.join(sym.parameters)})"

    analysis = analyze_symbol_semantics(body)
    out = [header]
    if isinstance(sym, ClassInfo):
        out.append(f"  methods: {len(sym.methods)}")
    if analysis["raises"]:
        out.append(f"  raises: {', '.join(analysis['raises'])}")
    if analysis["has_side_effects"]:
        out.append("  side-effects: yes (io/db/network detected)")
    if analysis["returns"]:
        out.append(f"  returns: {analysis['returns'][0][:60]}")
    doc = _first_doc_line(sym.docstring)
    if doc:
        out.append(f"  doc: {doc[:120]}")
    return "\n".join(out)


def _format_l3(sym: FunctionInfo | ClassInfo) -> str:
    """One-liner for dense indexes."""
    doc = _first_doc_line(sym.docstring) or "no description"
    if isinstance(sym, ClassInfo):
        return f"class {sym.name} - {doc}"
    params = list(sym.parameters)
    head = ", ".join(params[:3])
    if len(params) > 3:
        head += ", ..."
    return f"{sym.name}({head}) - {doc}"


class ProjectQueryEngine:
    """Query engine bound to a project-wide index.

    Each public method corresponds to a tool exposed by the MCP server.
    Use ``as_dict()`` for backward compatibility with code that expects
    the old ``create_project_query_functions`` dict interface.
    """

    _tools = [
        "get_project_summary",
        "list_files",
        "get_structure_summary",
        "get_lines",
        "get_functions",
        "get_classes",
        "get_imports",
        "get_function_source",
        "get_class_source",
        "find_symbol",
        "get_dependencies",
        "get_dependents",
        "get_call_chain",
        "get_file_dependencies",
        "get_file_dependents",
        "search_codebase",
        "get_change_impact",
        "get_routes",
        "get_env_usage",
        "get_components",
        "get_feature_files",
        "get_entry_points",
        "get_symbol_cluster",
        "get_backward_slice",
        "pack_context",
        "get_relevance_cluster",
        "find_semantic_duplicates",
    ]

    def __init__(self, index: ProjectIndex):
        self.index = index
        self._communities: dict[str, str] | None = None

    # ------------------------------------------------------------------
    # Public interface
    # ------------------------------------------------------------------

    def as_dict(self) -> dict[str, Callable]:
        """Retrocompatibility: returns the same dict as the old closure."""
        return {name: getattr(self, name) for name in self._tools}

    # ------------------------------------------------------------------
    # Tools
    # ------------------------------------------------------------------

    def get_project_summary(self) -> str:
        """Compact project overview: counts + top packages only."""
        index = self.index
        parts = [
            f"Project: {index.root_path}",
            f"Files: {index.total_files}, Lines: {index.total_lines}, "
            f"Functions: {index.total_functions}, Classes: {index.total_classes}",
        ]

        # Top-level packages only (deduplicated)
        top_packages = sorted({p.split("/")[0] for p in index.files if "/" in p})
        if top_packages:
            parts.append(f"Packages ({len(top_packages)}): {', '.join(top_packages[:15])}")
            if len(top_packages) > 15:
                parts.append(f"  ... and {len(top_packages) - 15} more")

        # Counts per type, no individual names
        class_count = sum(len(meta.classes) for meta in index.files.values())
        func_count = sum(
            sum(1 for f in meta.functions if not f.is_method) for meta in index.files.values()
        )
        if class_count:
            parts.append(f"Classes: {class_count} total")
        if func_count:
            parts.append(f"Top-level functions: {func_count} total")

        return "\n".join(parts)

    def list_files(self, pattern: str | None = None, max_results: int = 0) -> list[str]:
        """List indexed files, optional glob filter (using fnmatch)."""
        paths = sorted(self.index.files.keys())
        if pattern:
            paths = [p for p in paths if fnmatch.fnmatch(p, pattern)]
        if max_results > 0:
            paths = paths[:max_results]
        return paths

    def get_structure_summary(self, file_path: str | None = None) -> str:
        """Per-file or project-level summary."""
        if file_path is None:
            return self.get_project_summary()
        meta = _resolve_file(self.index, file_path)
        if meta is None:
            return f"Error: file '{file_path}' not found in index"
        file_funcs = create_file_query_functions(meta)
        return file_funcs["get_structure_summary"]()

    def get_lines(self, file_path: str, start: int, end: int) -> str:
        """Lines from a specific file."""
        meta = _resolve_file(self.index, file_path)
        if meta is None:
            return f"Error: file '{file_path}' not found in index"
        file_funcs = create_file_query_functions(meta)
        return file_funcs["get_lines"](start, end)

    def get_functions(self, file_path: str | None = None, max_results: int = 0) -> list[dict]:
        """Functions in a file, or all functions across the project."""
        if file_path is not None:
            meta = _resolve_file(self.index, file_path)
            if meta is None:
                return [{"error": f"file '{file_path}' not found in index"}]
            file_funcs = create_file_query_functions(meta)
            result = file_funcs["get_functions"]()
        else:
            # All functions across project
            result = []
            for path, meta in sorted(self.index.files.items()):
                for f in meta.functions:
                    result.append(
                        {
                            "name": f.name,
                            "qualified_name": f.qualified_name,
                            "lines": [f.line_range.start, f.line_range.end],
                            "params": f.parameters,
                            "is_method": f.is_method,
                            "parent_class": f.parent_class,
                            "file": path,
                        }
                    )
        if max_results > 0:
            result = result[:max_results]
        return result

    def get_classes(self, file_path: str | None = None, max_results: int = 0) -> list[dict]:
        """Classes in a file or across the project."""
        if file_path is not None:
            meta = _resolve_file(self.index, file_path)
            if meta is None:
                return [{"error": f"file '{file_path}' not found in index"}]
            file_funcs = create_file_query_functions(meta)
            result = file_funcs["get_classes"]()
        else:
            result = []
            for path, meta in sorted(self.index.files.items()):
                for cls in meta.classes:
                    result.append(
                        {
                            "name": cls.name,
                            "lines": [cls.line_range.start, cls.line_range.end],
                            "methods": [m.name for m in cls.methods],
                            "bases": cls.base_classes,
                            "file": path,
                        }
                    )
        if max_results > 0:
            result = result[:max_results]
        return result

    def get_imports(self, file_path: str | None = None, max_results: int = 0) -> list[dict]:
        """Imports in a file or across the project."""
        if file_path is not None:
            meta = _resolve_file(self.index, file_path)
            if meta is None:
                return [{"error": f"file '{file_path}' not found in index"}]
            file_funcs = create_file_query_functions(meta)
            result = file_funcs["get_imports"]()
        else:
            result = []
            for path, meta in sorted(self.index.files.items()):
                for imp in meta.imports:
                    result.append(
                        {
                            "module": imp.module,
                            "names": imp.names,
                            "line": imp.line_number,
                            "is_from_import": imp.is_from_import,
                            "file": path,
                        }
                    )
        if max_results > 0:
            result = result[:max_results]
        return result

    def get_function_source(
        self,
        name: str,
        file_path: str | None = None,
        max_lines: int = 0,
        level: int = 0,
    ) -> str:
        """Source of a function at the requested abstraction level (0-3)."""
        if level and level > 0:
            return self.get_symbol_abstract(name, level=level, file_path=file_path)
        return self._get_symbol_source(name, "function", file_path, max_lines)

    def get_class_source(
        self,
        name: str,
        file_path: str | None = None,
        max_lines: int = 0,
        level: int = 0,
    ) -> str:
        """Source of a class at the requested abstraction level (0-3)."""
        if level and level > 0:
            return self.get_symbol_abstract(name, level=level, file_path=file_path)
        return self._get_symbol_source(name, "class", file_path, max_lines)

    # -----------------------------------------------------------------
    # Abstraction levels (L0-L3) — trade detail for tokens.
    # -----------------------------------------------------------------

    def get_symbol_abstract(
        self, name: str, level: int = 2, file_path: str | None = None
    ) -> str:
        """Return a symbol (function, method, or class) at an abstraction level.

        L0 — full source (use get_function_source / get_class_source directly).
        L1 — signature + docstring only.
        L2 — semantic summary (raises, side effects, returns, doc first line).
        L3 — one-liner suitable for dense indexes.
        """
        if level < 0 or level > 3:
            return f"Error: level must be in 0..3, got {level}"

        resolved = self._resolve_any_symbol(name, file_path)
        if resolved is None:
            return f"Symbol '{name}' not found"
        kind, meta, sym = resolved

        if level == 0:
            return self._get_symbol_source(name, kind, file_path)

        if level == 1:
            return _format_l1(sym)
        if level == 2:
            body = "\n".join(
                meta.lines[sym.line_range.start - 1 : sym.line_range.end]
            )
            return _format_l2(sym, body)
        # level == 3
        return _format_l3(sym)

    def _resolve_any_symbol(
        self, name: str, file_path: str | None
    ) -> tuple[str, StructuralMetadata, FunctionInfo | ClassInfo] | None:
        """Find a symbol (function/method/class) across the project.

        Returns (kind, metadata, symbol) or None.
        """
        index = self.index
        candidate_paths: list[str] = []
        if file_path is not None:
            candidate_paths = [file_path]
        elif name in index.symbol_table:
            candidate_paths = [index.symbol_table[name]]
        else:
            candidate_paths = list(index.files.keys())

        for path in candidate_paths:
            meta = _resolve_file(index, path)
            if meta is None:
                continue
            for func in meta.functions:
                if func.name == name or func.qualified_name == name:
                    return ("function", meta, func)
            for cls in meta.classes:
                if cls.name == name:
                    return ("class", meta, cls)
                for method in cls.methods:
                    if method.qualified_name == name:
                        return ("function", meta, method)
        return None

    def find_symbol(self, name: str) -> dict:
        """Find where a symbol is defined: {file, line, type, signature, source_preview}."""
        result = self._resolve_symbol_info(name)
        if "file" not in result:
            return {"error": f"symbol '{name}' not found"}
        return result

    def get_dependencies(self, name: str, max_results: int = 0) -> list[dict]:
        """What this function/class references (from global_dependency_graph)."""
        deps = self.index.global_dependency_graph.get(name)
        if deps is None:
            return [{"error": f"'{name}' not found in dependency graph"}]
        result = sorted(deps)
        if max_results > 0:
            result = result[:max_results]
        return [self._resolve_symbol_info(dep) for dep in result]

    def get_dependents(self, name: str, max_results: int = 0, max_total_chars: int = 50_000) -> list[dict]:
        """What references this function/class (from reverse_dependency_graph)."""
        resolved_name, deps = self._resolve_dep_name(name)
        if deps is None:
            return [{"error": f"'{name}' not found in reverse dependency graph"}]
        result = sorted(deps)
        if max_results > 0:
            result = result[:max_results]
        entries = []
        chars_used = 0
        for dep in result:
            entry = self._resolve_symbol_info(dep)
            entry_len = len(str(entry))
            if max_total_chars > 0 and chars_used + entry_len > max_total_chars:
                entries.append({
                    "truncated": True,
                    "message": f"... output truncated at {max_total_chars} chars. "
                    "Use max_results to narrow the scope.",
                    "shown": len(entries),
                    "total": len(result),
                })
                break
            chars_used += entry_len
            entries.append(entry)
        return entries

    def get_call_chain(self, from_name: str, to_name: str) -> dict:
        """Shortest path in dependency graph (BFS).

        Returns {chain: [{name, file, line, end_line, type, signature, source_preview}, ...]}
        with rich info for each hop, so callers don't need follow-up lookups.
        """
        index = self.index
        if from_name not in index.global_dependency_graph:
            return {"error": f"'{from_name}' not found in dependency graph"}
        if from_name == to_name:
            info = self._resolve_symbol_info(from_name)
            info.setdefault("name", from_name)
            return {"chain": [info]}

        # BFS
        visited = {from_name}
        queue: deque[list[str]] = deque([[from_name]])
        path_names: list[str] | None = None
        while queue:
            path = queue.popleft()
            current = path[-1]
            neighbors = index.global_dependency_graph.get(current, set())
            for neighbor in sorted(neighbors):
                if neighbor == to_name:
                    path_names = path + [neighbor]
                    break
                if neighbor not in visited:
                    visited.add(neighbor)
                    queue.append(path + [neighbor])
            if path_names is not None:
                break

        if path_names is None:
            return {"error": f"no path from '{from_name}' to '{to_name}'"}

        # Enrich each hop with file, line, signature, source preview
        chain = []
        for name in path_names:
            info = self._resolve_symbol_info(name)
            info.setdefault("name", name)
            chain.append(info)

        return {"chain": chain}

    def get_file_dependencies(self, file_path: str, max_results: int = 0) -> list[str]:
        """What files this file imports from (from import_graph)."""
        deps = self.index.import_graph.get(file_path)
        if deps is None:
            return [f"Error: '{file_path}' not found in import graph"]
        result = sorted(deps)
        if max_results > 0:
            result = result[:max_results]
        return result

    def get_file_dependents(self, file_path: str, max_results: int = 0) -> list[str]:
        """What files import from this file (from reverse_import_graph)."""
        deps = self.index.reverse_import_graph.get(file_path)
        if deps is None:
            return [f"Error: '{file_path}' not found in reverse import graph"]
        result = sorted(deps)
        if max_results > 0:
            result = result[:max_results]
        return result

    def search_codebase(self, pattern: str, max_results: int = 100) -> list[dict]:
        """Regex across all files, returns [{file, line_number, content}]."""
        try:
            regex = re.compile(pattern)
        except re.error as e:
            return [{"error": f"Invalid regex: {e}"}]
        limit = max_results if max_results > 0 else 0
        results = []
        for path in sorted(self.index.files.keys()):
            meta = self.index.files[path]
            for i, line in enumerate(meta.lines):
                if regex.search(line):
                    results.append(
                        {
                            "file": path,
                            "line_number": i + 1,
                            "content": line,
                        }
                    )
                    if limit and len(results) >= limit:
                        return results
        return results

    def get_change_impact(
        self, name: str, max_direct: int = 0, max_transitive: int = 0, max_total_chars: int = 50_000
    ) -> dict:
        """Direct and transitive dependents of a symbol, each with confidence and depth."""
        index = self.index
        resolved_name, direct = self._resolve_dep_name(name)
        if direct is None:
            return {"error": f"'{name}' not found in reverse dependency graph"}

        # BFS tracking depth per symbol
        depth_map: dict[str, int] = {}
        queue: deque[tuple[str, int]] = deque((sym, 1) for sym in direct)
        visited: set[str] = set(direct) | {name}
        for sym in direct:
            depth_map[sym] = 1

        while queue:
            current, depth = queue.popleft()
            next_deps = index.reverse_dependency_graph.get(current, set())
            for dep in next_deps:
                if dep not in visited:
                    visited.add(dep)
                    depth_map[dep] = depth + 1
                    queue.append((dep, depth + 1))

        def _make_entry(sym: str) -> dict:
            d = depth_map[sym]
            confidence = max(0.05, 0.6 ** (d - 1))
            info = self._resolve_symbol_info(sym)
            return {**info, "confidence": confidence, "depth": d}

        direct_set = set(direct)
        direct_entries = [_make_entry(s) for s in direct_set]
        direct_entries.sort(key=lambda e: -e["confidence"])
        if max_direct > 0:
            direct_entries = direct_entries[:max_direct]

        transitive_entries = [_make_entry(s) for s in depth_map if s not in direct_set]
        transitive_entries.sort(key=lambda e: -e["confidence"])
        if max_transitive > 0:
            transitive_entries = transitive_entries[:max_transitive]

        result = {
            "direct": direct_entries,
            "transitive": transitive_entries,
        }

        if max_total_chars > 0:
            import json as _json
            serialized = _json.dumps(result, separators=(",", ":"), default=str)
            if len(serialized) > max_total_chars:
                # Trim transitive first, then direct if still too large
                while transitive_entries and len(serialized) > max_total_chars:
                    transitive_entries.pop()
                    result["transitive"] = transitive_entries
                    serialized = _json.dumps(result, separators=(",", ":"), default=str)
                while direct_entries and len(serialized) > max_total_chars:
                    direct_entries.pop()
                    result["direct"] = direct_entries
                    serialized = _json.dumps(result, separators=(",", ":"), default=str)
                result["truncated"] = True
                result["message"] = (
                    f"... output truncated at {max_total_chars} chars. "
                    "Use max_direct / max_transitive to narrow the scope."
                )

        return result

    # ------------------------------------------------------------------
    # v3: Route Map (Next.js App Router + Express-style)
    # ------------------------------------------------------------------

    def get_routes(self, max_results: int = 0) -> list[dict]:
        """Detect API routes and pages from the project structure.
        Returns [{route, file, methods, type}] for Next.js App Router,
        Express, and similar frameworks."""
        routes: list[dict] = []
        for path, meta in self.index.files.items():
            # Next.js App Router: app/**/route.ts -> API route
            if "/route." in path and ("app/" in path or "pages/api/" in path):
                # Extract HTTP methods from exported functions
                methods = []
                for func in meta.functions:
                    upper = func.name.upper()
                    if upper in ("GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"):
                        methods.append(upper)
                # Derive the route path from file path
                route_path = path
                for prefix in ("app/", "src/app/"):
                    if prefix in route_path:
                        route_path = "/" + route_path.split(prefix, 1)[1]
                        break
                route_path = route_path.rsplit("/route.", 1)[0]
                if not route_path:
                    route_path = "/"
                routes.append(
                    {
                        "route": route_path,
                        "file": path,
                        "methods": methods or ["GET"],
                        "type": "api",
                    }
                )
            # Next.js App Router: app/**/page.tsx -> Page
            elif "/page." in path and "app/" in path:
                route_path = path
                for prefix in ("app/", "src/app/"):
                    if prefix in route_path:
                        route_path = "/" + route_path.split(prefix, 1)[1]
                        break
                route_path = route_path.rsplit("/page.", 1)[0]
                if not route_path:
                    route_path = "/"
                routes.append(
                    {
                        "route": route_path,
                        "file": path,
                        "methods": [],
                        "type": "page",
                    }
                )
            # Next.js App Router: app/**/layout.tsx -> Layout
            elif "/layout." in path and "app/" in path:
                route_path = path
                for prefix in ("app/", "src/app/"):
                    if prefix in route_path:
                        route_path = "/" + route_path.split(prefix, 1)[1]
                        break
                route_path = route_path.rsplit("/layout.", 1)[0]
                if not route_path:
                    route_path = "/"
                routes.append(
                    {
                        "route": route_path,
                        "file": path,
                        "methods": [],
                        "type": "layout",
                    }
                )
        routes.sort(key=lambda r: (r["type"], r["route"]))
        if max_results > 0:
            routes = routes[:max_results]
        return routes

    # ------------------------------------------------------------------
    # v3: Env var cross-reference
    # ------------------------------------------------------------------

    def get_env_usage(self, var_name: str, max_results: int = 0) -> list[dict]:
        """Find all references to an environment variable across the codebase.
        Searches for process.env.VAR, os.environ["VAR"], os.getenv("VAR"),
        and ${{ secrets.VAR }} patterns."""
        results: list[dict] = []
        for path, meta in self.index.files.items():
            for line_idx, line in enumerate(meta.lines):
                if var_name in line:
                    context = line.strip()
                    usage_type = "reference"
                    if "process.env." in line:
                        usage_type = "process.env"
                    elif "os.environ" in line or "os.getenv" in line:
                        usage_type = "os.environ"
                    elif "secrets." in line:
                        usage_type = "github_secret"
                    elif line.strip().startswith(var_name + "=") or line.strip().startswith(
                        f'"{var_name}"'
                    ):
                        usage_type = "definition"
                    elif "printf" in line and var_name in line:
                        usage_type = "env_write"
                    results.append(
                        {
                            "file": path,
                            "line": line_idx + 1,
                            "usage_type": usage_type,
                            "content": context[:200],
                        }
                    )
        results.sort(key=lambda r: (r["usage_type"], r["file"]))
        if max_results > 0:
            results = results[:max_results]
        return results

    # ------------------------------------------------------------------
    # v3: React component detection
    # ------------------------------------------------------------------

    def get_components(self, file_path: str | None = None, max_results: int = 0) -> list[dict]:
        """Detect React components (functions returning JSX).
        Heuristic: exported functions whose name starts with uppercase
        or are default exports in page/layout/component files."""
        components: list[dict] = []
        targets = self.index.files.items()
        if file_path:
            meta = self.index.files.get(file_path)
            if meta:
                targets = [(file_path, meta)]
            else:
                return []
        for path, meta in targets:
            ext = path.rsplit(".", 1)[-1] if "." in path else ""
            if ext not in ("tsx", "jsx"):
                continue
            for func in meta.functions:
                is_component = False
                comp_type = "component"
                # Uppercase first letter = React component convention
                if func.name and func.name[0].isupper():
                    is_component = True
                # Default export in page/layout file
                elif func.name == "default":
                    is_component = True
                    if "/page." in path:
                        comp_type = "page"
                    elif "/layout." in path:
                        comp_type = "layout"
                    elif "/loading." in path:
                        comp_type = "loading"
                    elif "/error." in path:
                        comp_type = "error"
                    else:
                        comp_type = "default_export"
                if is_component:
                    components.append(
                        {
                            "name": func.name,
                            "file": path,
                            "line_range": f"{func.line_range.start}-{func.line_range.end}",
                            "params": func.parameters,
                            "type": comp_type,
                        }
                    )
        components.sort(key=lambda c: (c["type"], c["file"], c["name"]))
        if max_results > 0:
            components = components[:max_results]
        return components

    # ------------------------------------------------------------------
    # v3: Feature file discovery (keyword -> all related files via imports)
    # ------------------------------------------------------------------

    def get_feature_files(self, keyword: str, max_results: int = 0) -> list[dict]:
        """Find all files related to a feature keyword, then trace their imports
        transitively to build the complete feature file set.

        Example: get_feature_files("contrat") returns route files, components,
        lib helpers, types -- everything connected to contracts."""
        index = self.index
        kw_lower = keyword.lower()

        # Step 1: Seed files -- paths or symbols containing the keyword
        seeds: set[str] = set()
        for path in index.files:
            if kw_lower in path.lower():
                seeds.add(path)
            else:
                meta = index.files[path]
                for func in meta.functions:
                    if kw_lower in func.name.lower():
                        seeds.add(path)
                        break
                else:
                    for cls in meta.classes:
                        if kw_lower in cls.name.lower():
                            seeds.add(path)
                            break

        # Step 2: Expand via import graph (1 hop each direction)
        expanded: set[str] = set(seeds)
        for seed in seeds:
            expanded.update(index.import_graph.get(seed, set()))
            expanded.update(index.reverse_import_graph.get(seed, set()))

        # Step 3: Classify each file
        results: list[dict] = []
        for path in sorted(expanded):
            if path not in index.files:
                continue
            meta = index.files[path]
            role = "lib"
            if "/route." in path:
                role = "api"
            elif "/page." in path:
                role = "page"
            elif "/layout." in path:
                role = "layout"
            elif "/components/" in path:
                role = "component"
            elif "/types" in path or path.endswith(".d.ts"):
                role = "type"
            elif "/lib/" in path or "/utils/" in path:
                role = "lib"
            elif "test" in path.lower() or "spec" in path.lower():
                role = "test"
            symbols = [f.name for f in meta.functions[:5]]
            symbols += [c.name for c in meta.classes[:3]]
            results.append(
                {
                    "file": path,
                    "role": role,
                    "seed": path in seeds,
                    "symbols": symbols,
                    "lines": meta.total_lines,
                }
            )
        results.sort(key=lambda r: (0 if r["seed"] else 1, r["role"], r["file"]))
        if max_results > 0:
            results = results[:max_results]
        return results

    def get_entry_points(self, max_results: int = 20) -> list[dict]:
        """Score functions by likelihood of being execution entry points.
        Returns [{name, file, line, score, reasons, params}] sorted by score desc."""
        return score_entry_points(self.index, max_results=max_results)

    def get_symbol_cluster(self, name: str, max_members: int = 30) -> dict:
        """Get the functional cluster for a symbol -- all closely related symbols
        grouped by community detection on the dependency graph.
        Returns {community_id, queried_symbol, size, members: [{name, file, line, type}]}."""
        return get_cluster_for_symbol(name, self._get_communities(), self.index, max_members=max_members)

    # ------------------------------------------------------------------
    # Semantic duplicate detection (P9 part A integration)
    # ------------------------------------------------------------------

    def find_semantic_duplicates(self, min_lines: int = 4) -> str:
        """Group functions whose AST-normalised hash collides.

        *min_lines* skips trivial one-or-two-liner functions where collisions
        are noise (`return None`, getters, etc).
        """
        from token_savior.semantic_hasher import semantic_hash

        index = self.index
        hash_to_symbols: dict[str, list[str]] = {}

        for file_path, meta in index.files.items():
            for func in meta.functions:
                start = func.line_range.start
                end = func.line_range.end
                if (end - start + 1) < min_lines:
                    continue
                source_lines = meta.lines[start - 1 : end]
                source = "\n".join(source_lines)
                if len(source) < 50:
                    continue
                h = semantic_hash(source)
                hash_to_symbols.setdefault(h, []).append(
                    f"{func.qualified_name}  ({file_path}:{start})"
                )

        duplicates = [(h, syms) for h, syms in hash_to_symbols.items() if len(syms) > 1]
        if not duplicates:
            return "Semantic duplicates: none found."

        lines = [f"Semantic duplicates: {len(duplicates)} group(s) found"]
        for h, syms in duplicates:
            lines.append("")
            lines.append(f"hash {h} ({len(syms)} symbols):")
            for s in syms:
                lines.append(f"  - {s}")
        return "\n".join(lines)

    # ------------------------------------------------------------------
    # RWR relevance ranking
    # ------------------------------------------------------------------

    def get_relevance_cluster(
        self,
        name: str,
        budget: int = 10,
        include_reverse: bool = True,
    ) -> str:
        """Return the top-*budget* symbols mathematically closest to *name* via RWR."""
        from token_savior.graph_ranker import random_walk_with_restart

        index = self.index

        # Combined graph: forward + (optional) reverse deps. Use sets to
        # union-merge.
        combined: dict[str, set[str]] = {}
        for sym, deps in index.global_dependency_graph.items():
            combined[sym] = set(deps)
        if include_reverse:
            for sym, callers in index.reverse_dependency_graph.items():
                combined.setdefault(sym, set()).update(callers)

        scores = random_walk_with_restart(
            graph=combined,
            seed_node=name,
            restart_prob=0.15,
        )
        if not scores:
            return f"Symbol '{name}' not found in dependency graph"

        iterations = int(scores.pop("__iterations__", 0))

        ranked = sorted(
            ((sym, sc) for sym, sc in scores.items() if sym != name),
            key=lambda x: x[1],
            reverse=True,
        )[:budget]

        lines: list[str] = [
            f"RWR Relevance cluster for '{name}' (top {budget}, converged in {iterations} iter):",
            "-" * 60,
        ]
        for sym, score in ranked:
            file_path = index.symbol_table.get(sym, "?")
            lines.append(f"  {score:.4f}  {sym}  ({file_path})")

        lines.append(
            f"\nNote: use pack_context(query='{name}', budget_tokens=N) "
            f"to get the optimal source bundle."
        )
        return "\n".join(lines)

    # ------------------------------------------------------------------
    # Knapsack context packing
    # ------------------------------------------------------------------

    def pack_context(
        self,
        query: str,
        budget_tokens: int = 4000,
        max_symbols: int = 20,
    ) -> str:
        """Build the optimal context bundle for *query* under *budget_tokens*."""
        from token_savior.context_packer import (
            SymbolCandidate,
            bfs_distance,
            pack_context as knapsack,
            score_symbol,
        )

        index = self.index
        graph = index.global_dependency_graph

        # Use the first query token as a coarse "seed" for dep distance scoring.
        # If the query has multiple tokens and at least one matches an existing
        # symbol, prefer that one.
        seed_candidates = [t for t in query.split() if t in index.symbol_table]
        seed = seed_candidates[0] if seed_candidates else (
            query.split()[0] if query.split() else ""
        )

        candidates: list[SymbolCandidate] = []
        for sym_name, file_path in index.symbol_table.items():
            metadata = index.files.get(file_path)
            if metadata is None:
                continue

            func = next(
                (f for f in metadata.functions if f.name == sym_name or f.qualified_name == sym_name),
                None,
            )
            if func is None:
                continue

            start = func.line_range.start
            end = func.line_range.end
            body_lines = max(end - start, 1)
            token_cost = max(body_lines * 8, 20)

            dep_dist = bfs_distance(graph, seed, sym_name)

            value = score_symbol(
                symbol_name=sym_name,
                query=query,
                dep_distance=dep_dist,
                recency_days=0.0,
                access_count=0,
            )

            candidates.append(
                SymbolCandidate(
                    name=sym_name,
                    file_path=file_path,
                    token_cost=token_cost,
                    value=value,
                )
            )

        # Pre-rank by value to keep the knapsack input bounded.
        candidates.sort(key=lambda c: c.value, reverse=True)
        pool = candidates[: max_symbols * 3]
        selected = knapsack(pool, budget_tokens)

        if not selected:
            return f"No symbols found for query '{query}'"

        total_cost = sum(s.token_cost for s in selected)
        lines: list[str] = [
            f"Context pack for '{query}' "
            f"({len(selected)} symbols, ~{total_cost} tokens / {budget_tokens} budget)",
            "-" * 60,
        ]
        for sym in selected[:10]:
            try:
                source = self._get_symbol_source(
                    sym.name, "function", file_path=sym.file_path
                )
            except Exception:
                source = "<source unavailable>"
            lines.append(
                f"\n# {sym.name}  (value={sym.value:.2f}, cost={sym.token_cost}t, {sym.file_path})"
            )
            preview = source if len(source) <= 500 else source[:500] + "..."
            lines.append(preview)
        if len(selected) > 10:
            lines.append(f"\n... ({len(selected) - 10} more symbols selected, sources omitted)")

        return "\n".join(lines)

    # ------------------------------------------------------------------
    # Program slicing
    # ------------------------------------------------------------------

    def get_backward_slice(
        self,
        name: str,
        variable: str,
        line: int,
        file_path: str | None = None,
    ) -> str:
        """Backward slice of *variable* at *line* (1-based, absolute) inside symbol *name*."""
        from token_savior.program_slicer import backward_slice

        resolved = self._resolve_any_symbol(name, file_path)
        if resolved is None:
            return f"Symbol '{name}' not found"
        kind, meta, sym = resolved

        start = sym.line_range.start
        end = sym.line_range.end
        if not (start <= line <= end):
            return (
                f"Error: line {line} is outside symbol '{name}' (lines {start}-{end})"
            )

        body_lines = meta.lines[start - 1 : end]
        source = "\n".join(body_lines)

        # backward_slice works on 1-based lines relative to *source*, so map.
        relative_line = line - start + 1
        result = backward_slice(source, variable, relative_line)

        if not result.lines:
            return (
                f"Backward slice: {variable}@{line} in {name} -- "
                f"no defining statements found (variable may be a parameter or undefined)"
            )

        header = [
            f"Backward slice: {variable}@{line} in {name} ({meta.source_name})",
            f"{result.reduction_pct}% reduction "
            f"({len(result.lines)} lines / {result.total_lines} total in symbol)",
            "-" * 50,
        ]
        body: list[str] = []
        for rel_ln, src in zip(result.lines, result.source_lines):
            abs_ln = rel_ln + start - 1
            marker = "  <- target" if abs_ln == line else ""
            body.append(f"  {abs_ln:4d}: {src}{marker}")
        return "\n".join(header + body)

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _get_symbol_source(
        self, name: str, kind: str, file_path: str | None = None, max_lines: int = 0
    ) -> str:
        """Shared helper for get_function_source / get_class_source.

        kind is "function" or "class", controlling which file-level query
        and which symbol collection to search.
        """
        index = self.index
        file_qfn_key = f"get_{kind}_source"
        source: str | None = None

        if file_path is not None:
            meta = _resolve_file(index, file_path)
            if meta is None:
                return f"Error: file '{file_path}' not found in index"
            file_funcs = create_file_query_functions(meta)
            source = file_funcs[file_qfn_key](name)
        else:
            # Try symbol table first
            if name in index.symbol_table:
                resolved_path = index.symbol_table[name]
                meta = _resolve_file(index, resolved_path)
                if meta is not None:
                    file_funcs = create_file_query_functions(meta)
                    result = file_funcs[file_qfn_key](name)
                    if not result.startswith("Error:"):
                        source = result
            # Fallback: linear search
            if source is None:
                for path, meta in sorted(index.files.items()):
                    symbols = meta.functions if kind == "function" else meta.classes
                    for sym in symbols:
                        match = (
                            (sym.name == name or getattr(sym, "qualified_name", None) == name)
                            if kind == "function"
                            else sym.name == name
                        )
                        if match:
                            source = "\n".join(
                                meta.lines[sym.line_range.start - 1 : sym.line_range.end]
                            )
                            break
                    if source is not None:
                        break

        if source is None:
            return f"Error: {kind} '{name}' not found in project"
        if max_lines > 0:
            lines = source.split("\n")
            if len(lines) > max_lines:
                source = "\n".join(lines[:max_lines])
                source += f"\n... (truncated to {max_lines} lines)"
        return source

    def _func_result(self, func, path, meta):
        preview_lines = meta.lines[func.line_range.start - 1 : func.line_range.start + 19]
        return {
            "name": func.qualified_name,
            "file": path,
            "line": func.line_range.start,
            "end_line": func.line_range.end,
            "type": "method" if func.is_method else "function",
            "signature": f"def {func.name}({', '.join(func.parameters)})",
            "source_preview": "\n".join(preview_lines),
        }

    def _class_result(self, cls, path, meta):
        preview_lines = meta.lines[cls.line_range.start - 1 : cls.line_range.start + 19]
        return {
            "name": cls.name,
            "file": path,
            "line": cls.line_range.start,
            "end_line": cls.line_range.end,
            "type": "class",
            "methods": [m.name for m in cls.methods],
            "bases": cls.base_classes,
            "source_preview": "\n".join(preview_lines),
        }

    def _resolve_symbol_info(self, name: str) -> dict:
        """Resolve a symbol name to rich info (file, line, signature, preview)."""
        index = self.index
        # Try symbol table first
        if name in index.symbol_table:
            path = index.symbol_table[name]
            meta = _resolve_file(index, path)
            if meta is not None:
                for func in meta.functions:
                    if func.name == name or func.qualified_name == name:
                        return self._func_result(func, path, meta)
                for cls in meta.classes:
                    if cls.name == name:
                        return self._class_result(cls, path, meta)
        # Fallback: search all files
        for path, meta in sorted(index.files.items()):
            for func in meta.functions:
                if func.name == name or func.qualified_name == name:
                    return self._func_result(func, path, meta)
            for cls in meta.classes:
                if cls.name == name:
                    return self._class_result(cls, path, meta)
        return {"name": name}

    def _resolve_dep_name(self, name: str) -> tuple[str, set | None]:
        """Look up name in reverse dependency graph, falling back to class name for dotted methods."""
        deps = self.index.reverse_dependency_graph.get(name)
        if deps is not None:
            return name, deps
        # For "Class.method", fall back to dependents of "Class"
        if "." in name:
            class_name = name.split(".")[0]
            deps = self.index.reverse_dependency_graph.get(class_name)
            if deps is not None:
                return class_name, deps
        return name, None

    def _get_communities(self) -> dict[str, str]:
        if self._communities is None:
            self._communities = compute_communities(self.index)
        return self._communities


def create_project_query_functions(index: ProjectIndex) -> dict[str, Callable]:
    """Create query functions bound to a project-wide index.

    Returns a dict mapping function names to callables. Each function returns
    plain dicts or strings suitable for printing in a REPL.

    This is a thin wrapper around ``ProjectQueryEngine`` for backward compatibility.
    """
    return ProjectQueryEngine(index).as_dict()


# ---------------------------------------------------------------------------
# System prompt instructions
# ---------------------------------------------------------------------------

STRUCTURAL_QUERY_INSTRUCTIONS = """\
Your REPL environment includes structural navigation functions for the codebase.
These let you explore code structure without reading entire files into context.

PROJECT OVERVIEW:
  get_project_summary() -> str                  # File count, packages, entry points
  list_files(pattern?) -> list[str]             # List files, optional glob (e.g. "*.py")

FILE STRUCTURE:
  get_structure_summary(file?) -> str           # Functions, classes, line counts for a file
  get_lines(file, start, end) -> str            # Specific lines (1-indexed, inclusive)

CODE NAVIGATION:
  get_functions(file?) -> list[dict]            # All functions: name, lines, params
  get_classes(file?) -> list[dict]              # All classes: name, lines, methods, bases
  get_imports(file?) -> list[dict]              # All imports: module, names, line
  get_function_source(name, file?) -> str       # Full source of a specific function
  get_class_source(name, file?) -> str          # Full source of a specific class

DEPENDENCY ANALYSIS:
  find_symbol(name) -> dict                     # Where is this symbol defined?
  get_dependencies(name) -> list[dict]           # What does it call/use? (rich info per dep)
  get_dependents(name) -> list[dict]             # What calls/uses it? (rich info per dep)
  get_call_chain(from, to) -> list              # Shortest dependency path
  get_change_impact(name) -> dict               # Transitive impact of changing this symbol
  get_file_dependencies(file) -> list[str]      # Files this file imports from
  get_file_dependents(file) -> list[str]        # Files that import from this file

SEARCH:
  search_codebase(pattern) -> list[dict]        # Regex across all files (max 100 results)

STRATEGY: Start with get_project_summary() to understand the repo layout. Use
get_structure_summary(file) to understand a file before reading it. Use
get_function_source(name) to read only what you need. Use dependency analysis
to trace connections. This is dramatically cheaper than reading entire files.
"""
