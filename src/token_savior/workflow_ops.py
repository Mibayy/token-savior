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
) -> dict:
    """Replace a symbol and run impacted tests with a compact combined summary."""
    index = indexer._project_index
    if index is None:
        return {"error": "Project index is not initialized"}

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
    """Replace a symbol, validate impacted tests, and rollback automatically on failure."""
    index = indexer._project_index
    if index is None:
        return {"error": "Project index is not initialized"}

    location = resolve_symbol_location(index, symbol_name, file_path=file_path)
    if "error" in location:
        return location
    location_file = location["file"]

    checkpoint = create_checkpoint(index, [location_file])
    result = apply_symbol_change_and_validate(
        indexer,
        symbol_name,
        new_source,
        file_path=file_path,
        max_tests=max_tests,
        timeout_sec=timeout_sec,
        max_output_chars=max_output_chars,
        include_output=include_output,
        compact=compact,
    )
    if result.get("ok"):
        changed_file = location_file
        commit_summary = build_commit_summary(
            indexer._project_index, [changed_file], compact=compact
        )
        if compact:
            return {
                "ok": True,
                "summary": result["summary"],
                "checkpoint_id": checkpoint["checkpoint_id"],
                "commit_summary": commit_summary,
            }
        result["checkpoint"] = checkpoint
        result["commit_summary"] = commit_summary
        return result

    rollback = restore_checkpoint(indexer._project_index, checkpoint["checkpoint_id"])
    if rollback.get("ok"):
        for restored_file in rollback.get("restored_files", []):
            indexer.reindex_file(restored_file)
    commit_summary = build_commit_summary(indexer._project_index, [location_file], compact=compact)
    if compact:
        return {
            "ok": False,
            "summary": result.get("summary", {}),
            "checkpoint_id": checkpoint["checkpoint_id"],
            "rollback_ok": rollback.get("ok"),
            "commit_summary": commit_summary,
        }
    result["checkpoint"] = checkpoint
    result["rollback"] = rollback
    result["commit_summary"] = commit_summary
    return result
