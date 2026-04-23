"""Structural query API for single-file and project-wide codebase navigation.

Provides factory functions that create dictionaries of query functions
bound to a StructuralMetadata (single file) or ProjectIndex (project-wide).
All functions return plain dicts/strings for easy use in a REPL.
"""

from __future__ import annotations

import fnmatch
import os
import re
from collections import defaultdict, deque
from functools import partial
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

def _split_signature_suffix(name: str) -> tuple[str, str]:
    if name.endswith(")") and "(" in name:
        base, _, suffix = name.rpartition("(")
        return base, f"({suffix}"
    return name, ""


def _function_aliases(func) -> set[str]:
    aliases = {func.name, func.qualified_name}
    base_name, signature_suffix = _split_signature_suffix(func.qualified_name)
    aliases.add(base_name)
    if func.is_method and func.parent_class:
        aliases.add(f"{func.parent_class}.{func.name}")
        if signature_suffix:
            aliases.add(f"{func.parent_class}.{func.name}{signature_suffix}")
    return aliases


def _function_matches_name(func, name: str) -> bool:
    return name in _function_aliases(func)


def _graph_name_matches(candidate: str, name: str) -> bool:
    if candidate == name:
        return True
    if candidate.endswith(f".{name}"):
        return True
    candidate_base, _ = _split_signature_suffix(candidate)
    name_base, _ = _split_signature_suffix(name)
    if candidate_base == name_base:
        return True
    if candidate_base.endswith(f".{name_base}"):
        return True
    return False


def _is_constructor_symbol(name: str) -> bool:
    base_name, signature = _split_signature_suffix(name)
    if not signature or "." not in base_name:
        return False
    owner_name, _, method_name = base_name.rpartition(".")
    return bool(owner_name) and owner_name.rsplit(".", 1)[-1] == method_name


def _find_matching_functions(functions, name: str) -> list:
    return [func for func in functions if _function_matches_name(func, name)]


def _resolve_unique_function(functions, name: str):
    matches = _find_matching_functions(functions, name)
    if not matches:
        return None, "missing"
    exact_matches = [func for func in matches if func.qualified_name == name]
    if exact_matches:
        return exact_matches[0], None
    if len(matches) == 1:
        return matches[0], None
    return None, "ambiguous"


# ---------------------------------------------------------------------------
# Single-file query functions
# ---------------------------------------------------------------------------


def _file_structure_summary_impl(metadata: StructuralMetadata) -> str:
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


def _file_get_lines_impl(metadata: StructuralMetadata, start: int, end: int) -> str:
    """Get specific lines (1-indexed, inclusive)."""
    if start < 1:
        return "Error: start must be >= 1"
    if end > metadata.total_lines:
        end = metadata.total_lines
    if start > end:
        return f"Error: start ({start}) > end ({end})"
    return "\n".join(metadata.lines[start - 1 : end])


def _file_line_count_impl(metadata: StructuralMetadata) -> int:
    return metadata.total_lines


def _file_get_functions_impl(metadata: StructuralMetadata) -> list[dict]:
    return [
        {
            "name": f.name,
            "qualified_name": f.qualified_name,
            "lines": list(_effective_function_range(metadata, f)),
            "params": f.parameters,
            "is_method": f.is_method,
            "parent_class": f.parent_class,
        }
        for f in metadata.functions
    ]


def _file_get_classes_impl(metadata: StructuralMetadata) -> list[dict]:
    return [
        {
            "name": cls.name,
            "qualified_name": cls.qualified_name or cls.name,
            "lines": [cls.line_range.start, cls.line_range.end],
            "methods": sorted({m.name for m in cls.methods}),
            "method_signatures": [m.qualified_name for m in cls.methods],
            "bases": cls.base_classes,
        }
        for cls in metadata.classes
    ]


def _file_get_imports_impl(metadata: StructuralMetadata) -> list[dict]:
    return [
        {
            "module": imp.module,
            "names": imp.names,
            "line": imp.line_number,
            "is_from_import": imp.is_from_import,
        }
        for imp in metadata.imports
    ]


def _file_function_source_impl(metadata: StructuralMetadata, name: str) -> str:
    """Source of a function by name (searches top-level and methods)."""
    func, error = _resolve_unique_function(metadata.functions, name)
    if error == "ambiguous":
        return f"Error: function '{name}' is ambiguous; use a fully qualified signature"
    if func is None:
        return f"Error: function '{name}' not found"
    start, end = _effective_function_range(metadata, func)
    return "\n".join(metadata.lines[start - 1 : end])


def _file_class_source_impl(
    metadata: StructuralMetadata, name: str, level: int = 0
) -> str:
    """Source of a class by name."""
    for cls in metadata.classes:
        if cls.name == name or cls.qualified_name == name:
            if level == 1:
                return _format_l1(cls)
            if level == 2:
                body = "\n".join(
                    metadata.lines[cls.line_range.start - 1 : cls.line_range.end]
                )
                return _format_l2(cls, body)
            if level == 3:
                return _format_l3(cls)
            return "\n".join(
                metadata.lines[cls.line_range.start - 1 : cls.line_range.end]
            )
    return f"Error: class '{name}' not found"


def _file_get_sections_impl(metadata: StructuralMetadata) -> list[dict]:
    return [
        {
            "title": sec.title,
            "level": sec.level,
            "lines": [sec.line_range.start, sec.line_range.end],
        }
        for sec in metadata.sections
    ]


def _file_section_content_impl(metadata: StructuralMetadata, title: str) -> str:
    for sec in metadata.sections:
        if sec.title == title:
            return "\n".join(
                metadata.lines[sec.line_range.start - 1 : sec.line_range.end]
            )
    return f"Error: section '{title}' not found"


def _file_resolve_symbol_impl(metadata: StructuralMetadata, name: str) -> dict:
    """Resolve a symbol name to rich info from the file metadata."""
    func, error = _resolve_unique_function(metadata.functions, name)
    if error == "ambiguous":
        return {
            "name": name,
            "error": f"function '{name}' is ambiguous; use a fully qualified signature",
        }
    if func is not None:
        return {
            "name": func.qualified_name,
            "file": metadata.source_name,
            "line": func.line_range.start,
            "end_line": func.line_range.end,
            "type": "method" if func.is_method else "function",
        }
    for cls in metadata.classes:
        if cls.name == name or cls.qualified_name == name:
            return {
                "name": cls.qualified_name or cls.name,
                "file": metadata.source_name,
                "line": cls.line_range.start,
                "end_line": cls.line_range.end,
                "type": "class",
            }
    return {"name": name}


def _file_get_dependencies_impl(
    metadata: StructuralMetadata,
    resolve_symbol: Callable[[str], dict],
    name: str,
) -> list[dict]:
    """What this function/class references."""
    resolved_name = name
    resolved_class = None
    if name not in metadata.dependency_graph:
        func, error = _resolve_unique_function(metadata.functions, name)
        if error == "ambiguous":
            return [{"error": f"function '{name}' is ambiguous; use a fully qualified signature"}]
        if func is not None:
            resolved_name = func.qualified_name
        else:
            for cls in metadata.classes:
                if cls.name == name or cls.qualified_name == name:
                    resolved_name = cls.qualified_name or cls.name
                    resolved_class = cls
                    break
    deps = metadata.dependency_graph.get(resolved_name)
    if resolved_class is not None:
        aggregated_deps = set(deps or [])
        for method in resolved_class.methods:
            aggregated_deps.update(
                metadata.dependency_graph.get(method.qualified_name, [])
            )
        deps = sorted(aggregated_deps)
    if deps is None:
        return [{"error": f"'{name}' not found in dependency graph"}]
    return [resolve_symbol(dep) for dep in sorted(deps)]


def _file_get_dependents_impl(
    metadata: StructuralMetadata,
    resolve_symbol: Callable[[str], dict],
    name: str,
) -> list[dict]:
    """What references this function/class."""
    resolved_name = name
    resolved_class = None
    if name not in metadata.dependency_graph:
        func, error = _resolve_unique_function(metadata.functions, name)
        if error == "ambiguous":
            return [{"error": f"function '{name}' is ambiguous; use a fully qualified signature"}]
        if func is not None:
            resolved_name = func.qualified_name
        else:
            for cls in metadata.classes:
                if cls.name == name or cls.qualified_name == name:
                    resolved_name = cls.qualified_name or cls.name
                    break
    resolved_targets = {resolved_name}
    if resolved_class is not None:
        for method in resolved_class.methods:
            resolved_targets.add(method.qualified_name)
    result = []
    for source, targets in metadata.dependency_graph.items():
        if any(target in targets for target in resolved_targets):
            result.append(source)
    return [resolve_symbol(dep) for dep in sorted(result)]


def _file_search_lines_impl(
    metadata: StructuralMetadata, pattern: str
) -> list[dict]:
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


def create_file_query_functions(metadata: StructuralMetadata) -> dict[str, Callable]:
    """Create query functions bound to a single file's structural metadata.

    Returns a dict mapping function names to callables. Each function returns
    plain dicts or strings suitable for printing in a REPL.
    """
    resolve_symbol = partial(_file_resolve_symbol_impl, metadata)
    return {
        "get_structure_summary": partial(_file_structure_summary_impl, metadata),
        "get_lines": partial(_file_get_lines_impl, metadata),
        "get_line_count": partial(_file_line_count_impl, metadata),
        "get_functions": partial(_file_get_functions_impl, metadata),
        "get_classes": partial(_file_get_classes_impl, metadata),
        "get_imports": partial(_file_get_imports_impl, metadata),
        "get_function_source": partial(_file_function_source_impl, metadata),
        "get_class_source": partial(_file_class_source_impl, metadata),
        "get_sections": partial(_file_get_sections_impl, metadata),
        "get_section_content": partial(_file_section_content_impl, metadata),
        "get_dependencies": partial(
            _file_get_dependencies_impl, metadata, resolve_symbol
        ),
        "get_dependents": partial(
            _file_get_dependents_impl, metadata, resolve_symbol
        ),
        "search_lines": partial(_file_search_lines_impl, metadata),
    }


# ---------------------------------------------------------------------------
# Project-wide query functions
# ---------------------------------------------------------------------------


