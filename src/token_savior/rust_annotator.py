"""Regex-based Rust annotator (best-effort).

Handles common Rust patterns: function declarations, struct/enum/trait types,
impl blocks, use statements, attributes, doc comments, and macro_rules.
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
    skipping strings, raw strings, char literals, and comments."""
    depth = 0
    found_open = False
    in_block_comment = 0  # nesting depth for /* */
    for idx in range(start_line_0, len(lines)):
        line = lines[idx]
        i = 0
        while i < len(line):
            ch = line[i]
            # Block comment handling (Rust supports nested /* */)
            if in_block_comment > 0:
                if ch == "/" and i + 1 < len(line) and line[i + 1] == "*":
                    in_block_comment += 1
                    i += 2
                    continue
                if ch == "*" and i + 1 < len(line) and line[i + 1] == "/":
                    in_block_comment -= 1
                    i += 2
                    continue
                i += 1
                continue
            # Line comment
            if ch == "/" and i + 1 < len(line):
                if line[i + 1] == "/":
                    break  # rest is line comment
                if line[i + 1] == "*":
                    in_block_comment += 1
                    i += 2
                    continue
            # Raw string: r#"..."#, r##"..."##, etc.
            if ch == "r" and i + 1 < len(line) and line[i + 1] in ('"', "#"):
                hash_count = 0
                j = i + 1
                while j < len(line) and line[j] == "#":
                    hash_count += 1
                    j += 1
                if j < len(line) and line[j] == '"':
                    j += 1
                    # Find closing "###
                    closing = '"' + "#" * hash_count
                    while True:
                        pos = line.find(closing, j)
                        if pos >= 0:
                            i = pos + len(closing)
                            break
                        # Span to next line
                        idx += 1
                        if idx >= len(lines):
                            return len(lines) - 1
                        line = lines[idx]
                        j = 0
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
            # Char literal (skip 'a', '\n', etc. but not lifetime 'a)
            if ch == "'" and i + 1 < len(line):
                # Lifetime check: 'a where next is alpha and followed by non-'
                # Char literal: 'x' or '\n'
                if i + 2 < len(line) and line[i + 1] == "\\":
                    # Escaped char literal like '\n'
                    end = line.find("'", i + 2)
                    if end >= 0 and end <= i + 4:
                        i = end + 1
                        continue
                elif i + 2 < len(line) and line[i + 2] == "'":
                    # Simple char literal like 'a'
                    i += 3
                    continue
                # Otherwise it's a lifetime, skip
            if ch == "{":
                depth += 1
                found_open = True
            elif ch == "}":
                depth -= 1
                if found_open and depth == 0:
                    return idx
            i += 1
    return len(lines) - 1


def _find_semicolon_end(lines: list[str], start_line_0: int) -> int:
    """Find the line containing the terminating semicolon."""
    for idx in range(start_line_0, len(lines)):
        if ";" in lines[idx]:
            return idx
    return start_line_0


# ---------------------------------------------------------------------------
# Use statement detection
# ---------------------------------------------------------------------------

_USE_RE = re.compile(r"^\s*(?:pub\s+)?use\s+(.+?)\s*;")

_USE_MULTI_START_RE = re.compile(r"^\s*(?:pub\s+)?use\s+(.+)")


def _parse_use_statements(lines: list[str]) -> list[ImportInfo]:
    imports: list[ImportInfo] = []
    i = 0
    while i < len(lines):
        stripped = lines[i].strip()

        # Skip non-use lines
        if not (stripped.startswith("use ") or stripped.startswith("pub use ")):
            i += 1
            continue

        # Collect potentially multi-line use statement
        m = _USE_RE.match(stripped)
        if m:
            path = m.group(1).strip()
            _parse_use_path(path, i + 1, imports)
            i += 1
            continue

        # Multi-line use (no semicolon on first line)
        m2 = _USE_MULTI_START_RE.match(stripped)
        if m2:
            full = stripped
            start_line = i
            while i < len(lines) and ";" not in lines[i]:
                i += 1
            if i < len(lines):
                # Join all lines
                full = " ".join(lines[j].strip() for j in range(start_line, i + 1))
                m3 = re.match(r"(?:pub\s+)?use\s+(.+?)\s*;", full)
                if m3:
                    _parse_use_path(m3.group(1).strip(), start_line + 1, imports)
            i += 1
            continue

        i += 1
    return imports


