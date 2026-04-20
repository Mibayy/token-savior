"""Compact structural editing helpers."""

from __future__ import annotations

import os

from token_savior.models import ProjectIndex


import re


def replace_symbol_source(
    index: ProjectIndex,
    symbol_name: str,
    new_source: str,
    file_path: str | None = None,
) -> dict:
    """Replace an indexed symbol's full source block without editing the whole file."""
    location = resolve_symbol_location(index, symbol_name, file_path=file_path)
    if "error" in location:
        return location

    target_file = os.path.normpath(os.path.join(index.root_path, location["file"]))
    if os.path.commonpath([target_file, os.path.normpath(index.root_path)]) != os.path.normpath(
        index.root_path
    ):
        return {"error": f"Unsafe file path: {location['file']}"}
    file_result = _replace_line_range(
        target_file,
        location["line"],
        location["end_line"],
        new_source,
    )
    return {
        "ok": True,
        "operation": "replace_symbol_source",
        "symbol": location["name"],
        "type": location["type"],
        "file": location["file"],
        "old_lines": [location["line"], location["end_line"]],
        "new_line_count": file_result["inserted_lines"],
        "delta_lines": file_result["delta_lines"],
    }


def insert_near_symbol(
    index: ProjectIndex,
    symbol_name: str,
    content: str,
    position: str = "after",
    file_path: str | None = None,
) -> dict:
    """Insert content immediately before or after an indexed symbol."""
    if position not in {"before", "after"}:
        return {"error": "position must be 'before' or 'after'"}

    location = resolve_symbol_location(index, symbol_name, file_path=file_path)
    if "error" in location:
        return location

    insertion_line = location["line"] if position == "before" else location["end_line"] + 1
    target_file = os.path.normpath(os.path.join(index.root_path, location["file"]))
    if os.path.commonpath([target_file, os.path.normpath(index.root_path)]) != os.path.normpath(
        index.root_path
    ):
        return {"error": f"Unsafe file path: {location['file']}"}
    file_result = _insert_at_line(target_file, insertion_line, content)
    return {
        "ok": True,
        "operation": "insert_near_symbol",
        "symbol": location["name"],
        "type": location["type"],
        "file": location["file"],
        "position": position,
        "insert_line": insertion_line,
        "inserted_lines": file_result["inserted_lines"],
    }


def resolve_symbol_location(
    index: ProjectIndex,
    symbol_name: str,
    file_path: str | None = None,
) -> dict:
    """Resolve a symbol to file and line range using the structural index."""
    candidate_files = []
    if file_path is not None:
        if file_path in index.files:
            candidate_files.append((file_path, index.files[file_path]))
        else:
            for stored_path, meta in sorted(index.files.items()):
                if stored_path.endswith(file_path) or file_path.endswith(stored_path):
                    candidate_files.append((stored_path, meta))
    elif symbol_name in index.symbol_table:
        stored_path = index.symbol_table[symbol_name]
        meta = index.files.get(stored_path)
        if meta is not None:
            candidate_files.append((stored_path, meta))
    if not candidate_files:
        candidate_files = sorted(index.files.items())

    for stored_path, meta in candidate_files:
        for func in meta.functions:
            if func.qualified_name == symbol_name or func.name == symbol_name:
                return {
                    "name": func.qualified_name,
                    "file": stored_path,
                    "line": func.line_range.start,
                    "end_line": func.line_range.end,
                    "type": "method" if func.is_method else "function",
                }
        for cls in meta.classes:
            if cls.name == symbol_name:
                return {
                    "name": cls.name,
                    "file": stored_path,
                    "line": cls.line_range.start,
                    "end_line": cls.line_range.end,
                    "type": "class",
                }
        for sec in meta.sections:
            if sec.title == symbol_name:
                return {
                    "name": sec.title,
                    "file": stored_path,
                    "line": sec.line_range.start,
                    "end_line": sec.line_range.end,
                    "type": "section",
                }

    return {"error": f"Symbol '{symbol_name}' not found in project"}


def _replace_line_range(file_path: str, start_line: int, end_line: int, content: str) -> dict:
    """Replace an inclusive 1-indexed line range in a file."""
    lines, had_trailing_newline = _read_lines(file_path)
    new_lines = content.splitlines()
    old_count = max(0, end_line - start_line + 1)
    lines[start_line - 1 : end_line] = new_lines
    _write_lines(file_path, lines, had_trailing_newline)
    return {
        "inserted_lines": len(new_lines),
        "delta_lines": len(new_lines) - old_count,
    }


