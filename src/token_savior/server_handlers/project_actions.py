"""Handlers for project-action tools (discover/run)."""

from __future__ import annotations

from token_savior.project_actions import (
    discover_project_actions,
    run_project_action,
)
from token_savior.slot_manager import _ProjectSlot


def _h_discover_project_actions(slot: _ProjectSlot, args: dict) -> object:
    return discover_project_actions(slot.root)


def _h_run_project_action(slot: _ProjectSlot, args: dict) -> object:
    return run_project_action(
        slot.root,
        args["action_id"],
        timeout_sec=args.get("timeout_sec", 120),
        max_output_chars=args.get("max_output_chars", 12000),
        include_output=args.get("include_output", False),
    )


HANDLERS: dict[str, object] = {
    "discover_project_actions": _h_discover_project_actions,
    "run_project_action": _h_run_project_action,
}
