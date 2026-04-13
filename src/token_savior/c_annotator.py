"""Regex-based C annotator (best-effort, ISO C99/C11).

Handles common C patterns: function definitions, struct/union/enum definitions,
typedef declarations, #include directives, #define macros, static inline
functions, and forward declarations.
"""

import re
from typing import Optional

from token_savior.brace_matcher import find_brace_end_c as _find_brace_end
from token_savior.models import (
    ClassInfo,
    FunctionInfo,
    ImportInfo,
    LineRange,
    StructuralMetadata,
    build_line_char_offsets,
)


# ---------------------------------------------------------------------------
# #include parsing
# ---------------------------------------------------------------------------

_INCLUDE_RE = re.compile(r'^\s*#\s*include\s*([<"])([^>"]+)[>"]')


def _parse_includes(lines: list[str]) -> list[ImportInfo]:
    imports: list[ImportInfo] = []
    for i, line in enumerate(lines):
        m = _INCLUDE_RE.match(line)
        if m:
            bracket = m.group(1)
            module = m.group(2)
            is_system = bracket == "<"
            imports.append(
                ImportInfo(
                    module=module,
                    names=[],
                    alias="system" if is_system else "local",
                    line_number=i + 1,
                    is_from_import=False,
                )
            )
    return imports


# ---------------------------------------------------------------------------
# C type / specifier patterns
# ---------------------------------------------------------------------------

# C storage-class specifiers and qualifiers that can appear before a return type
_STORAGE_QUALS = (
    r"(?:(?:static|extern|inline|_Noreturn|_Thread_local|register"
    r"|const|volatile|restrict|__attribute__\s*\(\s*\([^)]*\)\s*\))\s+)*"
)

# Function declaration regex — uses a different strategy:
# Match everything up to the last identifier before '('.
# group(1): storage qualifiers + return type (everything before the function name)
# group(2): function name (the identifier immediately before '(')
# group(3): parameter list content (inside parens)
_FUNC_DEF_RE = re.compile(
    r"^(\s*"
    + _STORAGE_QUALS
    + r"(?:(?:struct|union|enum)\s+)?"  # optional struct/union/enum prefix
    + r"(?:\w+[\s*]+)*"  # type words with spaces or stars
    + r"(?:[*]\s*)*)"  # trailing pointer stars
    + r"(\w+)\s*"  # function name (last identifier before paren)
    + r"\(([^)]*)\)\s*$"  # parameter list — single line, end of line
)

# Multi-line function signature: name and opening paren on same line
_FUNC_START_RE = re.compile(
    r"^(\s*"
    + _STORAGE_QUALS
    + r"(?:(?:struct|union|enum)\s+)?"
    + r"(?:\w+[\s*]+)*"
    + r"(?:[*]\s*)*)"
    + r"(\w+)\s*\("
)

# struct/union/enum definition with opening brace
_STRUCT_RE = re.compile(r"^\s*(?:typedef\s+)?(struct|union|enum)\s+(\w+)\s*\{?")

# Anonymous typedef struct: typedef struct { ... } Name;
_TYPEDEF_ANON_RE = re.compile(r"^\s*typedef\s+(struct|union|enum)\s*\{")

# typedef alias: typedef existing_type new_name;
_TYPEDEF_ALIAS_RE = re.compile(r"^\s*typedef\s+(.+?)\s+(\w+)\s*;")

# typedef function pointer: typedef ret (*name)(params);
_TYPEDEF_FUNCPTR_RE = re.compile(r"^\s*typedef\s+.+?\(\s*\*\s*(\w+)\s*\)\s*\(")

# #define macro (function-like or object-like)
_DEFINE_RE = re.compile(r"^\s*#\s*define\s+(\w+)(?:\(([^)]*)\))?")

# Forward declaration: struct Foo; or typedef struct Foo Foo;
_FORWARD_DECL_RE = re.compile(r"^\s*(?:typedef\s+)?(?:struct|union|enum)\s+(\w+)\s*;")


# ---------------------------------------------------------------------------
# Parameter extraction
# ---------------------------------------------------------------------------