def _insert_at_line(file_path: str, line_number: int, content: str) -> dict:
    """Insert content before the given 1-indexed line number, or append at EOF+1."""
    lines, had_trailing_newline = _read_lines(file_path)
    new_lines = content.splitlines()
    insert_at = max(0, min(line_number - 1, len(lines)))
    lines[insert_at:insert_at] = new_lines
    _write_lines(file_path, lines, had_trailing_newline)
    return {"inserted_lines": len(new_lines)}


def _read_lines(file_path: str) -> tuple[list[str], bool]:
    """Read file as split lines and remember trailing newline state."""
    with open(file_path, encoding="utf-8") as f:
        original = f.read()
    return original.splitlines(), original.endswith("\n")


def _write_lines(file_path: str, lines: list[str], had_trailing_newline: bool) -> None:
    """Write split lines back to disk, preserving trailing newline when possible."""
    updated = "\n".join(lines)
    if lines and had_trailing_newline:
        updated += "\n"
    with open(file_path, "w", encoding="utf-8") as f:
        f.write(updated)


def add_field_to_model(
    index: ProjectIndex,
    model: str,
    field_name: str,
    field_type: str,
    file_path: str | None = None,
    after: str | None = None,
) -> dict:
    """Add a field to a model/class/interface across language boundaries.

    Supports Prisma models, Python dataclasses / SQLAlchemy, and TypeScript
    interfaces / type aliases.  Returns the file path and insertion line.
    """
    loc = resolve_symbol_location(index, model, file_path)
    if "error" in loc:
        return loc

    abs_path = os.path.normpath(os.path.join(index.root_path, loc["file"]))
    _validate_path(abs_path, os.path.normpath(index.root_path))

    start, end = loc["line"], loc["end_line"]
    lines, had_trailing_newline = _read_lines(abs_path)

    ext = os.path.splitext(loc["file"])[1]
    field_line = _format_field_line(ext, field_name, field_type)
    if field_line is None:
        return {"error": f"Unsupported file extension '{ext}' for add_field_to_model"}

    insert_at = _find_insert_position(lines, start, end, after)
    lines.insert(insert_at, field_line)
    _write_lines(abs_path, lines, had_trailing_newline)
    return {
        "ok": True,
        "file": loc["file"],
        "line": insert_at + 1,
        "field": field_line.strip(),
    }


def _validate_path(abs_path: str, root: str) -> None:
    """Ensure path stays within project root."""
    if os.path.commonpath([abs_path, root]) != root:
        msg = f"Path escapes project root: {abs_path}"
        raise ValueError(msg)


def _format_field_line(ext: str, field_name: str, field_type: str) -> str | None:
    """Return the field line formatted for the target language, or None."""
    if ext == ".prisma":
        # Prisma: "  fieldName  Type"
        return f"  {field_name}  {field_type}"
    if ext == ".py":
        # Python dataclass / SQLAlchemy: "    field_name: Type"
        return f"    {field_name}: {field_type}"
    if ext in {".ts", ".tsx"}:
        # TypeScript interface/type: "  fieldName: Type;"
        # Strip trailing ? from type and put it on field name if optional
        if field_type.endswith("?"):
            return f"  {field_name}?: {field_type[:-1]};"
        return f"  {field_name}: {field_type};"
    return None


def _find_insert_position(
    lines: list[str],
    start: int,
    end: int,
    after: str | None,
) -> int:
    """Determine 0-indexed insertion position within a block.

    If *after* is given, inserts after the first line containing that string.
    Otherwise inserts before the last closing brace/bracket line of the block.
    """
    # 0-indexed range
    block_start = start - 1
    block_end = min(end, len(lines))

    if after:
        for i in range(block_start, block_end):
            if after in lines[i]:
                return i + 1

    # Default: before the closing brace/bracket
    for i in range(block_end - 1, block_start, -1):
        stripped = lines[i].strip()
        if stripped in {"}", "}", "};", "):", ")"}:
            return i
    # Fallback: end of block
    return block_end


