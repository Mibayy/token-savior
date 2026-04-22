"""Handlers for code-quality and analysis tools.

Covers: analyze_config, find_dead_code, find_hotspots (fused
complexity/allocation/performance kinds), detect_breaking_changes,
find_cross_project_deps, analyze_docker.
"""

from __future__ import annotations

import os
import re
from typing import Any

from token_savior import memory_db
from token_savior import server_state as state
from token_savior.breaking_changes import detect_breaking_changes as run_breaking_changes
from token_savior.complexity import find_hotspots as run_hotspots
from token_savior.config_analyzer import analyze_config as run_config_analysis
from token_savior.cross_project import find_cross_project_deps as run_cross_project
from token_savior.db_schema import get_db_schema as run_get_db_schema
from token_savior.dead_code import find_dead_code as run_dead_code
from token_savior.docker_analyzer import analyze_docker as run_docker_analysis
from token_savior.library_api import (
    get_library_symbol as run_get_library_symbol,
    list_library_symbols as run_list_library_symbols,
)
from token_savior.java_quality import (
    find_allocation_hotspots as run_allocation_hotspots,
    find_performance_hotspots as run_performance_hotspots,
)
from token_savior.models import ProjectIndex
from token_savior.server_runtime import _prep
from token_savior.slot_manager import _ProjectSlot


def _h_analyze_config(slot: _ProjectSlot, args: dict) -> object:
    _prep(slot)
    # Default cap lowered 30 -> 10 to keep MCP output bounded. Bench showed
    # full audits returning ~9.6K chars were dominating active_tokens on
    # tasks where only the top issues matter. Pass explicit max_issues for
    # the full picture.
    return run_config_analysis(
        slot.indexer._project_index,
        checks=args.get("checks"),
        file_path=args.get("file_path"),
        severity=args.get("severity", "all"),
        max_issues=args.get("max_issues", 10),
    )


def _h_find_dead_code(slot: _ProjectSlot, args: dict) -> object:
    _prep(slot)
    loaded: dict[str, ProjectIndex] = {}
    for root, sibling_slot in state._slot_mgr.projects.items():
        state._slot_mgr.ensure(sibling_slot)
        if sibling_slot.indexer and sibling_slot.indexer._project_index:
            loaded[os.path.basename(root)] = sibling_slot.indexer._project_index
    # Default 50 -> 20. The header still prints the true total, so callers
    # know when they need to page. Raise via max_results= for audits.
    return run_dead_code(
        slot.indexer._project_index,
        max_results=args.get("max_results", 20),
        sibling_indices=loaded,
    )


_HOTSPOT_KINDS: dict[str, tuple[object, float]] = {
    "complexity": (run_hotspots, 0.0),
    "allocation": (run_allocation_hotspots, 1.0),
    "performance": (run_performance_hotspots, 1.0),
}


def _h_find_hotspots(slot: _ProjectSlot, args: dict) -> object:
    _prep(slot)
    kind = (args.get("kind") or "complexity").strip().lower()
    entry = _HOTSPOT_KINDS.get(kind)
    if entry is None:
        valid = ", ".join(sorted(_HOTSPOT_KINDS))
        return {"ok": False, "error": f"Unknown kind '{kind}'. Valid: {valid}."}
    runner, default_min = entry
    return runner(
        slot.indexer._project_index,
        max_results=args.get("max_results", 20),
        min_score=args.get("min_score", default_min),
    )


def _h_detect_breaking_changes(slot: _ProjectSlot, args: dict) -> object:
    _prep(slot)
    result = run_breaking_changes(
        slot.indexer._project_index,
        since_ref=args.get("since_ref", "HEAD~1"),
    )
    try:
        if "no breaking changes" not in result:
            saved = 0
            # Only auto-save observations from the BREAKING: section.
            # WARNING: and NON-BREAKING: entries are informational and must
            # not trigger the guardrail flow.
            in_breaking_section = False
            for raw in result.splitlines():
                line = raw.strip()
                if not line:
                    continue
                if line == "BREAKING:":
                    in_breaking_section = True
                    continue
                if line in ("WARNING:", "NON-BREAKING:"):
                    in_breaking_section = False
                    continue
                if not in_breaking_section:
                    continue
                # Accept both the legacy em-dash separator and the new ASCII hyphen.
                m = re.match(r"(.+?):(\d+)\s+(?:-|\u2014)\s+(.+)", line)
                if not m:
                    continue
                file_path, _, message = m.group(1), m.group(2), m.group(3)
                sym_m = re.match(r"(?:function|class|method)\s+(\w+)", message)
                symbol_name = sym_m.group(1) if sym_m else None
                obs_id = memory_db.observation_save(
                    session_id=None,
                    project_root=slot.root,
                    type="guardrail",
                    title=f"Breaking change: {symbol_name or file_path}",
                    content=f"API change detected: {message}",
                    symbol=symbol_name,
                    file_path=file_path,
                    tags=["breaking-change", "api"],
                )
                if obs_id is not None:
                    saved += 1
            if saved:
                result += f"\n\n\u26a0\ufe0f Guardrail auto-saved to memory for {saved} symbol(s)"
    except Exception:
        pass
    return result


