"""Handlers for project lifecycle tools (list/switch/set_project_root/reindex)."""

from __future__ import annotations

import os
from typing import Any

from token_savior._compat import TextContent
from token_savior._compat import types

from token_savior import server_state as state
from token_savior.server_runtime import _recompute_leiden
from token_savior.slot_manager import _ProjectSlot
from token_savior.slot_manager import _load_registered_roots
from token_savior.slot_manager import _persist_registered_root


def _hm_list_projects(arguments: dict[str, Any]) -> list[types.TextContent]:
    if not state._slot_mgr.projects:
        return [TextContent(
            type="text",
            text="No projects registered. Call set_project_root('/path') first.",
        )]
    lines = [f"Workspace projects ({len(state._slot_mgr.projects)}):"]
    for root, slot in state._slot_mgr.projects.items():
        status = "indexed" if slot.indexer is not None else "not yet loaded"
        active = " [active]" if root == state._slot_mgr.active_root else ""
        name_part = os.path.basename(root)
        if slot.indexer and slot.indexer._project_index:
            idx = slot.indexer._project_index
            lines.append(
                f"  {name_part}{active} -- {idx.total_files} files, "
                f"{idx.total_functions} functions ({root})"
            )
        else:
            lines.append(f"  {name_part}{active} -- {status} ({root})")
    return [TextContent(type="text", text="\n".join(lines))]


_HINT_REL_PATHS = (".token-savior/hint.md", ".token-savior.md")
_HINT_MAX_CHARS = 2000


def _read_project_hint(project_root: str) -> str | None:
    """Return the contents of a per-project hint file if present.

    Looked up at `<root>/.token-savior/hint.md` first, then `<root>/.token-savior.md`
    as a convenience single-file form. Capped to avoid polluting the context
    with long prose (use CLAUDE.md for that).
    """
    for rel in _HINT_REL_PATHS:
        path = os.path.join(project_root, rel)
        try:
            with open(path, encoding="utf-8") as f:
                text = f.read().strip()
        except OSError:
            continue
        if not text:
            continue
        if len(text) > _HINT_MAX_CHARS:
            text = text[:_HINT_MAX_CHARS] + "\n... [truncated; raise TS_HINT_MAX or split file]"
        return text
    return None


def _resolve_unregistered(hint: str) -> str | None:
    """Resolve a switch_project hint that the SlotManager didn't know about.

    Two sources, in order:
      1. the hint is (or expands to) a real directory path;
      2. a project persisted by a prior set_project_root/switch_project, matched
         by absolute path or (case-insensitive) basename, still on disk.

    Lets the agent reach a project registered in a previous stdio session via
    plain switch_project instead of a fresh set_project_root reindex -- the #1
    latency sink (audit 2026-07-04: 51 calls, p95 1.8s, 14.6s max).
    """
    cand = os.path.abspath(os.path.expanduser(hint))
    if os.path.isdir(cand):
        return cand
    hint_lower = hint.lower()
    for root in _load_registered_roots():
        base = os.path.basename(root)
        if (root == cand or base == hint or base.lower() == hint_lower) and os.path.isdir(root):
            return root
    return None


def _hm_switch_project(arguments: dict[str, Any]) -> list[types.TextContent]:
    hint = arguments["name"]
    slot, err = state._slot_mgr.resolve(hint)
    if err:
        cand = _resolve_unregistered(hint)
        if cand and cand not in state._slot_mgr.projects:
            state._slot_mgr.projects[cand] = _ProjectSlot(root=cand)
            state._slot_mgr.active_root = cand
            slot = state._slot_mgr.projects[cand]
            state._slot_mgr.ensure(slot)  # reuses disk cache when git ref matches
            _persist_registered_root(cand)
            idx = slot.indexer._project_index if slot.indexer else None
            info = f"{idx.total_files} files" if idx else "index not built"
            body = (
                f"Registered and switched to '{os.path.basename(cand)}' "
                f"({cand}) -- {info}."
            )
            hint_body = _read_project_hint(cand)
            if hint_body:
                body += "\n\n--- project hint (.token-savior/hint.md) ---\n" + hint_body
            return [TextContent(type="text", text=body)]
        return [TextContent(type="text", text=f"Error: {err}")]
    already_active = (state._slot_mgr.active_root == slot.root and slot.indexer is not None)
    state._slot_mgr.active_root = slot.root
    if not already_active:
        state._slot_mgr.ensure(slot)
    idx = slot.indexer._project_index if slot.indexer else None
    info = f"{idx.total_files} files" if idx else "index not built"
    prefix = "Already active" if already_active else "Switched to"
    body = f"{prefix} '{os.path.basename(slot.root)}' ({slot.root}) -- {info}."
    hint_body = _read_project_hint(slot.root)
    if hint_body:
        body += "\n\n--- project hint (.token-savior/hint.md) ---\n" + hint_body
    return [TextContent(type="text", text=body)]


