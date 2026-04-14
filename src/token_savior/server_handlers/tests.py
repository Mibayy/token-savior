"""Handlers for test-impact tools (find_impacted_test_files, run_impacted_tests)."""

from __future__ import annotations

from token_savior import memory_db
from token_savior.impacted_tests import find_impacted_test_files, run_impacted_tests
from token_savior.server_runtime import _prep
from token_savior.slot_manager import _ProjectSlot


def _h_find_impacted_test_files(slot: _ProjectSlot, args: dict) -> object:
    _prep(slot)
    return find_impacted_test_files(
        slot.indexer._project_index,
        changed_files=args.get("changed_files"),
        symbol_names=args.get("symbol_names"),
        max_tests=args.get("max_tests", 20),
    )


def _h_run_impacted_tests(slot: _ProjectSlot, args: dict) -> object:
    _prep(slot)
    result = run_impacted_tests(
        slot.indexer._project_index,
        changed_files=args.get("changed_files"),
        symbol_names=args.get("symbol_names"),
        max_tests=args.get("max_tests", 20),
        timeout_sec=args.get("timeout_sec", 120),
        max_output_chars=args.get("max_output_chars", 12000),
        include_output=args.get("include_output", False),
        compact=args.get("compact", False),
    )
    try:
        if isinstance(result, dict) and not result.get("ok", True):
            symbols = args.get("symbol_names") or []
            headline = result.get("summary", {}).get("headline", "test failure")
            timed_out = result.get("timed_out", False)
            obs_type = "warning" if timed_out else "error_pattern"
            title = f"Test failure: {headline}"[:120]
            content_parts = [f"Exit code: {result.get('exit_code')}"]
            if timed_out:
                content_parts.append(f"Timed out after {args.get('timeout_sec', 120)}s")
            if result.get("command"):
                content_parts.append(f"Command: {' '.join(result['command'])}")
            tail = result.get("summary", {}).get("tail", [])
            if tail:
                content_parts.append("Last lines:\n" + "\n".join(tail[-5:]))
            obs_id = memory_db.observation_save(
                session_id=None,
                project_root=slot.root,
                type=obs_type,
                title=title,
                content="\n".join(content_parts),
                symbol=symbols[0] if symbols else None,
                tags=["test-failure", "auto"],
            )
            if obs_id is not None:
                if isinstance(result, dict):
                    result["_memory_saved"] = f"#{obs_id}"
    except Exception:
        pass
    return result


HANDLERS: dict[str, object] = {
    "find_impacted_test_files": _h_find_impacted_test_files,
    "run_impacted_tests": _h_run_impacted_tests,
}
