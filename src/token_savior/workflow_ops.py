"""Compact multi-step workflows built on the structural index."""

from __future__ import annotations

from token_savior.checkpoint_ops import create_checkpoint, restore_checkpoint
from token_savior.edit_ops import replace_symbol_source, resolve_symbol_location
from token_savior.git_ops import build_commit_summary
from token_savior.impacted_tests import run_impacted_tests
from token_savior.project_indexer import ProjectIndexer


def apply_symbol_change_and_validate(
    indexer: ProjectIndexer,
    symbol_name: str,
    new_source: str,
    file_path: str | None = None,
    max_tests: int = 20,
    timeout_sec: int = 120,
    max_output_chars: int = 12000,
    include_output: bool = False,
    compact: bool = False,
    rollback_on_failure: bool = False,
) -> dict:
    """Replace a symbol, run impacted tests, optionally rollback on failure.

    When *rollback_on_failure* is True, a checkpoint is created before the edit
    and restored automatically if validation fails (previous behaviour of
    apply_symbol_change_validate_with_rollback).
    """
    index = indexer._project_index
    if index is None:
        return {"error": "Project index is not initialized"}

    # Optional checkpoint for rollback
    checkpoint = None
    location_file = None
    if rollback_on_failure:
        location = resolve_symbol_location(index, symbol_name, file_path=file_path)
        if "error" in location:
            return location
        location_file = location["file"]
        checkpoint = create_checkpoint(index, [location_file])

    edit_result = replace_symbol_source(index, symbol_name, new_source, file_path=file_path)
    if not edit_result.get("ok"):
        return edit_result

    indexer.reindex_file(edit_result["file"])
    validation = run_impacted_tests(
        indexer._project_index,
        changed_files=[edit_result["file"]],
        max_tests=max_tests,
        timeout_sec=timeout_sec,
        max_output_chars=max_output_chars,
        include_output=include_output,
        compact=compact,
    )

    payload = {
        "ok": edit_result.get("ok", False) and validation.get("ok", False),
        "workflow": "apply_symbol_change_and_validate",
        "edit": edit_result,
        "validation": validation,
        "summary": {
            "headline": validation.get("summary", {}).get("headline", "Validation not run"),
            "edited_symbol": edit_result.get("symbol"),
            "edited_file": edit_result.get("file"),
            "tests_run": len(validation.get("selection", {}).get("impacted_tests", [])),
            "validation_ok": validation.get("ok"),
        },
    }

    # Rollback path
    if rollback_on_failure and checkpoint and not payload["ok"]:
        rollback = restore_checkpoint(indexer._project_index, checkpoint["checkpoint_id"])
        if rollback.get("ok"):
            for restored_file in rollback.get("restored_files", []):
                indexer.reindex_file(restored_file)
        commit_summary = build_commit_summary(
            indexer._project_index, [location_file], compact=compact
        )
        if compact:
            return {
                "ok": False,
                "summary": payload["summary"],
                "checkpoint_id": checkpoint["checkpoint_id"],
                "rollback_ok": rollback.get("ok"),
                "commit_summary": commit_summary,
            }
        payload["checkpoint"] = checkpoint
        payload["rollback"] = rollback
        payload["commit_summary"] = commit_summary
        return payload

    # Success path (with optional checkpoint info)
    if rollback_on_failure and checkpoint and payload["ok"]:
        commit_summary = build_commit_summary(
            indexer._project_index, [location_file or edit_result.get("file")], compact=compact
        )
        if compact:
            return {
                "ok": True,
                "summary": payload["summary"],
                "checkpoint_id": checkpoint["checkpoint_id"],
                "commit_summary": commit_summary,
            }
        payload["checkpoint"] = checkpoint
        payload["commit_summary"] = commit_summary
        return payload

    if compact:
        return {
            "ok": payload["ok"],
            "summary": payload["summary"],
            "validation": payload["validation"],
        }
    return payload


def apply_symbol_change_validate_with_rollback(
    indexer: ProjectIndexer,
    symbol_name: str,
    new_source: str,
    file_path: str | None = None,
    max_tests: int = 20,
    timeout_sec: int = 120,
    max_output_chars: int = 12000,
    include_output: bool = False,
    compact: bool = False,
) -> dict:
    """Deprecated alias -- use apply_symbol_change_and_validate(rollback_on_failure=True)."""
    return apply_symbol_change_and_validate(
        indexer,
        symbol_name,
        new_source,
        file_path=file_path,
        max_tests=max_tests,
        timeout_sec=timeout_sec,
        max_output_chars=max_output_chars,
        include_output=include_output,
        compact=compact,
        rollback_on_failure=True,
    )
