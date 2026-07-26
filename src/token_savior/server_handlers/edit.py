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


def _refresh_target(slot: _ProjectSlot, args: dict) -> None:
    """Reindexe le fichier vise AVANT de l'editer.

    Les handlers ne reindexaient qu'apres coup. Des que le disque bouge
    autrement que par Token Savior -- un `git checkout`, un script, un autre
    outil -- les plages de lignes memorisees pointent a cote et l'ecriture tape
    sur les mauvaises lignes. Constate plusieurs fois dans une meme session :
    versions anterieures ressuscitees, definitions dupliquees, aucun signal.
    Un reparse d'un fichier ne coute rien devant une corruption silencieuse.
    """
    index = getattr(slot.indexer, "_project_index", None)
    chemin = args.get("file_path")
    if not chemin and index is not None:
        cible = args.get("symbol_name") or args.get("name")
        try:
            meta = index.symbols.get(cible) if hasattr(index, "symbols") else None
            if isinstance(meta, dict):
                chemin = meta.get("file")
            elif isinstance(meta, list) and meta:
                chemin = (meta[0] or {}).get("file")
        except Exception:
            chemin = None
    if not chemin:
        return
    try:
        slot.indexer.reindex_file(chemin)
    except Exception:
        pass


def _h_replace_symbol_source(slot: _ProjectSlot, args: dict) -> object:
    _prep(slot)
    _refresh_target(slot, args)
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
    _refresh_target(slot, args)
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
    _refresh_target(slot, args)
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
    _refresh_target(slot, args)
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
    _refresh_target(slot, args)
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
