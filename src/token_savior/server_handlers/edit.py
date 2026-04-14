"""Handlers for symbol-level edit tools (replace/insert/verify/apply)."""

from __future__ import annotations

import os

from token_savior.edit_ops import insert_near_symbol, replace_symbol_source
from token_savior.server_runtime import _prep
from token_savior.slot_manager import _ProjectSlot
from token_savior.workflow_ops import apply_symbol_change_and_validate


def _h_replace_symbol_source(slot: _ProjectSlot, args: dict) -> object:
    _prep(slot)
    result = replace_symbol_source(
        slot.indexer._project_index,
        args["symbol_name"],
        args["new_source"],
        file_path=args.get("file_path"),
    )
    if result.get("ok"):
        slot.indexer.reindex_file(result["file"])
    return result


def _h_insert_near_symbol(slot: _ProjectSlot, args: dict) -> object:
    _prep(slot)
    result = insert_near_symbol(
        slot.indexer._project_index,
        args["symbol_name"],
        args["content"],
        position=args.get("position", "after"),
        file_path=args.get("file_path"),
    )
    if result.get("ok"):
        slot.indexer.reindex_file(result["file"])
    return result


def _h_verify_edit(slot: _ProjectSlot, args: dict) -> object:
    """P9 — pure static EditSafety certificate, no mutation."""
    from token_savior.edit_ops import resolve_symbol_location
    from token_savior.edit_verifier import verify_edit

    _prep(slot)
    index = slot.indexer._project_index if slot.indexer else None
    if index is None:
        return "Error: index not built. Call reindex first."
    symbol_name = args["symbol_name"]
    new_source = args["new_source"]
    loc = resolve_symbol_location(
        index, symbol_name, file_path=args.get("file_path")
    )
    if "error" in loc:
        return f"Error: {loc['error']}"
    full_path = (
        loc["file"]
        if os.path.isabs(loc["file"])
        else os.path.join(index.root_path, loc["file"])
    )
    try:
        with open(full_path, "r", encoding="utf-8") as fh:
            source_lines = fh.read().splitlines()
    except OSError as exc:
        return f"Error: cannot read {full_path}: {exc}"
    old_source = "\n".join(source_lines[loc["line"] - 1 : loc["end_line"]])
    cert = verify_edit(old_source, new_source, symbol_name, index.root_path)
    return cert.format()


def _h_apply_symbol_change_and_validate(slot: _ProjectSlot, args: dict) -> object:
    _prep(slot)
    return apply_symbol_change_and_validate(
        slot.indexer,
        args["symbol_name"],
        args["new_source"],
        file_path=args.get("file_path"),
        max_tests=args.get("max_tests", 20),
        timeout_sec=args.get("timeout_sec", 120),
        max_output_chars=args.get("max_output_chars", 12000),
        include_output=args.get("include_output", False),
        compact=args.get("compact", False),
        rollback_on_failure=args.get("rollback_on_failure", False),
    )


HANDLERS: dict[str, object] = {
    "replace_symbol_source": _h_replace_symbol_source,
    "insert_near_symbol": _h_insert_near_symbol,
    "verify_edit": _h_verify_edit,
    "apply_symbol_change_and_validate": _h_apply_symbol_change_and_validate,
}