def _parse_use_path(path: str, line_number: int, imports: list[ImportInfo]) -> None:
    """Parse a use path like 'std::collections::{HashMap, HashSet}' or 'crate::module::Item'."""
    # Handle glob: use std::io::*;
    if path.endswith("::*"):
        module = path[:-3]
        imports.append(
            ImportInfo(
                module=module,
                names=["*"],
                alias=None,
                line_number=line_number,
                is_from_import=True,
            )
        )
        return

    # Handle alias: use std::io::Result as IoResult;
    alias_match = re.match(r"(.+?)\s+as\s+(\w+)", path)
    if alias_match:
        full_path = alias_match.group(1).strip()
        alias = alias_match.group(2).strip()
        module = full_path.rsplit("::", 1)[0] if "::" in full_path else full_path
        name = full_path.rsplit("::", 1)[-1] if "::" in full_path else full_path
        imports.append(
            ImportInfo(
                module=module,
                names=[name],
                alias=alias,
                line_number=line_number,
                is_from_import=True,
            )
        )
        return

    # Handle grouped: use std::collections::{HashMap, HashSet};
    brace_match = re.match(r"(.+?)::\{(.+)\}", path)
    if brace_match:
        module = brace_match.group(1).strip()
        items_str = brace_match.group(2).strip()
        names: list[str] = []
        for item in items_str.split(","):
            item = item.strip()
            if not item:
                continue
            # Handle nested aliases: HashMap as Map
            alias_m = re.match(r"(\w+)\s+as\s+(\w+)", item)
            if alias_m:
                names.append(alias_m.group(1))
            elif item == "self":
                names.append("self")
            else:
                names.append(item)
        imports.append(
            ImportInfo(
                module=module,
                names=names,
                alias=None,
                line_number=line_number,
                is_from_import=True,
            )
        )
        return

    # Simple path: use std::io::Read;
    if "::" in path:
        module = path.rsplit("::", 1)[0]
        name = path.rsplit("::", 1)[1]
        imports.append(
            ImportInfo(
                module=module,
                names=[name],
                alias=None,
                line_number=line_number,
                is_from_import=True,
            )
        )
    else:
        imports.append(
            ImportInfo(
                module=path,
                names=[],
                alias=None,
                line_number=line_number,
                is_from_import=False,
            )
        )


# ---------------------------------------------------------------------------
# Function detection
# ---------------------------------------------------------------------------

_FN_RE = re.compile(
    r"^\s*"
    r"((?:pub(?:\([^)]*\))?\s+)?"  # optional pub/pub(crate)
    r"(?:async\s+)?"
    r"(?:const\s+)?"
    r"(?:unsafe\s+)?"
    r'(?:extern\s+"[^"]*"\s+)?)'  # optional extern "C"
    r"fn\s+(\w+)"  # fn name
)

_MACRO_RULES_RE = re.compile(r"^\s*(?:pub(?:\([^)]*\))?\s+)?macro_rules!\s+(\w+)")


def _extract_fn_params(raw: str) -> list[str]:
    """Extract parameter names from Rust fn parameter string."""
    params: list[str] = []
    if not raw.strip():
        return params
    # Remove self-like params
    parts = raw.split(",")
    for part in parts:
        part = part.strip()
        if not part:
            continue
        # Skip self variants
        if part in ("self", "&self", "&mut self", "mut self"):
            continue
        if part.startswith("self:") or part.startswith("&self"):
            continue
        # Pattern: "name: Type" — extract name
        colon_idx = part.find(":")
        if colon_idx > 0:
            name = part[:colon_idx].strip()
            # Handle 'mut name: Type'
            if name.startswith("mut "):
                name = name[4:].strip()
            if name and name.isidentifier():
                params.append(name)
    return params