def move_symbol(
    index: ProjectIndex,
    symbol_name: str,
    target_file: str,
    create_if_missing: bool = True,
) -> dict:
    """Move a symbol from its current file to *target_file*.

    Steps:
    1. Snapshot pre-move callers (dependents from the dependency graph).
    2. Locate and read the symbol source.
    3. Delete the symbol block from the source file.
    4. Append the symbol to the target file (create it if needed).
    5. Update imports in all files that reference the symbol.

    Returns a summary dict with:
      - pre_move_callers: symbols that depended on the moved symbol before the
        move (enumerated for downstream consumers / LLM responses).
      - updated_imports: files whose import statements were rewritten.
      - callers_not_auto_rewritten: pre-move caller files whose imports were
        not automatically rewritten (manual review candidates).
    """
    loc = resolve_symbol_location(index, symbol_name)
    if "error" in loc:
        return loc

    root = os.path.normpath(index.root_path)
    src_rel = loc["file"]
    src_abs = os.path.normpath(os.path.join(root, src_rel))
    tgt_abs = os.path.normpath(os.path.join(root, target_file))
    _validate_path(src_abs, root)
    _validate_path(tgt_abs, root)

    # 1. Pre-move callers snapshot from the reverse dependency graph
    resolved_key = loc["name"] if loc["name"] in index.reverse_dependency_graph else symbol_name
    caller_symbols = sorted(index.reverse_dependency_graph.get(resolved_key, set()))
    pre_move_callers: list[dict] = []
    caller_files: set[str] = set()
    for caller in caller_symbols:
        caller_file = index.symbol_table.get(caller)
        if not caller_file:
            pre_move_callers.append({"symbol": caller, "file": None, "line": None})
            continue
        caller_files.add(caller_file)
        line: int | None = None
        meta = index.files.get(caller_file)
        if meta is not None:
            for func in meta.functions:
                if func.qualified_name == caller or func.name == caller:
                    line = func.line_range.start
                    break
            if line is None:
                for cls in meta.classes:
                    if cls.qualified_name == caller or cls.name == caller:
                        line = cls.line_range.start
                        break
        pre_move_callers.append({"symbol": caller, "file": caller_file, "line": line})

    src_lines, src_trailing = _read_lines(src_abs)
    start_0 = loc["line"] - 1
    end_0 = loc["end_line"]
    symbol_block = src_lines[start_0:end_0]

    del src_lines[start_0:end_0]
    cleaned: list[str] = []
    blank_run = 0
    for line_text in src_lines:
        if line_text.strip() == "":
            blank_run += 1
            if blank_run <= 2:
                cleaned.append(line_text)
        else:
            blank_run = 0
            cleaned.append(line_text)
    _write_lines(src_abs, cleaned, src_trailing)

    if os.path.exists(tgt_abs):
        tgt_lines, tgt_trailing = _read_lines(tgt_abs)
    else:
        if not create_if_missing:
            return {"error": f"Target file does not exist: {target_file}"}
        os.makedirs(os.path.dirname(tgt_abs), exist_ok=True)
        tgt_lines, tgt_trailing = [], True

    if tgt_lines and tgt_lines[-1].strip() != "":
        tgt_lines.append("")
        tgt_lines.append("")
    elif tgt_lines and len(tgt_lines) >= 1:
        if tgt_lines[-1].strip() != "":
            tgt_lines.append("")

    tgt_lines.extend(symbol_block)
    _write_lines(tgt_abs, tgt_lines, tgt_trailing)

    src_module = _file_to_module(src_rel)
    tgt_module = _file_to_module(target_file)
    updated_imports: list[str] = []

    if src_module and tgt_module:
        simple_name = symbol_name.rsplit(".", 1)[-1]
        for rel_path in sorted(index.files):
            if rel_path == src_rel:
                continue
            file_abs = os.path.join(root, rel_path)
            if not os.path.exists(file_abs):
                continue
            try:
                content = open(file_abs, encoding="utf-8").read()
            except Exception:
                continue
            new_content = _rewrite_imports(content, src_module, tgt_module, simple_name)
            if new_content != content:
                with open(file_abs, "w", encoding="utf-8") as f:
                    f.write(new_content)
                updated_imports.append(rel_path)

    callers_not_auto_rewritten = sorted(
        f for f in caller_files if f not in set(updated_imports) and f != src_rel
    )

    return {
        "ok": True,
        "from_file": src_rel,
        "to_file": target_file,
        "symbol": loc["name"],
        "pre_move_callers": pre_move_callers,
        "updated_imports": updated_imports,
        "callers_not_auto_rewritten": callers_not_auto_rewritten,
    }


