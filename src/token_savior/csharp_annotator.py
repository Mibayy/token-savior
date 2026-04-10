"""Regex-based C# annotator (best-effort).

Handles common C# patterns: class/interface/struct/enum/record declarations,
method and constructor detection, using directives, [Attributes], and
/// XML doc comments.
"""

import re
from typing import Optional

from token_savior.models import (
    ClassInfo,
    FunctionInfo,
    ImportInfo,
    LineRange,
    StructuralMetadata,
)


def _build_line_offsets(text: str, lines: list[str]) -> list[int]:
    offsets: list[int] = []
    pos = 0
    for line in lines:
        offsets.append(pos)
        pos += len(line) + 1
    return offsets


def _find_brace_end(lines: list[str], start_line_0: int) -> int:
    """Find the 0-based line where the outermost brace closes,
    skipping strings, verbatim strings, interpolated strings, char literals, and comments."""
    depth = 0
    found_open = False
    in_block_comment = False
    for idx in range(start_line_0, len(lines)):
        line = lines[idx]
        i = 0
        while i < len(line):
            ch = line[i]
            # Block comment handling
            if in_block_comment:
                if ch == "*" and i + 1 < len(line) and line[i + 1] == "/":
                    in_block_comment = False
                    i += 2
                    continue
                i += 1
                continue
            # Line comment
            if ch == "/" and i + 1 < len(line):
                if line[i + 1] == "/":
                    break  # rest is line comment
                if line[i + 1] == "*":
                    in_block_comment = True
                    i += 2
                    continue
            # Verbatim/interpolated strings: $@"...", @$"...", @"...", $"..."
            if ch in ("@", "$") and i + 1 < len(line):
                # Check for $@" or @$" (interpolated verbatim)
                if (
                    ch == "$" and line[i + 1] == "@" and i + 2 < len(line) and line[i + 2] == '"'
                ) or (
                    ch == "@" and line[i + 1] == "$" and i + 2 < len(line) and line[i + 2] == '"'
                ):
                    # Interpolated verbatim string — "" for escaped quote
                    i += 3
                    while i < len(line):
                        if line[i] == '"':
                            if i + 1 < len(line) and line[i + 1] == '"':
                                i += 2
                                continue
                            i += 1
                            break
                        i += 1
                    else:
                        # Multi-line verbatim string
                        idx += 1
                        while idx < len(lines):
                            line = lines[idx]
                            i = 0
                            while i < len(line):
                                if line[i] == '"':
                                    if i + 1 < len(line) and line[i + 1] == '"':
                                        i += 2
                                        continue
                                    i += 1
                                    break
                                i += 1
                            else:
                                idx += 1
                                continue
                            break
                        else:
                            return len(lines) - 1
                    continue
                # Verbatim string: @"..."
                if ch == "@" and line[i + 1] == '"':
                    i += 2
                    while i < len(line):
                        if line[i] == '"':
                            if i + 1 < len(line) and line[i + 1] == '"':
                                i += 2
                                continue
                            i += 1
                            break
                        i += 1
                    else:
                        # Multi-line verbatim
                        idx += 1
                        while idx < len(lines):
                            line = lines[idx]
                            i = 0
                            while i < len(line):
                                if line[i] == '"':
                                    if i + 1 < len(line) and line[i + 1] == '"':
                                        i += 2
                                        continue
                                    i += 1
                                    break
                                i += 1
                            else:
                                idx += 1
                                continue
                            break
                        else:
                            return len(lines) - 1
                    continue
                # Interpolated string: $"..."
                if ch == "$" and line[i + 1] == '"':
                    i += 2
                    while i < len(line):
                        if line[i] == "\\":
                            i += 2
                            continue
                        if line[i] == '"':
                            i += 1
                            break
                        i += 1
                    continue
            # Regular string
            if ch == '"':
                i += 1
                while i < len(line):
                    if line[i] == "\\":
                        i += 2
                        continue
                    if line[i] == '"':
                        i += 1
                        break
                    i += 1
                continue
            # Char literal
            if ch == "'" and i + 1 < len(line):
                if i + 2 < len(line) and line[i + 1] == "\\":
                    end = line.find("'", i + 2)
                    if end >= 0 and end <= i + 4:
                        i = end + 1
                        continue
                elif i + 2 < len(line) and line[i + 2] == "'":
                    i += 3
                    continue
            if ch == "{":
                depth += 1
                found_open = True
            elif ch == "}":
                depth -= 1
                if found_open and depth == 0:
                    return idx
            i += 1
    return len(lines) - 1


