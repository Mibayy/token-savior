"""Regex-based Go annotator (best-effort).

Handles common Go patterns: function/method declarations, struct/interface
types, import statements, and doc comments using regex and brace counting.
"""

import re
from typing import Optional

from token_savior.brace_matcher import find_brace_end_go as _find_brace_end
from token_savior.models import (
    ClassInfo,
    FunctionInfo,
    ImportInfo,
    LineRange,
    StructuralMetadata,
    build_line_char_offsets,
)

# ---------------------------------------------------------------------------
# Dependency graph helpers
# ---------------------------------------------------------------------------

_IDENT_RE = re.compile(r"\b([A-Za-z_]\w*)\b")

_GO_KEYWORDS = frozenset({
    # Language keywords
    "break", "case", "chan", "const", "continue", "default", "defer",
    "else", "fallthrough", "for", "func", "go", "goto", "if", "import",
    "interface", "map", "package", "range", "return", "select", "struct",
    "switch", "type", "var",
    # Built-in identifiers and types
    "append", "any", "bool", "byte", "cap", "clear", "close", "complex",
    "complex64", "complex128", "copy", "delete", "error", "false",
    "float32", "float64", "imag", "int", "int8", "int16", "int32", "int64",
    "iota", "len", "make", "max", "min", "new", "nil", "panic", "print",
    "println", "real", "recover", "rune", "string", "true", "uint", "uint8",
    "uint16", "uint32", "uint64", "uintptr",
})


def _build_dependency_graph(
    functions: list[FunctionInfo],
    classes: list[ClassInfo],
    lines: list[str],
    defined_names: set[str],
) -> dict[str, list[str]]:
    """Build intra-file dependency graph for functions and structs."""
    graph: dict[str, list[str]] = {}

    for func in functions:
        start = func.line_range.start - 1  # 0-indexed
        end = func.line_range.end  # exclusive
        body_text = "\n".join(lines[start:end])
        refs = set(_IDENT_RE.findall(body_text))
        deps = sorted((refs & defined_names) - {func.name} - _GO_KEYWORDS)
        graph[func.name] = deps

    for cls in classes:
        start = cls.line_range.start - 1
        end = cls.line_range.end
        body_text = "\n".join(lines[start:end])
        refs = set(_IDENT_RE.findall(body_text))
        deps = sorted((refs & defined_names) - {cls.name} - _GO_KEYWORDS)
        graph[cls.name] = deps

    return graph


# ---------------------------------------------------------------------------
# Import detection
# ---------------------------------------------------------------------------

_SINGLE_IMPORT_RE = re.compile(
    r"^\s*import\s+"
    r"(?:(\w+)\s+)?"  # optional alias
    r'"([^"]+)"'
)

_IMPORT_GROUP_START_RE = re.compile(r"^\s*import\s*\(")

_IMPORT_LINE_RE = re.compile(
    r"^\s*"
    r"(?:(\.|_|\w+)\s+)?"  # optional alias (., _, or name)
    r'"([^"]+)"'
)


def _parse_imports(lines: list[str]) -> list[ImportInfo]:
    imports: list[ImportInfo] = []
    i = 0
    while i < len(lines):
        stripped = lines[i].strip()

        # Single-line import
        m = _SINGLE_IMPORT_RE.match(stripped)
        if m and "(" not in stripped.split('"')[0]:
            alias = m.group(1)
            module = m.group(2)
            # Extract short name from module path
            short_name = module.rsplit("/", 1)[-1] if "/" in module else module
            imports.append(
                ImportInfo(
                    module=module,
                    names=[short_name] if not alias else [],
                    alias=alias,
                    line_number=i + 1,
                    is_from_import=False,
                )
            )
            i += 1
            continue

        # Grouped import
        if _IMPORT_GROUP_START_RE.match(stripped):
            i += 1
            while i < len(lines):
                line = lines[i].strip()
                if line == ")":
                    i += 1
                    break
                im = _IMPORT_LINE_RE.match(line)
                if im:
                    alias = im.group(1)
                    module = im.group(2)
                    short_name = module.rsplit("/", 1)[-1] if "/" in module else module
                    # dot import: alias='.'
                    # blank import: alias='_'
                    effective_alias: Optional[str] = None
                    if alias and alias not in (".", "_"):
                        effective_alias = alias
                    elif alias == ".":
                        effective_alias = "."
                    elif alias == "_":
                        effective_alias = "_"
                    imports.append(
                        ImportInfo(
                            module=module,
                            names=[short_name] if not effective_alias else [],
                            alias=effective_alias,
                            line_number=i + 1,
                            is_from_import=False,
                        )
                    )
                i += 1
            continue

        i += 1
    return imports