def _resolve_file(index: ProjectIndex, file_path: str) -> StructuralMetadata | None:
    """Resolve a file path to its StructuralMetadata, trying exact and relative matches."""
    if file_path in index.files:
        return index.files[file_path]
    # Fast path: bare filename or tail match via basename_map.
    bmap = index.basename_map
    if bmap:
        base = os.path.basename(file_path)
        candidates = bmap.get(base)
        if candidates:
            for stored in candidates:
                if stored == file_path or stored.endswith(file_path) or file_path.endswith(stored):
                    return index.files[stored]
            # Exactly one file with that basename and the query has no other
            # path components to disambiguate: trust it.
            if len(candidates) == 1 and "/" not in file_path and os.sep not in file_path:
                return index.files[candidates[0]]
    # Fallback: linear endswith scan (covers weird cases like an absolute
    # path stored with a different prefix).
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
        out = [header]
        if sym.decorators:
            out.append(f"  decorators: {', '.join(sym.decorators)}")
        doc = _first_doc_line(sym.docstring)
        if doc:
            out.append(f"  doc: {doc[:120]}")
        out.append(f"  methods: {len(sym.methods)}")
        for method in sym.methods[:12]:
            method_doc = _first_doc_line(method.docstring)
            params = ", ".join(method.parameters)
            summary = f"  - {method.name}({params})"
            if method_doc:
                summary += f" — {method_doc[:100]}"
            out.append(summary)
        if len(sym.methods) > 12:
            out.append(f"  ... {len(sym.methods) - 12} more methods")
        return "\n".join(out)

    header = f"[L2] {sym.name}({', '.join(sym.parameters)})"
    analysis = analyze_symbol_semantics(body)
    out = [header]
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


def _display_function_start_line(meta: StructuralMetadata, func: FunctionInfo) -> int:
    start = max(1, func.line_range.start)
    end = min(len(meta.lines), func.line_range.end)
    name_pattern = re.compile(rf"\b{re.escape(func.name)}\s*\(")
    for line_no in range(start, end + 1):
        line = meta.lines[line_no - 1]
        stripped = line.strip()
        if not stripped or stripped.startswith("@"):
            continue
        if name_pattern.search(line):
            return line_no
    return func.line_range.start


def _effective_function_range(meta: StructuralMetadata, func: FunctionInfo) -> tuple[int, int]:
    start = _display_function_start_line(meta, func)
    if not str(meta.source_name).endswith(".java"):
        return start, func.line_range.end

    max_end = min(len(meta.lines), max(start, func.line_range.end))
    if start > max_end:
        return func.line_range.start, func.line_range.end

    seen_body = False
    brace_depth = 0
    for line_no in range(start, max_end + 1):
        line = meta.lines[line_no - 1]
        stripped = line.strip()
        if not stripped or stripped.startswith("@"):
            continue
        if not seen_body and "{" not in line and stripped.endswith(";"):
            return start, line_no
        for ch in line:
            if ch == "{":
                brace_depth += 1
                seen_body = True
            elif ch == "}":
                brace_depth = max(0, brace_depth - 1)
        if seen_body and brace_depth == 0:
            return start, line_no

    return start, func.line_range.end


def _infer_component_end_line(meta: StructuralMetadata, func) -> int:
    start_0 = max(0, func.line_range.start - 1)
    if func.line_range.end > func.line_range.start:
        return func.line_range.end
    if start_0 >= len(meta.lines):
        return func.line_range.end

    depth_paren = 0
    depth_brace = 0
    depth_bracket = 0
    seen_arrow = False
    seen_function_body = False

    for idx in range(start_0, min(len(meta.lines), start_0 + 120)):
        line = meta.lines[idx]
        if "=>" in line:
            seen_arrow = True
        if re.search(r"\bfunction\b", line):
            seen_function_body = True

        for ch in line:
            if ch == "(":
                depth_paren += 1
            elif ch == ")":
                depth_paren = max(0, depth_paren - 1)
            elif ch == "{":
                depth_brace += 1
            elif ch == "}":
                depth_brace = max(0, depth_brace - 1)
            elif ch == "[":
                depth_bracket += 1
            elif ch == "]":
                depth_bracket = max(0, depth_bracket - 1)

        stripped = line.strip()
        if idx == start_0:
            continue

        if stripped in {"};", "}", ");", ")", "</>"} and depth_brace == 0 and depth_paren == 0:
            return idx + 1
        if (
            (";" in line or stripped.endswith(")") or stripped.endswith("}") or stripped.endswith("};"))
            and depth_paren == 0
            and depth_brace == 0
            and depth_bracket == 0
            and (seen_arrow or seen_function_body)
        ):
            return idx + 1

    return func.line_range.end
_SPRING_HTTP_METHODS_BY_DECORATOR: dict[str, list[str]] = {
    "GetMapping": ["GET"],
    "PostMapping": ["POST"],
    "PutMapping": ["PUT"],
    "PatchMapping": ["PATCH"],
    "DeleteMapping": ["DELETE"],
}
_SPRING_REQUEST_MAPPING_RE = re.compile(r"@RequestMapping\s*\((.*?)\)", re.DOTALL)
_SPRING_GENERIC_MAPPING_RE = re.compile(r"@([A-Za-z]+Mapping)\s*(\((.*?)\))?", re.DOTALL)
_SPRING_REQUEST_METHOD_RE = re.compile(r"RequestMethod\.([A-Z]+)")
_SPRING_QUOTED_VALUE_RE = re.compile(r'"([^"]+)"')
_SPRING_VALUE_RE = re.compile(r'@Value\(\s*"[^"]*\$\{([^}:"]+)')


def _is_spring_controller(cls) -> bool:
    decorators = set(getattr(cls, "decorators", []))
    return bool(decorators & {"RestController", "Controller", "RequestMapping"})


def _spring_declaration_lines(
    meta: StructuralMetadata,
    line_range,
    max_lines: int | None = None,
) -> list[str]:
    start = max(0, line_range.start - 1)
    end = min(len(meta.lines), line_range.end)
    if max_lines is not None:
        end = min(end, start + max_lines)
    if end <= start:
        end = min(len(meta.lines), line_range.start + 1)
    return list(meta.lines[start:end])


def _extract_spring_paths(annotation_block: str) -> list[str]:
    paths = [value for value in _SPRING_QUOTED_VALUE_RE.findall(annotation_block) if value]
    return paths or [""]


def _extract_spring_request_mapping(annotation_block: str) -> tuple[list[str], list[str]]:
    methods = _SPRING_REQUEST_METHOD_RE.findall(annotation_block) or ["ANY"]
    return methods, _extract_spring_paths(annotation_block)


def _combine_route_paths(prefix_paths: list[str], method_paths: list[str]) -> list[str]:
    routes: list[str] = []
    for prefix in prefix_paths or [""]:
        for method_path in method_paths or [""]:
            prefix_clean = prefix.strip("/")
            method_clean = method_path.strip("/")
            if prefix_clean and method_clean:
                route = f"/{prefix_clean}/{method_clean}"
            elif prefix_clean:
                route = f"/{prefix_clean}"
            elif method_clean:
                route = f"/{method_clean}"
            else:
                route = "/"
            routes.append(re.sub(r"/+", "/", route))
    return routes or ["/"]


def _extract_spring_class_paths(meta: StructuralMetadata, cls) -> list[str]:
    annotation_block = getattr(cls, "decorator_details", {}).get("RequestMapping", "")
    if not annotation_block:
        return [""]
    return _extract_spring_paths(annotation_block)


def _extract_spring_method_mappings(meta: StructuralMetadata, func) -> list[tuple[list[str], list[str]]]:
    mappings: list[tuple[list[str], list[str]]] = []
    decorator_details = getattr(func, "decorator_details", {})

    for decorator, methods in _SPRING_HTTP_METHODS_BY_DECORATOR.items():
        if decorator not in getattr(func, "decorators", []):
            continue
        annotation_block = decorator_details.get(decorator, "")
        mappings.append((methods, _extract_spring_paths(annotation_block)))

    if "RequestMapping" in getattr(func, "decorators", []):
        annotation_block = decorator_details.get("RequestMapping", "")
        mappings.append(_extract_spring_request_mapping(annotation_block))

    return mappings