def _find_fn_params(lines: list[str], start_line_0: int) -> tuple[str, int]:
    """Extract the parameter string from a fn declaration that may span multiple lines.
    Returns (param_string, line_index_after_params)."""
    # Find opening paren
    text = ""
    idx = start_line_0
    while idx < len(lines):
        text += lines[idx] + "\n"
        if "(" in text:
            break
        idx += 1

    # Now count parens to find the closing one
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


# ---------------------------------------------------------------------------
# Attribute / doc-comment collection
# ---------------------------------------------------------------------------


def _collect_attrs_and_docs(lines: list[str], decl_line_0: int) -> tuple[list[str], Optional[str]]:
    """Collect #[...] attributes and /// doc comments above a declaration."""
    attrs: list[str] = []
    doc_lines: list[str] = []
    j = decl_line_0 - 1
    while j >= 0:
        stripped = lines[j].strip()
        if stripped.startswith("///"):
            doc_lines.insert(0, stripped[3:].strip())
            j -= 1
        elif stripped.startswith("#[") or stripped.startswith("#!["):
            # Extract attribute name
            attr_match = re.match(r"#!?\[(\w+)", stripped)
            if attr_match:
                # For derive, include the full derive list
                if attr_match.group(1) == "derive":
                    derive_match = re.match(r"#\[derive\(([^)]+)\)\]", stripped)
                    if derive_match:
                        attrs.insert(0, f"derive({derive_match.group(1).strip()})")
                    else:
                        attrs.insert(0, "derive")
                else:
                    attrs.insert(0, attr_match.group(1))
            j -= 1
        else:
            break
    docstring = "\n".join(doc_lines) if doc_lines else None
    return attrs, docstring


# ---------------------------------------------------------------------------
# Main annotator
# ---------------------------------------------------------------------------