# ---------------------------------------------------------------------------
# Using directive detection
# ---------------------------------------------------------------------------

_USING_ALIAS_RE = re.compile(r"^\s*(?:global\s+)?using\s+(\w+)\s*=\s*([^;]+);")
_USING_STATIC_RE = re.compile(r"^\s*(?:global\s+)?using\s+static\s+([^;]+);")
_USING_RE = re.compile(r"^\s*(?:global\s+)?using\s+([^;=]+);")


def _parse_using_directives(lines: list[str]) -> list[ImportInfo]:
    imports: list[ImportInfo] = []
    for i, raw_line in enumerate(lines):
        stripped = raw_line.strip()
        if not (stripped.startswith("using ") or stripped.startswith("global using ")):
            continue

        # Alias: using Alias = Namespace.Type;
        m = _USING_ALIAS_RE.match(stripped)
        if m:
            alias = m.group(1).strip()
            module = m.group(2).strip()
            imports.append(
                ImportInfo(
                    module=module,
                    names=[],
                    alias=alias,
                    line_number=i + 1,
                    is_from_import=False,
                )
            )
            continue

        # Static: using static System.Math;
        m = _USING_STATIC_RE.match(stripped)
        if m:
            module = m.group(1).strip()
            imports.append(
                ImportInfo(
                    module=module,
                    names=["*"],
                    alias=None,
                    line_number=i + 1,
                    is_from_import=True,
                )
            )
            continue

        # Simple: using System.Collections.Generic;
        m = _USING_RE.match(stripped)
        if m:
            module = m.group(1).strip()
            # Skip if it accidentally matches a using statement (block)
            if module.startswith("(") or module.startswith("var "):
                continue
            imports.append(
                ImportInfo(
                    module=module,
                    names=[],
                    alias=None,
                    line_number=i + 1,
                    is_from_import=False,
                )
            )

    return imports


# ---------------------------------------------------------------------------
# Attribute / XML doc-comment collection
# ---------------------------------------------------------------------------


def _collect_attrs_and_docs(lines: list[str], decl_line_0: int) -> tuple[list[str], Optional[str]]:
    """Collect [Attribute] and /// XML doc comments above a declaration."""
    attrs: list[str] = []
    doc_lines: list[str] = []
    j = decl_line_0 - 1
    while j >= 0:
        stripped = lines[j].strip()
        if stripped.startswith("///"):
            # Strip the /// prefix and any XML tags for a clean docstring
            content = stripped[3:].strip()
            doc_lines.insert(0, content)
            j -= 1
        elif stripped.startswith("[") and "]" in stripped:
            # Extract attribute name from [Name] or [Name(...)]
            attr_match = re.match(r"\[(\w+)", stripped)
            if attr_match:
                attrs.insert(0, attr_match.group(1))
            j -= 1
        else:
            break
    docstring = "\n".join(doc_lines) if doc_lines else None
    return attrs, docstring


# ---------------------------------------------------------------------------
# Parameter extraction
# ---------------------------------------------------------------------------


def _extract_params(raw: str) -> list[str]:
    """Extract parameter names from a C# parameter list string.
    Handles generic depth <T,U>, ref/out/in/params modifiers."""
    params: list[str] = []
    if not raw.strip():
        return params

    # Split on commas, respecting generic angle brackets
    parts: list[str] = []
    depth = 0
    current: list[str] = []
    for ch in raw:
        if ch in ("<", "("):
            depth += 1
            current.append(ch)
        elif ch in (">", ")"):
            depth -= 1
            current.append(ch)
        elif ch == "," and depth == 0:
            parts.append("".join(current).strip())
            current = []
        else:
            current.append(ch)
    remainder = "".join(current).strip()
    if remainder:
        parts.append(remainder)

    for part in parts:
        part = part.strip()
        if not part:
            continue
        # Remove modifiers: ref, out, in, params, this (extension methods)
        for mod in ("ref ", "out ", "in ", "params ", "this "):
            if part.startswith(mod):
                part = part[len(mod) :].strip()
        # Remove default value: "int x = 5" -> "int x"
        eq_idx = part.find("=")
        if eq_idx > 0:
            part = part[:eq_idx].strip()
        # Last token is the parameter name: "int x" -> "x", "List<int> items" -> "items"
        tokens = part.split()
        if tokens:
            name = tokens[-1]
            # Strip any trailing array brackets
            name = name.rstrip("[]")
            if name and name.isidentifier():
                params.append(name)

    return params