def _hm_set_project_root(arguments: dict[str, Any]) -> list[types.TextContent]:
    new_root = os.path.abspath(arguments["path"])
    if not os.path.isdir(new_root):
        return [TextContent(type="text", text=f"Error: '{new_root}' is not a directory.")]
    force = bool(arguments.get("force"))
    already_registered = new_root in state._slot_mgr.projects
    if already_registered and not force:
        # Audit 17/05 (confirmed 26/05): 32% of calls hit an already-registered
        # project. Cheap path avoids the reindex; the nudge below redirects the
        # caller to switch_project for next time -- that's the documented entry
        # point in CLAUDE.md and one round-trip lighter than set_project_root.
        state._slot_mgr.active_root = new_root
        name = os.path.basename(new_root)
        return [TextContent(
            type="text",
            text=(
                f"[NUDGE] Use switch_project('{name}') next time -- the project is "
                f"already registered.\n\n"
                f"Already registered '{new_root}'; switched active project without "
                "reindex. Pass force=true to rebuild the index."
            ),
        )]
    if not already_registered:
        state._slot_mgr.projects[new_root] = _ProjectSlot(root=new_root)
    state._slot_mgr.active_root = new_root
    slot = state._slot_mgr.projects[new_root]
    # Persist so switch_project reaches this project after a stdio restart
    # instead of forcing another set_project_root reindex every session.
    _persist_registered_root(new_root)
    if force:
        slot.indexer = None
        slot.query_fns = None
        state._slot_mgr.build(slot)
        return [TextContent(type="text", text=f"Added and indexed '{new_root}' (rebuilt).")]
    # Cache-aware: ensure() reuses the on-disk index when the git ref matches,
    # avoiding the unconditional full rebuild the old build() call paid every
    # session (the 14.6s outlier in the 2026-07-04 audit).
    state._slot_mgr.ensure(slot)
    return [TextContent(
        type="text",
        text=(
            f"Added and indexed '{new_root}' (cache-aware). "
            "Use switch_project next time."
        ),
    )]


def _hm_reindex(arguments: dict[str, Any]) -> list[types.TextContent]:
    project_hint = arguments.get("project")
    force = bool(arguments.get("force", False))
    slot, err = state._slot_mgr.resolve(project_hint)
    if err:
        return [TextContent(type="text", text=f"Error: {err}")]

    if not force and slot.indexer is not None and slot.indexer._project_index is not None:
        idx = slot.indexer._project_index
        root = slot.root
        stored = idx.file_mtimes
        fresh = True
        for rel_path, mtime in stored.items():
            abs_path = os.path.join(root, rel_path)
            try:
                if os.stat(abs_path).st_mtime != mtime:
                    fresh = False
                    break
            except OSError:
                fresh = False
                break
        if fresh:
            return [TextContent(
                type="text",
                text=(
                    f"Project '{os.path.basename(slot.root)}' already up-to-date "
                    f"({idx.total_files} files, no mtime changes). "
                    "Pass force=true to rebuild anyway."
                ),
            )]

    slot.indexer = None
    slot.query_fns = None
    state._slot_mgr.build(slot)
    _recompute_leiden(slot)
    return [TextContent(
        type="text",
        text=f"Project '{os.path.basename(slot.root)}' re-indexed successfully.",
    )]


HANDLERS: dict[str, Any] = {
    "list_projects": _hm_list_projects,
    "switch_project": _hm_switch_project,
    "set_project_root": _hm_set_project_root,
    "reindex": _hm_reindex,
}