def _spring_method_declaration_line(meta: StructuralMetadata, func) -> int:
    start = max(0, func.line_range.start - 1)
    end = min(len(meta.lines), func.line_range.end)
    signature_pattern = re.compile(rf"\b{re.escape(func.name)}\s*\(")
    for idx in range(start, end):
        if signature_pattern.search(meta.lines[idx]):
            return idx + 1
    return func.line_range.start


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
        "search_in_symbols",
        "get_change_impact",
        "get_full_context",
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
        "find_import_cycles",
        "get_duplicate_classes",
        "find_impacted_test_files",
    ]

    def __init__(self, index: ProjectIndex):
        self.index = index
        self._communities: dict[str, str] | None = None
        self._semantic_hash_cache: dict[str, str] | None = None

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
            infra_top = {"infra", "infrastructure", "deploy", "deployment", "k8s", "kubernetes",
                         "terraform", "docker", "helm", "ansible", ".github", "ci", "cicd"}
            infra_hits = [p for p in top_packages if p.lower() in infra_top]
            if infra_hits:
                infra_techs: set[str] = set()
                for path in index.files:
                    parts_l = path.lower().split("/")
                    if not parts_l or parts_l[0] not in infra_hits:
                        continue
                    for seg in parts_l[1:]:
                        if seg in {"docker", "k8s", "kubernetes", "terraform", "helm", "ansible"}:
                            infra_techs.add(seg)
                    fn = parts_l[-1]
                    if fn.startswith("dockerfile") or fn.endswith(".dockerfile"):
                        infra_techs.add("docker")
                    if fn.endswith((".tf", ".tfvars")):
                        infra_techs.add("terraform")
                    if fn.endswith((".yaml", ".yml")) and any(
                        seg in {"k8s", "kubernetes", "manifests"} for seg in parts_l
                    ):
                        infra_techs.add("k8s")
                tech_str = f" (techs: {', '.join(sorted(infra_techs))})" if infra_techs else ""
                parts.append(f"Infra dirs: {', '.join(infra_hits)}{tech_str}")

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
            index = self.index
            package_counts: dict[str, dict[str, int]] = defaultdict(
                lambda: {"files": 0, "classes": 0, "functions": 0}
            )
            for path, meta in index.files.items():
                package = os.path.dirname(path) or "."
                package_counts[package]["files"] += 1
                package_counts[package]["classes"] += len(meta.classes)
                package_counts[package]["functions"] += sum(
                    1 for func in meta.functions if not func.is_method
                )

            parts = [f"Project Structure Summary: {index.root_path}"]
            parts.append(f"Files: {index.total_files}")
            parts.append(f"Lines: {index.total_lines}")
            parts.append(
                f"Packages/dirs: {len(package_counts)}"
            )
            parts.append("")
            parts.append("Top directories:")
            for package, counts in sorted(
                package_counts.items(),
                key=lambda item: (-item[1]["files"], item[0]),
            )[:10]:
                parts.append(
                    f"- {package}: {counts['files']} files, {counts['classes']} classes, {counts['functions']} top-level functions"
                )
            return "\n".join(parts)
        meta = _resolve_file(self.index, file_path)
        if meta is None:
            return f"Error: file '{file_path}' not found in index"
        file_funcs = create_file_query_functions(meta)
        base = file_funcs["get_structure_summary"]()

        # Resolve the actual rel_path used as key in the project index,
        # so cross-module deps work whether the caller passed a basename,
        # a suffix, or a full relative path.
        resolved_path: str | None = None
        if file_path in self.index.files:
            resolved_path = file_path
        else:
            for p in self.index.files:
                if p == file_path or p.endswith("/" + file_path) or p.endswith(file_path):
                    resolved_path = p
                    break

        if resolved_path is None:
            return base

        imports_out = sorted(self.index.import_graph.get(resolved_path, set()))
        imported_by = sorted(self.index.reverse_import_graph.get(resolved_path, set()))

        if not imports_out and not imported_by:
            return base

        parts = [base, "", "Cross-module dependencies:"]
        if imports_out:
            shown = imports_out[:10]
            suffix = f" (+{len(imports_out) - 10} more)" if len(imports_out) > 10 else ""
            parts.append(f"- Imports from: {', '.join(shown)}{suffix}")
        else:
            parts.append("- Imports from: (none in-project)")
        if imported_by:
            shown = imported_by[:10]
            suffix = f" (+{len(imported_by) - 10} more)" if len(imported_by) > 10 else ""
            parts.append(f"- Imported by: {', '.join(shown)}{suffix}")
        else:
            parts.append("- Imported by: (none in-project)")
        return "\n".join(parts)

    def get_lines(self, file_path: str, start: int, end: int) -> str:
        """Lines from a specific file."""
        meta = _resolve_file(self.index, file_path)
        if meta is None:
            return f"Error: file '{file_path}' not found in index"
        file_funcs = create_file_query_functions(meta)
        return file_funcs["get_lines"](start, end)

    def get_functions(self, file_path: str | None = None, max_results: int = 0) -> list[dict]:
        """Functions in a file, or all functions across the project."""
        from token_savior.project_indexer import is_path_excluded_from_scans

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
                if is_path_excluded_from_scans(path):
                    continue
                for f in meta.functions:
                    result.append(
                        {
                            "name": f.name,
                            "qualified_name": f.qualified_name,
                            "lines": list(_effective_function_range(meta, f)),
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
        from token_savior.project_indexer import is_path_excluded_from_scans

        if file_path is not None:
            meta = _resolve_file(self.index, file_path)
            if meta is None:
                return [{"error": f"file '{file_path}' not found in index"}]
            file_funcs = create_file_query_functions(meta)
            result = file_funcs["get_classes"]()
        else:
            result = []
            for path, meta in sorted(self.index.files.items()):
                if is_path_excluded_from_scans(path):
                    continue
                for cls in meta.classes:
                    method_names = [m.name for m in cls.methods]
                    method_signatures = [m.qualified_name for m in cls.methods]
                    result.append(
                        {
                            "name": cls.name,
                            "qualified_name": cls.qualified_name or cls.name,
                            "lines": [cls.line_range.start, cls.line_range.end],
                            "methods": sorted(set(method_names)),
                            "method_signatures": method_signatures,
                            "bases": cls.base_classes,
                            "file": path,
                        }
                    )
        if max_results > 0:
            result = result[:max_results]
        return result

    def get_imports(self, file_path: str | None = None, max_results: int = 0) -> list[dict]:
        """Imports in a file or across the project.

        When a file is resolved but genuinely has no imports, returns a
        single descriptive marker entry (``_empty: True``) instead of a
        bare ``[]``. The bare-empty response was causing agent retry storms
        (IMPROVEMENT-SIGNALS.md: 5 'empty' get_imports calls that looked
        like failures but were files without imports).
        """
        if file_path is not None:
            meta = _resolve_file(self.index, file_path)
            if meta is None:
                return [{"error": f"file '{file_path}' not found in index"}]
            file_funcs = create_file_query_functions(meta)
            result = file_funcs["get_imports"]()
            if not result:
                resolved_path = getattr(meta, "source_name", None) or file_path
                return [{
                    "_empty": True,
                    "file": resolved_path,
                    "message": "no imports in this file",
                }]
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
            if not result:
                return [{
                    "_empty": True,
                    "file": None,
                    "message": "no imports found in the indexed project",
                }]
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
        body = self._get_symbol_source(name, "function", file_path, max_lines)
        if body.startswith("Error:"):
            return body
        resolved = self._resolve_any_symbol(name, file_path)
        if resolved is not None:
            kind, _, sym = resolved
            if kind == "function" and getattr(sym, "scaffold_kind", "") == "stub":
                return f"[scaffold: stub — body is pass/docstring/NotImplementedError]\n{body}"
        return body

    def get_class_source(
        self,
        name: str,
        file_path: str | None = None,
        max_lines: int = 0,
        level: int = 0,
    ) -> str:
        """Source of a class at the requested abstraction level (0-3).

        When ``max_lines`` is 0 (caller took the default) and the class body
        exceeds 300 lines, auto-downgrades to a method-signature summary
        (level=2 equivalent) and appends a hint. Pass an explicit
        ``max_lines`` or ``force_full`` flag elsewhere to override.
        """
        if level and level > 0:
            return self.get_symbol_abstract(name, level=level, file_path=file_path)
        source = self._get_symbol_source(name, "class", file_path, max_lines)
        if (
            max_lines == 0
            and not source.startswith("Error:")
            and source.count("\n") > 300
        ):
            abstract = self.get_symbol_abstract(name, level=2, file_path=file_path)
            return (
                f"[class too large -- {source.count(chr(10)) + 1} lines. "
                f"Showing L2 summary; pass max_lines=N for raw source.]\n{abstract}"
            )
        return source

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
            for cls in meta.classes:
                qualified_name = cls.qualified_name or cls.name
                if cls.name == name or qualified_name == name:
                    return ("class", meta, cls)
            for func in meta.functions:
                if func.name == name or func.qualified_name == name:
                    return ("function", meta, func)
            for cls in meta.classes:
                for method in cls.methods:
                    if method.name == name or method.qualified_name == name:
                        return ("function", meta, method)
        return None

    def find_symbol(self, name: str, level: int = 0) -> dict:
        """Find where a symbol is defined: {file, line, type, signature, source_preview}.

        level: 0 full (preview included), 1 no source_preview, 2 minimal {name, file, line, type}.

        Adds ``complete=True`` on success and ``scanned_files=N`` on the error
        path so callers know the scan was exhaustive and they do not need to
        fall back to grep/search_codebase.
        """
        result = self._resolve_symbol_info(name, level=level)
        if "file" not in result:
            return {
                "error": f"symbol '{name}' not found",
                "scanned_files": len(self.index.files),
                "complete": True,
            }
        result["complete"] = True
        return result

    def get_dependencies(
        self, name: str, max_results: int = 0, depth: int = 1
    ) -> list[dict]:
        """What this function/class references (from global_dependency_graph).

        ``depth`` controls transitive walking (BFS). Default 1 = direct deps only.
        When depth>1, returns a flat ordered list with each entry tagged ``depth=N``;
        the BFS visits nodes in increasing distance from the seed and stops when
        either ``max_results`` or ``depth`` is reached. Cycles are skipped.

        Appends a trailing ``{"_complete": true, "total": N}`` entry when the
        full set was walked (no truncation).
        """
        resolved_name = self._resolve_graph_symbol_name(name)
        if resolved_name is None:
            return [{"error": f"'{name}' not found in dependency graph"}]
        if depth <= 1:
            deps = self._get_aggregated_dependencies(resolved_name)
            if deps is None:
                return [{"error": f"'{name}' not found in dependency graph"}]
            result = sorted(deps)
            total = len(result)
            if max_results > 0:
                result = result[:max_results]
            entries = [self._resolve_symbol_info(dep, strip_preview=True) for dep in result]
            if max_results == 0 or len(entries) == total:
                entries.append({"_complete": True, "total": total})
            return entries

        seen: set[str] = {resolved_name}
        out: list[dict] = []
        frontier = [resolved_name]
        hit_cap = False
        for layer in range(1, depth + 1):
            next_frontier: list[str] = []
            for node in frontier:
                deps = self._get_aggregated_dependencies(node) or set()
                for dep in sorted(deps):
                    if dep in seen:
                        continue
                    seen.add(dep)
                    info = self._resolve_symbol_info(dep, strip_preview=True)
                    info["depth"] = layer
                    info["from"] = node
                    out.append(info)
                    next_frontier.append(dep)
                    if max_results > 0 and len(out) >= max_results:
                        hit_cap = True
                        break
                if hit_cap:
                    break
            if hit_cap:
                break
            frontier = next_frontier
            if not frontier:
                break
        if not hit_cap:
            out.append({"_complete": True, "total": len(out), "depth_reached": depth})
        return out

    def get_dependents(self, name: str, max_results: int = 0, max_total_chars: int = 50_000) -> list[dict]:
        """What references this function/class (from reverse_dependency_graph).

        Appends a trailing ``{"_complete": true, "total": N}`` entry when the
        full set was returned (no truncation), so callers know no further
        scanning is needed.
        """
        resolved_name, deps = self._resolve_dep_name(name)
        if deps is None:
            return [{"error": f"'{name}' not found in reverse dependency graph"}]
        result = sorted(deps)
        total = len(result)
        if max_results > 0:
            result = result[:max_results]
        entries: list[dict] = []
        chars_used = 0
        truncated = False
        for dep in result:
            entry = self._resolve_symbol_info(dep, strip_preview=True)
            entry_len = len(str(entry))
            if max_total_chars > 0 and chars_used + entry_len > max_total_chars:
                entries.append({
                    "truncated": True,
                    "message": f"... output truncated at {max_total_chars} chars. "
                    "Use max_results to narrow the scope.",
                    "shown": len(entries),
                    "total": total,
                })
                truncated = True
                break
            chars_used += entry_len
            entries.append(entry)
        if not truncated and (max_results == 0 or len(entries) == total):
            entries.append({"_complete": True, "total": total})
        return entries

    def get_call_chain(self, from_name: str, to_name: str, level: int = 2) -> dict:
        """Shortest path in dependency graph (BFS).

        ``level`` controls per-hop verbosity (default 2 — path names plus
        file/line, no source). Use level=1 for signature+file, level=0 for
        full per-hop info including source_preview.

        Returns {chain: [{name, file, line, ...}, ...]}.
        """
        resolved_from = self._resolve_graph_symbol_name(from_name)
        resolved_to = (
            self._resolve_graph_symbol_name(to_name)
            or self._resolve_exact_class_name(to_name)
            or self._resolve_symbol_info(to_name).get("name")
        )
        if resolved_from is None:
            return {"error": f"'{from_name}' not found in dependency graph"}
        if resolved_to is None:
            return {"error": f"'{to_name}' not found in dependency graph"}
        if resolved_from == resolved_to:
            info = self._resolve_symbol_info(resolved_from, level=level, strip_preview=True)
            info.setdefault("name", from_name)
            return {"chain": [info]}

        # BFS
        target_names = self._get_graph_target_names(resolved_to)
        visited = {resolved_from}
        queue: deque[list[str]] = deque([[resolved_from]])
        path_names: list[str] | None = None
        while queue:
            path = queue.popleft()
            current = path[-1]
            neighbors = self._get_call_chain_neighbors(current)
            expanded_neighbors: list[tuple[str, str]] = []
            for neighbor in neighbors:
                candidates = self._resolve_graph_candidate_names(neighbor)
                if not candidates:
                    candidates = {neighbor}
                for candidate in candidates:
                    expanded_neighbors.append((neighbor, candidate))

            for raw_neighbor, neighbor in sorted(
                expanded_neighbors,
                key=lambda item: self._call_chain_neighbor_key(item[1]),
            ):
                if neighbor in target_names or raw_neighbor in target_names:
                    path_names = path + [resolved_to if neighbor != resolved_to else neighbor]
                    break
                if neighbor not in visited and self._has_any_graph_presence(neighbor):
                    visited.update(self._resolve_graph_candidate_names(neighbor))
                    queue.append(path + [neighbor])
            if path_names is not None:
                break

        if path_names is None:
            return {"error": f"no path from '{from_name}' to '{to_name}'"}

        # Enrich each hop with file, line, signature, source preview
        chain = []
        for name in path_names:
            info = self._resolve_symbol_info(name, level=level, strip_preview=True)
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

    def search_codebase(
        self,
        pattern: str,
        max_results: int = 100,
        max_line_chars: int = 160,
        ignore_generated: bool = True,
        semantic: bool = False,
    ) -> list[dict]:
        """Regex (default) or semantic search across project files.

        Regex mode (``semantic=False``) returns ``[{file, line_number,
        content}]``. Each hit's `content` is truncated to
        ``max_line_chars`` (default 160, suffixed with "…"). Pass 0 to
        disable truncation.

        ``ignore_generated`` (default True) filters out files that are
        typically auto-generated or minified — ``*.generated.*``,
        ``*.min.*``, ``*.pb.*``, ``*.proto``, ``dist/``, ``build/``,
        ``.next/``, ``node_modules/`` — so Prisma/proto schema lookups
        don't drown in boilerplate hits. Pass False to scan everything.

        Semantic mode (``semantic=True``) treats ``pattern`` as a natural
        language description and returns the top-K matching symbols
        (Python functions/classes/methods) by embedding cosine similarity.
        Each hit carries ``{symbol, kind, file, line, score, signature,
        docstring_head}`` for disambiguation. No low-confidence warning
        is emitted — tests/benchmarks/code_retrieval showed that top1
        score distributions overlap too much between correct and wrong
        retrievals for any absolute-score warning to be informative
        (12% precision / 25% recall at best-tuned thresholds). Agents
        should always verify a semantic hit via ``find_symbol`` before
        any destructive operation, regardless of score. **Never act on a semantic hit (call / edit / delete) without
        verifying via ``find_symbol(exact_name)`` first** — semantic
        near-misses are plausible by design.

        First semantic call triggers a one-time embedding reindex of all
        project symbols (~2 min for 1000 symbols on CPU). Subsequent calls
        are fast (hash-based skip of unchanged symbols). Silent fallback
        to an empty result list when the vector stack is unavailable.
        """
        if semantic:
            return self._search_codebase_semantic(pattern, max_results)
        from token_savior.project_indexer import is_path_excluded_from_scans

        try:
            regex = re.compile(pattern)
        except re.error as e:
            return [{"error": f"Invalid regex: {e}"}]
        limit = max_results if max_results > 0 else 0
        char_cap = max_line_chars if max_line_chars and max_line_chars > 0 else 0

        _GEN_MARKERS = (
            ".generated.", ".min.", ".pb.",
            "/dist/", "/build/", "/.next/", "/node_modules/",
        )
        _GEN_SUFFIXES = (".proto",)

        def _is_generated(path: str) -> bool:
            p = "/" + path.lstrip("/")
            if any(m in p for m in _GEN_MARKERS):
                return True
            return any(path.endswith(s) for s in _GEN_SUFFIXES)

        def _trim(line: str) -> str:
            if char_cap and len(line) > char_cap:
                return line[:char_cap] + "…"
            return line

        raw_paths = self.index.sorted_paths or sorted(self.index.files.keys())
        paths = [p for p in raw_paths if not is_path_excluded_from_scans(p)]
        if ignore_generated:
            paths = [p for p in paths if not _is_generated(p)]
        files = self.index.files

        def _scan(path: str) -> list[dict]:
            meta = files[path]
            hits: list[dict] = []
            for i, line in enumerate(meta.lines):
                if regex.search(line):
                    hits.append({"file": path, "line_number": i + 1, "content": _trim(line)})
                    if limit and len(hits) >= limit:
                        break
            return hits

        if len(paths) <= 8:
            results: list[dict] = []
            for path in paths:
                for hit in _scan(path):
                    results.append(hit)
                    if limit and len(results) >= limit:
                        return results
            return results

        from concurrent.futures import ThreadPoolExecutor

        results = []
        with ThreadPoolExecutor(max_workers=4) as pool:
            BATCH = 16
            for i in range(0, len(paths), BATCH):
                batch_paths = paths[i : i + BATCH]
                for path, hits in zip(batch_paths, pool.map(_scan, batch_paths)):
                    for hit in hits:
                        results.append(hit)
                        if limit and len(results) >= limit:
                            return results
        return results

    def _search_codebase_semantic(
        self, pattern: str, max_results: int,
    ) -> list[dict]:
        """Semantic path of ``search_codebase``. Auto-reindexes symbols on
        the first call per project, then runs a k-NN query. Returns a list
        where the first item may be a ``{"warning": ...}`` dict followed
        by hit dicts, mirroring the shape of the regex path (so both modes
        stay uniform for JSON consumers).
        """
        try:
            from token_savior.memory.symbol_embeddings import (
                reindex_project_symbols,
                search_symbols_semantic,
            )
        except ImportError as exc:
            return [{"error": f"semantic search unavailable: {exc}"}]

        root = self.index.root_path
        limit = max(int(max_results), 5) if max_results else 10

        reindex = reindex_project_symbols(root)
        if reindex.get("status") != "ok":
            return [{
                "error": (
                    f"semantic index unavailable: "
                    f"{reindex.get('reason', 'unknown')}"
                ),
            }]

        result = search_symbols_semantic(pattern, root, limit=limit)
        if result.get("status") != "ok":
            return [{"error": result.get("reason", "semantic search failed")}]

        out: list[dict] = []
        if result.get("warning"):
            out.append({"warning": result["warning"]})
        out.extend(result["hits"])
        if reindex.get("indexed", 0) > 0 or reindex.get("removed", 0) > 0:
            out.append({
                "reindex_info": (
                    f"indexed={reindex['indexed']} "
                    f"skipped={reindex['skipped']} "
                    f"removed={reindex['removed']} "
                    f"elapsed_s={reindex['elapsed_s']}"
                ),
            })
        return out

    def search_in_symbols(
        self,
        pattern: str,
        max_results: int = 100,
        max_line_chars: int = 160,
    ) -> list[dict]:
        """Content search that returns STRUCTURAL locations.

        Same regex as ``search_codebase`` but each hit is annotated with the
        enclosing function/class, so the caller can jump straight to
        ``get_function_source(symbol)`` instead of pasting random line
        numbers. Lines outside any function/class are returned with
        ``symbol=None, kind="module"`` so they are still actionable.
        """
        raw_hits = self.search_codebase(pattern, max_results=max_results, max_line_chars=max_line_chars)
        if raw_hits and isinstance(raw_hits[0], dict) and "error" in raw_hits[0]:
            return raw_hits

        files = self.index.files
        out: list[dict] = []
        for hit in raw_hits:
            path = hit.get("file")
            line = hit.get("line_number")
            if not path or line is None or path not in files:
                out.append(hit)
                continue
            meta = files[path]
            enclosing_sym: str | None = None
            enclosing_kind: str = "module"
            # Methods first (more specific than classes).
            for cls in meta.classes:
                if cls.line_range.start <= line <= cls.line_range.end:
                    enclosing_sym = cls.qualified_name or cls.name
                    enclosing_kind = "class"
                    for m in cls.methods:
                        if m.line_range.start <= line <= m.line_range.end:
                            enclosing_sym = m.qualified_name
                            enclosing_kind = "method"
                            break
                    break
            if enclosing_sym is None:
                for func in meta.functions:
                    if func.is_method:
                        continue
                    if func.line_range.start <= line <= func.line_range.end:
                        enclosing_sym = func.qualified_name
                        enclosing_kind = "function"
                        break
            out.append({
                **hit,
                "symbol": enclosing_sym,
                "kind": enclosing_kind,
            })
        return out

    def get_change_impact(
        self, name: str, max_direct: int = 0, max_transitive: int = 0, max_total_chars: int = 50_000
    ) -> dict:
        """Direct and transitive dependents of a symbol, each with confidence and depth."""
        resolved_name, direct = self._resolve_dep_name(name)
        if direct is None:
            return {"error": f"'{name}' not found in reverse dependency graph"}

        # BFS tracking depth per symbol
        depth_map: dict[str, int] = {}
        queue: deque[tuple[str, int]] = deque((sym, 1) for sym in direct)
        visited: set[str] = set(direct) | self._get_class_symbol_aliases(resolved_name)
        for sym in direct:
            depth_map[sym] = 1

        while queue:
            current, depth = queue.popleft()
            next_deps = self._get_aggregated_dependents(current) or set()
            for dep in next_deps:
                if dep not in visited:
                    visited.add(dep)
                    depth_map[dep] = depth + 1
                    queue.append((dep, depth + 1))

        def _make_entry(sym: str) -> dict:
            d = depth_map[sym]
            confidence = max(0.05, 0.6 ** (d - 1))
            info = self._resolve_symbol_info(sym, level=2, strip_preview=True)
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

    def get_full_context(
        self,
        name: str,
        depth: int = 1,
        max_lines: int = 200,
        brief: bool = False,
    ) -> dict:
        """Symbol location + source + optional dep graph, in one call.

        Replaces the frequent find_symbol -> get_function_source -> get_dependents
        chain (see IMPROVEMENT-SIGNALS.md: 46 occurrences across bench).

        depth=0 : {symbol, source}
        depth=1 : {symbol, source, dependencies, dependents}   (default)
        depth=2 : + {change_impact}

        ``brief=True`` tightens the dependency lists (12 entries, 4000 char budget)
        so the structural view fits in a few hundred tokens when the agent only
        needs a quick orientation.
        """
        symbol = self.find_symbol(name, level=2)
        if "error" in symbol or "file" not in symbol:
            return {"error": symbol.get("error", f"symbol '{name}' not found")}

        result: dict = {"symbol": symbol}

        sym_type = symbol.get("type")
        try:
            if sym_type == "class":
                result["source"] = self.get_class_source(name, max_lines=max_lines)
            else:
                result["source"] = self.get_function_source(name, max_lines=max_lines)
        except Exception as exc:  # pragma: no cover
            result["source"] = {"error": str(exc)}

        if depth <= 0:
            return result

        deps_cap = 12 if brief else 20
        chars_cap = 4000 if brief else 8000
        try:
            result["dependencies"] = self.get_dependencies(name, max_results=deps_cap)
        except Exception as exc:  # pragma: no cover
            result["dependencies"] = [{"error": str(exc)}]
        try:
            result["dependents"] = self.get_dependents(
                name, max_results=deps_cap, max_total_chars=chars_cap
            )
        except Exception as exc:  # pragma: no cover
            result["dependents"] = [{"error": str(exc)}]

        if depth >= 2:
            try:
                result["change_impact"] = self.get_change_impact(
                    name,
                    max_direct=deps_cap,
                    max_transitive=30 if not brief else 15,
                    max_total_chars=chars_cap,
                )
            except Exception as exc:  # pragma: no cover
                result["change_impact"] = {"error": str(exc)}

        return result

    def find_impacted_test_files(
        self,
        symbol_names: list[str] | None = None,
        changed_files: list[str] | None = None,
        max_tests: int = 10,
    ) -> dict:
        """Thin wrapper so edit_context can call impacted-test inference via qfns."""
        from token_savior.impacted_tests import (
            find_impacted_test_files as _find_impacted,
        )

        return _find_impacted(
            self.index,
            changed_files=changed_files,
            symbol_names=symbol_names,
            max_tests=max_tests,
        )

    # ------------------------------------------------------------------
    # v3: Route Map (Next.js App Router + Express-style)
    # ------------------------------------------------------------------

    def get_routes(self, max_results: int = 0) -> list[dict]:
        """Detect API routes and pages from the project structure.
        Returns [{route, file, methods, type, stub?}] for Next.js App Router,
        Spring controllers, and similar frameworks. `stub` flags a handler
        whose body is a scaffold (pass / NotImplementedError / empty / TODO)."""
        routes: list[dict] = []
        seen_route_keys: set[tuple[str, str, str, int]] = set()

        def _fn_is_stub(meta, func) -> bool:
            if getattr(func, "scaffold_kind", "") == "stub":
                return True
            if getattr(func, "scaffold_kind", "") == "impl":
                return False
            try:
                body = "\n".join(meta.lines[func.line_range.start - 1 : func.line_range.end])
            except Exception:
                return False
            low = body.lower()
            if "not implemented" in low or "todo" in low or "fixme" in low:
                return True
            stripped = [ln.strip() for ln in body.splitlines() if ln.strip()]
            if len(stripped) <= 2:
                return True
            return False

        def add_route(route: dict) -> None:
            methods = route.get("methods") or [""]
            line = int(route.get("line", 1))
            for method in methods:
                key = (route["route"], method, route["file"], line)
                if key in seen_route_keys:
                    continue
                seen_route_keys.add(key)
                routes.append(route)
                break

        for path, meta in self.index.files.items():
            # Next.js App Router: app/**/route.ts -> API route
            if "/route." in path and ("app/" in path or "pages/api/" in path):
                methods = []
                stub_flags: list[bool] = []
                for func in meta.functions:
                    upper = func.name.upper()
                    if upper in ("GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"):
                        methods.append(upper)
                        stub_flags.append(_fn_is_stub(meta, func))
                route_path = path
                for prefix in ("app/", "src/app/"):
                    if prefix in route_path:
                        route_path = "/" + route_path.split(prefix, 1)[1]
                        break
                route_path = route_path.rsplit("/route.", 1)[0]
                if not route_path:
                    route_path = "/"
                entry = {
                    "route": route_path,
                    "file": path,
                    "methods": methods or ["GET"],
                    "type": "api",
                    "line": 1,
                }
                if stub_flags and all(stub_flags):
                    entry["stub"] = True
                add_route(entry)
            elif "/page." in path and "app/" in path:
                route_path = path
                for prefix in ("app/", "src/app/"):
                    if prefix in route_path:
                        route_path = "/" + route_path.split(prefix, 1)[1]
                        break
                route_path = route_path.rsplit("/page.", 1)[0]
                if not route_path:
                    route_path = "/"
                add_route(
                    {
                        "route": route_path,
                        "file": path,
                        "methods": [],
                        "type": "page",
                        "line": 1,
                    }
                )
            elif "/layout." in path and "app/" in path:
                route_path = path
                for prefix in ("app/", "src/app/"):
                    if prefix in route_path:
                        route_path = "/" + route_path.split(prefix, 1)[1]
                        break
                route_path = route_path.rsplit("/layout.", 1)[0]
                if not route_path:
                    route_path = "/"
                add_route(
                    {
                        "route": route_path,
                        "file": path,
                        "methods": [],
                        "type": "layout",
                        "line": 1,
                    }
                )
            elif path.endswith(".java"):
                spring_classes = [cls for cls in meta.classes if _is_spring_controller(cls)]
                for cls in spring_classes:
                    class_paths = _extract_spring_class_paths(meta, cls)
                    for func in meta.functions:
                        if func.parent_class != cls.name:
                            continue
                        mappings = _extract_spring_method_mappings(meta, func)
                        decl_line = _spring_method_declaration_line(meta, func)
                        is_stub = _fn_is_stub(meta, func)
                        for methods, method_paths in mappings:
                            for route_path in _combine_route_paths(class_paths, method_paths):
                                entry = {
                                    "route": route_path,
                                    "file": path,
                                    "methods": methods,
                                    "type": "api",
                                    "line": decl_line,
                                }
                                if is_stub:
                                    entry["stub"] = True
                                add_route(entry)
        routes.sort(key=lambda r: (r["type"], r["route"], r["file"], r.get("line", 1)))
        if max_results > 0:
            routes = routes[:max_results]
        return routes

    # ------------------------------------------------------------------
    # v3: Env var cross-reference
    # ------------------------------------------------------------------

    def get_env_usage(self, var_name: str, max_results: int = 0) -> list[dict]:
        """Find all references to an environment variable across the codebase.
        Searches for process.env.VAR, os.environ["VAR"], os.getenv("VAR"),
        and ${{ secrets.VAR }} patterns. Includes enclosing function/class
        when the line falls inside a known symbol range."""
        results: list[dict] = []
        for path, meta in self.index.files.items():
            for line_idx, line in enumerate(meta.lines):
                if var_name not in line:
                    continue
                context = line.strip()
                usage_type = "reference"
                if "process.env." in line:
                    usage_type = "process.env"
                elif "os.environ" in line or "os.getenv" in line:
                    usage_type = "os.environ"
                elif "System.getenv" in line:
                    usage_type = "system.getenv"
                elif "System.getProperty" in line:
                    usage_type = "system.getProperty"
                elif "@Value" in line and _SPRING_VALUE_RE.search(line):
                    usage_type = "spring.value"
                elif "secrets." in line:
                    usage_type = "github_secret"
                elif line.strip().startswith(var_name + "=") or line.strip().startswith(
                    f'"{var_name}"'
                ):
                    usage_type = "definition"
                elif "printf" in line and var_name in line:
                    usage_type = "env_write"
                lineno = line_idx + 1
                enclosing_function = None
                enclosing_class = None
                for func in getattr(meta, "functions", []) or []:
                    if func.line_range.start <= lineno <= func.line_range.end:
                        enclosing_function = func.qualified_name or func.name
                        if func.parent_class:
                            enclosing_class = func.parent_class
                        break
                if enclosing_class is None:
                    for cls in getattr(meta, "classes", []) or []:
                        if cls.line_range.start <= lineno <= cls.line_range.end:
                            enclosing_class = cls.name
                            break
                row: dict = {
                    "file": path,
                    "line": lineno,
                    "usage_type": usage_type,
                    "content": context[:200],
                }
                if enclosing_function:
                    row["function"] = enclosing_function
                if enclosing_class:
                    row["class"] = enclosing_class
                results.append(row)
        if not results:
            return [
                {
                    "var_name": var_name,
                    "usage_type": "not_found",
                    "searched_files": len(self.index.files),
                    "content": f"No usage found for {var_name}",
                }
            ]
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
                    params = [param for param in func.parameters if param != "destructured"]
                    end_line = _infer_component_end_line(meta, func)
                    components.append(
                        {
                            "name": func.name,
                            "file": path,
                            "line_range": f"{func.line_range.start}-{end_line}",
                            "params": params,
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

    def get_duplicate_classes(
        self,
        name: str | None = None,
        max_results: int = 0,
        simple_name_mode: bool = False,
    ) -> list[dict]:
        """Return duplicate Java classes by FQN, or by simple name when requested."""
        duplicates = []
        if simple_name_mode:
            grouped: dict[str, list[tuple[str, str]]] = defaultdict(list)
            for path, meta in sorted(self.index.files.items()):
                for cls in meta.classes:
                    if (
                        cls.name.isupper()
                        and len(cls.name) <= 4
                        and not any(ch.islower() for ch in cls.name)
                    ):
                        continue
                    qualified_name = cls.qualified_name or cls.name
                    grouped[cls.name].append((qualified_name, path))
            for simple_name, entries in sorted(grouped.items()):
                qualified_names = sorted({qualified_name for qualified_name, _ in entries})
                files = sorted({path for _, path in entries})
                if len(files) < 2:
                    continue
                if name is not None and name not in {simple_name, *qualified_names}:
                    continue
                duplicates.append(
                    {
                        "name": simple_name,
                        "qualified_names": qualified_names,
                        "count": len(files),
                        "files": files,
                    }
                )
        else:
            for qualified_name, files in sorted(self.index.duplicate_classes.items()):
                simple_name = qualified_name.rsplit(".", 1)[-1]
                if name is not None and name not in {simple_name, qualified_name}:
                    continue
                duplicates.append(
                    {
                        "name": simple_name,
                        "qualified_name": qualified_name,
                        "count": len(files),
                        "files": files,
                    }
                )
        if max_results > 0:
            duplicates = duplicates[:max_results]
        return duplicates

    # ------------------------------------------------------------------
    # Semantic duplicate detection (P9 part A integration)
    # ------------------------------------------------------------------

    def find_semantic_duplicates(
        self,
        min_lines: int = 2,
        max_groups: int = 30,
        max_members_per_group: int = 6,
        method: str = "ast",
        min_similarity: float = 0.90,
    ) -> str:
        """Group functions that do the same thing.

        Two strategies:

        * ``method="ast"`` (default, fast): group functions whose
          AST-normalised hash collides. Catches copy-paste and minor
          renames cleanly, misses conceptual clones rewritten from
          scratch.
        * ``method="embedding"`` (slower, requires Nomic stack):
          rank every pair of functions by cosine similarity of their
          embedding; emit clusters above ``min_similarity``. Each
          cluster in the output carries a ``sim=min..mean`` tag so the
          caller can triage tight clones vs loose conceptual matches.
          Catches rewrites that AST hash can't see (reordered branches,
          different variable names) but introduces false positives
          around boilerplate — always cross-check via
          ``get_function_source`` before merging. Reuses the
          ``symbol_vectors`` index populated by
          ``search_codebase(semantic=True)``; first call triggers a
          reindex.

        *min_lines* skips trivial one-liner functions where collisions
        are noise (`return None`, getters, etc). Default 2 catches
        short utilities (slugify, start_of_day etc.) that are common
        duplication patterns.
        *max_groups* caps the number of duplicate groups returned.
        *max_members_per_group* truncates large boilerplate clusters
        (20-way __init__ duplicates) to the first N members plus a
        "+K more" suffix. Pairs (size==2) are always shown in full.
        Groups of size 2 (exact pairs) are surfaced first because a
        clean 2-member clone is usually more actionable than a 20-way
        boilerplate cluster.
        """
        if method == "embedding":
            return self._find_semantic_duplicates_embedding(
                min_similarity=min_similarity,
                max_groups=max_groups,
                max_members_per_group=max_members_per_group,
            )
        if self._semantic_hash_cache is None:
            self._build_semantic_hash_cache(min_lines)

        cache = self._semantic_hash_cache
        hash_to_symbols: dict[str, list[str]] = {}
        for key, h in cache.items():
            hash_to_symbols.setdefault(h, []).append(key)

        duplicates = [(h, syms) for h, syms in hash_to_symbols.items() if len(syms) > 1]
        if not duplicates:
            return "Semantic duplicates: none found."

        total = len(duplicates)
        # Pairs first (size == 2), then bigger groups by descending size.
        duplicates.sort(key=lambda x: (0 if len(x[1]) == 2 else 1, -len(x[1])))
        duplicates = duplicates[:max_groups]

        lines = [f"Semantic duplicates: {total} group(s) found (showing top {len(duplicates)})"]
        cap = max(2, max_members_per_group) if max_members_per_group > 0 else 0
        for h, syms in duplicates:
            if cap and len(syms) > cap:
                shown = ", ".join(syms[:cap])
                lines.append(f"  hash {h}: {shown}, +{len(syms) - cap} more")
            else:
                lines.append(f"  hash {h}: {', '.join(syms)}")
        return "\n".join(lines)

    def _find_semantic_duplicates_embedding(
        self,
        min_similarity: float,
        max_groups: int,
        max_members_per_group: int,
    ) -> str:
        """Embedding-based duplicate detection — clusters symbols whose
        pairwise cosine similarity exceeds ``min_similarity``.

        Uses a simple union-find over edges ``sim(a, b) >= threshold``
        to collapse transitive clusters. On a 1000-symbol project the
        pair enumeration is 500k comparisons; each is a cheap dot
        product on normalised 768d vectors — runs in 1-2 seconds once
        the index is warm. The real cost is the first-call reindex
        (~2 min, same as ``search_codebase(semantic=True)``).
        """
        try:
            from token_savior.memory.symbol_embeddings import (
                reindex_project_symbols,
            )
            from token_savior import memory_db
            import sqlite_vec  # noqa: F401  # loaded side-effect
        except ImportError as exc:
            return f"Semantic duplicates (embedding): unavailable ({exc})"

        root = self.index.root_path
        reindex = reindex_project_symbols(root)
        if reindex.get("status") != "ok":
            return (
                "Semantic duplicates (embedding): unavailable "
                f"({reindex.get('reason', 'unknown')})"
            )

        # Fetch every (symbol_id, symbol_key, embedding) for this project.
        # sqlite-vec doesn't expose the raw float vector via the vec0 table
        # once serialised, so we deserialise client-side.
        import struct

        with memory_db.db_session() as conn:
            rows = conn.execute(
                "SELECT s.id, s.symbol_key, s.kind, v.embedding "
                "FROM symbols s JOIN symbol_vectors v ON v.symbol_id = s.id "
                "WHERE s.project_root = ?",
                (root,),
            ).fetchall()

        symbols: list[tuple[int, str, str, list[float]]] = []
        for r in rows:
            blob = bytes(r["embedding"])
            if len(blob) % 4 != 0:
                continue
            n = len(blob) // 4
            vec = list(struct.unpack(f"{n}f", blob))
            symbols.append((r["id"], r["symbol_key"], r["kind"], vec))

        # Collapse clusters via Union-Find: every edge whose cosine
        # meets the threshold joins two nodes. For normalised vectors,
        # cos(a, b) = dot(a, b).
        parent = list(range(len(symbols)))

        def _find(i: int) -> int:
            while parent[i] != i:
                parent[i] = parent[parent[i]]
                i = parent[i]
            return i

        def _union(a: int, b: int) -> None:
            ra, rb = _find(a), _find(b)
            if ra != rb:
                parent[ra] = rb

        def _is_container_member_pair(a_key: str, b_key: str) -> bool:
            """True when one symbol is a direct member of the other
            (e.g. ``pkg/x.py::Foo`` vs ``pkg/x.py::Foo.__init__``). The
            embeddings are naturally close because both share the class
            name, but structurally they're not clones — skip.
            """
            if "::" not in a_key or "::" not in b_key:
                return False
            a_file, a_qname = a_key.split("::", 1)
            b_file, b_qname = b_key.split("::", 1)
            if a_file != b_file:
                return False
            if a_qname == b_qname:
                return True
            return (
                b_qname.startswith(a_qname + ".")
                or a_qname.startswith(b_qname + ".")
            )

        threshold = float(min_similarity)
        pairs_checked = 0
        skipped_container = 0
        # Record every above-threshold edge so we can expose per-cluster
        # min/mean similarity in the output — safety contract #1 for the
        # embedding duplicate path: "present pairs with score".
        edges: list[tuple[int, int, float]] = []
        for i in range(len(symbols)):
            vi = symbols[i][3]
            key_i = symbols[i][1]
            for j in range(i + 1, len(symbols)):
                key_j = symbols[j][1]
                if _is_container_member_pair(key_i, key_j):
                    skipped_container += 1
                    continue
                vj = symbols[j][3]
                dot = 0.0
                for a, b in zip(vi, vj, strict=True):
                    dot += a * b
                pairs_checked += 1
                if dot >= threshold:
                    _union(i, j)
                    edges.append((i, j, dot))

        # Materialise clusters of size >= 2, keyed by cluster root id.
        clusters: dict[int, list[tuple[int, str]]] = {}
        for i, (_, key, _, _) in enumerate(symbols):
            root_id = _find(i)
            clusters.setdefault(root_id, []).append((i, key))
        # Per-cluster edge similarities (internal edges only).
        cluster_sims: dict[int, list[float]] = {}
        for a, b, sim in edges:
            ra = _find(a)
            cluster_sims.setdefault(ra, []).append(sim)

        # Group as (members sorted by key, min_sim, mean_sim).
        groups: list[tuple[list[str], float, float]] = []
        for root_id, members in clusters.items():
            if len(members) < 2:
                continue
            keys = sorted(k for _, k in members)
            sims = cluster_sims.get(root_id, [])
            if sims:
                smin = min(sims)
                smean = sum(sims) / len(sims)
            else:
                smin = smean = threshold
            groups.append((keys, smin, smean))

        if not groups:
            return (
                f"Semantic duplicates (embedding, sim>={threshold:.2f}): "
                f"none found across {len(symbols)} symbols "
                f"({pairs_checked} pairs checked)."
            )

        # Pairs first, then larger clusters, as in the AST path.
        groups.sort(key=lambda g: (0 if len(g[0]) == 2 else 1, -len(g[0])))
        total = len(groups)
        groups = groups[:max_groups]

        lines = [
            f"Semantic duplicates (embedding, sim>={threshold:.2f}): "
            f"{total} cluster(s) found across {len(symbols)} symbols "
            f"(showing top {len(groups)})",
            "WARNING: embedding matches can be conceptual but not functional."
            " Verify with get_function_source before merging or deleting.",
        ]
        cap = max(2, max_members_per_group) if max_members_per_group > 0 else 0
        for members, smin, smean in groups:
            score_tag = f"sim={smin:.2f}..{smean:.2f}"
            if cap and len(members) > cap:
                shown = ", ".join(members[:cap])
                lines.append(
                    f"  cluster({len(members)}) {score_tag}: "
                    f"{shown}, +{len(members) - cap} more"
                )
            else:
                lines.append(
                    f"  cluster({len(members)}) {score_tag}: "
                    f"{', '.join(members)}"
                )
        return "\n".join(lines)

    # ------------------------------------------------------------------
    # Import cycle detection
    # ------------------------------------------------------------------

    def find_import_cycles(self, max_cycles: int = 20) -> list[list[str]]:
        """Detect import cycles in the file-level dependency graph.

        Runs Tarjan's strongly-connected-components algorithm on
        ``self.index.import_graph``. Returns one list of file paths per
        non-trivial cycle (SCC size >= 2, plus any file that imports itself).
        """
        graph = self.index.import_graph
        index_counter = 0
        stack: list[str] = []
        on_stack: set[str] = set()
        indices: dict[str, int] = {}
        lowlinks: dict[str, int] = {}
        cycles: list[list[str]] = []

        def strongconnect(node: str) -> None:
            nonlocal index_counter
            indices[node] = index_counter
            lowlinks[node] = index_counter
            index_counter += 1
            stack.append(node)
            on_stack.add(node)

            for neighbour in graph.get(node, ()):
                if neighbour not in indices:
                    strongconnect(neighbour)
                    lowlinks[node] = min(lowlinks[node], lowlinks[neighbour])
                elif neighbour in on_stack:
                    lowlinks[node] = min(lowlinks[node], indices[neighbour])

            if lowlinks[node] == indices[node]:
                component: list[str] = []
                while True:
                    w = stack.pop()
                    on_stack.discard(w)
                    component.append(w)
                    if w == node:
                        break
                if len(component) > 1 or node in graph.get(node, ()):
                    cycles.append(sorted(component))

        for node in list(graph):
            if node not in indices:
                strongconnect(node)

        cycles.sort(key=lambda c: (len(c), c))
        return cycles[:max_cycles] if max_cycles > 0 else cycles

    def _build_semantic_hash_cache(self, min_lines: int = 2) -> None:
        """Pre-compute semantic hashes for all functions in the index."""
        from token_savior.semantic_hasher import semantic_hash
        from token_savior.project_indexer import is_path_excluded_from_scans

        cache: dict[str, str] = {}
        for file_path, meta in self.index.files.items():
            if is_path_excluded_from_scans(file_path):
                continue
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
                key = f"{func.qualified_name}  ({file_path}:{start})"
                cache[key] = h
        self._semantic_hash_cache = cache

    # ------------------------------------------------------------------
    # RWR relevance ranking
    # ------------------------------------------------------------------

    def get_relevance_cluster(
        self,
        name: str,
        budget: int = 10,
        include_reverse: bool = True,
    ) -> str:
        """Return the top-*budget* symbols closest to *name* via RWR ⊕ TCA.

        Final score = 0.6 × RWR(name, sym) + 0.4 × NPMI(name, sym), where
        NPMI comes from the Tenseur de Co-Activation. When TCA has no
        observations yet (new install), RWR alone drives the ranking.
        """
        from token_savior.graph_ranker import random_walk_with_restart

        index = self.index

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

        # Pull TCA scores (NPMI) for this seed if an engine snapshot is available.
        tca_scores: dict[str, float] = {}
        tca_used = False
        try:
            from pathlib import Path
            import os
            from token_savior.tca_engine import TCAEngine

            stats_dir = Path(
                os.path.expanduser(
                    os.environ.get("TOKEN_SAVIOR_STATS_DIR", "~/.local/share/token-savior")
                )
            )
            if (stats_dir / "tca_coactivation.json").exists():
                engine = TCAEngine(stats_dir)
                co = engine.get_coactive_symbols(name, top_k=200, min_coactivation=1)
                if co:
                    tca_used = True
                    tca_scores = {sym: pmi for sym, pmi in co}
        except Exception:
            pass

        def _combined(sym: str) -> float:
            rwr = scores.get(sym, 0.0)
            pmi = tca_scores.get(sym, 0.0)
            # Map NPMI ∈ [-1, 1] → [0, 1] so negative PMI doesn't punish strong RWR.
            pmi_01 = max(0.0, (pmi + 1.0) / 2.0) if pmi > 0 else 0.0
            return 0.6 * rwr + 0.4 * pmi_01

        ranked = sorted(
            ((sym, _combined(sym)) for sym in scores if sym != name),
            key=lambda x: x[1],
            reverse=True,
        )[:budget]

        header = (
            f"RWR+TCA Relevance cluster for '{name}' "
            f"(top {budget}, converged in {iterations} iter"
            + (", TCA active" if tca_used else "")
            + "):"
        )
        lines: list[str] = [header, "-" * 60]
        for sym, score in ranked:
            file_path = index.symbol_table.get(sym, "?")
            rwr_part = scores.get(sym, 0.0)
            pmi_part = tca_scores.get(sym)
            extra = (
                f"  [rwr={rwr_part:.4f}"
                + (f", pmi={pmi_part:+.2f}" if pmi_part is not None else "")
                + "]"
            )
            lines.append(f"  {score:.4f}  {sym}  ({file_path}){extra}")

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
        max_symbol_lines: int = 500,
    ) -> str:
        """Backward slice of *variable* at *line* (1-based, absolute) inside symbol *name*.

        Refuses to run on symbols larger than ``max_symbol_lines`` (default 500)
        to avoid the multi-second CFG walks seen in bench TASK-050. Pass a
        larger cap explicitly if you need it, or narrow the slice by calling
        on a smaller enclosing function first.
        """
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

        symbol_size = end - start + 1
        if max_symbol_lines and symbol_size > max_symbol_lines:
            return (
                f"Error: symbol '{name}' is {symbol_size} lines "
                f"(cap {max_symbol_lines}). Narrow to a smaller enclosing "
                f"function, or raise max_symbol_lines explicitly."
            )

        body_lines = meta.lines[start - 1 : end]
        source = "\n".join(body_lines)

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
                    if kind == "function":
                        sym, error = _resolve_unique_function(symbols, name)
                        if error == "ambiguous":
                            return (
                                f"Error: function '{name}' is ambiguous; "
                                "use a fully qualified signature"
                            )
                        if sym is not None:
                            source = "\n".join(
                                meta.lines[sym.line_range.start - 1 : sym.line_range.end]
                            )
                    else:
                        for sym in symbols:
                            if sym.name == name or getattr(sym, "qualified_name", None) == name:
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

    def _func_result(self, func, path, meta, level: int = 0):
        kind = "method" if func.is_method else "function"
        if level >= 2:
            return {
                "name": func.qualified_name,
                "file": path,
                "line": func.line_range.start,
                "type": kind,
            }
        out = {
            "name": func.qualified_name,
            "file": path,
            "line": func.line_range.start,
            "end_line": func.line_range.end,
            "type": kind,
        }
        if level <= 1:
            out["signature"] = f"def {func.name}({', '.join(func.parameters)})"
        if level == 0:
            preview_lines = meta.lines[func.line_range.start - 1 : func.line_range.start + 19]
            out["source_preview"] = "\n".join(preview_lines)
        return out

    def _class_result(self, cls, path, meta, level: int = 0):
        qname = cls.qualified_name or cls.name
        if level >= 2:
            return {
                "name": qname,
                "file": path,
                "line": cls.line_range.start,
                "type": "class",
            }
        out = {
            "name": qname,
            "qualified_name": qname,
            "file": path,
            "line": cls.line_range.start,
            "end_line": cls.line_range.end,
            "type": "class",
            "methods": [m.name for m in cls.methods],
            "bases": cls.base_classes,
        }
        if level == 0:
            preview_lines = meta.lines[cls.line_range.start - 1 : cls.line_range.start + 19]
            out["source_preview"] = "\n".join(preview_lines)
        return out

    def _resolve_symbol_info(
        self, name: str, level: int = 0, strip_preview: bool = False
    ) -> dict:
        """Resolve a symbol name to rich info (file, line, signature, preview).

        ``strip_preview=True`` drops ``source_preview`` and ``docstring`` from
        the returned dict. Used by list-producing tools (get_dependents,
        get_dependencies, get_call_chain) where each entry is a pointer — the
        caller can fetch the body via get_function_source when needed.

        Cross-language partial fallback: when the exact name misses, try the
        normalized-name index so ``UserService`` matches ``user_service`` /
        ``userService`` across Python and TS.
        """
        def _strip(info: dict) -> dict:
            if strip_preview and isinstance(info, dict):
                info.pop("source_preview", None)
                info.pop("docstring", None)
            return info

        index = self.index
        class_info = self._resolve_exact_class_info(name, level=level)
        if class_info is not None:
            return _strip(class_info)
        # Try symbol table first
        if name in index.symbol_table:
            path = index.symbol_table[name]
            meta = _resolve_file(index, path)
            if meta is not None:
                func, error = _resolve_unique_function(meta.functions, name)
                if error == "ambiguous":
                    return {
                        "name": name,
                        "error": f"function '{name}' is ambiguous; use a fully qualified signature",
                    }
                if func is not None:
                    return _strip(self._func_result(func, path, meta, level=level))
                for cls in meta.classes:
                    if cls.name == name or cls.qualified_name == name:
                        return _strip(self._class_result(cls, path, meta, level=level))
        # Fallback: search all files
        candidate_results: list[dict] = []
        for path, meta in sorted(index.files.items()):
            func, error = _resolve_unique_function(meta.functions, name)
            if error == "ambiguous":
                return {
                    "name": name,
                    "error": f"function '{name}' is ambiguous; use a fully qualified signature",
                }
            if func is not None:
                candidate_results.append(_strip(self._func_result(func, path, meta, level=level)))
            for cls in meta.classes:
                if cls.name == name or cls.qualified_name == name:
                    return _strip(self._class_result(cls, path, meta, level=level))
        if len(candidate_results) == 1:
            return candidate_results[0]
        if len(candidate_results) > 1:
            return {
                "name": name,
                "error": f"function '{name}' is ambiguous; use a fully qualified signature",
            }
        # Cross-language partial fallback via normalized-name index.
        norm_idx = getattr(index, "normalized_symbol_index", None) or {}
        if norm_idx:
            key = name.rsplit(".", 1)[-1].replace("_", "").replace("-", "").lower()
            candidates = norm_idx.get(key) or []
            candidates = [c for c in candidates if c != name]
            if len(candidates) == 1:
                info = self._resolve_symbol_info(
                    candidates[0], level=level, strip_preview=strip_preview
                )
                if "file" in info:
                    info["matched_via"] = f"normalized:{candidates[0]}"
                    return info
            elif len(candidates) > 1:
                return {
                    "name": name,
                    "error": (
                        f"no exact match; normalized-name candidates "
                        f"({len(candidates)}): {', '.join(candidates[:5])}"
                    ),
                    "normalized_candidates": candidates[:10],
                }
        return {"name": name}

    def _resolve_dep_name(self, name: str) -> tuple[str, set | None]:
        """Look up name in reverse dependency graph, falling back to class name for dotted methods."""
        exact_class_name = self._resolve_exact_class_name(name)
        if exact_class_name is not None:
            deps = self._get_aggregated_dependents(exact_class_name)
            if deps is not None:
                return exact_class_name, deps
        resolved_name = self._resolve_graph_symbol_name(name)
        deps = self._get_aggregated_dependents(resolved_name or name)
        if deps is not None:
            return resolved_name or name, deps
        # For "Class.method", fall back to dependents of "Class"
        if "." in name:
            class_name = name.rsplit(".", 1)[0]
            deps = self._get_aggregated_dependents(class_name)
            if deps is not None:
                return class_name, deps
        return name, None

    def _resolve_graph_symbol_name(self, name: str) -> str | None:
        exact_class_name = self._resolve_exact_class_name(name)
        if exact_class_name and self._has_forward_graph_presence(exact_class_name):
            return exact_class_name
        if name in self.index.global_dependency_graph:
            return name
        info = self._resolve_symbol_info(name)
        resolved_name = info.get("name")
        if resolved_name in self.index.global_dependency_graph:
            return resolved_name
        if "." in name:
            class_name = name.rsplit(".", 1)[0]
            if class_name in self.index.global_dependency_graph:
                return class_name
        return None

    def _get_communities(self) -> dict[str, str]:
        if self._communities is None:
            self._communities = compute_communities(self.index)
        return self._communities

    def _get_aggregated_dependencies(self, resolved_name: str) -> set[str] | None:
        aggregated: set[str] = set()
        found_dependency_data = False
        for alias in self._get_symbol_graph_aliases(resolved_name):
            deps = self.index.global_dependency_graph.get(alias)
            if deps is not None:
                found_dependency_data = True
                aggregated.update(deps)
            class_symbol = self._find_class_by_qualified_name(alias)
            if class_symbol is None:
                continue
            for method in class_symbol.methods:
                method_deps = self.index.global_dependency_graph.get(method.qualified_name)
                if method_deps is not None:
                    found_dependency_data = True
                    aggregated.update(method_deps)
        expanded: set[str] = set(aggregated)
        for dep in list(aggregated):
            class_symbol = self._find_class_by_qualified_name(dep)
            if class_symbol is None:
                continue
            expanded.update(method.qualified_name for method in class_symbol.methods)
        if found_dependency_data:
            return expanded
        return None

    def _get_aggregated_dependents(self, resolved_name: str) -> set[str] | None:
        aggregated: set[str] = set()
        found_dependency_data = False
        for alias in self._get_symbol_graph_aliases(resolved_name):
            deps = self.index.reverse_dependency_graph.get(alias)
            if deps is not None:
                found_dependency_data = True
                aggregated.update(deps)
            class_symbol = self._find_class_by_qualified_name(alias)
            if class_symbol is None:
                continue
            for method in class_symbol.methods:
                method_deps = self.index.reverse_dependency_graph.get(method.qualified_name)
                if method_deps is not None:
                    found_dependency_data = True
                    aggregated.update(method_deps)
        if found_dependency_data:
            return aggregated
        return None

    def _get_class_symbol_aliases(self, qualified_name: str) -> set[str]:
        class_symbol = self._find_class_by_qualified_name(qualified_name)
        if class_symbol is None:
            return {qualified_name}

        aliases = {qualified_name}
        for method in class_symbol.methods:
            aliases.add(method.qualified_name)
        return aliases

    def _get_symbol_graph_aliases(self, qualified_name: str) -> set[str]:
        aliases = set(self._get_class_symbol_aliases(qualified_name))
        if "." not in qualified_name:
            return aliases

        parent_name = qualified_name.rsplit(".", 1)[0]
        parent_class = self._find_class_by_qualified_name(parent_name)
        if parent_class is None:
            return aliases

        if any(method.qualified_name == qualified_name for method in parent_class.methods):
            aliases.add(parent_name)
            aliases.update(method.qualified_name for method in parent_class.methods)
        return aliases

    def _get_graph_target_names(self, resolved_name: str) -> set[str]:
        names = set(self._get_symbol_graph_aliases(resolved_name))
        for alias in list(names):
            names.update(self._resolve_graph_candidate_names(alias))
        return names

    @staticmethod
    def _call_chain_neighbor_key(name: str) -> tuple[int, int, int, str]:
        is_qualified = "." in name
        is_method = "(" in name
        is_constructor = _is_constructor_symbol(name)
        return (0 if is_method else 1, 1 if is_constructor else 0, 0 if is_qualified else 1, name)

    def _get_call_chain_neighbors(self, resolved_name: str) -> set[str]:
        class_symbol = self._find_class_by_qualified_name(resolved_name)
        if class_symbol is None:
            return self._get_call_chain_method_dependencies(resolved_name)

        neighbors: set[str] = set()
        for alias in {resolved_name, class_symbol.name}:
            deps = self.index.global_dependency_graph.get(alias)
            if deps is not None:
                neighbors.update(deps)
        for method in class_symbol.methods:
            if self._has_any_graph_presence(method.qualified_name):
                neighbors.add(method.qualified_name)
        return neighbors

    def _get_call_chain_method_dependencies(self, resolved_name: str) -> set[str]:
        aliases = {resolved_name}
        base_name, _ = _split_signature_suffix(resolved_name)
        aliases.add(base_name)
        parent_classes: set[str] = set()

        info = self._resolve_symbol_info(resolved_name)
        info_name = info.get("name")
        if info_name:
            aliases.add(info_name)
            info_base, _ = _split_signature_suffix(info_name)
            aliases.add(info_base)

        for meta in self.index.files.values():
            for func in _find_matching_functions(meta.functions, resolved_name):
                aliases.update(_function_aliases(func))
                if func.parent_class:
                    parent_classes.add(func.parent_class)
                    qualified_parent = func.qualified_name.rsplit(".", 1)[0]
                    parent_classes.add(qualified_parent)

        neighbors: set[str] = set()
        for alias in aliases:
            deps = self.index.global_dependency_graph.get(alias)
            if deps is not None:
                neighbors.update(deps)
        for parent_class in parent_classes:
            if parent_class in self.index.global_dependency_graph or self._has_forward_graph_presence(parent_class):
                neighbors.add(parent_class)
        return neighbors

    def _has_any_graph_presence(self, name: str) -> bool:
        if name in self.index.global_dependency_graph or name in self.index.reverse_dependency_graph:
            return True
        return self._has_forward_graph_presence(name)

    def _resolve_graph_candidate_names(self, name: str) -> set[str]:
        candidates = {name}
        resolved_name = self._resolve_graph_symbol_name(name)
        if resolved_name:
            candidates.add(resolved_name)
        info = self._resolve_symbol_info(name)
        info_name = info.get("name")
        if info_name:
            candidates.add(info_name)
        if "." in name:
            base_name, _ = _split_signature_suffix(name)
            candidates.add(base_name)
        for meta in self.index.files.values():
            for func in _find_matching_functions(meta.functions, name):
                candidates.update(_function_aliases(func))
            for cls in meta.classes:
                qualified_name = cls.qualified_name or cls.name
                if name in {cls.name, qualified_name} or qualified_name.endswith(f".{name}"):
                    candidates.add(qualified_name)
                    candidates.update(method.qualified_name for method in cls.methods)
        graph_symbols = set(self.index.global_dependency_graph) | set(self.index.reverse_dependency_graph)
        for deps in self.index.global_dependency_graph.values():
            graph_symbols.update(deps)
        for deps in self.index.reverse_dependency_graph.values():
            graph_symbols.update(deps)
        for symbol in graph_symbols:
            if symbol.startswith("__") and not name.startswith("__"):
                continue
            if _graph_name_matches(symbol, name):
                candidates.add(symbol)
        return candidates

    def _has_forward_graph_presence(self, qualified_name: str) -> bool:
        if qualified_name in self.index.global_dependency_graph:
            return True
        class_symbol = self._find_class_by_qualified_name(qualified_name)
        if class_symbol is None:
            return False
        for method in class_symbol.methods:
            if method.qualified_name in self.index.global_dependency_graph:
                return True
        return False

    def _resolve_exact_class_info(self, name: str, level: int = 0) -> dict | None:
        for path, meta in self._candidate_class_files(name):
            for cls in meta.classes:
                if cls.name == name or cls.qualified_name == name:
                    return self._class_result(cls, path, meta, level=level)
        return None

    def _resolve_exact_class_name(self, name: str) -> str | None:
        info = self._resolve_exact_class_info(name)
        if info is None:
            return None
        return info.get("qualified_name") or info.get("name")

    def _find_class_by_qualified_name(self, qualified_name: str):
        for meta in self.index.files.values():
            for cls in meta.classes:
                if (cls.qualified_name or cls.name) == qualified_name:
                    return cls
        return None

    def _candidate_class_files(self, name: str):
        if name in self.index.symbol_table:
            path = self.index.symbol_table[name]
            meta = _resolve_file(self.index, path)
            if meta is not None:
                yield path, meta
        for path, meta in sorted(self.index.files.items()):
            if name in self.index.symbol_table and path == self.index.symbol_table[name]:
                continue
            yield path, meta


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
  get_duplicate_classes(name?) -> list[dict]    # Java classes duplicated across files

SEARCH:
  search_codebase(pattern) -> list[dict]        # Regex across all files (max 100 results)

STRATEGY: Start with get_project_summary() to understand the repo layout. Use
get_structure_summary(file) to understand a file before reading it. Use
get_function_source(name) to read only what you need. Use dependency analysis
to trace connections. This is dramatically cheaper than reading entire files.
"""