# ---------------------------------------------------------------------------
# Function/method detection
# ---------------------------------------------------------------------------

# func Name(params) returnType {
_FUNC_RE = re.compile(
    r"^\s*func\s+(\w+)\s*"
    r"(?:\[([^\]]*)\]\s*)?"  # optional type params
    r"\(([^)]*)\)"
)

# func (r *Type) Name(params) returnType {
_METHOD_RE = re.compile(
    r"^\s*func\s+\(\s*(\w+)\s+\*?(\w+)\s*\)\s*(\w+)\s*"
    r"(?:\[([^\]]*)\]\s*)?"  # optional type params
    r"\(([^)]*)\)"
)


def _extract_params(raw: str) -> list[str]:
    """Extract parameter names from a Go parameter string."""
    params: list[str] = []
    if not raw.strip():
        return params
    for part in raw.split(","):
        part = part.strip()
        if not part:
            continue
        # Go params: "name type" or "name, name2 type" or just "type" (unnamed)
        # Handle variadic: "args ...int"
        tokens = part.split()
        if len(tokens) >= 2:
            name = tokens[0]
            if name == "...":
                continue
            # Check if the first token looks like a name (not a type)
            if (
                not name[0].isupper()
                and not name.startswith("*")
                and not name.startswith("[]")
                and not name.startswith("...")
            ):
                params.append(name)
            elif name.startswith("..."):
                params.append(name.lstrip("."))
        elif len(tokens) == 1:
            # Could be just a type (unnamed param) or a name
            # In Go, unnamed params are common in interfaces, skip them
            pass
    return params


# ---------------------------------------------------------------------------
# Doc comment collection
# ---------------------------------------------------------------------------


def _collect_doc_comment(lines: list[str], decl_line_0: int) -> Optional[str]:
    """Collect consecutive // comment lines immediately before decl_line_0."""
    doc_lines: list[str] = []
    j = decl_line_0 - 1
    while j >= 0:
        stripped = lines[j].strip()
        if stripped.startswith("//"):
            doc_lines.insert(0, stripped[2:].strip())
            j -= 1
        else:
            break
    return "\n".join(doc_lines) if doc_lines else None


# ---------------------------------------------------------------------------
# Type detection
# ---------------------------------------------------------------------------

_STRUCT_RE = re.compile(r"^\s*type\s+(\w+)\s+struct\s*\{?")
_INTERFACE_RE = re.compile(r"^\s*type\s+(\w+)\s+interface\s*\{?")
_TYPE_ALIAS_RE = re.compile(r"^\s*type\s+(\w+)\s*=\s*(\w+)")


def _extract_embedded_types(lines: list[str], start_0: int, end_0: int) -> list[str]:
    """Extract embedded type names from a struct or interface body."""
    bases: list[str] = []
    for idx in range(start_0 + 1, end_0):
        stripped = lines[idx].strip()
        if not stripped or stripped.startswith("//") or stripped == "}":
            continue
        # Embedded types: just a type name on its own line (possibly with *)
        # Fields have "name type" pattern, embedded types are a single token
        tokens = stripped.split()
        if len(tokens) == 1:
            name = tokens[0].lstrip("*")
            if name and name[0].isupper():
                bases.append(name)
    return bases


