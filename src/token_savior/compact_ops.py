"""Compact, token-efficient operational helpers built on top of the index."""

from __future__ import annotations

from token_savior.git_tracker import get_changed_files, get_head_commit
from token_savior.models import ProjectIndex, StructuralMetadata


def get_changed_symbols(
    index: ProjectIndex,
    max_files: int = 20,
    max_symbols_per_file: int = 20,
) -> dict:
    """Return a compact symbol-oriented summary of current worktree changes."""
    head_ref = get_head_commit(index.root_path)
    changes = get_changed_files(index.root_path, head_ref)

    file_entries: list[dict] = []

    def append_entry(file_path: str, status: str) -> None:
        if len(file_entries) >= max_files:
            return
        metadata = index.files.get(file_path)
        file_entries.append(
            {
                "file": file_path,
                "status": status,
                "symbols": _extract_symbols(metadata, max_symbols_per_file),
            }
        )

    for file_path in changes.modified:
        append_entry(file_path, "modified")
    for file_path in changes.added:
        append_entry(file_path, "added")
    for file_path in changes.deleted:
        append_entry(file_path, "deleted")

    total_symbol_count = sum(len(entry["symbols"]) for entry in file_entries)
    remaining_files = max(
        0, len(changes.modified) + len(changes.added) + len(changes.deleted) - len(file_entries)
    )

    return {
        "modified_files": len(changes.modified),
        "added_files": len(changes.added),
        "deleted_files": len(changes.deleted),
        "reported_files": len(file_entries),
        "remaining_files": remaining_files,
        "reported_symbols": total_symbol_count,
        "files": file_entries,
    }


def _extract_symbols(metadata: StructuralMetadata | None, max_symbols: int) -> list[dict]:
    """Collect a bounded list of structural symbols from a file."""
    if metadata is None:
        return []

    symbols: list[dict] = []

    for func in metadata.functions:
        if len(symbols) >= max_symbols:
            break
        symbols.append(
            {
                "name": func.qualified_name,
                "type": "function",
                "line": func.line_range.start,
            }
        )

    for cls in metadata.classes:
        if len(symbols) >= max_symbols:
            break
        symbols.append(
            {
                "name": cls.name,
                "type": "class",
                "line": cls.line_range.start,
            }
        )

    for section in metadata.sections:
        if len(symbols) >= max_symbols:
            break
        symbols.append(
            {
                "name": section.title,
                "type": "section",
                "line": section.line_range.start,
            }
        )

    return symbols
