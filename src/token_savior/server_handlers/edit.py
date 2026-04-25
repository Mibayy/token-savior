"""Handlers for symbol-level edit tools (replace/insert/verify/apply)."""

from __future__ import annotations

import os

from token_savior.edit_ops import (
    add_field_to_model,
    apply_refactoring,
    edit_lines_in_symbol,
    insert_near_symbol,
    move_symbol,
    replace_symbol_source,
)
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


def _h_edit_lines_in_symbol(slot: _ProjectSlot, args: dict) -> object:
    _prep(slot)
    result = edit_lines_in_symbol(
        slot.indexer._project_index,
        args["symbol_name"],
        args["old_string"],
        args["new_string"],
        file_path=args.get("file_path"),
        replace_all=bool(args.get("replace_all", False)),
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


def _h_add_field_to_model(slot: _ProjectSlot, args: dict) -> object:
    _prep(slot)
    result = add_field_to_model(
        slot.indexer._project_index,
        model=args["model"],
        field_name=args["field_name"],
        field_type=args["field_type"],
        file_path=args.get("file_path"),
        after=args.get("after"),
    )
    if result.get("ok"):
        slot.indexer.reindex_file(result["file"])
    return result


def _h_move_symbol(slot: _ProjectSlot, args: dict) -> object:
    _prep(slot)
    result = move_symbol(
        slot.indexer._project_index,
        symbol_name=args["symbol"],
        target_file=args["target_file"],
        create_if_missing=args.get("create_if_missing", True),
    )
    if result.get("ok"):
        slot.indexer.reindex()
    return result


def _h_apply_refactoring(slot: _ProjectSlot, args: dict) -> object:
    _prep(slot)
    result = apply_refactoring(
        slot.indexer._project_index,
        refactoring_type=args["type"],
        symbol=args.get("symbol"),
        new_name=args.get("new_name"),
        target_file=args.get("target_file"),
        create_if_missing=args.get("create_if_missing", True),
        model=args.get("model"),
        field_name=args.get("field_name"),
        field_type=args.get("field_type"),
        file_path=args.get("file_path"),
        after=args.get("after"),
        start_line=args.get("start_line"),
        end_line=args.get("end_line"),
    )
    if result.get("ok"):
        slot.indexer.reindex()
    return result


HANDLERS: dict[str, object] = {
    "replace_symbol_source": _h_replace_symbol_source,
    "insert_near_symbol": _h_insert_near_symbol,
    "edit_lines_in_symbol": _h_edit_lines_in_symbol,
    "verify_edit": _h_verify_edit,
    "apply_symbol_change_and_validate": _h_apply_symbol_change_and_validate,
    "add_field_to_model": _h_add_field_to_model,
    "move_symbol": _h_move_symbol,
    "apply_refactoring": _h_apply_refactoring,
}
