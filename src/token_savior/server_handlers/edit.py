"""Handlers for symbol-level edit tools (replace, insert, edit, move, add_field)."""

from __future__ import annotations

from token_savior.edit_ops import (
    add_field_to_model,
    edit_lines_in_symbol,
    insert_near_symbol,
    move_symbol,
    replace_symbol_source,
)
from token_savior.server_runtime import _prep
from token_savior.slot_manager import _ProjectSlot


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


HANDLERS: dict[str, object] = {
    "replace_symbol_source": _h_replace_symbol_source,
    "insert_near_symbol": _h_insert_near_symbol,
    "edit_lines_in_symbol": _h_edit_lines_in_symbol,
    "add_field_to_model": _h_add_field_to_model,
    "move_symbol": _h_move_symbol,
}