def annotate_rust(source: str, source_name: str = "<source>") -> StructuralMetadata:
    """Parse Rust source and extract structural metadata using regex.

    Detects:
      - fn declarations (pub, async, const, unsafe, extern)
      - struct declarations (regular, tuple, unit)
      - enum declarations
      - trait declarations (with supertraits)
      - impl blocks (inherent + trait impls, methods extracted)
      - use statements (simple, grouped, glob, aliased)
      - #[...] attributes and /// doc comments
      - macro_rules! definitions
    """
    lines = source.split("\n")
    total_lines = len(lines)
    total_chars = len(source)
    line_offsets = _build_line_offsets(source, lines)

    imports = _parse_use_statements(lines)

    functions: list[FunctionInfo] = []
    classes: list[ClassInfo] = []

    consumed: set[int] = set()

    # First pass: detect impl blocks and extract methods
    impl_methods: dict[str, list[FunctionInfo]] = {}  # type_name -> methods

    _IMPL_RE = re.compile(
        r"^\s*impl"
        r"(?:<[^>]*>)?\s+"  # optional generic params
        r"(?:([\w:]+)\s+for\s+)?"  # optional Trait for (supports qualified paths like fmt::Display)
        r"(\w+)"  # Type
    )

    i = 0
    while i < total_lines:
        stripped = lines[i].strip()
        if stripped.startswith("impl") or (stripped.startswith("pub") and " impl" in stripped):
            # Remove pub prefix for matching
            check = stripped
            if check.startswith("pub "):
                check = check[4:].strip()

            m = _IMPL_RE.match(check)
            if m:
                trait_name = m.group(1)  # None for inherent impl
                type_name = m.group(2)

                if "{" in stripped or (i + 1 < total_lines and "{" in lines[i + 1].strip()):
                    impl_end = _find_brace_end(lines, i)
                else:
                    i += 1
                    continue

                # Scan impl body for fn declarations
                for j in range(i + 1, impl_end):
                    if j in consumed:
                        continue
                    fn_stripped = lines[j].strip()
                    fn_m = _FN_RE.match(fn_stripped)
                    if fn_m:
                        fn_name = fn_m.group(2)
                        attrs, docstring = _collect_attrs_and_docs(lines, j)
                        if trait_name:
                            attrs.append(f"impl:{trait_name}")
                        param_str, _ = _find_fn_params(lines, j)
                        params = _extract_fn_params(param_str)

                        if "{" in fn_stripped or (
                            j + 1 < len(lines) and "{" in lines[j + 1].strip()
                        ):
                            fn_end = _find_brace_end(lines, j)
                        else:
                            fn_end = j

                        func_info = FunctionInfo(
                            name=fn_name,
                            qualified_name=f"{type_name}.{fn_name}",
                            line_range=LineRange(start=j + 1, end=fn_end + 1),
                            parameters=params,
                            decorators=attrs,
                            docstring=docstring,
                            is_method=True,
                            parent_class=type_name,
                        )
                        functions.append(func_info)
                        impl_methods.setdefault(type_name, []).append(func_info)

                        for k in range(j, fn_end + 1):
                            consumed.add(k)

                for k in range(i, impl_end + 1):
                    consumed.add(k)
                i = impl_end + 1
                continue
        i += 1

    # Second pass: detect top-level items
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
            or stripped.startswith("#[")
            or stripped.startswith("#![")
        ):
            i += 1
            continue

        # Skip use statements (already parsed)
        if stripped.startswith("use ") or stripped.startswith("pub use "):
            if ";" not in stripped:
                while i < total_lines and ";" not in lines[i]:
                    i += 1
            i += 1
            continue

        # macro_rules!
        macro_m = _MACRO_RULES_RE.match(stripped)
        if macro_m:
            name = macro_m.group(1)
            attrs, docstring = _collect_attrs_and_docs(lines, i)
            if "{" in stripped or (i + 1 < total_lines and "{" in lines[i + 1].strip()):
                end_0 = _find_brace_end(lines, i)
            else:
                end_0 = i
            functions.append(
                FunctionInfo(
                    name=name,
                    qualified_name=name,
                    line_range=LineRange(start=i + 1, end=end_0 + 1),
                    parameters=[],
                    decorators=attrs + ["macro"],
                    docstring=docstring,
                    is_method=False,
                    parent_class=None,
                )
            )
            for k in range(i, end_0 + 1):
                consumed.add(k)
            i = end_0 + 1
            continue

        # Struct
        struct_m = re.match(r"^\s*(?:pub(?:\([^)]*\))?\s+)?struct\s+(\w+)", stripped)
        if struct_m:
            name = struct_m.group(1)
            attrs, docstring = _collect_attrs_and_docs(lines, i)
            if "{" in stripped or (i + 1 < total_lines and "{" in lines[i + 1].strip()):
                end_0 = _find_brace_end(lines, i)
            elif "(" in stripped:
                # Tuple struct: struct Name(T);
                end_0 = _find_semicolon_end(lines, i)
            else:
                # Unit struct: struct Name;
                end_0 = i

            methods = impl_methods.get(name, [])
            classes.append(
                ClassInfo(
                    name=name,
                    line_range=LineRange(start=i + 1, end=end_0 + 1),
                    base_classes=[],
                    methods=methods,
                    decorators=attrs,
                    docstring=docstring,
                )
            )
            for k in range(i, end_0 + 1):
                consumed.add(k)
            i = end_0 + 1
            continue

        # Enum
        enum_m = re.match(r"^\s*(?:pub(?:\([^)]*\))?\s+)?enum\s+(\w+)", stripped)
        if enum_m:
            name = enum_m.group(1)
            attrs, docstring = _collect_attrs_and_docs(lines, i)
            if "{" in stripped or (i + 1 < total_lines and "{" in lines[i + 1].strip()):
                end_0 = _find_brace_end(lines, i)
            else:
                end_0 = i

            methods = impl_methods.get(name, [])
            classes.append(
                ClassInfo(
                    name=name,
                    line_range=LineRange(start=i + 1, end=end_0 + 1),
                    base_classes=[],
                    methods=methods,
                    decorators=attrs,
                    docstring=docstring,
                )
            )
            for k in range(i, end_0 + 1):
                consumed.add(k)
            i = end_0 + 1
            continue

        # Trait
        trait_m = re.match(
            r"^\s*(?:pub(?:\([^)]*\))?\s+)?(?:unsafe\s+)?trait\s+(\w+)"
            r"(?:\s*:\s*(.+?))?"  # optional supertraits
            r"\s*(?:\{|where)",
            stripped,
        )
        if not trait_m:
            # Try simpler match without where/brace requirement
            trait_m = re.match(
                r"^\s*(?:pub(?:\([^)]*\))?\s+)?(?:unsafe\s+)?trait\s+(\w+)"
                r"(?:\s*:\s*([^{]+?))?"
                r"\s*\{?",
                stripped,
            )
        if trait_m and "trait" in stripped:
            name = trait_m.group(1)
            supers_str = trait_m.group(2)
            attrs, docstring = _collect_attrs_and_docs(lines, i)

            bases: list[str] = []
            if supers_str:
                for s in supers_str.split("+"):
                    s = s.strip().rstrip("{").strip()
                    if s and s != "where":
                        # Strip generic params
                        s = re.sub(r"<.*>", "", s).strip()
                        if s:
                            bases.append(s)

            if "{" in stripped or (i + 1 < total_lines and "{" in lines[i + 1].strip()):
                end_0 = _find_brace_end(lines, i)
            else:
                end_0 = i

            # Extract trait method signatures
            trait_methods: list[FunctionInfo] = []
            for j in range(i + 1, end_0):
                fn_stripped = lines[j].strip()
                fn_m = _FN_RE.match(fn_stripped)
                if fn_m:
                    fn_name = fn_m.group(2)
                    param_str, _ = _find_fn_params(lines, j)
                    params = _extract_fn_params(param_str)
                    # Find end: either brace end or semicolon
                    if "{" in fn_stripped or (j + 1 < len(lines) and "{" in lines[j + 1].strip()):
                        fn_end = _find_brace_end(lines, j)
                    elif ";" in fn_stripped:
                        fn_end = j
                    else:
                        fn_end = _find_semicolon_end(lines, j)

                    func_info = FunctionInfo(
                        name=fn_name,
                        qualified_name=f"{name}.{fn_name}",
                        line_range=LineRange(start=j + 1, end=fn_end + 1),
                        parameters=params,
                        decorators=[],
                        docstring=None,
                        is_method=True,
                        parent_class=name,
                    )
                    trait_methods.append(func_info)
                    functions.append(func_info)

            methods = impl_methods.get(name, [])
            classes.append(
                ClassInfo(
                    name=name,
                    line_range=LineRange(start=i + 1, end=end_0 + 1),
                    base_classes=bases,
                    methods=trait_methods + methods,
                    decorators=attrs,
                    docstring=docstring,
                )
            )
            for k in range(i, end_0 + 1):
                consumed.add(k)
            i = end_0 + 1
            continue

        # Top-level function
        fn_m = _FN_RE.match(stripped)
        if fn_m:
            name = fn_m.group(2)
            attrs, docstring = _collect_attrs_and_docs(lines, i)
            param_str, _ = _find_fn_params(lines, i)
            params = _extract_fn_params(param_str)

            if "{" in stripped or (i + 1 < total_lines and "{" in lines[i + 1].strip()):
                end_0 = _find_brace_end(lines, i)
            else:
                end_0 = _find_semicolon_end(lines, i)

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