def _h_find_cross_project_deps(slot: _ProjectSlot, args: dict) -> object:
    loaded: dict[str, ProjectIndex] = {}
    for root, sibling_slot in state._slot_mgr.projects.items():
        state._slot_mgr.ensure(sibling_slot)
        if sibling_slot.indexer and sibling_slot.indexer._project_index:
            loaded[os.path.basename(root)] = sibling_slot.indexer._project_index
    return run_cross_project(loaded)


def _h_analyze_docker(slot: _ProjectSlot, args: dict) -> object:
    _prep(slot)
    return run_docker_analysis(slot.indexer._project_index)


def _h_audit_file(slot: _ProjectSlot, args: dict) -> object:
    """Mega-batch: dead_code + hotspots + semantic duplicates filtered to one file.

    Replaces three round-trips with a single call when the agent is triaging a
    specific file. Each section is capped tight; raise the per-section limits
    via ``max_dead``/``max_hotspots``/``max_dup_groups`` for a deeper audit.
    """
    _prep(slot)
    file_path = args.get("file_path")
    if not file_path:
        return {"ok": False, "error": "file_path is required"}

    index = slot.indexer._project_index
    out: dict[str, Any] = {"file": file_path}

    # Dead code — filter project-wide result to this file only.
    try:
        loaded: dict[str, ProjectIndex] = {}
        for root, sibling_slot in state._slot_mgr.projects.items():
            state._slot_mgr.ensure(sibling_slot)
            if sibling_slot.indexer and sibling_slot.indexer._project_index:
                loaded[os.path.basename(root)] = sibling_slot.indexer._project_index
        dead_raw = run_dead_code(
            index,
            max_results=args.get("max_dead", 50),
            sibling_indices=loaded,
        )
        dead_lines = [ln for ln in str(dead_raw).splitlines() if file_path in ln]
        out["dead_code"] = "\n".join(dead_lines) if dead_lines else "(none)"
    except Exception as exc:  # pragma: no cover
        out["dead_code"] = f"error: {exc}"

    # Hotspots — same filter.
    try:
        hot_raw = run_hotspots(
            index,
            max_results=args.get("max_hotspots", 50),
            min_score=args.get("min_score", 0.0),
        )
        hot_lines = [ln for ln in str(hot_raw).splitlines() if file_path in ln]
        out["hotspots"] = "\n".join(hot_lines) if hot_lines else "(none)"
    except Exception as exc:  # pragma: no cover
        out["hotspots"] = f"error: {exc}"

    # Semantic duplicates — filter groups that touch the file.
    try:
        qfns = slot.query_fns
        find_dups = qfns.get("find_semantic_duplicates") if qfns else None
        if find_dups is not None:
            dups = find_dups(
                min_lines=args.get("min_lines", 6),
                max_groups=args.get("max_dup_groups", 20),
                max_members_per_group=5,
            )
            touching = []
            for group in dups.get("groups", []) if isinstance(dups, dict) else []:
                members = group.get("members", [])
                if any(m.get("file") == file_path for m in members):
                    touching.append(group)
            out["duplicates"] = touching or "(none)"
        else:
            out["duplicates"] = "(unavailable)"
    except Exception as exc:  # pragma: no cover
        out["duplicates"] = f"error: {exc}"

    return out


def _h_get_db_schema(slot: _ProjectSlot, args: dict) -> object:
    tables = args.get("tables")
    if isinstance(tables, str):
        tables = [t.strip() for t in tables.split(",") if t.strip()]
    return run_get_db_schema(
        slot.root,
        migrations_dir=args.get("migrations_dir"),
        dialect=args.get("dialect", "postgres"),
        tables=tables,
    )


def _h_get_library_symbol(slot: _ProjectSlot, args: dict) -> object:
    package = args.get("package")
    symbol_path = args.get("symbol_path", "")
    if not package:
        return {"ok": False, "error": "'package' is required"}
    return run_get_library_symbol(
        package,
        symbol_path,
        project_root=slot.root,
        max_files=int(args.get("max_files", 200)),
    )


def _h_list_library_symbols(slot: _ProjectSlot, args: dict) -> object:
    package = args.get("package")
    if not package:
        return {"ok": False, "error": "'package' is required"}
    return run_list_library_symbols(
        package,
        project_root=slot.root,
        pattern=args.get("pattern"),
        max_files=int(args.get("max_files", 100)),
        limit=int(args.get("limit", 100)),
    )


HANDLERS: dict[str, Any] = {
    "analyze_config": _h_analyze_config,
    "find_dead_code": _h_find_dead_code,
    "find_hotspots": _h_find_hotspots,
    "detect_breaking_changes": _h_detect_breaking_changes,
    "find_cross_project_deps": _h_find_cross_project_deps,
    "analyze_docker": _h_analyze_docker,
    "audit_file": _h_audit_file,
    "get_db_schema": _h_get_db_schema,
    "get_library_symbol": _h_get_library_symbol,
    "list_library_symbols": _h_list_library_symbols,
}