def _extract_c_params(raw: str) -> list[str]:
    """Extract parameter NAMES from a C parameter list, stripping types."""
    raw = raw.strip()
    if not raw or raw == "void":
        return []
    params: list[str] = []
    # Handle variadic
    parts = raw.split(",")
    for part in parts:
        part = part.strip()
        if not part or part == "...":
            continue
        # Remove array brackets: e.g. "int arr[10]" -> "int arr"
        part = re.sub(r"\[.*?\]", "", part).strip()
        # Function pointer param: void (*callback)(int)
        fp_m = re.match(r".*\(\s*\*\s*(\w+)\s*\)", part)
        if fp_m:
            params.append(fp_m.group(1))
            continue
        # Get the last identifier token (the name), strip leading *
        tokens = part.split()
        if tokens:
            name = tokens[-1].lstrip("*").strip()
            if name and re.match(r"^[A-Za-z_]\w*$", name):
                # Exclude type-only params like "int" in old-style K&R
                if len(tokens) > 1 or "*" in part:
                    params.append(name)
    return params


# ---------------------------------------------------------------------------
# Doc comment extraction (Doxygen-style)
# ---------------------------------------------------------------------------


def _collect_doc_comment(lines: list[str], decl_line_0: int) -> Optional[str]:
    """Collect /** ... */ or /// comments directly above a declaration."""
    doc_lines: list[str] = []
    j = decl_line_0 - 1

    # Check for /** ... */ block immediately above
    while j >= 0:
        stripped = lines[j].strip()
        if stripped.startswith("///"):
            doc_lines.insert(0, stripped[3:].strip())
            j -= 1
        elif stripped.startswith("/**"):
            # Start of block doc comment — collect and stop
            clean = stripped.lstrip("/* ").rstrip("*/").strip()
            if clean:
                doc_lines.insert(0, clean)
            break
        elif stripped == "*/" or stripped.endswith("*/"):
            # Closing of block comment — skip
            j -= 1
        elif stripped.startswith("*"):
            # Interior line of a block doc comment
            clean = stripped.lstrip("* ").strip()
            if clean:
                doc_lines.insert(0, clean)
            j -= 1
        else:
            break

    return "\n".join(doc_lines) if doc_lines else None


# ---------------------------------------------------------------------------
# Modifier extraction
# ---------------------------------------------------------------------------


def _extract_modifiers(prefix: str) -> list[str]:
    """Extract C storage-class specifiers and qualifiers from the prefix."""
    mods: list[str] = []
    keywords = ("static", "extern", "inline", "_Noreturn", "_Thread_local", "register")
    for kw in keywords:
        if re.search(rf"\b{kw}\b", prefix):
            mods.append(kw)
    return mods


# ---------------------------------------------------------------------------
# Collect full parameter string from multi-line signatures
# ---------------------------------------------------------------------------


def _collect_params_multiline(lines: list[str], start_line_0: int) -> tuple[str, int]:
    """Collect the full parameter string from a potentially multi-line
    function signature. Returns (param_string, line_of_closing_paren_0)."""
    depth = 0
    collecting = False
    param_chars: list[str] = []
    for idx in range(start_line_0, len(lines)):
        line = lines[idx]
        for ch in line:
            if ch == "(":
                if collecting:
                    param_chars.append(ch)
                depth += 1
                if depth == 1:
                    collecting = True
            elif ch == ")":
                depth -= 1
                if depth == 0 and collecting:
                    return "".join(param_chars), idx
                if collecting:
                    param_chars.append(ch)
            elif collecting:
                param_chars.append(ch)
        if collecting:
            param_chars.append(" ")  # line continuation
    return "".join(param_chars), start_line_0


# ---------------------------------------------------------------------------
# Typedef end finding
# ---------------------------------------------------------------------------


def _find_typedef_name_after_brace(lines: list[str], brace_end_0: int) -> Optional[str]:
    """After a closing brace for a typedef struct/union/enum, find the name.
    e.g. '} MyType;' -> 'MyType'"""
    line = lines[brace_end_0]
    m = re.search(r"\}\s*(\w+)\s*;", line)
    if m:
        return m.group(1)
    # Name might be on next line
    if brace_end_0 + 1 < len(lines):
        m = re.match(r"\s*(\w+)\s*;", lines[brace_end_0 + 1])
        if m:
            return m.group(1)
    return None