# ---------------------------------------------------------------------------
# Regex patterns
# ---------------------------------------------------------------------------

# Namespace: namespace Foo.Bar { OR namespace Foo.Bar;
_NAMESPACE_RE = re.compile(r"^\s*namespace\s+([\w.]+)")

# Type declaration: [modifiers] [partial] (class|interface|struct|enum|record [class|struct]) Name<T> [: Base, IFace]
_TYPE_RE = re.compile(
    r"^\s*"
    r"(?:(?:public|private|protected|internal|static|abstract|sealed|partial|new|readonly|unsafe)\s+)*"
    r"(?:(?:record)\s+)?"  # record class / record struct
    r"(class|interface|struct|enum|record)\s+"
    r"(\w+)"  # name
    r"(?:<[^>]*>)?"  # optional generic params
    r"(?:\s*\([^)]*\))?"  # optional positional record params
    r"(?:\s*:\s*([^{;]+?))?"  # optional base list
    r"\s*(?:\{|;|where\s)"  # opening brace, semicolon, or where clause
)

# Method/constructor: [modifiers] ReturnType Name<T>(params) { or => or ;
# We match: [modifiers] <identifier> <identifier>(<...>) to distinguish from control flow
_METHOD_RE = re.compile(
    r"^\s*"
    r"(?:(?:public|private|protected|internal|static|abstract|virtual|override|sealed|async|extern|new|partial|unsafe|readonly)\s+)*"
    r"(?:[\w<>\[\]?,.\s]+?\s+)?"  # return type (optional for constructors)
    r"(\w+)"  # method/constructor name
    r"\s*(?:<[^>]*>)?\s*"  # optional generic params
    r"\("  # opening paren
)

# Keywords that look like method calls but aren't
_NOT_METHOD_NAMES = frozenset(
    {
        "if",
        "else",
        "for",
        "foreach",
        "while",
        "switch",
        "return",
        "new",
        "throw",
        "using",
        "lock",
        "catch",
        "typeof",
        "sizeof",
        "nameof",
        "default",
        "when",
        "where",
        "yield",
        "await",
        "checked",
        "unchecked",
        "fixed",
        "stackalloc",
        "base",
        "this",
        "var",
        "get",
        "set",
        "add",
        "remove",
        "value",
        "from",
        "select",
        "group",
        "into",
        "orderby",
        "join",
        "let",
        "on",
        "equals",
        "by",
        "ascending",
        "descending",
    }
)


def _find_method_params(lines: list[str], start_line_0: int) -> tuple[str, int]:
    """Extract the parameter string from a method declaration.
    Returns (param_string, line_index_after_params)."""
    depth = 0
    collecting = False
    param_chars: list[str] = []
    for line_idx in range(start_line_0, len(lines)):
        line = lines[line_idx]
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
                    return "".join(param_chars), line_idx
                if collecting:
                    param_chars.append(ch)
            elif collecting:
                param_chars.append(ch)
    return "".join(param_chars), start_line_0


def _is_method_line(stripped: str, class_name: str | None) -> re.Match | None:
    """Check if a line looks like a method or constructor declaration.
    Returns the match object if it is, None otherwise."""
    # Quick reject: must contain '('
    if "(" not in stripped:
        return None

    m = _METHOD_RE.match(stripped)
    if not m:
        return None

    name = m.group(1)

    # Reject C# keywords
    if name in _NOT_METHOD_NAMES:
        return None

    return m


# ---------------------------------------------------------------------------
# Main annotator
# ---------------------------------------------------------------------------


