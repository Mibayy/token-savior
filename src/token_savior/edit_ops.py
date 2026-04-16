"""Compact structural editing helpers."""

from __future__ import annotations

import os

from token_savior.models import ProjectIndex


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
