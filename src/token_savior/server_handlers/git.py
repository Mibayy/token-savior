"""Handlers for git inspection and patch-summary tools."""

from __future__ import annotations

from token_savior.compact_ops import get_changed_symbols
from token_savior.git_ops import build_commit_summary, summarize_patch_by_symbol
from token_savior.git_tracker import get_git_status
from token_savior.server_runtime import _prep
from token_savior.slot_manager import _ProjectSlot


def _h_get_git_status(slot: _ProjectSlot, args: dict) -> object:
    return get_git_status(slot.root)


def _h_get_changed_symbols(slot: _ProjectSlot, args: dict) -> object:
    _prep(slot)
    return get_changed_symbols(
        slot.indexer._project_index,
        ref=args.get("ref") or args.get("since_ref"),
        max_files=args.get("max_files", 20),
        max_symbols_per_file=args.get("max_symbols_per_file", 20),
    )


def _h_summarize_patch_by_symbol(slot: _ProjectSlot, args: dict) -> object:
    _prep(slot)
    return summarize_patch_by_symbol(
        slot.indexer._project_index,
        changed_files=args.get("changed_files"),
        max_files=args.get("max_files", 20),
        max_symbols_per_file=args.get("max_symbols_per_file", 20),
    )


def _h_build_commit_summary(slot: _ProjectSlot, args: dict) -> object:
    _prep(slot)
    return build_commit_summary(
        slot.indexer._project_index,
        changed_files=args["changed_files"],
        max_files=args.get("max_files", 20),
        max_symbols_per_file=args.get("max_symbols_per_file", 20),
    )


HANDLERS: dict[str, object] = {
    "get_git_status": _h_get_git_status,
    "get_changed_symbols": _h_get_changed_symbols,
    "summarize_patch_by_symbol": _h_summarize_patch_by_symbol,
    "build_commit_summary": _h_build_commit_summary,
}