# ---------------------------------------------------------------------------
# Dependency graph building
# ---------------------------------------------------------------------------

_IDENT_RE = re.compile(r"\b([A-Za-z_]\w*)\b")

# C keywords to exclude from dependency references
_C_KEYWORDS = frozenset(
    {
        "auto",
        "break",
        "case",
        "char",
        "const",
        "continue",
        "default",
        "do",
        "double",
        "else",
        "enum",
        "extern",
        "float",
        "for",
        "goto",
        "if",
        "inline",
        "int",
        "long",
        "register",
        "restrict",
        "return",
        "short",
        "signed",
        "sizeof",
        "static",
        "struct",
        "switch",
        "typedef",
        "union",
        "unsigned",
        "void",
        "volatile",
        "while",
        "_Alignas",
        "_Alignof",
        "_Atomic",
        "_Bool",
        "_Complex",
        "_Generic",
        "_Imaginary",
        "_Noreturn",
        "_Static_assert",
        "_Thread_local",
        "NULL",
        "true",
        "false",
        "bool",
        "size_t",
        # Common standard library names to skip
        "printf",
        "fprintf",
        "sprintf",
        "snprintf",
        "malloc",
        "calloc",
        "realloc",
        "free",
        "memcpy",
        "memset",
        "memmove",
        "strcmp",
        "strlen",
        "strcpy",
        "strncpy",
        "strcat",
        "strncat",
        "strstr",
        "atoi",
        "atof",
        "abs",
        "assert",
        "exit",
        "abort",
    }
)


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
        deps = sorted((refs & defined_names) - {func.name} - _C_KEYWORDS)
        graph[func.name] = deps

    for cls in classes:
        start = cls.line_range.start - 1
        end = cls.line_range.end
        body_text = "\n".join(lines[start:end])
        refs = set(_IDENT_RE.findall(body_text))
        deps = sorted((refs & defined_names) - {cls.name} - _C_KEYWORDS)
        graph[cls.name] = deps

    return graph


# ---------------------------------------------------------------------------
# Multi-line #define tracking
# ---------------------------------------------------------------------------


def _count_define_lines(lines: list[str], start_line_0: int) -> int:
    """Count how many lines a #define spans (with backslash continuation).
    Returns the 0-based index of the last line."""
    idx = start_line_0
    while idx < len(lines) and lines[idx].rstrip().endswith("\\"):
        idx += 1
    return idx


# ---------------------------------------------------------------------------
# Main annotator
# ---------------------------------------------------------------------------