def _extract_interface_methods(lines: list[str], start_0: int, end_0: int) -> list[FunctionInfo]:
    """Extract method signatures from an interface body."""
    methods: list[FunctionInfo] = []
    iface_name = ""
    # Get interface name from start line
    m = _INTERFACE_RE.match(lines[start_0].strip())
    if m:
        iface_name = m.group(1)

    for idx in range(start_0 + 1, end_0):
        stripped = lines[idx].strip()
        if not stripped or stripped.startswith("//") or stripped == "}":
            continue
        # Method sig: Name(params) returnType
        mm = re.match(r"(\w+)\s*\(([^)]*)\)", stripped)
        if mm:
            name = mm.group(1)
            params = _extract_params(mm.group(2))
            methods.append(
                FunctionInfo(
                    name=name,
                    qualified_name=f"{iface_name}.{name}" if iface_name else name,
                    line_range=LineRange(start=idx + 1, end=idx + 1),
                    parameters=params,
                    decorators=[],
                    docstring=None,
                    is_method=True,
                    parent_class=iface_name,
                )
            )
    return methods


# ---------------------------------------------------------------------------
# Main annotator
# ---------------------------------------------------------------------------


def annotate_go(source: str, source_name: str = "<source>") -> StructuralMetadata:
    """Parse Go source and extract structural metadata using regex.

    Detects:
      - function declarations (func name(...))
      - method declarations (func (r *Type) name(...))
      - struct types (type Name struct { })
      - interface types (type Name interface { })
      - type aliases (type Name = Other)
      - import statements (single and grouped)
      - doc comments (consecutive // lines before declarations)
    """
    lines = source.split("\n")
    total_lines = len(lines)
    total_chars = len(source)
    line_offsets = build_line_char_offsets(lines)

    imports = _parse_imports(lines)

    functions: list[FunctionInfo] = []
    classes: list[ClassInfo] = []

    # Track consumed line ranges to avoid duplicate detection
    consumed: set[int] = set()

    i = 0
    while i < total_lines:
        if i in consumed:
            i += 1
            continue

        stripped = lines[i].strip()

        # Skip empty lines and comments
        if not stripped or stripped.startswith("//") or stripped.startswith("/*"):
            i += 1
            continue

        # Skip import blocks (already parsed)
        if stripped.startswith("import"):
            if "(" in stripped:
                while i < total_lines and ")" not in lines[i]:
                    i += 1
            i += 1
            continue

        # Method declaration (check before function - more specific pattern)
        mm = _METHOD_RE.match(stripped)
        if mm:
            receiver_type = mm.group(2)
            method_name = mm.group(3)
            params = _extract_params(mm.group(5))
            docstring = _collect_doc_comment(lines, i)

            if "{" in stripped or (i + 1 < total_lines and "{" in lines[i + 1].strip()):
                end_0 = _find_brace_end(lines, i)
            else:
                end_0 = i

            func_info = FunctionInfo(
                name=method_name,
                qualified_name=f"{receiver_type}.{method_name}",
                line_range=LineRange(start=i + 1, end=end_0 + 1),
                parameters=params,
                decorators=[],
                docstring=docstring,
                is_method=True,
                parent_class=receiver_type,
            )
            functions.append(func_info)

            for j in range(i, end_0 + 1):
                consumed.add(j)
            i = end_0 + 1
            continue

        # Function declaration
        fm = _FUNC_RE.match(stripped)
        if fm and not stripped.startswith("type"):
            name = fm.group(1)
            params = _extract_params(fm.group(3))
            docstring = _collect_doc_comment(lines, i)

            if "{" in stripped or (i + 1 < total_lines and "{" in lines[i + 1].strip()):
                end_0 = _find_brace_end(lines, i)
            else:
                end_0 = i

            func_info = FunctionInfo(
                name=name,
                qualified_name=name,
                line_range=LineRange(start=i + 1, end=end_0 + 1),
                parameters=params,
                decorators=[],
                docstring=docstring,
                is_method=False,
                parent_class=None,
            )
            functions.append(func_info)

            for j in range(i, end_0 + 1):
                consumed.add(j)
            i = end_0 + 1
            continue

        # Struct type
        sm = _STRUCT_RE.match(stripped)
        if sm:
            name = sm.group(1)
            docstring = _collect_doc_comment(lines, i)
            if "{" in stripped or (i + 1 < total_lines and "{" in lines[i + 1].strip()):
                end_0 = _find_brace_end(lines, i)
                bases = _extract_embedded_types(lines, i, end_0)
            else:
                end_0 = i
                bases = []

            classes.append(
                ClassInfo(
                    name=name,
                    line_range=LineRange(start=i + 1, end=end_0 + 1),
                    base_classes=bases,
                    methods=[],
                    decorators=[],
                    docstring=docstring,
                )
            )

            for j in range(i, end_0 + 1):
                consumed.add(j)
            i = end_0 + 1
            continue

        # Interface type
        im = _INTERFACE_RE.match(stripped)
        if im:
            name = im.group(1)
            docstring = _collect_doc_comment(lines, i)
            if "{" in stripped or (i + 1 < total_lines and "{" in lines[i + 1].strip()):
                end_0 = _find_brace_end(lines, i)
                bases = _extract_embedded_types(lines, i, end_0)
                iface_methods = _extract_interface_methods(lines, i, end_0)
            else:
                end_0 = i
                bases = []
                iface_methods = []

            classes.append(
                ClassInfo(
                    name=name,
                    line_range=LineRange(start=i + 1, end=end_0 + 1),
                    base_classes=bases,
                    methods=iface_methods,
                    decorators=[],
                    docstring=docstring,
                )
            )
            functions.extend(iface_methods)

            for j in range(i, end_0 + 1):
                consumed.add(j)
            i = end_0 + 1
            continue

        # Type alias
        ta = _TYPE_ALIAS_RE.match(stripped)
        if ta:
            name = ta.group(1)
            alias_target = ta.group(2)
            docstring = _collect_doc_comment(lines, i)

            classes.append(
                ClassInfo(
                    name=name,
                    line_range=LineRange(start=i + 1, end=i + 1),
                    base_classes=[alias_target],
                    methods=[],
                    decorators=[],
                    docstring=docstring,
                )
            )
            consumed.add(i)
            i += 1
            continue

        i += 1

    # Attach methods to their parent struct classes
    method_map: dict[str, list[FunctionInfo]] = {}
    for f in functions:
        if f.is_method and f.parent_class:
            method_map.setdefault(f.parent_class, []).append(f)

    updated_classes: list[ClassInfo] = []
    for cls in classes:
        if cls.name in method_map and not cls.methods:
            # struct with methods defined via receivers
            updated_classes.append(
                ClassInfo(
                    name=cls.name,
                    line_range=cls.line_range,
                    base_classes=cls.base_classes,
                    methods=cls.methods + method_map[cls.name],
                    decorators=cls.decorators,
                    docstring=cls.docstring,
                )
            )
        else:
            updated_classes.append(cls)

    # Build intra-file dependency graph
    defined_names = {f.name for f in functions} | {c.name for c in updated_classes}
    dependency_graph = _build_dependency_graph(functions, updated_classes, lines, defined_names)

    return StructuralMetadata(
        source_name=source_name,
        total_lines=total_lines,
        total_chars=total_chars,
        lines=lines,
        line_char_offsets=line_offsets,
        functions=functions,
        classes=updated_classes,
        imports=imports,
        dependency_graph=dependency_graph,
    )
