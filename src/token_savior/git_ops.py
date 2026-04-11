"""Compact git-oriented summaries built on the structural index."""

from __future__ import annotations

from token_savior.compact_ops import _extract_symbols
from token_savior.git_tracker import get_changed_files
from token_savior.models import ProjectIndex


def get_changed_symbols_since_ref(
    index: ProjectIndex,
    since_ref: str,
    max_files: int = 20,
    max_symbols_per_file: int = 20,
) -> dict:
    """Deprecated alias -- use get_changed_symbols(ref=...) instead."""
    from token_savior.compact_ops import get_changed_symbols

    return get_changed_symbols(
        index, ref=since_ref, max_files=max_files, max_symbols_per_file=max_symbols_per_file
    )


def summarize_patch_by_symbol(
    index: ProjectIndex,
    changed_files: list[str] | None = None,
    max_files: int = 20,
    max_symbols_per_file: int = 20,
) -> dict:
    """Summarize current changed files as symbol-level entries for compact review."""
    candidates = changed_files or sorted(index.files.keys())
    entries: list[dict] = []

    for file_path in candidates:
        if len(entries) >= max_files:
            break
        metadata = index.files.get(file_path)
        if metadata is None:
            entries.append({"file": file_path, "status": "unknown", "symbols": []})
            continue
        entries.append(
            {
                "file": file_path,
                "status": "changed",
                "symbols": _extract_symbols(metadata, max_symbols_per_file),
            }
        )

    return {
        "reported_files": len(entries),
        "remaining_files": max(0, len(candidates) - len(entries)),
        "files": entries,
    }


def build_commit_summary(
    index: ProjectIndex,
    changed_files: list[str],
    max_files: int = 20,
    max_symbols_per_file: int = 20,
    compact: bool = False,
) -> dict:
    """Build a compact commit/review summary from changed files."""
    summary = summarize_patch_by_symbol(
        index,
        changed_files=changed_files,
        max_files=max_files,
        max_symbols_per_file=max_symbols_per_file,
    )
    files = summary["files"]
    symbol_count = sum(len(entry.get("symbols", [])) for entry in files)
    top_files = [entry["file"] for entry in files[:5]]
    headline = (
        f"{len(changed_files)} file(s), {symbol_count} symbol(s) affected"
        if changed_files
        else "No changed files supplied"
    )
    payload = {
        "headline": headline,
        "top_files": top_files,
        "reported_files": summary["reported_files"],
        "reported_symbols": symbol_count,
        "files": files,
    }
    if compact:
        return {
            "headline": payload["headline"],
            "top_files": payload["top_files"],
            "reported_symbols": payload["reported_symbols"],
        }
    return payload
