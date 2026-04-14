"""Handlers for checkpoint create/list/restore/diff tools."""

from __future__ import annotations

from token_savior.checkpoint_ops import (
    compare_checkpoint_by_symbol,
    create_checkpoint,
    delete_checkpoint,
    list_checkpoints,
    prune_checkpoints,
    restore_checkpoint,
)
from token_savior.server_runtime import _prep
from token_savior.slot_manager import _ProjectSlot


def _h_create_checkpoint(slot: _ProjectSlot, args: dict) -> object:
    _prep(slot)
    return create_checkpoint(slot.indexer._project_index, args["file_paths"])


def _h_list_checkpoints(slot: _ProjectSlot, args: dict) -> object:
    _prep(slot)
    return list_checkpoints(slot.indexer._project_index)


def _h_delete_checkpoint(slot: _ProjectSlot, args: dict) -> object:
    _prep(slot)
    return delete_checkpoint(slot.indexer._project_index, args["checkpoint_id"])


def _h_prune_checkpoints(slot: _ProjectSlot, args: dict) -> object:
    _prep(slot)
    return prune_checkpoints(slot.indexer._project_index, keep_last=args.get("keep_last", 10))


def _h_restore_checkpoint(slot: _ProjectSlot, args: dict) -> object:
    _prep(slot)
    result = restore_checkpoint(slot.indexer._project_index, args["checkpoint_id"])
    if result.get("ok"):
        for f in result.get("restored_files", []):
            slot.indexer.reindex_file(f)
    return result


def _h_compare_checkpoint_by_symbol(slot: _ProjectSlot, args: dict) -> object:
    _prep(slot)
    return compare_checkpoint_by_symbol(
        slot.indexer._project_index,
        args["checkpoint_id"],
        max_files=args.get("max_files", 20),
    )


HANDLERS: dict[str, object] = {
    "create_checkpoint": _h_create_checkpoint,
    "list_checkpoints": _h_list_checkpoints,
    "delete_checkpoint": _h_delete_checkpoint,
    "prune_checkpoints": _h_prune_checkpoints,
    "restore_checkpoint": _h_restore_checkpoint,
    "compare_checkpoint_by_symbol": _h_compare_checkpoint_by_symbol,
}