def annotate_csharp(source: str, source_name: str = "<source>") -> StructuralMetadata:
    """Parse C# source and extract structural metadata using regex.

    Detects:
      - using directives (simple, static, alias, global)
      - namespace declarations (block and file-scoped)
      - type declarations: class, interface, struct, enum, record
      - method and constructor declarations within types
      - [Attribute] decorators and /// XML doc comments
    """
    lines = source.split("\n")
    total_lines = len(lines)
    total_chars = len(source)
    line_offsets = _build_line_offsets(source, lines)

    imports = _parse_using_directives(lines)

    functions: list[FunctionInfo] = []
    classes: list[ClassInfo] = []

    consumed: set[int] = set()

    # Pass 1: Detect type declarations and extract methods within them
    i = 0
    while i < total_lines:
        if i in consumed:
            i += 1
            continue

        stripped = lines[i].strip()

        # Skip empty lines, comments, using directives
        if not stripped or stripped.startswith("//") or stripped.startswith("/*"):
            i += 1
            continue
        if stripped.startswith("using ") or stripped.startswith("global using "):
            i += 1
            continue

        # Skip attributes and doc comments (will be collected by _collect_attrs_and_docs)
        if stripped.startswith("[") or stripped.startswith("///"):
            i += 1
            continue

        # Skip namespace declarations (not emitted as ClassInfo)
        ns_m = _NAMESPACE_RE.match(stripped)
        if ns_m:
            # File-scoped namespace (ends with ;)
            if stripped.rstrip().endswith(";"):
                i += 1
                continue
            # Block namespace — just skip the declaration line, don't consume contents
            if "{" in stripped:
                i += 1
                continue
            # Opening brace on next line
            if i + 1 < total_lines and "{" in lines[i + 1].strip():
                i += 2
                continue
            i += 1
            continue

        # Type declaration
        type_m = _TYPE_RE.match(stripped)
        if type_m:
            type_kind = type_m.group(1)  # class, interface, struct, enum, record
            type_name = type_m.group(2)
            base_str = type_m.group(3)

            attrs, docstring = _collect_attrs_and_docs(lines, i)

            # Parse base classes / interfaces
            base_classes: list[str] = []
            if base_str:
                for base in base_str.split(","):
                    base = base.strip()
                    # Remove generic params for clean names
                    base = re.sub(r"<.*>", "", base).strip()
                    # Remove where clause remnants
                    if base.startswith("where ") or not base:
                        continue
                    if base and base[0].isupper():
                        base_classes.append(base)

            # Find body end
            if "{" in stripped or (i + 1 < total_lines and "{" in lines[i + 1].strip()):
                type_end = _find_brace_end(lines, i)
            elif stripped.rstrip().endswith(";"):
                # enum or record with no body
                type_end = i
            else:
                # Brace might be further down (after where clause)
                scan = i + 1
                while scan < total_lines and "{" not in lines[scan] and ";" not in lines[scan]:
                    scan += 1
                if scan < total_lines and "{" in lines[scan]:
                    type_end = _find_brace_end(lines, scan)
                else:
                    type_end = scan if scan < total_lines else i
                    for k in range(i, type_end + 1):
                        consumed.add(k)
                    classes.append(
                        ClassInfo(
                            name=type_name,
                            line_range=LineRange(start=i + 1, end=type_end + 1),
                            base_classes=base_classes,
                            methods=[],
                            decorators=attrs,
                            docstring=docstring,
                        )
                    )
                    i = type_end + 1
                    continue

            # Extract methods within the type body (skip enums)
            type_methods: list[FunctionInfo] = []
            if type_kind != "enum":
                j = i + 1
                while j < type_end:
                    if j in consumed:
                        j += 1
                        continue

                    mline = lines[j].strip()

                    # Skip nested types — detect and consume them
                    nested_type_m = _TYPE_RE.match(mline)
                    if nested_type_m:
                        if "{" in mline or (j + 1 < total_lines and "{" in lines[j + 1].strip()):
                            nested_end = _find_brace_end(lines, j)
                            for k in range(j, nested_end + 1):
                                consumed.add(k)
                            j = nested_end + 1
                            continue
                        j += 1
                        continue

                    # Skip empty, comments, attributes, doc comments
                    if (
                        not mline
                        or mline.startswith("//")
                        or mline.startswith("/*")
                        or mline.startswith("[")
                        or mline.startswith("///")
                    ):
                        j += 1
                        continue

                    method_m = _is_method_line(mline, type_name)
                    if method_m:
                        method_name = method_m.group(1)
                        method_attrs, method_doc = _collect_attrs_and_docs(lines, j)

                        param_str, _ = _find_method_params(lines, j)
                        params = _extract_params(param_str)

                        # Determine method end
                        # Check for body: {, =>, or ; (abstract/interface)
                        rest_after_paren = ""
                        # Scan forward from declaration to find {, =>, or ;
                        scan_end = min(j + 3, type_end)
                        for scan_j in range(j, scan_end + 1):
                            rest_after_paren += lines[scan_j]

                        if "{" in rest_after_paren:
                            # Find the line with the opening brace
                            brace_line = j
                            while brace_line <= scan_end and "{" not in lines[brace_line]:
                                brace_line += 1
                            method_end = _find_brace_end(lines, brace_line)
                        elif "=>" in rest_after_paren:
                            # Expression-bodied: find the semicolon
                            method_end = j
                            while method_end < type_end and ";" not in lines[method_end]:
                                method_end += 1
                        else:
                            # Abstract/interface method: ends at semicolon
                            method_end = j
                            while method_end < type_end and ";" not in lines[method_end]:
                                method_end += 1

                        func_info = FunctionInfo(
                            name=method_name,
                            qualified_name=f"{type_name}.{method_name}",
                            line_range=LineRange(start=j + 1, end=method_end + 1),
                            parameters=params,
                            decorators=method_attrs,
                            docstring=method_doc,
                            is_method=True,
                            parent_class=type_name,
                        )
                        functions.append(func_info)
                        type_methods.append(func_info)

                        for k in range(j, method_end + 1):
                            consumed.add(k)
                        j = method_end + 1
                        continue

                    j += 1

            classes.append(
                ClassInfo(
                    name=type_name,
                    line_range=LineRange(start=i + 1, end=type_end + 1),
                    base_classes=base_classes,
                    methods=type_methods,
                    decorators=attrs,
                    docstring=docstring,
                )
            )

            for k in range(i, type_end + 1):
                consumed.add(k)
            i = type_end + 1
            continue

        i += 1

    # Pass 2: Detect top-level functions (C# 9+ top-level statements)
    i = 0
    while i < total_lines:
        if i in consumed:
            i += 1
            continue

        stripped = lines[i].strip()
        if (
            not stripped
            or stripped.startswith("//")
            or stripped.startswith("/*")
            or stripped.startswith("[")
            or stripped.startswith("///")
            or stripped.startswith("using ")
            or stripped.startswith("global using ")
            or stripped.startswith("namespace ")
        ):
            i += 1
            continue

        method_m = _is_method_line(stripped, None)
        if method_m:
            name = method_m.group(1)
            attrs, docstring = _collect_attrs_and_docs(lines, i)
            param_str, _ = _find_method_params(lines, i)
            params = _extract_params(param_str)

            # Find end
            rest = ""
            scan_end = min(i + 3, total_lines - 1)
            for scan_j in range(i, scan_end + 1):
                rest += lines[scan_j]

            if "{" in rest:
                brace_line = i
                while brace_line <= scan_end and "{" not in lines[brace_line]:
                    brace_line += 1
                end_0 = _find_brace_end(lines, brace_line)
            elif "=>" in rest:
                end_0 = i
                while end_0 < total_lines - 1 and ";" not in lines[end_0]:
                    end_0 += 1
            else:
                end_0 = i
                while end_0 < total_lines - 1 and ";" not in lines[end_0]:
                    end_0 += 1

            functions.append(
                FunctionInfo(
                    name=name,
                    qualified_name=name,
                    line_range=LineRange(start=i + 1, end=end_0 + 1),
                    parameters=params,
                    decorators=attrs,
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

    return StructuralMetadata(
        source_name=source_name,
        total_lines=total_lines,
        total_chars=total_chars,
        lines=lines,
        line_char_offsets=line_offsets,
        functions=functions,
        classes=classes,
        imports=imports,
    )