def _file_to_module(rel_path: str) -> str | None:
    """Convert a relative file path to a dotted module path, or None."""
    if not rel_path:
        return None
    # Strip extension
    base, ext = os.path.splitext(rel_path)
    if ext not in {".py", ".ts", ".tsx", ".js", ".jsx"}:
        return None
    # Convert slashes to dots
    return base.replace("/", ".").replace("\\", ".")


def _rewrite_imports(
    content: str,
    old_module: str,
    new_module: str,
    symbol_name: str,
) -> str:
    """Rewrite import statements from old_module to new_module for symbol_name.

    Handles Python-style and TypeScript-style imports.
    """
    import re

    lines = content.split("\n")
    result = []
    for line in lines:
        # Python: from old_module import symbol_name
        py_match = re.match(
            r"^(\s*from\s+)" + re.escape(old_module) + r"(\s+import\s+)(.*)",
            line,
        )
        if py_match:
            imports_part = py_match.group(3)
            names = [n.strip() for n in imports_part.split(",")]
            if symbol_name in names:
                remaining = [n for n in names if n != symbol_name]
                # Add new import for the moved symbol
                new_import = f"{py_match.group(1)}{new_module}{py_match.group(2)}{symbol_name}"
                if remaining:
                    result.append(
                        f"{py_match.group(1)}{old_module}{py_match.group(2)}{', '.join(remaining)}"
                    )
                result.append(new_import)
                continue

        # Python: import old_module (less common for symbols)
        py_import_match = re.match(
            r"^(\s*import\s+)" + re.escape(old_module) + r"\s*$",
            line,
        )
        if py_import_match:
            result.append(f"{py_import_match.group(1)}{new_module}")
            continue

        # TypeScript/JS: import { symbol } from 'old_module'
        ts_match = re.match(
            r"""^(\s*import\s*\{)([^}]*)\}(\s*from\s*['"])"""
            + re.escape(old_module)
            + r"""(['"].*)\s*$""",
            line,
        )
        if ts_match:
            imports_part = ts_match.group(2)
            names = [n.strip() for n in imports_part.split(",") if n.strip()]
            if symbol_name in names:
                remaining = [n for n in names if n != symbol_name]
                new_import = (
                    f"{ts_match.group(1)} {symbol_name} }}"
                    f"{ts_match.group(3)}{new_module}{ts_match.group(4)}"
                )
                if remaining:
                    result.append(
                        f"{ts_match.group(1)} {', '.join(remaining)} }}"
                        f"{ts_match.group(3)}{old_module}{ts_match.group(4)}"
                    )
                result.append(new_import)
                continue

        result.append(line)

    return "\n".join(result)


# ---------------------------------------------------------------------------
# apply_refactoring — unified dispatcher for rename / move / add_field / extract
# ---------------------------------------------------------------------------

_REFACTORING_TYPES = {"rename", "move", "add_field", "extract"}


def apply_refactoring(
    index: ProjectIndex,
    refactoring_type: str,
    *,
    # rename
    symbol: str | None = None,
    new_name: str | None = None,
    # move
    target_file: str | None = None,
    create_if_missing: bool = True,
    # add_field
    model: str | None = None,
    field_name: str | None = None,
    field_type: str | None = None,
    file_path: str | None = None,
    after: str | None = None,
    # extract
    start_line: int | None = None,
    end_line: int | None = None,
) -> dict:
    """Unified refactoring dispatcher.

    ``refactoring_type`` selects the operation:

    * ``rename``    – rename *symbol* to *new_name* across the project.
    * ``move``      – delegate to :func:`move_symbol`.
    * ``add_field`` – delegate to :func:`add_field_to_model`.
    * ``extract``   – extract lines from *file_path* into a new function *new_name*.
    """
    if refactoring_type not in _REFACTORING_TYPES:
        return {"error": f"Unknown refactoring type '{refactoring_type}'. "
                f"Must be one of: {', '.join(sorted(_REFACTORING_TYPES))}"}

    # ── rename ────────────────────────────────────────────────────────────
    if refactoring_type == "rename":
        if not symbol or not new_name:
            return {"error": "rename requires 'symbol' and 'new_name'"}
        return _refactor_rename(index, symbol, new_name, file_path)

    # ── move ──────────────────────────────────────────────────────────────
    if refactoring_type == "move":
        if not symbol or not target_file:
            return {"error": "move requires 'symbol' and 'target_file'"}
        return move_symbol(index, symbol, target_file, create_if_missing)

    # ── add_field ─────────────────────────────────────────────────────────
    if refactoring_type == "add_field":
        if not model or not field_name or not field_type:
            return {"error": "add_field requires 'model', 'field_name', 'field_type'"}
        return add_field_to_model(index, model, field_name, field_type,
                                  file_path=file_path, after=after)

    # ── extract ───────────────────────────────────────────────────────────
    if refactoring_type == "extract":
        if not file_path or start_line is None or end_line is None or not new_name:
            return {"error": "extract requires 'file_path', 'start_line', 'end_line', 'new_name'"}
        return _refactor_extract(index, file_path, start_line, end_line, new_name)

    return {"error": "unreachable"}  # pragma: no cover