def annotate_c(source: str, source_name: str = "<source>") -> StructuralMetadata:
    """Parse C source and extract structural metadata using regex.

    Detects:
      - #include directives (system and local)
      - Function definitions (including static, inline, _Noreturn)
      - struct, union, enum definitions (named and typedef'd)
      - typedef aliases and function pointer typedefs
      - #define macros (object-like and function-like)
      - Doxygen-style doc comments (/** */ and ///)
      - Intra-file dependency graph
    """
    lines = source.split("\n")
    total_lines = len(lines)
    total_chars = len(source)
    line_offsets = build_line_char_offsets(lines)

    imports = _parse_includes(lines)

    functions: list[FunctionInfo] = []
    classes: list[ClassInfo] = []

    consumed: set[int] = set()

    i = 0
    while i < total_lines:
        if i in consumed:
            i += 1
            continue

        stripped = lines[i].strip()

        # Skip empty lines and pure comments
        if not stripped or stripped.startswith("//"):
            i += 1
            continue

        # Skip block comments
        if stripped.startswith("/*"):
            while i < total_lines and "*/" not in lines[i]:
                i += 1
            i += 1
            continue

        # Skip preprocessor (except #define which we handle below)
        if stripped.startswith("#") and not stripped.lstrip("#").lstrip().startswith("define"):
            # Multi-line preprocessor with backslash
            while i < total_lines and lines[i].rstrip().endswith("\\"):
                i += 1
            i += 1
            continue

        # --- #define macros ---
        define_m = _DEFINE_RE.match(stripped)
        if define_m:
            macro_name = define_m.group(1)
            macro_params_raw = define_m.group(2)
            end_0 = _count_define_lines(lines, i)
            docstring = _collect_doc_comment(lines, i)

            params: list[str] = []
            if macro_params_raw is not None:
                params = [
                    p.strip()
                    for p in macro_params_raw.split(",")
                    if p.strip() and p.strip() != "..."
                ]

            functions.append(
                FunctionInfo(
                    name=macro_name,
                    qualified_name=macro_name,
                    line_range=LineRange(start=i + 1, end=end_0 + 1),
                    parameters=params,
                    decorators=["macro"],
                    docstring=docstring,
                    is_method=False,
                    parent_class=None,
                )
            )
            for k in range(i, end_0 + 1):
                consumed.add(k)
            i = end_0 + 1
            continue

        # --- Forward declarations (skip, don't create entities) ---
        if _FORWARD_DECL_RE.match(stripped) and "{" not in stripped:
            i += 1
            continue

        # --- typedef function pointer ---
        fp_m = _TYPEDEF_FUNCPTR_RE.match(stripped)
        if fp_m:
            fp_name = fp_m.group(1)
            # Find the end (semicolon)
            end_0 = i
            while end_0 < total_lines and ";" not in lines[end_0]:
                end_0 += 1
            docstring = _collect_doc_comment(lines, i)
            classes.append(
                ClassInfo(
                    name=fp_name,
                    line_range=LineRange(start=i + 1, end=end_0 + 1),
                    base_classes=[],
                    methods=[],
                    decorators=["typedef", "function_pointer"],
                    docstring=docstring,
                )
            )
            for k in range(i, end_0 + 1):
                consumed.add(k)
            i = end_0 + 1
            continue

        # --- Anonymous typedef struct/union/enum: typedef struct { ... } Name; ---
        anon_m = _TYPEDEF_ANON_RE.match(stripped)
        if anon_m and "{" in stripped:
            kind = anon_m.group(1)  # struct/union/enum
            end_0 = _find_brace_end(lines, i)
            typedef_name = _find_typedef_name_after_brace(lines, end_0)
            if typedef_name:
                docstring = _collect_doc_comment(lines, i)
                classes.append(
                    ClassInfo(
                        name=typedef_name,
                        line_range=LineRange(start=i + 1, end=end_0 + 1),
                        base_classes=[],
                        methods=[],
                        decorators=["typedef", kind],
                        docstring=docstring,
                    )
                )
                for k in range(i, end_0 + 1):
                    consumed.add(k)
                # Also consume the name line after brace if separate
                if end_0 + 1 < total_lines:
                    consumed.add(end_0 + 1)
                i = end_0 + 1
                continue

        # --- Named struct/union/enum definition ---
        struct_m = _STRUCT_RE.match(stripped)
        if struct_m:
            kind = struct_m.group(1)  # struct/union/enum
            name = struct_m.group(2)
            has_brace = "{" in stripped
            next_has_brace = i + 1 < total_lines and "{" in lines[i + 1]

            if has_brace or next_has_brace:
                end_0 = _find_brace_end(lines, i)
                docstring = _collect_doc_comment(lines, i)

                # Check if this is a typedef: typedef struct Name { ... } AliasName;
                is_typedef = stripped.lstrip().startswith("typedef")
                decorators = [kind]
                if is_typedef:
                    decorators.insert(0, "typedef")
                    alias = _find_typedef_name_after_brace(lines, end_0)
                    if alias and alias != name:
                        decorators.append(f"alias:{alias}")

                classes.append(
                    ClassInfo(
                        name=name,
                        line_range=LineRange(start=i + 1, end=end_0 + 1),
                        base_classes=[],
                        methods=[],
                        decorators=decorators,
                        docstring=docstring,
                    )
                )
                for k in range(i, end_0 + 1):
                    consumed.add(k)
                i = end_0 + 1
                continue

        # --- typedef alias (simple): typedef int myint; ---
        alias_m = _TYPEDEF_ALIAS_RE.match(stripped)
        if alias_m:
            # Make sure it's not a struct/union/enum we already handled
            base_type = alias_m.group(1).strip()
            alias_name = alias_m.group(2).strip()
            if not re.match(r"(?:struct|union|enum)\s+\w+", base_type):
                docstring = _collect_doc_comment(lines, i)
                classes.append(
                    ClassInfo(
                        name=alias_name,
                        line_range=LineRange(start=i + 1, end=i + 1),
                        base_classes=[base_type],
                        methods=[],
                        decorators=["typedef"],
                        docstring=docstring,
                    )
                )
                consumed.add(i)
                i += 1
                continue

        # --- Function definition ---
        # Try single-line signature first
        func_m = _FUNC_DEF_RE.match(stripped)
        if func_m:
            prefix = func_m.group(1)
            fn_name = func_m.group(2)
            param_str = func_m.group(3)

            # Next non-empty line should have { for a definition
            next_i = i + 1
            while next_i < total_lines and not lines[next_i].strip():
                next_i += 1

            if next_i < total_lines and lines[next_i].strip().startswith("{"):
                end_0 = _find_brace_end(lines, next_i)
                docstring = _collect_doc_comment(lines, i)
                modifiers = _extract_modifiers(prefix)
                params = _extract_c_params(param_str)

                functions.append(
                    FunctionInfo(
                        name=fn_name,
                        qualified_name=fn_name,
                        line_range=LineRange(start=i + 1, end=end_0 + 1),
                        parameters=params,
                        decorators=modifiers,
                        docstring=docstring,
                        is_method=False,
                        parent_class=None,
                    )
                )
                for k in range(i, end_0 + 1):
                    consumed.add(k)
                i = end_0 + 1
                continue
            elif stripped.endswith("{") or ("{" in stripped and "}" not in stripped):
                # Brace on same line as signature
                end_0 = _find_brace_end(lines, i)
                docstring = _collect_doc_comment(lines, i)
                modifiers = _extract_modifiers(prefix)
                params = _extract_c_params(param_str)

                functions.append(
                    FunctionInfo(
                        name=fn_name,
                        qualified_name=fn_name,
                        line_range=LineRange(start=i + 1, end=end_0 + 1),
                        parameters=params,
                        decorators=modifiers,
                        docstring=docstring,
                        is_method=False,
                        parent_class=None,
                    )
                )
                for k in range(i, end_0 + 1):
                    consumed.add(k)
                i = end_0 + 1
                continue

        # Try multi-line function signature
        func_start_m = _FUNC_START_RE.match(stripped)
        if func_start_m and not stripped.endswith(";"):
            prefix = func_start_m.group(1)
            fn_name = func_start_m.group(2)

            param_str, param_end_0 = _collect_params_multiline(lines, i)

            # The closing paren line or next line should have {
            rest_after_paren = (
                lines[param_end_0][lines[param_end_0].index(")") + 1 :]
                if ")" in lines[param_end_0]
                else ""
            )
            if "{" in rest_after_paren:
                end_0 = _find_brace_end(lines, param_end_0)
            else:
                check = param_end_0 + 1
                while check < total_lines and not lines[check].strip():
                    check += 1
                if check < total_lines and lines[check].strip().startswith("{"):
                    end_0 = _find_brace_end(lines, check)
                else:
                    i += 1
                    continue

            docstring = _collect_doc_comment(lines, i)
            modifiers = _extract_modifiers(prefix)
            params = _extract_c_params(param_str)

            functions.append(
                FunctionInfo(
                    name=fn_name,
                    qualified_name=fn_name,
                    line_range=LineRange(start=i + 1, end=end_0 + 1),
                    parameters=params,
                    decorators=modifiers,
                    docstring=docstring,
                    is_method=False,
                    parent_class=None,
                )
            )
            for k in range(i, end_0 + 1):
                consumed.add(k)
            i = end_0 + 1
            continue

        i += 1

    # Build dependency graph
    defined_names = {f.name for f in functions} | {c.name for c in classes}
    dependency_graph = _build_dependency_graph(functions, classes, lines, defined_names)

    return StructuralMetadata(
        source_name=source_name,
        total_lines=total_lines,
        total_chars=total_chars,
        lines=lines,
        line_char_offsets=line_offsets,
        functions=functions,
        classes=classes,
        imports=imports,
        dependency_graph=dependency_graph,
    )
