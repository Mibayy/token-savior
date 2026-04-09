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
    ProjectIndex,
    StructuralMetadata,
)


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
                return "\n".join(
                    metadata.lines[f.line_range.start - 1 : f.line_range.end]
                )
        return f"Error: function '{name}' not found"

    def get_class_source(name: str) -> str:
        """Source of a class by name."""
        for cls in metadata.classes:
            if cls.name == name:
                return "\n".join(
                    metadata.lines[cls.line_range.start - 1 : cls.line_range.end]
                )
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
                return "\n".join(
                    metadata.lines[sec.line_range.start - 1 : sec.line_range.end]
                )
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


def create_project_query_functions(index: ProjectIndex) -> dict[str, Callable]:
    """Create query functions bound to a project-wide index.

    Returns a dict mapping function names to callables. Each function returns
    plain dicts or strings suitable for printing in a REPL.
    """

    def get_project_summary() -> str:
        """Compact project overview: counts + top packages only."""
        parts = [
            f"Project: {index.root_path}",
            f"Files: {index.total_files}, Lines: {index.total_lines}, "
            f"Functions: {index.total_functions}, Classes: {index.total_classes}",
        ]

        # Top-level packages only (deduplicated)
        top_packages = sorted({
            p.split("/")[0]
            for p in index.files
            if "/" in p
        })
        if top_packages:
            parts.append(f"Packages ({len(top_packages)}): {', '.join(top_packages[:15])}")
            if len(top_packages) > 15:
                parts.append(f"  ... and {len(top_packages) - 15} more")

        # Counts per type, no individual names
        class_count = sum(len(meta.classes) for meta in index.files.values())
        func_count = sum(
            sum(1 for f in meta.functions if not f.is_method)
            for meta in index.files.values()
        )
        if class_count:
            parts.append(f"Classes: {class_count} total")
        if func_count:
            parts.append(f"Top-level functions: {func_count} total")

        return "\n".join(parts)

    def list_files(pattern: str | None = None, max_results: int = 0) -> list[str]:
        """List indexed files, optional glob filter (using fnmatch)."""
        paths = sorted(index.files.keys())
        if pattern:
            paths = [p for p in paths if fnmatch.fnmatch(p, pattern)]
        if max_results > 0:
            paths = paths[:max_results]
        return paths

    def get_structure_summary(file_path: str | None = None) -> str:
        """Per-file or project-level summary."""
        if file_path is None:
            return get_project_summary()
        meta = _resolve_file(index, file_path)
        if meta is None:
            return f"Error: file '{file_path}' not found in index"
        file_funcs = create_file_query_functions(meta)
        return file_funcs["get_structure_summary"]()

    def get_lines(file_path: str, start: int, end: int) -> str:
        """Lines from a specific file."""
        meta = _resolve_file(index, file_path)
        if meta is None:
            return f"Error: file '{file_path}' not found in index"
        file_funcs = create_file_query_functions(meta)
        return file_funcs["get_lines"](start, end)

    def get_functions(file_path: str | None = None, max_results: int = 0) -> list[dict]:
        """Functions in a file, or all functions across the project."""
        if file_path is not None:
            meta = _resolve_file(index, file_path)
            if meta is None:
                return [{"error": f"file '{file_path}' not found in index"}]
            file_funcs = create_file_query_functions(meta)
            result = file_funcs["get_functions"]()
        else:
            # All functions across project
            result = []
            for path, meta in sorted(index.files.items()):
                for f in meta.functions:
                    result.append({
                        "name": f.name,
                        "qualified_name": f.qualified_name,
                        "lines": [f.line_range.start, f.line_range.end],
                        "params": f.parameters,
                        "is_method": f.is_method,
                        "parent_class": f.parent_class,
                        "file": path,
                    })
        if max_results > 0:
            result = result[:max_results]
        return result

    def get_classes(file_path: str | None = None, max_results: int = 0) -> list[dict]:
        """Classes in a file or across the project."""
        if file_path is not None:
            meta = _resolve_file(index, file_path)
            if meta is None:
                return [{"error": f"file '{file_path}' not found in index"}]
            file_funcs = create_file_query_functions(meta)
            result = file_funcs["get_classes"]()
        else:
            result = []
            for path, meta in sorted(index.files.items()):
                for cls in meta.classes:
                    result.append({
                        "name": cls.name,
                        "lines": [cls.line_range.start, cls.line_range.end],
                        "methods": [m.name for m in cls.methods],
                        "bases": cls.base_classes,
                        "file": path,
                    })
        if max_results > 0:
            result = result[:max_results]
        return result

    def get_imports(file_path: str | None = None, max_results: int = 0) -> list[dict]:
        """Imports in a file or across the project."""
        if file_path is not None:
            meta = _resolve_file(index, file_path)
            if meta is None:
                return [{"error": f"file '{file_path}' not found in index"}]
            file_funcs = create_file_query_functions(meta)
            result = file_funcs["get_imports"]()
        else:
            result = []
            for path, meta in sorted(index.files.items()):
                for imp in meta.imports:
                    result.append({
                        "module": imp.module,
                        "names": imp.names,
                        "line": imp.line_number,
                        "is_from_import": imp.is_from_import,
                        "file": path,
                    })
        if max_results > 0:
            result = result[:max_results]
        return result

    def _get_symbol_source(
        name: str, kind: str, file_path: str | None = None, max_lines: int = 0
    ) -> str:
        """Shared helper for get_function_source / get_class_source.

        kind is "function" or "class", controlling which file-level query
        and which symbol collection to search.
        """
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

    def get_function_source(
        name: str, file_path: str | None = None, max_lines: int = 0
    ) -> str:
        """Source of a function, uses symbol_table to find file if not specified."""
        return _get_symbol_source(name, "function", file_path, max_lines)

    def get_class_source(
        name: str, file_path: str | None = None, max_lines: int = 0
    ) -> str:
        """Source of a class, uses symbol_table to find file if not specified."""
        return _get_symbol_source(name, "class", file_path, max_lines)

    def _func_result(func, path, meta):
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

    def _class_result(cls, path, meta):
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

    def _resolve_symbol_info(name: str) -> dict:
        """Resolve a symbol name to rich info (file, line, signature, preview)."""
        # Try symbol table first
        if name in index.symbol_table:
            path = index.symbol_table[name]
            meta = _resolve_file(index, path)
            if meta is not None:
                for func in meta.functions:
                    if func.name == name or func.qualified_name == name:
                        return _func_result(func, path, meta)
                for cls in meta.classes:
                    if cls.name == name:
                        return _class_result(cls, path, meta)
        # Fallback: search all files
        for path, meta in sorted(index.files.items()):
            for func in meta.functions:
                if func.name == name or func.qualified_name == name:
                    return _func_result(func, path, meta)
            for cls in meta.classes:
                if cls.name == name:
                    return _class_result(cls, path, meta)
        return {"name": name}

    def find_symbol(name: str) -> dict:
        """Find where a symbol is defined: {file, line, type, signature, source_preview}."""
        result = _resolve_symbol_info(name)
        if "file" not in result:
            return {"error": f"symbol '{name}' not found"}
        return result

    def get_dependencies(name: str, max_results: int = 0) -> list[dict]:
        """What this function/class references (from global_dependency_graph)."""
        deps = index.global_dependency_graph.get(name)
        if deps is None:
            return [{"error": f"'{name}' not found in dependency graph"}]
        result = sorted(deps)
        if max_results > 0:
            result = result[:max_results]
        return [_resolve_symbol_info(dep) for dep in result]

    def _resolve_dep_name(name: str) -> tuple[str, set | None]:
        """Look up name in reverse dependency graph, falling back to class name for dotted methods."""
        deps = index.reverse_dependency_graph.get(name)
        if deps is not None:
            return name, deps
        # For "Class.method", fall back to dependents of "Class"
        if "." in name:
            class_name = name.split(".")[0]
            deps = index.reverse_dependency_graph.get(class_name)
            if deps is not None:
                return class_name, deps
        return name, None

    def get_dependents(name: str, max_results: int = 0) -> list[dict]:
        """What references this function/class (from reverse_dependency_graph)."""
        resolved_name, deps = _resolve_dep_name(name)
        if deps is None:
            return [{"error": f"'{name}' not found in reverse dependency graph"}]
        result = sorted(deps)
        if max_results > 0:
            result = result[:max_results]
        return [_resolve_symbol_info(dep) for dep in result]

    def get_call_chain(from_name: str, to_name: str) -> dict:
        """Shortest path in dependency graph (BFS).

        Returns {chain: [{name, file, line, end_line, type, signature, source_preview}, ...]}
        with rich info for each hop, so callers don't need follow-up lookups.
        """
        if from_name not in index.global_dependency_graph:
            return {"error": f"'{from_name}' not found in dependency graph"}
        if from_name == to_name:
            info = _resolve_symbol_info(from_name)
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
            info = _resolve_symbol_info(name)
            info.setdefault("name", name)
            chain.append(info)

        return {"chain": chain}

    def get_file_dependencies(file_path: str, max_results: int = 0) -> list[str]:
        """What files this file imports from (from import_graph)."""
        deps = index.import_graph.get(file_path)
        if deps is None:
            return [f"Error: '{file_path}' not found in import graph"]
        result = sorted(deps)
        if max_results > 0:
            result = result[:max_results]
        return result

    def get_file_dependents(file_path: str, max_results: int = 0) -> list[str]:
        """What files import from this file (from reverse_import_graph)."""
        deps = index.reverse_import_graph.get(file_path)
        if deps is None:
            return [f"Error: '{file_path}' not found in reverse import graph"]
        result = sorted(deps)
        if max_results > 0:
            result = result[:max_results]
        return result

    def search_codebase(pattern: str, max_results: int = 100) -> list[dict]:
        """Regex across all files, returns [{file, line_number, content}]."""
        try:
            regex = re.compile(pattern)
        except re.error as e:
            return [{"error": f"Invalid regex: {e}"}]
        limit = max_results if max_results > 0 else 0
        results = []
        for path in sorted(index.files.keys()):
            meta = index.files[path]
            for i, line in enumerate(meta.lines):
                if regex.search(line):
                    results.append({
                        "file": path,
                        "line_number": i + 1,
                        "content": line,
                    })
                    if limit and len(results) >= limit:
                        return results
        return results

    def get_change_impact(
        name: str, max_direct: int = 0, max_transitive: int = 0
    ) -> dict:
        """Direct and transitive dependents of a symbol, each with confidence and depth."""
        resolved_name, direct = _resolve_dep_name(name)
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
            info = _resolve_symbol_info(sym)
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

        return {
            "direct": direct_entries,
            "transitive": transitive_entries,
        }

    # ------------------------------------------------------------------
    # v3: Route Map (Next.js App Router + Express-style)
    # ------------------------------------------------------------------

    def get_routes(max_results: int = 0) -> list[dict]:
        """Detect API routes and pages from the project structure.
        Returns [{route, file, methods, type}] for Next.js App Router,
        Express, and similar frameworks."""
        routes: list[dict] = []
        for path, meta in index.files.items():
            # Next.js App Router: app/**/route.ts → API route
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
                routes.append({
                    "route": route_path,
                    "file": path,
                    "methods": methods or ["GET"],
                    "type": "api",
                })
            # Next.js App Router: app/**/page.tsx → Page
            elif "/page." in path and "app/" in path:
                route_path = path
                for prefix in ("app/", "src/app/"):
                    if prefix in route_path:
                        route_path = "/" + route_path.split(prefix, 1)[1]
                        break
                route_path = route_path.rsplit("/page.", 1)[0]
                if not route_path:
                    route_path = "/"
                routes.append({
                    "route": route_path,
                    "file": path,
                    "methods": [],
                    "type": "page",
                })
            # Next.js App Router: app/**/layout.tsx → Layout
            elif "/layout." in path and "app/" in path:
                route_path = path
                for prefix in ("app/", "src/app/"):
                    if prefix in route_path:
                        route_path = "/" + route_path.split(prefix, 1)[1]
                        break
                route_path = route_path.rsplit("/layout.", 1)[0]
                if not route_path:
                    route_path = "/"
                routes.append({
                    "route": route_path,
                    "file": path,
                    "methods": [],
                    "type": "layout",
                })
        routes.sort(key=lambda r: (r["type"], r["route"]))
        if max_results > 0:
            routes = routes[:max_results]
        return routes

    # ------------------------------------------------------------------
    # v3: Env var cross-reference
    # ------------------------------------------------------------------

    def get_env_usage(var_name: str, max_results: int = 0) -> list[dict]:
        """Find all references to an environment variable across the codebase.
        Searches for process.env.VAR, os.environ["VAR"], os.getenv("VAR"),
        and ${{ secrets.VAR }} patterns."""
        results: list[dict] = []
        for path, meta in index.files.items():
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
                    elif line.strip().startswith(var_name + "=") or line.strip().startswith(f'"{var_name}"'):
                        usage_type = "definition"
                    elif "printf" in line and var_name in line:
                        usage_type = "env_write"
                    results.append({
                        "file": path,
                        "line": line_idx + 1,
                        "usage_type": usage_type,
                        "content": context[:200],
                    })
        results.sort(key=lambda r: (r["usage_type"], r["file"]))
        if max_results > 0:
            results = results[:max_results]
        return results

    # ------------------------------------------------------------------
    # v3: React component detection
    # ------------------------------------------------------------------

    def get_components(file_path: str | None = None, max_results: int = 0) -> list[dict]:
        """Detect React components (functions returning JSX).
        Heuristic: exported functions whose name starts with uppercase
        or are default exports in page/layout/component files."""
        components: list[dict] = []
        targets = index.files.items()
        if file_path:
            meta = index.files.get(file_path)
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
                    components.append({
                        "name": func.name,
                        "file": path,
                        "line_range": f"{func.line_range.start}-{func.line_range.end}",
                        "params": func.parameters,
                        "type": comp_type,
                    })
        components.sort(key=lambda c: (c["type"], c["file"], c["name"]))
        if max_results > 0:
            components = components[:max_results]
        return components

    # ------------------------------------------------------------------
    # v3: Feature file discovery (keyword → all related files via imports)
    # ------------------------------------------------------------------

    def get_feature_files(keyword: str, max_results: int = 0) -> list[dict]:
        """Find all files related to a feature keyword, then trace their imports
        transitively to build the complete feature file set.

        Example: get_feature_files("contrat") returns route files, components,
        lib helpers, types — everything connected to contracts."""
        kw_lower = keyword.lower()

        # Step 1: Seed files — paths or symbols containing the keyword
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
            results.append({
                "file": path,
                "role": role,
                "seed": path in seeds,
                "symbols": symbols,
                "lines": meta.total_lines,
            })
        results.sort(key=lambda r: (0 if r["seed"] else 1, r["role"], r["file"]))
        if max_results > 0:
            results = results[:max_results]
        return results

    def get_entry_points(max_results: int = 20) -> list[dict]:
        """Score functions by likelihood of being execution entry points.
        Returns [{name, file, line, score, reasons, params}] sorted by score desc."""
        return score_entry_points(index, max_results=max_results)

    # Lazy-computed communities (computed once on first call)
    _communities: dict[str, str] | None = None

    def _get_communities() -> dict[str, str]:
        nonlocal _communities
        if _communities is None:
            _communities = compute_communities(index)
        return _communities

    def get_symbol_cluster(name: str, max_members: int = 30) -> dict:
        """Get the functional cluster for a symbol — all closely related symbols
        grouped by community detection on the dependency graph.
        Returns {community_id, queried_symbol, size, members: [{name, file, line, type}]}."""
        return get_cluster_for_symbol(name, _get_communities(), index, max_members=max_members)

    return {
        "get_project_summary": get_project_summary,
        "list_files": list_files,
        "get_structure_summary": get_structure_summary,
        "get_lines": get_lines,
        "get_functions": get_functions,
        "get_classes": get_classes,
        "get_imports": get_imports,
        "get_function_source": get_function_source,
        "get_class_source": get_class_source,
        "find_symbol": find_symbol,
        "get_dependencies": get_dependencies,
        "get_dependents": get_dependents,
        "get_call_chain": get_call_chain,
        "get_file_dependencies": get_file_dependencies,
        "get_file_dependents": get_file_dependents,
        "search_codebase": search_codebase,
        "get_change_impact": get_change_impact,
        "get_routes": get_routes,
        "get_env_usage": get_env_usage,
        "get_components": get_components,
        "get_feature_files": get_feature_files,
        "get_entry_points": get_entry_points,
        "get_symbol_cluster": get_symbol_cluster,
    }


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