def _refactor_rename(
    index: ProjectIndex,
    symbol_name: str,
    new_name: str,
    file_path: str | None = None,
) -> dict:
    """Rename a symbol and update all references across the project."""
    loc = resolve_symbol_location(index, symbol_name, file_path)
    if "error" in loc:
        return loc

    root = os.path.normpath(index.root_path)
    simple_old = symbol_name.rsplit(".", 1)[-1]
    simple_new = new_name.rsplit(".", 1)[-1]

    # Build word-boundary pattern for the old name
    pattern = re.compile(r"(?<![A-Za-z0-9_])" + re.escape(simple_old) + r"(?![A-Za-z0-9_])")

    updated_files: list[str] = []
    for rel_path in sorted(index.files):
        abs_path = os.path.join(root, rel_path)
        if not os.path.exists(abs_path):
            continue
        try:
            content = open(abs_path, encoding="utf-8").read()
        except Exception:
            continue
        new_content = pattern.sub(simple_new, content)
        if new_content != content:
            with open(abs_path, "w", encoding="utf-8") as f:
                f.write(new_content)
            updated_files.append(rel_path)

    return {
        "ok": True,
        "operation": "rename",
        "old_name": simple_old,
        "new_name": simple_new,
        "files_updated": updated_files,
        "count": len(updated_files),
    }


def _refactor_extract(
    index: ProjectIndex,
    file_path: str,
    start_line: int,
    end_line: int,
    new_name: str,
) -> dict:
    """Extract lines [start_line, end_line] into a new function *new_name*."""
    root = os.path.normpath(index.root_path)
    abs_path = os.path.normpath(os.path.join(root, file_path))
    _validate_path(abs_path, root)

    if not os.path.exists(abs_path):
        return {"error": f"File not found: {file_path}"}

    lines, had_trailing = _read_lines(abs_path)
    if start_line < 1 or end_line > len(lines) or start_line > end_line:
        return {"error": f"Invalid line range [{start_line}, {end_line}] "
                f"(file has {len(lines)} lines)"}

    extracted = lines[start_line - 1:end_line]

    # Detect indentation of the extracted block
    indents = [len(ln) - len(ln.lstrip()) for ln in extracted if ln.strip()]
    base_indent = min(indents) if indents else 0

    # Build the new function body with normalized indentation
    body_lines = []
    for ln in extracted:
        stripped = ln[base_indent:] if len(ln) >= base_indent else ln.lstrip()
        body_lines.append("    " + stripped if stripped else "")

    ext = os.path.splitext(file_path)[1]
    if ext in {".ts", ".tsx", ".js", ".jsx"}:
        func_def = f"function {new_name}() {{\n"
        func_end = "}\n"
    else:
        func_def = f"def {new_name}():\n"
        func_end = ""

    new_func = func_def + "\n".join(body_lines) + "\n" + func_end

    # Replace extracted lines with a call to the new function
    indent_prefix = " " * base_indent
    if ext in {".ts", ".tsx", ".js", ".jsx"}:
        call_line = f"{indent_prefix}{new_name}();"
    else:
        call_line = f"{indent_prefix}{new_name}()"

    lines[start_line - 1:end_line] = [call_line]

    # Append the new function at the end of the file
    if lines and lines[-1].strip() != "":
        lines.append("")
    lines.append("")
    lines.append(new_func.rstrip())
    lines.append("")

    _write_lines(abs_path, lines, had_trailing)

    return {
        "ok": True,
        "operation": "extract",
        "file": file_path,
        "new_function": new_name,
        "extracted_lines": [start_line, end_line],
        "call_inserted_at": start_line,
    }
