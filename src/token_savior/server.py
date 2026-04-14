"""Token Savior — MCP server.

Exposes project-wide structural query functions as MCP tools,
enabling Claude Code to navigate codebases efficiently without
reading entire files into context.

Single-project usage (original):
    PROJECT_ROOT=/path/to/project token-savior

Multi-project workspace usage:
    WORKSPACE_ROOTS=/root/hermes-agent,/root/token-savior,/root/improvence token-savior

Each root gets its own isolated index — no symbol collision, no dependency
graph pollution, no shared RAM between unrelated projects.
"""

from __future__ import annotations

import json
import os
import time
import traceback
from typing import Any

from mcp.server.stdio import stdio_server
from mcp.types import Tool, TextContent
import mcp.types as types

from token_savior.checkpoint_ops import (
    compare_checkpoint_by_symbol,
    create_checkpoint,
    delete_checkpoint,
    list_checkpoints,
    prune_checkpoints,
    restore_checkpoint,
)
from token_savior.edit_ops import insert_near_symbol, replace_symbol_source
from token_savior.impacted_tests import find_impacted_test_files, run_impacted_tests
from token_savior.models import ProjectIndex
from token_savior.server_handlers.git import HANDLERS as _GIT_HANDLERS
from token_savior.server_handlers.project_actions import (
    HANDLERS as _PROJECT_ACTION_HANDLERS,
)
from token_savior.workflow_ops import (
    apply_symbol_change_and_validate,
)
from token_savior.breaking_changes import detect_breaking_changes as run_breaking_changes
from token_savior.complexity import find_hotspots as run_hotspots
from token_savior.config_analyzer import analyze_config as run_config_analysis
from token_savior.cross_project import find_cross_project_deps as run_cross_project
from token_savior.dead_code import find_dead_code as run_dead_code
from token_savior.docker_analyzer import analyze_docker as run_docker_analysis
from token_savior.java_quality import (
    find_allocation_hotspots as run_allocation_hotspots,
    find_performance_hotspots as run_performance_hotspots,
)
from token_savior.slot_manager import _ProjectSlot
from token_savior import memory_db

# ---------------------------------------------------------------------------
# Module-level state delegated to server_state
# ---------------------------------------------------------------------------

from token_savior import server_state as s
from token_savior.server_state import server


# ---------------------------------------------------------------------------
# Cross-cutting helpers (compact-output formatting, slot prep, naive-cost
# estimation, async pre-warm, project-root resolution) live in
# server_runtime. Re-exported so existing callers and tests keep working.
# ---------------------------------------------------------------------------

from token_savior.server_runtime import (
    _CLIENT_NAME,
    _SESSION_LABEL,
    _TOOL_COST_MULTIPLIERS,
    _count_and_wrap_result,
    _detect_client_name,
    _estimate_naive_chars_for_call,
    _flush_stats,
    _fmt_lines,
    _format_result,
    _get_stats_file,
    _load_cumulative_stats,
    _parse_workspace_roots,
    _prep,
    _recompute_leiden,
    _register_roots,
    _resolve_project_root,
    _warm_cache_async,
    compress_symbol_output,
)

# Called once at module import so slots exist before any tool call.
_register_roots(_parse_workspace_roots())


def _usage_session_header(elapsed: float, query_calls: int) -> list[str]:
    lines = [f"Session: {_format_duration(elapsed)}, {query_calls} queries"]
    if len(s._slot_mgr.projects) > 1:
        loaded = sum(1 for s in s._slot_mgr.projects.values() if s.indexer is not None)
        lines.append(
            f"Projects: {loaded}/{len(s._slot_mgr.projects)} loaded, active: {os.path.basename(s._slot_mgr.active_root)}"
        )
    return lines


def _usage_tool_counts() -> list[str]:
    if not s._tool_call_counts:
        return []
    top_tools = sorted(
        ((t, c) for t, c in s._tool_call_counts.items() if t != "get_usage_stats"),
        key=lambda x: -x[1],
    )
    tool_str = ", ".join(f"{t}:{c}" for t, c in top_tools[:8])
    if len(top_tools) > 8:
        tool_str += f" +{len(top_tools) - 8} more"
    return [f"Tools: {tool_str}"]


def _usage_chars_savings(source_chars: int, query_calls: int) -> list[str]:
    lines = [f"Chars returned: {s._total_chars_returned:,}"]
    if source_chars > 0 and query_calls > 0 and s._total_naive_chars > s._total_chars_returned:
        reduction = (1 - s._total_chars_returned / s._total_naive_chars) * 100
        lines.append(
            f"Savings: {reduction:.1f}% "
            f"({s._total_chars_returned // 4:,} vs {s._total_naive_chars // 4:,} tokens)"
        )
    return lines


def _usage_csc() -> list[str]:
    if s._csc_hits <= 0:
        return []
    return [f"CSC hits this session: {s._csc_hits} ({s._csc_tokens_saved:,} tokens saved)"]


def _usage_markov() -> list[str]:
    mstats = s._prefetcher.get_stats()
    if mstats["transitions"] <= 0:
        return []
    lines = [
        f"Markov model: {mstats['states']} states, {mstats['transitions']} transitions",
        f"Top sequence: {mstats['top_sequence']}",
    ]
    if "ppm_max_order_active" in mstats:
        coverage = mstats.get("ppm_coverage", {})
        cov_str = ", ".join(
            f"{k.replace('order_', 'o')}:{v}" for k, v in coverage.items() if v > 0
        ) or "none"
        lines.append(
            f"PPM Model: {mstats['ppm_max_order_active']} active | "
            f"last used: order-{mstats.get('ppm_last_order_used', 1)} | "
            f"coverage: {cov_str}"
        )
    with s._prefetch_lock:
        lines.append(f"Prefetch cache: {len(s._prefetch_cache)} warmed entries")
    return lines


def _usage_dcp() -> list[str]:
    if s._dcp_calls == 0 and s._dcp_total_chunks == 0:
        return []
    stable_pct = (
        (s._dcp_stable_chunks / s._dcp_total_chunks * 100)
        if s._dcp_total_chunks else 0.0
    )
    # Each stable chunk ≈ 256B ≈ 64 tokens of cache savings
    benefit_tokens = (s._dcp_stable_chunks * 256) // 4
    return [
        f"DCP: {s._dcp_total_chunks} chunks registered | "
        f"{stable_pct:.0f}% stable | "
        f"est. cache benefit: {benefit_tokens:,}t"
    ]


def _usage_tcs() -> list[str]:
    if s._tcs_calls == 0:
        return []
    tcs_saved = s._tcs_chars_before - s._tcs_chars_after
    tcs_pct = (tcs_saved / s._tcs_chars_before * 100) if s._tcs_chars_before else 0.0
    return [
        f"Schema compression: {s._tcs_calls} calls, "
        f"{s._tcs_chars_before:,} → {s._tcs_chars_after:,} chars "
        f"(-{tcs_pct:.1f}%, ~{tcs_saved // 4:,} tokens saved)"
    ]


def _usage_linucb() -> list[str]:
    try:
        linucb_s = s._linucb.get_stats()
    except Exception:
        return []
    if linucb_s.get("updates", 0) <= 0 and linucb_s.get("scored", 0) <= 0:
        return []
    return [
        f"LinUCB: {linucb_s['updates']} updates | "
        f"{linucb_s['scored']} scored | "
        f"top feature: {linucb_s['top_feature']} ({linucb_s['top_weight']:+.2f})"
    ]


def _usage_warm_start() -> list[str]:
    try:
        ws_s = s._warm_start.get_stats()
    except Exception:
        return []
    if ws_s.get("signatures", 0) <= 0:
        return []
    return [
        f"Warm Start: {ws_s['signatures']} sessions | "
        f"avg similarity: {ws_s.get('avg_pairwise_similarity', 0.0):.0%}"
    ]


def _usage_consistency() -> list[str]:
    try:
        cs_s = memory_db.get_consistency_stats()
    except Exception:
        return []
    if cs_s.get("scored", 0) <= 0:
        return []
    return [
        f"Consistency: {cs_s['scored']} scored | "
        f"{cs_s['quarantined']} quarantined | "
        f"{cs_s['stale_suspected']} stale | "
        f"avg validity: {cs_s['avg_validity']:.0%}"
    ]


def _usage_leiden() -> list[str]:
    try:
        ls = s._leiden.get_stats()
    except Exception:
        return []
    if ls.get("total_communities", 0) <= 0:
        return []
    return [
        f"Leiden: {ls['total_communities']} communities | "
        f"{ls['covered_symbols']} symbols | "
        f"Q={ls['modularity']} | avg size={ls['avg_size']}"
    ]


def _usage_mdl() -> list[str]:
    try:
        mdl_s = memory_db.get_mdl_stats()
    except Exception:
        return []
    if mdl_s.get("abstractions", 0) <= 0 and mdl_s.get("distilled", 0) <= 0:
        return []
    return [
        f"MDL: {mdl_s['abstractions']} abstractions | "
        f"{mdl_s['distilled']} obs distilled"
    ]


def _usage_roi_tokens() -> list[str]:
    try:
        roi_s = memory_db.get_roi_stats()
    except Exception:
        return []
    if roi_s.get("total", 0) <= 0:
        return []
    return [
        f"Token Economy: {roi_s['total']} obs | "
        f"stored {roi_s['total_tokens_stored']:,}t | "
        f"expected savings {roi_s['total_expected_savings']:,.0f}t | "
        f"net ROI {roi_s.get('net_roi', 0):+,.0f} | "
        f"GC candidates: {roi_s['negative_roi_count']}"
    ]


def _usage_speculative_tree() -> list[str]:
    if not (s._spec_branches_explored or s._spec_branches_warmed):
        return []
    hit_rate = (
        (s._spec_branches_hit / s._spec_branches_warmed * 100)
        if s._spec_branches_warmed else 0.0
    )
    return [
        f"Speculative Tree: {s._spec_branches_explored} explored, "
        f"{s._spec_branches_warmed} warmed, {s._spec_branches_hit} hit "
        f"({hit_rate:.1f}%), ~{s._spec_tokens_saved:,} tokens saved"
    ]


def _usage_symbol_reindex() -> list[str]:
    lines: list[str] = []
    for root, slot in s._slot_mgr.projects.items():
        idx = getattr(slot.indexer, "_project_index", None)
        if idx is None:
            continue
        checked = getattr(idx, "last_reindex_symbols_checked", 0)
        if not checked:
            continue
        reindexed = idx.last_reindex_symbols_reindexed
        lines.append(
            f"Symbol-level reindex ({os.path.basename(root)}): "
            f"{reindexed}/{checked} symbols reindexed (last file change)"
        )
    return lines


def _usage_cumulative() -> list[str]:
    all_project_stats = []
    for root, slot in s._slot_mgr.projects.items():
        sf = slot.stats_file or _get_stats_file(root)
        cum = _load_cumulative_stats(sf)
        if cum.get("total_calls", 0) > 0:
            all_project_stats.append((os.path.basename(root.rstrip("/")), cum))

    if not all_project_stats:
        return []

    lines = ["", "Project | Sessions | Queries | Used | Naive | Savings"]
    total_chars = total_naive = total_calls_cum = total_sessions = 0
    for name, cum in sorted(
        all_project_stats, key=lambda x: -x[1].get("total_naive_chars", 0)
    ):
        c = cum.get("total_chars_returned", 0)
        n = cum.get("total_naive_chars", 0)
        sess = cum.get("sessions", 0)
        q = cum.get("total_calls", 0)
        pct = f"{(1 - c / n) * 100:.0f}%" if n > c > 0 else "--"
        lines.append(f"{name} | {sess} | {q} | {c // 4:,} | {n // 4:,} | {pct}")
        total_chars += c
        total_naive += n
        total_calls_cum += q
        total_sessions += sess

    pct = (
        f"{(1 - total_chars / total_naive) * 100:.0f}%"
        if total_naive > total_chars > 0
        else "--"
    )
    lines.append(
        f"TOTAL | {total_sessions} | {total_calls_cum} | {total_chars // 4:,} | {total_naive // 4:,} | {pct}"
    )

    latest_name, latest_stats = max(
        all_project_stats, key=lambda x: x[1].get("last_session", "")
    )
    history = latest_stats.get("history", [])[-3:]
    if history:
        lines.append("")
        lines.append(f"Recent ({latest_name}):")
        for entry in history:
            when = entry.get("timestamp", "")[5:19].replace("T", " ")
            lines.append(
                f"  {when} | {entry.get('query_calls', 0)} queries | "
                f"{entry.get('tokens_used', 0):,} / {entry.get('tokens_naive', 0):,} | "
                f"{entry.get('savings_pct', 0):.0f}%"
            )
    return lines


def _usage_memory_engine_roi() -> list[str]:
    try:
        active_root = s._slot_mgr.active_root or ""
        if not active_root:
            return []
        roi = memory_db.get_injection_stats(active_root)
    except Exception:
        return []
    if roi.get("sessions", 0) <= 0:
        return []
    return [
        "",
        "──────────────────────────────",
        "MEMORY ENGINE ROI",
        "──────────────────────────────",
        f"Sessions tracked : {roi['sessions']}",
        f"Tokens injected  : {roi['total_injected']} "
        f"(avg {roi['avg_injected']}/session)",
        f"Tokens saved est.: {roi['total_saved_est']} "
        f"(avg {roi['avg_saved']}/session)",
        f"ROI ratio        : {roi['roi_ratio']}x",
    ]


def _usage_memory_engine() -> list[str]:
    try:
        db = memory_db.get_db()
        obs_row = db.execute(
            "SELECT COUNT(*) AS total_obs, "
            "COUNT(DISTINCT project_root) AS projects, "
            "COUNT(DISTINCT session_id) AS sessions, "
            "COUNT(DISTINCT type) AS types, "
            "SUM(CASE WHEN symbol IS NOT NULL THEN 1 ELSE 0 END) AS linked_to_symbol "
            "FROM observations WHERE archived=0"
        ).fetchone()
        prompts_count = db.execute("SELECT COUNT(*) FROM user_prompts").fetchone()[0]
        summaries_count = db.execute("SELECT COUNT(*) FROM summaries").fetchone()[0]
        db.close()
    except Exception:
        return []

    linked = obs_row["linked_to_symbol"] or 0
    lines = [
        "",
        "──────────────────────────────",
        "MEMORY ENGINE",
        "──────────────────────────────",
        f"Observations  : {obs_row['total_obs']} ({linked} liées à un symbole)",
        f"Sessions      : {obs_row['sessions'] or 0}",
        f"Projets       : {obs_row['projects'] or 0}",
        f"Summaries     : {summaries_count}",
        f"Prompts       : {prompts_count}",
        f"Types         : {obs_row['types'] or 0} types distincts",
    ]
    lines.extend(_usage_memory_engine_roi())
    return lines


def _format_usage_stats(include_cumulative: bool = False) -> str:
    """Format session usage statistics, optionally with cumulative history."""
    elapsed = time.time() - s._session_start
    total_calls = sum(s._tool_call_counts.values())
    query_calls = total_calls - s._tool_call_counts.get("get_usage_stats", 0)

    source_chars = 0
    for slot in s._slot_mgr.projects.values():
        if slot.indexer and slot.indexer._project_index:
            source_chars += sum(
                m.total_chars for m in slot.indexer._project_index.files.values()
            )

    lines: list[str] = []
    lines.extend(_usage_session_header(elapsed, query_calls))
    lines.extend(_usage_tool_counts())
    lines.extend(_usage_chars_savings(source_chars, query_calls))
    lines.extend(_usage_csc())
    lines.extend(_usage_markov())
    lines.extend(_usage_dcp())
    lines.extend(_usage_tcs())
    lines.extend(_usage_linucb())
    lines.extend(_usage_warm_start())
    lines.extend(_usage_consistency())
    lines.extend(_usage_leiden())
    lines.extend(_usage_mdl())
    lines.extend(_usage_roi_tokens())
    lines.extend(_usage_speculative_tree())
    lines.extend(_usage_symbol_reindex())
    if include_cumulative:
        lines.extend(_usage_cumulative())
    lines.extend(_usage_memory_engine())
    return "\n".join(lines)


def _format_duration(seconds: float) -> str:
    """Format seconds into a human-readable duration."""
    if seconds < 60:
        return f"{seconds:.0f}s"
    minutes = int(seconds // 60)
    secs = int(seconds % 60)
    if minutes < 60:
        return f"{minutes}m {secs}s"
    hours = minutes // 60
    mins = minutes % 60
    return f"{hours}h {mins}m"



# ---------------------------------------------------------------------------
# Tool definitions (schemas live in tool_schemas.py)
# ---------------------------------------------------------------------------

from token_savior.tool_schemas import TOOL_SCHEMAS  # noqa: E402

TOOLS = [Tool(name=name, description=s["description"], inputSchema=s["inputSchema"])
         for name, s in TOOL_SCHEMAS.items()]



# ---------------------------------------------------------------------------
# MCP handlers
# ---------------------------------------------------------------------------


@server.list_tools()
async def list_tools() -> list[Tool]:
    return TOOLS


# ---------------------------------------------------------------------------
# Tool handler functions — each returns a raw result (not wrapped)
# ---------------------------------------------------------------------------


# ── Index-level handlers (slot + ensure + update → result) ────────────────


def _h_create_checkpoint(slot, args):
    _prep(slot)
    return create_checkpoint(slot.indexer._project_index, args["file_paths"])


def _h_list_checkpoints(slot, args):
    _prep(slot)
    return list_checkpoints(slot.indexer._project_index)


def _h_delete_checkpoint(slot, args):
    _prep(slot)
    return delete_checkpoint(slot.indexer._project_index, args["checkpoint_id"])


def _h_prune_checkpoints(slot, args):
    _prep(slot)
    return prune_checkpoints(slot.indexer._project_index, keep_last=args.get("keep_last", 10))


def _h_restore_checkpoint(slot, args):
    _prep(slot)
    result = restore_checkpoint(slot.indexer._project_index, args["checkpoint_id"])
    if result.get("ok"):
        for f in result.get("restored_files", []):
            slot.indexer.reindex_file(f)
    return result


def _h_compare_checkpoint_by_symbol(slot, args):
    _prep(slot)
    return compare_checkpoint_by_symbol(
        slot.indexer._project_index,
        args["checkpoint_id"],
        max_files=args.get("max_files", 20),
    )


def _h_replace_symbol_source(slot, args):
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


def _h_insert_near_symbol(slot, args):
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


def _h_find_impacted_test_files(slot, args):
    _prep(slot)
    return find_impacted_test_files(
        slot.indexer._project_index,
        changed_files=args.get("changed_files"),
        symbol_names=args.get("symbol_names"),
        max_tests=args.get("max_tests", 20),
    )


def _h_run_impacted_tests(slot, args):
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


def _h_verify_edit(slot, args):
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


def _h_apply_symbol_change_and_validate(slot, args):
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


def _h_analyze_config(slot, args):
    _prep(slot)
    return run_config_analysis(
        slot.indexer._project_index,
        checks=args.get("checks"),
        file_path=args.get("file_path"),
        severity=args.get("severity", "all"),
    )


def _h_find_dead_code(slot, args):
    _prep(slot)
    loaded: dict[str, ProjectIndex] = {}
    for root, sibling_slot in s._slot_mgr.projects.items():
        s._slot_mgr.ensure(sibling_slot)
        if sibling_slot.indexer and sibling_slot.indexer._project_index:
            loaded[os.path.basename(root)] = sibling_slot.indexer._project_index
    return run_dead_code(
        slot.indexer._project_index,
        max_results=args.get("max_results", 50),
        sibling_indices=loaded,
    )


def _h_find_hotspots(slot, args):
    _prep(slot)
    return run_hotspots(
        slot.indexer._project_index,
        max_results=args.get("max_results", 20),
        min_score=args.get("min_score", 0.0),
    )


def _h_find_allocation_hotspots(slot, args):
    _prep(slot)
    return run_allocation_hotspots(
        slot.indexer._project_index,
        max_results=args.get("max_results", 20),
        min_score=args.get("min_score", 1.0),
    )


def _h_find_performance_hotspots(slot, args):
    _prep(slot)
    return run_performance_hotspots(
        slot.indexer._project_index,
        max_results=args.get("max_results", 20),
        min_score=args.get("min_score", 1.0),
    )


def _h_detect_breaking_changes(slot, args):
    _prep(slot)
    result = run_breaking_changes(
        slot.indexer._project_index,
        since_ref=args.get("since_ref", "HEAD~1"),
    )
    try:
        if "no breaking changes" not in result:
            import re
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


def _h_find_cross_project_deps(slot, args):
    loaded: dict[str, ProjectIndex] = {}
    for root, s in s._slot_mgr.projects.items():
        s._slot_mgr.ensure(s)
        if s.indexer and s.indexer._project_index:
            loaded[os.path.basename(root)] = s.indexer._project_index
    return run_cross_project(loaded)


def _h_analyze_docker(slot, args):
    _prep(slot)
    return run_docker_analysis(slot.indexer._project_index)


# ── Memory Engine handlers ────────────────────────────────────────────────


def _resolve_memory_project(arguments: dict[str, Any]) -> str:
    """Resolve the project_root for memory tools.

    Falls back from explicit hint → active slot (if it has observations) →
    project with the most observations → active slot → workspace default.
    This lets `memory_index`/`memory_search` work even when the active slot
    is a code project but observations live under a different project_root.
    """
    hint = arguments.get("project")
    if hint:
        slot, _ = s._slot_mgr.resolve(hint)
        if slot:
            return slot.root
        return hint

    active_root = s._slot_mgr.active_root
    if active_root:
        conn = memory_db.get_db()
        try:
            row = conn.execute(
                "SELECT COUNT(*) FROM observations WHERE project_root=?",
                (active_root,),
            ).fetchone()
        finally:
            conn.close()
        if row and row[0] > 0:
            return active_root

    conn = memory_db.get_db()
    try:
        row = conn.execute(
            "SELECT project_root FROM observations "
            "GROUP BY project_root ORDER BY COUNT(*) DESC LIMIT 1"
        ).fetchone()
    finally:
        conn.close()
    if row:
        return row[0]

    return active_root or _resolve_project_root(arguments)


def _mh_memory_bus_push(args: dict[str, Any]) -> str:
    root = _resolve_memory_project(args)
    agent_id = (args.get("agent_id") or "").strip()
    if not agent_id:
        return "Error: agent_id is required."
    title = args["title"]
    content = args["content"]
    obs_id = memory_db.observation_save_volatile(
        project_root=root,
        agent_id=agent_id,
        title=title,
        content=content,
        obs_type=args.get("type", "note"),
        symbol=args.get("symbol"),
        file_path=args.get("file_path"),
        tags=list(args.get("tags") or []),
        ttl_days=int(args.get("ttl_days") or memory_db.DEFAULT_VOLATILE_TTL_DAYS),
    )
    if obs_id is None:
        return "Bus push skipped (duplicate or invalid)."
    return f"🤖 Bus push #{obs_id} from agent '{agent_id}': {title}"


def _mh_memory_bus_list(args: dict[str, Any]) -> str:
    root = _resolve_memory_project(args)
    rows = memory_db.memory_bus_list(
        project_root=root,
        agent_id=args.get("agent_id") or None,
        limit=int(args.get("limit") or 20),
        include_expired=bool(args.get("include_expired", False)),
    )
    if not rows:
        scope = f" (agent '{args.get('agent_id')}')" if args.get("agent_id") else ""
        return f"Bus is quiet{scope}."
    lines = [f"🤖 Inter-agent bus ({len(rows)} live message(s)):"]
    for r in rows:
        lines.append(
            f"  #{r['id']}  [{r.get('agent_id') or '?'}]  "
            f"{r['title']}  —  {r.get('age', '?')}"
        )
    return "\n".join(lines)


def _mh_reasoning_save(args: dict[str, Any]) -> str:
    root = _resolve_memory_project(args)
    goal = (args.get("goal") or "").strip()
    conclusion = (args.get("conclusion") or "").strip()
    steps = args.get("steps") or []
    if not goal or not conclusion:
        return "Error: goal and conclusion are required."
    if not isinstance(steps, list):
        return "Error: steps must be an array."
    rc_id = memory_db.reasoning_save(
        project_root=root,
        goal=goal,
        steps=steps,
        conclusion=conclusion,
        confidence=float(args.get("confidence", 0.8)),
        evidence_obs_ids=args.get("evidence_obs_ids"),
        ttl_days=args.get("ttl_days"),
    )
    if rc_id is None:
        return "Reasoning chain already exists (same goal)."
    return f"🧠 Reasoning chain #{rc_id} saved: {goal[:80]}"


def _mh_reasoning_search(args: dict[str, Any]) -> str:
    root = _resolve_memory_project(args)
    query = (args.get("query") or "").strip()
    if not query:
        return "Error: query is required."
    rows = memory_db.reasoning_search(
        root,
        query,
        threshold=float(args.get("threshold", 0.3)),
        limit=int(args.get("limit", 5)),
    )
    if not rows:
        return "No matching reasoning chains."
    lines = [f"🧠 {len(rows)} matching chain(s):"]
    for r in rows:
        sim = float(r.get("relevance", 0.0) or 0.0)
        lines.append(
            f"  #{r['id']}  sim={sim:.2f}  {r['goal'][:80]}\n    → {r['conclusion'][:100]}"
        )
    return "\n".join(lines)


def _mh_reasoning_list(args: dict[str, Any]) -> str:
    root = _resolve_memory_project(args)
    rows = memory_db.reasoning_list(root, limit=int(args.get("limit", 50)))
    if not rows:
        return "No reasoning chains stored."
    lines = [f"🧠 {len(rows)} reasoning chain(s):"]
    for r in rows:
        lines.append(
            f"  #{r['id']}  [access={r.get('access_count', 0)}]  {r['goal'][:80]}"
        )
    return "\n".join(lines)


def _mh_memory_save(args: dict[str, Any]) -> str:
    root = _resolve_memory_project(args)
    obs_type = args["type"]
    title = args["title"]
    content = args["content"]

    conflicts = memory_db.detect_contradictions(root, title, content, obs_type)
    tags = list(args.get("tags") or [])
    if conflicts and "potential-conflict" not in tags:
        tags.append("potential-conflict")

    semantic = memory_db.semantic_dedup_check(root, title, obs_type, threshold=0.85)
    near_dup_warning = None
    if semantic and semantic["score"] < 0.95:
        near_dup_warning = semantic

    obs_id = memory_db.observation_save(
        args.get("session_id"),
        root,
        obs_type,
        title,
        content,
        why=args.get("why"),
        how_to_apply=args.get("how_to_apply"),
        symbol=args.get("symbol"),
        file_path=args.get("file_path"),
        context=args.get("context"),
        tags=tags,
        importance=args.get("importance", 5),
        is_global=bool(args.get("is_global", False)),
        ttl_days=args.get("ttl_days"),
    )
    if obs_id is None:
        return "Duplicate observation (already exists with same content hash)."
    _invalidate_injection_hash()

    try:
        memory_db.auto_link_observation(
            obs_id, root, contradict_ids=[c["id"] for c in conflicts]
        )
    except Exception:
        pass

    scope = " 🌐 global" if args.get("is_global") else ""
    lines = [f"Observation #{obs_id} saved ({obs_type}: {title}){scope}."]
    if conflicts:
        lines.append("⚠️ Potential contradictions detected:")
        for c in conflicts[:5]:
            lines.append(f"  #{c['id']} [{c['type']}] {c['title']} — check if still valid")
    if near_dup_warning:
        lines.append(
            f"⚠️ Near-duplicate: #{near_dup_warning['id']} "
            f"'{near_dup_warning['title']}' (similarity: {near_dup_warning['score']})"
        )
    return "\n".join(lines)


def _mh_memory_promote(args: dict[str, Any]) -> str:
    dry = bool(args.get("dry_run", True))
    root = args.get("project")
    try:
        target = _resolve_memory_project({"project": root}) if root else ""
    except Exception:
        target = ""
    res = memory_db.run_promotions(project_root=target, dry_run=dry)
    lst = res.get("promoted", [])
    if not lst:
        return "No promotion candidates."
    verb = "Would promote" if dry else "Promoted"
    lines = [f"{verb} {len(lst)} observations:"]
    for p in lst:
        lines.append(
            f"  #{p['id']}  [{p['from_type']}→{p['to_type']}]  {p['title']}  ({p['access_count']} accesses)"
        )
    return "\n".join(lines)


def _mh_memory_export_md(args: dict[str, Any]) -> str:
    import subprocess
    out_dir = args.get("output_dir") or "/root/memory-backup"
    script = "/root/token-savior/scripts/export_markdown.py"
    try:
        proc = subprocess.run(
            ["/root/.local/token-savior-venv/bin/python3", script, "--output-dir", out_dir],
            capture_output=True, text=True, timeout=60,
        )
        out = (proc.stdout or "").strip()
        err = (proc.stderr or "").strip()
        if proc.returncode != 0:
            return f"Export failed: {err or out}"
        return out or f"Exported → {out_dir}"
    except Exception as exc:
        return f"Export error: {exc}"


def _mh_memory_relink(args: dict[str, Any]) -> str:
    root = _resolve_memory_project(args)
    dry = bool(args.get("dry_run", False))
    res = memory_db.relink_all(root, dry_run=dry)
    verb = "Would process" if dry else "Processed"
    return (
        f"{verb} {res['processed']} obs → {res['links_created']} links created "
        f"(total in DB: {res['total_links_in_db']}, delta: +{res['delta']})"
    )


def _mh_memory_top(args: dict[str, Any]) -> str:
    root = _resolve_memory_project(args)
    rows = memory_db.get_top_observations(
        root,
        limit=int(args.get("limit", 20)),
        sort_by=args.get("sort_by", "score"),
    )
    if not rows:
        return "No observations."
    lines = [
        f"{'#':>5}  {'TYPE':12}  {'SCORE':>5}  {'ACC':>4}  TITLE",
        "─" * 70,
    ]
    for r in rows:
        flags = ""
        if r.get("is_global"):
            flags += "🌐"
        if r.get("decay_immune"):
            flags += "🔒"
        title = (r["title"] or "")[:40]
        lines.append(
            f"#{r['id']:<4} [{r['type']:10s}] {r['score']:5.2f}  "
            f"{(r.get('access_count') or 0):4d}  {title} {flags}"
        )
    return "\n".join(lines)


def _mh_memory_why(args: dict[str, Any]) -> str:
    res = memory_db.explain_observation(int(args["id"]), args.get("query"))
    if "error" in res:
        return res["error"]
    lines = [
        "═" * 60,
        f"Why #{res['obs_id']} [{res['type']}] '{res['title']}' appears:",
        "─" * 60,
    ]
    for r in res["reasons"]:
        lines.append(f"  {r}")
    return "\n".join(lines)


def _mh_memory_doctor(args: dict[str, Any]) -> str:
    root = _resolve_memory_project(args)
    issues = memory_db.run_health_check(root)
    summary = issues["summary"]
    lines = ["🏥 Memory Health Check", "══════════════════════"]
    if issues["orphan_symbols"]:
        lines.append(f"⚠️  {len(issues['orphan_symbols'])} orphan symbols:")
        for o in issues["orphan_symbols"][:10]:
            lines.append(f"  #{o['id']} {o['symbol']} ({o['file_path']}) — {o['title']}")
    else:
        lines.append("✅ No orphan symbols")
    if issues["near_duplicates"]:
        lines.append(f"⚠️  {len(issues['near_duplicates'])} near-duplicates:")
        for d in issues["near_duplicates"][:10]:
            lines.append(
                f"  #{d['id_a']} ≈ #{d['id_b']} ({d['score']}) "
                f"'{d['title_a']}' ≈ '{d['title_b']}'"
            )
    else:
        lines.append("✅ No near-duplicates")
    if issues["incomplete_obs"]:
        lines.append(f"⚠️  {len(issues['incomplete_obs'])} incomplete obs:")
        for o in issues["incomplete_obs"][:10]:
            lines.append(f"  #{o['id']} [{o['type']}] {o['title']}")
    else:
        lines.append("✅ No incomplete obs")
    lines.append(f"\nTotal issues: {summary['total_issues']}")
    return "\n".join(lines)


def _mh_memory_distill(args: dict[str, Any]) -> str:
    root = _resolve_memory_project(args)
    dry = args.get("dry_run", True)
    mcs = int(args.get("min_cluster_size", 3))
    cr = float(args.get("compression_required", 0.2))
    res = memory_db.run_mdl_distillation(
        root, dry_run=dry, min_cluster_size=mcs, compression_required=cr,
    )
    header = "MDL Distillation" + (" (dry run)" if dry else "")
    lines = [
        header + ":",
        "─" * 60,
        f" Clusters found: {res['clusters_found']} "
        + (f"→ {sum(len(p['obs_ids']) for p in res['preview'])} obs affected" if res.get("preview") else ""),
    ]
    if not dry:
        lines.append(
            f" Applied: {res['clusters_applied']} clusters | "
            f"{res['abstractions_created']} abstractions created | "
            f"{res['obs_distilled']} obs distilled"
        )
    lines.append(f" Tokens freed (estimate): ~{res['tokens_freed_estimate']:,}t")
    for i, p in enumerate(res.get("preview", [])[:5], 1):
        pct = p["compression_ratio"] * 100
        shared = ", ".join(p["shared_tokens"][:4]) or "(n/a)"
        lines.append("")
        lines.append(
            f"Cluster {i} ({p['size']} obs, -{pct:.0f}% MDL): "
            f"type={p['dominant_type']} ids={p['obs_ids'][:5]}"
        )
        lines.append(f"  Shared: {shared}")
        lines.append(f"  MDL before: {p['mdl_before']:.1f}t → after: {p['mdl_after']:.1f}t")
        head_line = (p["abstraction"].splitlines() or [""])[0]
        lines.append(f"  Abstraction: {head_line[:100]}")
    return "\n".join(lines)


def _mh_memory_roi_gc(args: dict[str, Any]) -> str:
    root = _resolve_memory_project(args)
    dry = args.get("dry_run", True)
    threshold = args.get("threshold")
    res = memory_db.run_roi_gc(root, dry_run=dry, threshold=threshold)
    verb = "Would archive" if dry else "Archived"
    lines = [
        f"💰 Token Economy ROI GC {'(dry run)' if dry else ''}",
        "─" * 60,
        f"{verb}: {res['candidates'] if dry else res['archived']} "
        f"| kept: {res['kept']} "
        f"| threshold: {res['threshold']}",
    ]
    if res.get("preview"):
        lines.append("\nLowest-ROI preview:")
        for c in res["preview"][:10]:
            lines.append(
                f"  #{c['id']} [{c['type']}] roi={c['roi']:+.1f} "
                f"p_hit={c['p_hit']:.3f} tok={c['tokens_stored']} "
                f"ac={c['access_count']} '{c['title'][:60]}'"
            )
    return "\n".join(lines)


def _mh_memory_roi_stats(args: dict[str, Any]) -> str:
    root = _resolve_memory_project(args)
    stats = memory_db.get_roi_stats(root)
    lines = [
        "💰 Token Economy ROI Stats",
        "─" * 60,
        f"Observations: {stats['total']}",
        f"Tokens stored: {stats['total_tokens_stored']:,}",
        f"Expected savings (30d horizon): {stats['total_expected_savings']:,.0f}",
        f"Net ROI: {stats.get('net_roi', 0):+,.0f}",
        f"Negative ROI (GC candidates): {stats['negative_roi_count']}",
        f"λ={stats.get('lambda', 0.05)} | horizon={stats.get('horizon_days', 30)}d",
    ]
    if stats.get("by_type"):
        lines.append("\nBy type:")
        for t, b in sorted(stats["by_type"].items(), key=lambda x: -x[1]["expected_savings"])[:12]:
            lines.append(
                f"  {t:16s} n={b['count']:4d} tok={b['tokens']:6,d} "
                f"exp_savings={b['expected_savings']:,.0f}"
            )
    return "\n".join(lines)


def _mh_memory_patterns(args: dict[str, Any]) -> str:
    root = _resolve_memory_project(args)
    window = int(args.get("window_days", 14))
    min_occ = int(args.get("min_occurrences", 3))
    suggestions = memory_db.analyze_prompt_patterns(
        root, window_days=window, min_occurrences=min_occ
    )
    if not suggestions:
        return f"No recurring patterns (window={window}d, min={min_occ})."
    lines = [f"Found {len(suggestions)} recurring topics without strong memory:"]
    for sug in suggestions:
        lines.append(
            f"  · '{sug['token']}' mentioned {sug['count']}x "
            f"({sug['existing_obs_count']} existing obs)"
        )
        for sp in sug["sample_prompts"][:2]:
            lines.append(f"      > {sp}")
    lines.append("\n💡 Consider memory_save for recurring topics above.")
    return "\n".join(lines)


def _mh_memory_from_bash(args: dict[str, Any]) -> str:
    root = _resolve_memory_project(args)
    command = (args.get("command") or "").strip()
    if not command:
        return "Empty command."
    obs_type = args.get("type") or "command"
    ctx = args.get("context")
    if not ctx:
        import re as _re
        m = _re.search(r"(systemctl|docker|nginx|crontab|hermes|sirius|python3?|pip|npm|apt)\s+(\S+)", command)
        ctx = m.group(0) if m else command[:60]
    title = command[:60] + ("..." if len(command) > 60 else "")
    obs_id = memory_db.observation_save(
        session_id=None,
        project_root=root,
        type=obs_type,
        title=title,
        content=command,
        context=ctx,
        tags=["bash", "command"],
    )
    if obs_id is None:
        return "Duplicate observation (already exists)."
    return f"Saved bash command #{obs_id}: {title}"


def _mh_memory_make_global(args: dict[str, Any]) -> str:
    obs_id = int(args["id"])
    conn = memory_db.get_db()
    cur = conn.execute(
        "UPDATE observations SET is_global=1, updated_at=? WHERE id=?",
        (memory_db._now_iso(), obs_id),
    )
    conn.commit()
    ok = cur.rowcount > 0
    conn.close()
    return f"Observation #{obs_id} is now 🌐 global." if ok else f"Observation #{obs_id} not found."


def _mh_memory_make_local(args: dict[str, Any]) -> str:
    obs_id = int(args["id"])
    conn = memory_db.get_db()
    cur = conn.execute(
        "UPDATE observations SET is_global=0, updated_at=? WHERE id=?",
        (memory_db._now_iso(), obs_id),
    )
    conn.commit()
    ok = cur.rowcount > 0
    conn.close()
    return f"Observation #{obs_id} scoped back to its project." if ok else f"Observation #{obs_id} not found."


def _mh_memory_search(args: dict[str, Any]) -> str:
    root = _resolve_memory_project(args)
    rows = memory_db.observation_search(
        project_root=root,
        query=args["query"],
        type_filter=args.get("type_filter"),
        limit=args.get("limit", 20),
    )
    if not rows:
        return "No observations match the query."
    lines = ["| ID | Type | Title | Importance | Age |", "|---|---|---|---|---|"]
    for r in rows:
        age = r.get("age") or "?"
        glob = "🌐 " if r.get("is_global") else ""
        lines.append(f"| {r['id']} | {r['type']} | {glob}{r['title']} | {r['importance']} | {age} |")
    lines.append(f"\n{len(rows)} results. Use `memory_get` with IDs for full details.")
    return "\n".join(lines)


def _mh_memory_get(args: dict[str, Any]) -> str:
    ids = args["ids"]
    full = bool(args.get("full", False))
    # LinUCB reward: if any of the requested ids was recently injected by
    # memory_index, credit it (reward=1). That's the direct click-through.
    _linucb_credit_reward(ids, reward=1.0)
    all_obs = memory_db.observation_get(ids)
    obs_map = {o["id"]: o for o in all_obs}
    blocks = []
    for obs_id in ids:
        obs = obs_map.get(obs_id)
        if obs is None:
            blocks.append(f"## #{obs_id}\nNot found.")
            continue
        b = [f"## #{obs['id']} — {obs['title']}"]
        b.append(f"**Type:** {obs['type']}  **Importance:** {obs['importance']}  **Created:** {obs['created_at'][:10]}")
        if obs.get("symbol"):
            b.append(f"**Symbol:** `{obs['symbol']}`")
        if obs.get("file_path"):
            b.append(f"**File:** {obs['file_path']}")
        content = obs['content'] or ''
        if not full and len(content) > 80:
            content = content[:80] + "... (use full=true for complete content)"
        b.append(f"\n{content}")
        if obs.get("why"):
            w = obs['why']
            if not full and len(w) > 80:
                w = w[:80] + "..."
            b.append(f"\n**Why:** {w}")
        if obs.get("how_to_apply"):
            h = obs['how_to_apply']
            if not full and len(h) > 80:
                h = h[:80] + "..."
            b.append(f"\n**How to apply:** {h}")
        if obs.get("tags"):
            b.append(f"\n**Tags:** {obs['tags']}")
        try:
            links = memory_db.get_linked_observations(obs["id"])
        except Exception:
            links = {"related": [], "contradicts": [], "supersedes": []}
        if links.get("related"):
            parts = [f"#{lk['id']} [{lk['type']}] {lk['title']}" for lk in links["related"][:5]]
            b.append("\n🔗 See also: " + " · ".join(parts))
        if links.get("contradicts"):
            parts = [f"#{lk['id']} [{lk['type']}] {lk['title']}" for lk in links["contradicts"][:5]]
            b.append("⚠️ Contradicts: " + " · ".join(parts))
        if links.get("supersedes"):
            parts = [f"#{lk['id']} {lk['title']}" for lk in links["supersedes"][:5]]
            b.append("↳ Supersedes: " + " · ".join(parts))
        blocks.append("\n".join(b))
    return "\n\n---\n\n".join(blocks)


def _invalidate_injection_hash() -> None:
    for p in (
        "/root/.local/share/token-savior/last_injected_hash",
        "/root/.local/share/token-savior/last_injected_state.json",
    ):
        try:
            if os.path.exists(p):
                os.remove(p)
        except Exception:
            pass


def _mh_memory_delete(args: dict[str, Any]) -> str:
    obs_list = memory_db.observation_get([args["id"]])
    if not obs_list:
        return f"Observation #{args['id']} not found."
    memory_db.observation_delete(args["id"])
    _invalidate_injection_hash()
    return f"Observation #{args['id']} archived."


def _linucb_credit_reward(obs_ids: list[int], reward: float = 1.0) -> None:
    """Apply LinUCB online update for previously-injected obs ids."""
    now = int(time.time())
    for oid in obs_ids:
        slot = s._linucb_pending.pop(oid, None)
        if slot is None:
            continue
        # Only credit if the click happened within ~30 min of injection.
        if now - slot.get("injected_epoch", now) > 1800:
            continue
        try:
            phi = slot["features"]
            for i in range(s._linucb.FEATURE_DIM):
                for j in range(s._linucb.FEATURE_DIM):
                    s._linucb.A[i][j] += phi[i] * phi[j]
                s._linucb.b[i] += reward * phi[i]
            s._linucb.updates += 1
            if s._linucb.updates % 5 == 0:
                s._linucb.save()
        except Exception:
            pass


def _build_linucb_context(root: str, prompt: str = "") -> dict[str, Any]:
    """Build the feature context used by the LinUCB bandit."""
    try:
        mode_info = memory_db.get_current_mode(root)
    except Exception:
        mode_info = {}
    auto_types = frozenset(mode_info.get("auto_capture_types") or [])
    last_tool = ""
    if s._prefetcher.call_sequence:
        last = s._prefetcher.call_sequence[-1]
        last_tool = last.split(":", 1)[0] if ":" in last else last
    recent_symbols: list[str] = []
    for st in reversed(s._prefetcher.call_sequence[-12:]):
        if ":" in st:
            _, sym = st.split(":", 1)
            if sym and sym not in recent_symbols:
                recent_symbols.append(sym)
        if len(recent_symbols) >= 8:
            break
    # Tokens-used proxy: cap at 200k ≈ context budget.
    tokens_used = s._total_chars_returned / 4.0
    tokens_used_pct = min(1.0, tokens_used / 200_000.0)
    return {
        "prompt": prompt,
        "auto_capture_types": auto_types,
        "last_tool": last_tool,
        "recent_symbols": tuple(recent_symbols),
        "tokens_used_pct": tokens_used_pct,
        "now_epoch": int(time.time()),
    }


def _mh_memory_index(args: dict[str, Any]) -> str:
    root = _resolve_memory_project(args)
    desired_limit = int(args.get("limit") or 10)
    pool_limit = max(30, desired_limit * 3)
    rows = memory_db.get_recent_index(
        project_root=root,
        limit=pool_limit,
        type_filter=args.get("type_filter"),
    )
    if not rows:
        return "No observations yet for this project."

    ctx = _build_linucb_context(root, prompt=args.get("prompt") or "")
    ranked = s._linucb.rank_observations(rows, ctx, top_k=desired_limit)
    if not ranked:
        ranked = [(r, float(r.get("relevance_score", 1.0))) for r in rows[:desired_limit]]

    # Track injected obs for reward attribution.
    now = int(time.time())
    for obs, _score in ranked:
        oid = obs.get("id")
        if oid is not None:
            s._linucb_pending[oid] = {
                "features": s._linucb.extract_features(obs, ctx),
                "context": ctx,
                "injected_epoch": now,
                "access_count_at_inject": int(obs.get("access_count") or 0),
            }

    lines = [
        "| ID | Type | Title | Imp. | Rel. | UCB | Age |",
        "|---|---|---|---|---|---|---|",
    ]
    for obs, ucb in ranked:
        age = obs.get("age") or "?"
        rel = f"{obs.get('relevance_score', 1.0):.2f}"
        glob = "🌐 " if obs.get("is_global") else ""
        lines.append(
            f"| {obs['id']} | {obs['type']} | {glob}{obs['title']} | "
            f"{obs['importance']} | {rel} | {ucb:+.3f} | {age} |"
        )
    lines.append(
        f"\n{len(ranked)} obs (LinUCB-ranked from {len(rows)} candidates). "
        "Use `memory_get` with IDs for full details."
    )
    return "\n".join(lines)


def _mh_memory_timeline(args: dict[str, Any]) -> str:
    root = _resolve_memory_project(args)
    obs_id = args["observation_id"]
    window_hours = args.get("window", 24)
    rows = memory_db.get_timeline_around(
        root,
        obs_id,
        window_hours=window_hours,
    )
    if not rows:
        return f"No timeline context for observation #{obs_id}."
    lines = []
    for r in rows:
        marker = " **⟵**" if r["id"] == obs_id else ""
        date = r["created_at"][:16] if r.get("created_at") else "?"
        lines.append(f"- #{r['id']} [{r['type']}] {r['title']} ({date}){marker}")
    return "\n".join(lines)


def _mh_prompt_save(args: dict[str, Any]) -> str:
    root = _resolve_memory_project(args)
    pid = memory_db.prompt_save(
        None,
        root,
        args["prompt_text"],
        prompt_number=args.get("prompt_number"),
    )
    if pid is None:
        return "Failed to save prompt."
    return f"Prompt #{pid} saved."


def _mh_prompt_search(args: dict[str, Any]) -> str:
    root = _resolve_memory_project(args)
    rows = memory_db.prompt_search(
        project_root=root,
        query=args["query"],
        limit=args.get("limit", 10),
    )
    if not rows:
        return "No prompts match the query."
    lines = ["| ID | # | Excerpt | Date |", "|---|---|---|---|"]
    for r in rows:
        date = r["created_at"][:10] if r.get("created_at") else "?"
        num = r.get("prompt_number") if r.get("prompt_number") is not None else "—"
        excerpt = (r.get("excerpt") or "").replace("|", "\\|").replace("\n", " ")
        lines.append(f"| {r['id']} | {num} | {excerpt} | {date} |")
    lines.append(f"\n{len(rows)} prompt(s) matched.")
    return "\n".join(lines)


def _mh_memory_get_mode(args: dict[str, Any]) -> str:
    try:
        project = _resolve_memory_project({})
    except Exception:
        project = None
    mode = memory_db.get_current_mode(project_root=project)
    lines = [
        f"**Active mode:** `{mode['name']}` · origin: _{mode.get('origin','global')}_ — {mode.get('description', '')}",
        "",
        f"- auto_capture_types   : {', '.join(mode.get('auto_capture_types') or []) or '(none)'}",
        f"- notify_telegram_types: {', '.join(mode.get('notify_telegram_types') or []) or '(none)'}",
        f"- session_summary      : {mode.get('session_summary')}",
        f"- prompt_archive       : {mode.get('prompt_archive')}",
    ]
    all_modes = [m["name"] for m in memory_db.list_modes()]
    lines.append("")
    lines.append(f"Available modes: {', '.join(all_modes)}")
    return "\n".join(lines)


def _mh_memory_set_mode(args: dict[str, Any]) -> str:
    mode = args["mode"]
    ok = memory_db.set_mode(mode, source="manual")
    if not ok:
        return f"Unknown mode: {mode}. Valid: code, review, debug, infra, silent."
    cfg = memory_db.get_current_mode()
    return (
        f"Mode set to `{mode}` (manual — auto-switch disabled until session end) — "
        f"{cfg.get('description', '')}."
    )


def _mh_corpus_build(args: dict[str, Any]) -> str:
    root = _resolve_memory_project(args)
    result = memory_db.corpus_build(
        root,
        args["name"],
        filter_type=args.get("filter_type"),
        filter_tags=args.get("filter_tags"),
        filter_symbol=args.get("filter_symbol"),
    )
    if result["count"] == 0:
        return f"Corpus '{args['name']}' built: 0 observations matched filters."
    tc = result.get("type_counts") or {}
    breakdown = ", ".join(f"{t} x{n}" for t, n in sorted(tc.items(), key=lambda x: -x[1]))
    lines = [f"Corpus '{result['name']}' built: {result['count']} observations ({breakdown})."]
    if result.get("preview"):
        lines.append("")
        lines.append("Preview:")
        for t in result["preview"]:
            lines.append(f"  - {t}")
    return "\n".join(lines)


def _mh_corpus_query(args: dict[str, Any]) -> str:
    root = _resolve_memory_project(args)
    data = memory_db.corpus_get(root, args["name"])
    if not data:
        return f"Corpus '{args['name']}' not found. Build it first with corpus_build."
    obs = data.get("observations") or []
    if not obs:
        return f"Corpus '{args['name']}' is empty."
    lines = [f"=== CORPUS: {args['name']} ({len(obs)} obs) ==="]
    for o in obs:
        lines.append("")
        lines.append(f"### #{o['id']} [{o['type']}] {o['title']}")
        if o.get("symbol"):
            lines.append(f"**Symbol:** `{o['symbol']}`")
        if o.get("file_path"):
            lines.append(f"**File:** {o['file_path']}")
        lines.append("")
        lines.append(o.get("content") or "")
        if o.get("why"):
            lines.append(f"\n**Why:** {o['why']}")
        if o.get("how_to_apply"):
            lines.append(f"\n**How to apply:** {o['how_to_apply']}")
    lines.append("")
    lines.append(f"QUESTION: {args['question']}")
    return "\n".join(lines)


def _mh_memory_decay(args: dict[str, Any]) -> str:
    root = _resolve_memory_project(args) if args.get("project") else None
    dry = args.get("dry_run", True)
    result = memory_db.run_decay(project_root=root, dry_run=dry)
    tag = "DRY RUN — " if dry else ""
    lines = [
        f"**{tag}Decay report** (project: {root or 'all'})",
        "",
        f"- candidates   : {result['candidates']}",
        f"- archived     : {result['archived']}",
        f"- kept active  : {result['kept']}",
        f"- immune       : {result['immune']}",
    ]
    if result.get("preview"):
        lines.append("")
        lines.append("Preview:")
        for p in result["preview"]:
            lines.append(
                f"  #{p['id']} [{p['type']}] {p['title']} "
                f"— accessed {p['access_count']}x — {p['created_at'][:10]}"
            )
    return "\n".join(lines)


def _mh_memory_archived(args: dict[str, Any]) -> str:
    root = _resolve_memory_project(args) if args.get("project") else None
    limit = args.get("limit", 50)
    rows = memory_db.observation_list_archived(project_root=root, limit=limit)
    if not rows:
        return "No archived observations."
    lines = ["| ID | Type | Title | Archived from | Date |", "|---|---|---|---|---|"]
    for r in rows:
        date = r["created_at"][:10] if r.get("created_at") else "?"
        proj = (r.get("project_root") or "").rsplit("/", 1)[-1]
        lines.append(f"| {r['id']} | {r['type']} | {r['title']} | {proj} | {date} |")
    lines.append(f"\n{len(rows)} archived observation(s).")
    return "\n".join(lines)


def _mh_memory_restore(args: dict[str, Any]) -> str:
    ok = memory_db.observation_restore(args["id"])
    return (
        f"Observation #{args['id']} restored."
        if ok
        else f"Observation #{args['id']} not found or already active."
    )


def _mh_memory_set_project_mode(args: dict[str, Any]) -> str:
    project = args["project"]
    mode_name = args["mode"]
    ok = memory_db.set_project_mode(project, mode_name)
    if not ok:
        return f"Unknown mode '{mode_name}'. Valid: code, review, debug, silent."
    return f"Project {project} → mode {mode_name}"


def _mh_memory_status(args: dict[str, Any]) -> str:
    project = _resolve_memory_project(args)
    db = memory_db.get_db()
    active = db.execute(
        "SELECT COUNT(*) FROM observations WHERE project_root=? AND archived=0",
        [project],
    ).fetchone()[0]
    archived = db.execute(
        "SELECT COUNT(*) FROM observations WHERE project_root=? AND archived=1",
        [project],
    ).fetchone()[0]
    sessions = db.execute(
        "SELECT COUNT(*) FROM sessions WHERE project_root=?", [project]
    ).fetchone()[0]
    last_session = db.execute(
        "SELECT created_at, end_type, status FROM sessions "
        "WHERE project_root=? ORDER BY id DESC LIMIT 1",
        [project],
    ).fetchone()
    last_summary = db.execute(
        "SELECT created_at FROM summaries WHERE project_root=? ORDER BY id DESC LIMIT 1",
        [project],
    ).fetchone()
    summary_count = db.execute(
        "SELECT COUNT(*) FROM summaries WHERE project_root=?", [project]
    ).fetchone()[0]
    prompts = db.execute(
        "SELECT COUNT(*) FROM user_prompts WHERE project_root=?", [project]
    ).fetchone()[0]
    db.close()

    mode = memory_db.get_current_mode()
    mode_name = mode.get("name", "code")

    sess_line = f"{sessions}"
    if last_session:
        day = (last_session["created_at"] or "")[:10]
        et = last_session["end_type"] or last_session["status"] or "?"
        sess_line = f"{sessions} (last: {day} end={et})"

    sum_line = f"{summary_count}"
    if last_summary:
        sum_line = f"{summary_count} (last: {(last_summary['created_at'] or '')[:10]})"

    rows = [
        ("Project", project),
        ("Mode", mode_name),
        ("Obs", f"{active} active · {archived} archived"),
        ("Sessions", sess_line),
        ("Summaries", sum_line),
        ("Prompts", f"{prompts} archived"),
    ]
    label_w = max(len(r[0]) for r in rows)
    val_w = max(len(r[1]) for r in rows)
    inner = label_w + val_w + 5
    top = "┌─ Memory Engine Status " + "─" * (inner - len(" Memory Engine Status ") - 1) + "┐"
    bot = "└" + "─" * (inner) + "┘"
    body = [f"│ {k.ljust(label_w)} : {v.ljust(val_w)} │" for k, v in rows]
    return "\n".join([top] + body + [bot])


def _mh_memory_set_global(args: dict[str, Any]) -> str:
    if args.get("is_global"):
        return _mh_memory_make_global({"id": args["id"]})
    return _mh_memory_make_local({"id": args["id"]})


def _mh_memory_mode(args: dict[str, Any]) -> str:
    action = args.get("action", "get")
    if action == "get":
        return _mh_memory_get_mode({})
    if action == "set":
        return _mh_memory_set_mode({"mode": args["mode"]})
    if action == "set_project":
        return _mh_memory_set_project_mode({"project": args["project"], "mode": args["mode"]})
    return f"Unknown action: {action}"


def _mh_memory_archive(args: dict[str, Any]) -> str:
    action = args.get("action", "list")
    if action == "run":
        return _mh_memory_decay({"dry_run": args.get("dry_run", True), "project": args.get("project")})
    if action == "list":
        return _mh_memory_archived({"limit": args.get("limit", 50), "project": args.get("project")})
    if action == "restore":
        return _mh_memory_restore({"id": args["id"]})
    return f"Unknown action: {action}"


def _mh_memory_maintain(args: dict[str, Any]) -> str:
    action = args.get("action")
    if action == "promote":
        return _mh_memory_promote({"dry_run": args.get("dry_run", True), "project": args.get("project")})
    if action == "relink":
        return _mh_memory_relink({"dry_run": args.get("dry_run", False)})
    if action == "export":
        return _mh_memory_export_md({"output_dir": args.get("output_dir", "/root/memory-backup")})
    if action == "patterns":
        return _mh_memory_patterns({
            "window_days": args.get("window_days", 14),
            "min_occurrences": args.get("min_occurrences", 3),
        })
    return f"Unknown action: {action}"


def _mh_memory_prompts(args: dict[str, Any]) -> str:
    action = args.get("action")
    if action == "save":
        return _mh_prompt_save({
            "prompt_text": args["prompt_text"],
            "prompt_number": args.get("prompt_number"),
            "project": args.get("project"),
        })
    if action == "search":
        return _mh_prompt_search({
            "query": args["query"],
            "limit": args.get("limit", 10),
            "project": args.get("project"),
        })
    return f"Unknown action: {action}"


# ---------------------------------------------------------------------------
# Meta handlers — stats/admin tools that don't need a project slot. Each
# returns a list[TextContent] so call_tool() can delegate directly.
# ---------------------------------------------------------------------------


def _hm_get_usage_stats(arguments: dict[str, Any]) -> list[types.TextContent]:
    return [TextContent(type="text", text=_format_usage_stats(include_cumulative=True))]


def _hm_get_session_budget(arguments: dict[str, Any]) -> list[types.TextContent]:
    project = _resolve_memory_project(arguments)
    budget = int(arguments.get("budget_tokens") or memory_db.DEFAULT_SESSION_BUDGET_TOKENS)
    stats = memory_db.get_session_budget_stats(project, budget_tokens=budget)
    return [TextContent(type="text", text=memory_db.format_session_budget_box(stats))]


def _hm_get_coactive_symbols(arguments: dict[str, Any]) -> list[types.TextContent]:
    seed = (arguments.get("name") or "").strip()
    if not seed:
        return [TextContent(type="text", text="Error: 'name' required.")]
    top_k = int(arguments.get("top_k", 5))
    co = s._tca_engine.get_coactive_symbols(seed, top_k=top_k)
    if not co:
        return [TextContent(type="text", text=f"No co-activation data yet for '{seed}'.")]
    lines = [f"🔄 Co-active with '{seed}' (top {len(co)}):"]
    for sym, pmi in co:
        lines.append(f"  {pmi:+.3f}  {sym}")
    return [TextContent(type="text", text="\n".join(lines))]


def _hm_get_tca_stats(arguments: dict[str, Any]) -> list[types.TextContent]:
    stats = s._tca_engine.get_stats()
    lines = [
        "Tenseur de Co-Activation (TCA):",
        f"  Symbols tracked      : {stats['symbols_tracked']}",
        f"  Co-activation pairs  : {stats['co_activation_pairs']}",
        f"  Sessions flushed     : {stats['sessions_flushed']}",
        f"  Current session      : {stats['session_activations']} symbols active",
    ]
    if stats["top_pairs"]:
        lines.append("  Top pairs:")
        for a, b, c in stats["top_pairs"]:
            lines.append(f"    ×{c}  {a}  ↔  {b}")
    return [TextContent(type="text", text="\n".join(lines))]


def _hm_get_dcp_stats(arguments: dict[str, Any]) -> list[types.TextContent]:
    stats = memory_db.dcp_stats()
    lines = [
        "Differential Context Protocol (DCP):",
        f"  Registered chunks : {stats['total']}",
        f"  Stable (seen>1)   : {stats['stable']}",
        f"  Total sightings   : {stats['total_seen']:,}",
    ]
    if s._dcp_calls:
        sess_pct = (s._dcp_stable_chunks / s._dcp_total_chunks * 100) if s._dcp_total_chunks else 0.0
        lines.append(
            f"  Session: {s._dcp_calls} DCP calls, "
            f"{s._dcp_stable_chunks}/{s._dcp_total_chunks} chunks stable ({sess_pct:.1f}%)"
        )
    if stats["top"]:
        lines.append("  Top chunks:")
        for t in stats["top"]:
            preview = (t["content_preview"] or "").replace("\n", " ")[:40]
            lines.append(f"    {t['fingerprint']}  ×{t['seen_count']}  {preview!r}")
    return [TextContent(type="text", text="\n".join(lines))]


def _hm_get_community(arguments: dict[str, Any]) -> list[types.TextContent]:
    sym = arguments.get("symbol")
    cname = arguments.get("name")
    if not sym and not cname:
        return [TextContent(type="text", text="Error: provide 'symbol' or 'name'.")]
    comm = s._leiden.get_community_for(sym) if sym else s._leiden.get_community(cname)
    if not comm:
        hint = sym or cname
        return [TextContent(type="text", text=f"No community for '{hint}'.")]
    lines = [
        f"🏘️  Community '{comm['name']}' — {comm['size']} members",
        "─" * 60,
    ]
    for m in comm["members"]:
        marker = " ← query" if m == sym else ""
        lines.append(f"  {m}{marker}")
    return [TextContent(type="text", text="\n".join(lines))]


def _hm_get_linucb_stats(arguments: dict[str, Any]) -> list[types.TextContent]:
    stats = s._linucb.get_stats()
    lines = ["LinUCB Injection Model:", "  Feature weights (θ):"]
    for i, fw in enumerate(stats["feature_weights"]):
        marker = "  ← top feature" if i == 0 else ""
        lines.append(f"    {fw['name']:<14}: {fw['weight']:+.4f}{marker}")
    lines.append(f"  Updates: {stats['updates']} | Observations scored: {stats['scored']}")
    return [TextContent(type="text", text="\n".join(lines))]


def _hm_get_warmstart_stats(arguments: dict[str, Any]) -> list[types.TextContent]:
    stats = s._warm_start.get_stats()
    lines = [
        "Cross-session Warm Start:",
        f"  Signatures stored   : {stats['signatures']}",
        f"  Avg pairwise cosine : {stats.get('avg_pairwise_similarity', 0.0):.3f}",
    ]
    by_proj = stats.get("by_project") or {}
    if by_proj:
        lines.append("  By project:")
        for p, n in sorted(by_proj.items(), key=lambda x: -x[1])[:5]:
            label = p if p != "(none)" else "(unknown)"
            lines.append(f"    {label} — {n}")
    return [TextContent(type="text", text="\n".join(lines))]


def _hm_memory_consistency(arguments: dict[str, Any]) -> list[types.TextContent]:
    proj = arguments.get("project_root") or None
    limit = int(arguments.get("limit") or 100)
    dry = bool(arguments.get("dry_run") or False)
    res = memory_db.run_consistency_check(project_root=proj, limit=limit, dry_run=dry)
    stats = memory_db.get_consistency_stats(project_root=proj)
    tag = " [dry-run]" if dry else ""
    lines = [
        f"Self-consistency check{tag}:",
        f"  Checked            : {res['checked']}",
        f"  Failures (moved)   : {res['failed']}",
        f"  → quarantined      : {res['quarantined']}",
        f"  → stale_suspected  : {res['stale_suspected']}",
        "",
        "Aggregate:",
        f"  Scored obs         : {stats['scored']}",
        f"  Currently quarantined : {stats['quarantined']}",
        f"  Currently stale       : {stats['stale_suspected']}",
        f"  Average validity   : {stats['avg_validity']:.2%}",
    ]
    return [TextContent(type="text", text="\n".join(lines))]


def _hm_memory_quarantine_list(arguments: dict[str, Any]) -> list[types.TextContent]:
    proj = arguments.get("project_root") or None
    limit = int(arguments.get("limit") or 50)
    rows = memory_db.list_quarantined_observations(project_root=proj, limit=limit)
    if not rows:
        return [TextContent(type="text", text="No quarantined observations.")]
    lines = [f"⚠️  Quarantined observations ({len(rows)}):"]
    for r in rows:
        sym = f" [{r['symbol']}]" if r.get("symbol") else ""
        lines.append(
            f"  #{r['id']}  [{r['type']}]  {r['title']}{sym}  "
            f"validity={r['validity']:.0%}  {r['age']}"
        )
    return [TextContent(type="text", text="\n".join(lines))]


def _hm_get_leiden_stats(arguments: dict[str, Any]) -> list[types.TextContent]:
    stats = s._leiden.get_stats()
    lines = [
        "Leiden community detector:",
        f"  Communities        : {stats['total_communities']}",
        f"  Covered symbols    : {stats['covered_symbols']}",
        f"  Size min/avg/max   : {stats['smallest']}/{stats['avg_size']}/{stats['largest']}",
        f"  Graph              : {stats['nodes']} nodes, {stats['edges']} edges",
        f"  Modularity (Q)     : {stats['modularity']}",
    ]
    if stats.get("top"):
        lines.append("  Top communities:")
        for c in stats["top"]:
            lines.append(f"    {c['name']} (n={c['size']})")
    return [TextContent(type="text", text="\n".join(lines))]


def _hm_get_speculation_stats(arguments: dict[str, Any]) -> list[types.TextContent]:
    with s._prefetch_lock:
        cache_size = len(s._prefetch_cache)
    hit_rate = (s._spec_branches_hit / s._spec_branches_warmed * 100) if s._spec_branches_warmed else 0.0
    lines = [
        "Speculative Tool Tree Execution:",
        f"  Branches explored : {s._spec_branches_explored}",
        f"  Branches warmed   : {s._spec_branches_warmed}",
        f"  Branches hit      : {s._spec_branches_hit}",
        f"  Hit rate          : {hit_rate:.1f}%",
        f"  Tokens saved (est): {s._spec_tokens_saved:,}",
        f"  Warm cache size   : {cache_size}",
    ]
    return [TextContent(type="text", text="\n".join(lines))]


def _hm_get_lattice_stats(arguments: dict[str, Any]) -> list[types.TextContent]:
    ctx_filter = arguments.get("context_type") or None
    rows = memory_db.get_lattice_stats(context_type=ctx_filter)
    if not rows:
        return [TextContent(type="text", text="Adaptive lattice has no entries yet.")]
    header = f"Adaptive lattice ({'context=' + ctx_filter if ctx_filter else 'all contexts'}):"
    lines = [header, f"  {'CONTEXT':<12} {'LVL':>3} {'α':>6} {'β':>6} {'mean':>6} {'trials':>7}  age"]
    for r in rows:
        lines.append(
            f"  {r['context_type']:<12} {r['level']:>3} "
            f"{r['alpha']:>6.1f} {r['beta']:>6.1f} "
            f"{r['mean']:>6.3f} {r['trials']:>7d}  {r['age']}"
        )
    return [TextContent(type="text", text="\n".join(lines))]


def _hm_get_call_predictions(arguments: dict[str, Any]) -> list[types.TextContent]:
    tool_name = arguments.get("tool_name", "")
    symbol_name = arguments.get("symbol_name", "")
    top_k = int(arguments.get("top_k", 5))
    preds = s._prefetcher.predict_next(tool_name, symbol_name, top_k=top_k)
    if not preds:
        return [TextContent(
            type="text",
            text=f"No transitions recorded for state '{tool_name}:{symbol_name}' yet.",
        )]
    lines = [f"Markov predictions after {tool_name}({symbol_name}):"]
    for state, prob in preds:
        lines.append(f"  {prob*100:5.1f}%  {state}")
    return [TextContent(type="text", text="\n".join(lines))]


def _hm_list_projects(arguments: dict[str, Any]) -> list[types.TextContent]:
    if not s._slot_mgr.projects:
        return [TextContent(
            type="text",
            text="No projects registered. Call set_project_root('/path') first.",
        )]
    lines = [f"Workspace projects ({len(s._slot_mgr.projects)}):"]
    for root, slot in s._slot_mgr.projects.items():
        status = "indexed" if slot.indexer is not None else "not yet loaded"
        active = " [active]" if root == s._slot_mgr.active_root else ""
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


def _hm_switch_project(arguments: dict[str, Any]) -> list[types.TextContent]:
    hint = arguments["name"]
    slot, err = s._slot_mgr.resolve(hint)
    if err:
        return [TextContent(type="text", text=f"Error: {err}")]
    s._slot_mgr.active_root = slot.root
    s._slot_mgr.ensure(slot)
    idx = slot.indexer._project_index if slot.indexer else None
    info = f"{idx.total_files} files" if idx else "index not built"
    return [TextContent(
        type="text",
        text=f"Switched to '{os.path.basename(slot.root)}' ({slot.root}) -- {info}.",
    )]


def _hm_set_project_root(arguments: dict[str, Any]) -> list[types.TextContent]:
    new_root = os.path.abspath(arguments["path"])
    if not os.path.isdir(new_root):
        return [TextContent(type="text", text=f"Error: '{new_root}' is not a directory.")]
    if new_root not in s._slot_mgr.projects:
        s._slot_mgr.projects[new_root] = _ProjectSlot(root=new_root)
    s._slot_mgr.active_root = new_root
    slot = s._slot_mgr.projects[new_root]
    slot.indexer = None
    slot.query_fns = None
    s._slot_mgr.build(slot)
    return [TextContent(type="text", text=f"Added and indexed '{new_root}' successfully.")]


def _hm_reindex(arguments: dict[str, Any]) -> list[types.TextContent]:
    project_hint = arguments.get("project")
    slot, err = s._slot_mgr.resolve(project_hint)
    if err:
        return [TextContent(type="text", text=f"Error: {err}")]
    slot.indexer = None
    slot.query_fns = None
    s._slot_mgr.build(slot)
    _recompute_leiden(slot)
    return [TextContent(
        type="text",
        text=f"Project '{os.path.basename(slot.root)}' re-indexed successfully.",
    )]


_META_HANDLERS: dict[str, object] = {
    "get_usage_stats": _hm_get_usage_stats,
    "get_session_budget": _hm_get_session_budget,
    "get_coactive_symbols": _hm_get_coactive_symbols,
    "get_tca_stats": _hm_get_tca_stats,
    "get_dcp_stats": _hm_get_dcp_stats,
    "get_community": _hm_get_community,
    "get_linucb_stats": _hm_get_linucb_stats,
    "get_warmstart_stats": _hm_get_warmstart_stats,
    "memory_consistency": _hm_memory_consistency,
    "memory_quarantine_list": _hm_memory_quarantine_list,
    "get_leiden_stats": _hm_get_leiden_stats,
    "get_speculation_stats": _hm_get_speculation_stats,
    "get_lattice_stats": _hm_get_lattice_stats,
    "get_call_predictions": _hm_get_call_predictions,
    "list_projects": _hm_list_projects,
    "switch_project": _hm_switch_project,
    "set_project_root": _hm_set_project_root,
    "reindex": _hm_reindex,
}


_MEMORY_HANDLERS: dict[str, object] = {
    "memory_bus_push": _mh_memory_bus_push,
    "memory_bus_list": _mh_memory_bus_list,
    "reasoning_save": _mh_reasoning_save,
    "reasoning_search": _mh_reasoning_search,
    "reasoning_list": _mh_reasoning_list,
    "memory_save": _mh_memory_save,
    "memory_search": _mh_memory_search,
    "memory_get": _mh_memory_get,
    "memory_delete": _mh_memory_delete,
    "memory_index": _mh_memory_index,
    "memory_timeline": _mh_memory_timeline,
    "memory_prompts": _mh_memory_prompts,
    "memory_mode": _mh_memory_mode,
    "corpus_build": _mh_corpus_build,
    "corpus_query": _mh_corpus_query,
    "memory_archive": _mh_memory_archive,
    "memory_status": _mh_memory_status,
    "memory_maintain": _mh_memory_maintain,
    "memory_from_bash": _mh_memory_from_bash,
    "memory_doctor": _mh_memory_doctor,
    "memory_distill": _mh_memory_distill,
    "memory_roi_gc": _mh_memory_roi_gc,
    "memory_roi_stats": _mh_memory_roi_stats,
    "memory_why": _mh_memory_why,
    "memory_top": _mh_memory_top,
    "memory_set_global": _mh_memory_set_global,
}

# Dispatch table: tool name → handler(slot, arguments) → result
_SLOT_HANDLERS: dict[str, object] = {
    **_GIT_HANDLERS,
    "create_checkpoint": _h_create_checkpoint,
    "list_checkpoints": _h_list_checkpoints,
    "delete_checkpoint": _h_delete_checkpoint,
    "prune_checkpoints": _h_prune_checkpoints,
    "restore_checkpoint": _h_restore_checkpoint,
    "compare_checkpoint_by_symbol": _h_compare_checkpoint_by_symbol,
    "replace_symbol_source": _h_replace_symbol_source,
    "insert_near_symbol": _h_insert_near_symbol,
    "find_impacted_test_files": _h_find_impacted_test_files,
    "run_impacted_tests": _h_run_impacted_tests,
    "apply_symbol_change_and_validate": _h_apply_symbol_change_and_validate,
    "verify_edit": _h_verify_edit,
    **_PROJECT_ACTION_HANDLERS,
    "analyze_config": _h_analyze_config,
    "find_dead_code": _h_find_dead_code,
    "find_hotspots": _h_find_hotspots,
    "find_allocation_hotspots": _h_find_allocation_hotspots,
    "find_performance_hotspots": _h_find_performance_hotspots,
    "detect_breaking_changes": _h_detect_breaking_changes,
    "find_cross_project_deps": _h_find_cross_project_deps,
    "analyze_docker": _h_analyze_docker,
}


# ── Query-function handlers (qfns dict + arguments → result) ─────────────


def _lookup_symbol_meta(slot, args: dict[str, Any]) -> tuple[str, str, str] | None:
    """Return (kind, body_hash, signature) for a symbol, or None if unresolved."""
    try:
        idx = slot.indexer._project_index
    except AttributeError:
        return None
    name = args.get("name")
    if not name or idx is None:
        return None
    file_path = args.get("file_path")

    def _check(meta, rel_path):
        for func in meta.functions:
            if func.name == name or func.qualified_name == name:
                sig = f"def {func.name}({', '.join(func.parameters)})"
                return ("function", func.body_hash, sig)
        for cls in meta.classes:
            if cls.name == name:
                sig = f"class {cls.name}"
                if cls.base_classes:
                    sig += f"({', '.join(cls.base_classes)})"
                return ("class", cls.body_hash, sig)
            for method in cls.methods:
                if method.qualified_name == name:
                    sig = f"def {method.qualified_name}({', '.join(method.parameters)})"
                    return ("function", method.body_hash, sig)
        return None

    if file_path:
        for rel_path, meta in idx.files.items():
            if rel_path == file_path or rel_path.endswith(file_path):
                got = _check(meta, rel_path)
                if got:
                    return got
        return None
    if name in idx.symbol_table:
        rel_path = idx.symbol_table[name]
        meta = idx.files.get(rel_path)
        if meta is not None:
            got = _check(meta, rel_path)
            if got:
                return got
    for rel_path, meta in idx.files.items():
        got = _check(meta, rel_path)
        if got:
            return got
    return None


def _csc_compact_response(
    name: str,
    signature: str,
    cache_tok: str,
    view_count: int,
    modified: bool,
    diff_preview: str = "",
) -> str:
    """Format a compact CSC hit response."""
    tag = "[MODIFIED]" if modified else ""
    header = f"@sym:{cache_tok} [{name}] {tag}".rstrip()
    lines = [header]
    if signature:
        lines.append(f"Signature: {signature}")
    if modified:
        lines.append(f"Changed body, prior views: {view_count}")
        if diff_preview:
            lines.append("Diff (first lines):")
            lines.append(diff_preview)
    else:
        lines.append(
            f"(body unchanged since last view - {view_count} view{'s' if view_count != 1 else ''} this session)"
        )
    lines.append(
        "Use force_full=true to bypass the session cache and get the full body."
    )
    return "\n".join(lines)


def _csc_diff_preview(old_full: str, new_full: str, max_lines: int = 5) -> str:
    """Return the first `max_lines` diff hunks between two bodies (trivial line diff)."""
    import difflib

    diff = difflib.unified_diff(
        old_full.splitlines(),
        new_full.splitlines(),
        lineterm="",
        n=1,
    )
    out: list[str] = []
    for line in diff:
        if line.startswith("@@") or line.startswith("---") or line.startswith("+++"):
            continue
        out.append(line)
        if len(out) >= max_lines:
            break
    return "\n".join(out)


def _csc_maybe_serve(
    slot,
    kind: str,
    args: dict[str, Any],
    produce_full,
) -> str:
    """Entry point for get_function_source / get_class_source with CSC.

    `produce_full` is a zero-arg callable returning the full formatted source.
    Returns either the compact stub (cache hit, body unchanged) or the full
    source (miss / force_full / modified).
    """

    force_full = bool(args.get("force_full", False))
    level = int(args.get("level", 0) or 0)

    full = produce_full()
    # Skip cache when:
    # - caller asked for non-L0 abstraction (already compact by design)
    # - symbol wasn't resolvable (error messages must pass through verbatim)
    # - force_full is set
    if level > 0 or force_full or full.startswith("Error:"):
        return full

    meta = _lookup_symbol_meta(slot, args)
    if meta is None:
        return full
    _kind, body_hash, signature = meta
    if not body_hash:
        return full

    project_root = getattr(slot, "root", "") or ""
    name = args["name"]
    key = f"{kind}:{project_root}:{name}"
    entry = s._session_symbol_cache.get(key)

    from token_savior.symbol_hash import cache_token

    tok = cache_token(body_hash)

    if entry is None:
        # Miss — return full, record.
        s._session_symbol_cache[key] = {
            "cache_token": tok,
            "body_hash": body_hash,
            "view_count": 1,
            "full_source": full,
            "signature": signature,
        }
        return full

    entry["view_count"] += 1
    prior_full = entry.get("full_source", "")
    if entry["body_hash"] == body_hash:
        compact = _csc_compact_response(
            name=name,
            signature=signature,
            cache_tok=tok,
            view_count=entry["view_count"],
            modified=False,
        )
        saved = max(0, len(full) - len(compact))
        s._csc_hits += 1
        s._csc_tokens_saved += saved // 4
        return compact

    # Modified — return compact with diff preview; refresh cache.
    diff_preview = _csc_diff_preview(prior_full, full)
    compact = _csc_compact_response(
        name=name,
        signature=signature,
        cache_tok=tok,
        view_count=entry["view_count"],
        modified=True,
        diff_preview=diff_preview,
    )
    saved = max(0, len(full) - len(compact))
    s._csc_hits += 1
    s._csc_tokens_saved += saved // 4
    entry.update(
        {
            "cache_token": tok,
            "body_hash": body_hash,
            "full_source": full,
            "signature": signature,
        }
    )
    return compact


def _q_get_class_source(qfns, args: dict[str, Any]) -> str:
    slot, _ = s._slot_mgr.resolve(args.get("project"))
    explicit_level = "level" in args and args.get("level") is not None
    if explicit_level:
        chosen_level = int(args.get("level") or 0)
        ctx_type = None
    else:
        ctx_type = memory_db._detect_context_type(s._prefetcher.call_sequence)
        chosen_level = memory_db.thompson_sample_level(ctx_type)
    result = _csc_maybe_serve(
        slot,
        "class",
        args,
        lambda: qfns["get_class_source"](
            args["name"],
            args.get("file_path"),
            max_lines=args.get("max_lines", 0),
            level=chosen_level,
        ),
    )
    if ctx_type is not None:
        try:
            success = bool(result and not result.startswith("Error"))
            memory_db.record_lattice_feedback(ctx_type, chosen_level, success)
        except Exception:
            pass
    return result


def _q_get_function_source(qfns, args: dict[str, Any]) -> str:
    slot, _ = s._slot_mgr.resolve(args.get("project"))
    explicit_level = "level" in args and args.get("level") is not None
    if explicit_level:
        chosen_level = int(args.get("level") or 0)
        ctx_type = None
    else:
        ctx_type = memory_db._detect_context_type(s._prefetcher.call_sequence)
        chosen_level = memory_db.thompson_sample_level(ctx_type)
    result = _csc_maybe_serve(
        slot,
        "function",
        args,
        lambda: qfns["get_function_source"](
            args["name"],
            args.get("file_path"),
            max_lines=args.get("max_lines", 0),
            level=chosen_level,
        ),
    )
    if ctx_type is not None:
        # Optimistic feedback: a non-empty result at the sampled level counts as
        # a success. Subsequent calls that re-request the same symbol at level=0
        # will register failures naturally as the prior corrects itself.
        try:
            success = bool(result and not result.startswith("Error"))
            memory_db.record_lattice_feedback(ctx_type, chosen_level, success)
        except Exception:
            pass
    try:
        project_root = _resolve_project_root(args)
        symbol_name = args["name"]
        file_path = args.get("file_path")
        rows = memory_db.observation_get_by_symbol(
            project_root, symbol_name, file_path=file_path, limit=3
        )
        if rows:
            lines = ["\n───", f"📌 Memory ({len(rows)}):"]
            for r in rows:
                age = r.get("age") or "?"
                stale = "⚠️ " if r.get("stale") else ""
                glob = "🌐 " if r.get("is_global") else ""
                lines.append(f"  #{r['id']}  [{r['type']}]  {stale}{glob}{r['title']}  — {age}")
            result += "\n".join(lines)
    except Exception:
        pass
    try:
        coactives = s._tca_engine.get_coactive_symbols(args["name"], top_k=3)
        if coactives:
            co_lines = ["\n🔄 Often accessed together:"]
            for co_sym, pmi in coactives:
                co_lines.append(f"  {co_sym} (PMI: {pmi:.2f})")
            result += "\n".join(co_lines)
    except Exception:
        pass
    try:
        comm = s._leiden.get_community_for(args["name"])
        if comm and comm["size"] <= 20:
            peers = [m for m in comm["members"] if m != args["name"]][:8]
            if peers:
                result += (
                    f"\n🏘️ Community '{comm['name']}' ({comm['size']} members): "
                    + ", ".join(peers)
                )
    except Exception:
        pass
    return result


def _q_get_edit_context(qfns, args):
    sym_name = args["name"]
    max_deps = args.get("max_deps", 10)
    max_callers = args.get("max_callers", 10)
    ctx: dict[str, Any] = {"symbol": sym_name}
    location = None
    try:
        location = qfns["find_symbol"](sym_name)
    except Exception:
        location = None

    is_class = isinstance(location, dict) and location.get("type") == "class"
    try:
        if is_class:
            ctx["source"] = qfns["get_class_source"](sym_name, max_lines=200)
        else:
            ctx["source"] = qfns["get_function_source"](sym_name, max_lines=200)
    except Exception:
        try:
            if is_class:
                ctx["source"] = qfns["get_function_source"](sym_name, max_lines=200)
            else:
                ctx["source"] = qfns["get_class_source"](sym_name, max_lines=200)
        except Exception:
            ctx["source"] = None
    ctx["location"] = location
    try:
        dependencies = qfns["get_dependencies"](sym_name, max_results=max_deps)
        if is_class:
            class_name = location.get("name") if isinstance(location, dict) else None
            filtered_dependencies = []
            for dep in dependencies:
                dep_name = dep.get("name") if isinstance(dep, dict) else None
                dep_type = dep.get("type") if isinstance(dep, dict) else None
                if dep_name and dep_name.endswith("()"):
                    owner = dep_name.rsplit(".", 1)[0] if "." in dep_name else None
                    method_base = dep_name.rsplit(".", 1)[-1].split("(", 1)[0]
                    owner_simple = owner.rsplit(".", 1)[-1] if owner else None
                    if (
                        owner
                        and class_name == owner
                        and method_base == owner_simple
                        and dep_type in {None, "method"}
                    ):
                        continue
                filtered_dependencies.append(dep)
            dependencies = filtered_dependencies
        ctx["dependencies"] = dependencies
    except Exception:
        ctx["dependencies"] = []
    try:
        ctx["callers"] = qfns["get_dependents"](sym_name, max_results=max_callers)
    except Exception:
        ctx["callers"] = []
    return ctx


# Dispatch table: tool name → handler(qfns, arguments) → result
_QFN_HANDLERS: dict[str, object] = {
    "get_project_summary": lambda q, a: q["get_project_summary"](),
    "list_files": lambda q, a: q["list_files"](
        a.get("pattern"), max_results=a.get("max_results", 0)
    ),
    "get_structure_summary": lambda q, a: q["get_structure_summary"](a.get("file_path")),
    "get_function_source": _q_get_function_source,
    "get_class_source": _q_get_class_source,
    "get_functions": lambda q, a: q["get_functions"](
        a.get("file_path"), max_results=a.get("max_results", 0)
    ),
    "get_classes": lambda q, a: q["get_classes"](
        a.get("file_path"), max_results=a.get("max_results", 0)
    ),
    "get_imports": lambda q, a: q["get_imports"](
        a.get("file_path"), max_results=a.get("max_results", 0)
    ),
    "find_symbol": lambda q, a: q["find_symbol"](a["name"]),
    "get_dependencies": lambda q, a: q["get_dependencies"](
        a["name"], max_results=a.get("max_results", 0)
    ),
    "get_dependents": lambda q, a: q["get_dependents"](
        a["name"], max_results=a.get("max_results", 0),
        max_total_chars=a.get("max_total_chars", 50_000),
    ),
    "get_change_impact": lambda q, a: q["get_change_impact"](
        a["name"], max_direct=a.get("max_direct", 0), max_transitive=a.get("max_transitive", 0),
        max_total_chars=a.get("max_total_chars", 50_000),
    ),
    "get_call_chain": lambda q, a: q["get_call_chain"](a["from_name"], a["to_name"]),
    "get_edit_context": _q_get_edit_context,
    "get_file_dependencies": lambda q, a: q["get_file_dependencies"](
        a["file_path"], max_results=a.get("max_results", 0)
    ),
    "get_file_dependents": lambda q, a: q["get_file_dependents"](
        a["file_path"], max_results=a.get("max_results", 0)
    ),
    "search_codebase": lambda q, a: q["search_codebase"](
        a["pattern"], max_results=a.get("max_results", 100)
    ),
    "get_routes": lambda q, a: q["get_routes"](max_results=a.get("max_results", 0)),
    "get_env_usage": lambda q, a: q["get_env_usage"](
        a["var_name"], max_results=a.get("max_results", 0)
    ),
    "get_components": lambda q, a: q["get_components"](
        file_path=a.get("file_path"), max_results=a.get("max_results", 0)
    ),
    "get_feature_files": lambda q, a: q["get_feature_files"](
        a["keyword"], max_results=a.get("max_results", 0)
    ),
    "get_entry_points": lambda q, a: q["get_entry_points"](max_results=a.get("max_results", 20)),
    "get_symbol_cluster": lambda q, a: q["get_symbol_cluster"](
        a["name"], max_members=a.get("max_members", 30)
    ),
    "get_backward_slice": lambda q, a: q["get_backward_slice"](
        a["name"], a["variable"], a["line"], file_path=a.get("file_path")
    ),
    "pack_context": lambda q, a: q["pack_context"](
        a["query"],
        budget_tokens=a.get("budget_tokens", 4000),
        max_symbols=a.get("max_symbols", 20),
    ),
    "get_relevance_cluster": lambda q, a: q["get_relevance_cluster"](
        a["name"],
        budget=a.get("budget", 10),
        include_reverse=a.get("include_reverse", True),
    ),
    "find_semantic_duplicates": lambda q, a: q["find_semantic_duplicates"](
        min_lines=a.get("min_lines", 4)
    ),
    "get_duplicate_classes": lambda q, a: q["get_duplicate_classes"](
        a.get("name"),
        max_results=a.get("max_results", 0),
        simple_name_mode=a.get("simple_name_mode", False),
    ),
}


s._PREFETCHABLE_TOOLS = frozenset({
    "get_function_source",
    "get_class_source",
    "get_dependents",
    "get_dependencies",
    "find_symbol",
})


def _track_call(name: str, arguments: dict[str, Any]) -> str:
    """Tool-call telemetry: counts, PPM record, TCA activation, STTE hit."""

    s._tool_call_counts[name] = s._tool_call_counts.get(name, 0) + 1
    record_symbol = arguments.get("name") or arguments.get("symbol_name", "")
    try:
        s._prefetcher.record_call(name, record_symbol or "")
    except Exception:
        pass
    if record_symbol:
        try:
            s._tca_engine.record_activation(record_symbol)
        except Exception:
            pass
    if record_symbol and name in s._PREFETCHABLE_TOOLS:
        with s._prefetch_lock:
            cached = s._prefetch_cache.get(f"{name}:{record_symbol}")
        if cached is not None:
            s._spec_branches_hit += 1
            s._spec_tokens_saved += len(cached) // 4
    return record_symbol


def _maybe_compress(name: str, arguments: dict[str, Any], result):
    """Apply TCS structural compression if eligible."""
    if name not in s._COMPRESSIBLE_TOOLS or not arguments.get("compress", True):
        return result

    raw = _format_result(result)
    compressed = compress_symbol_output(name, result)
    before, after = len(raw), len(compressed)
    if after < before and compressed:
        saved_pct = (1 - after / before) * 100 if before else 0.0
        s._tcs_calls += 1
        s._tcs_chars_before += before
        s._tcs_chars_after += after
        return f"{compressed}\n[compressed: {before} → {after} chars, -{saved_pct:.1f}%]"
    return result


def _prefetch_next(name: str, record_symbol: str, slot) -> None:
    """Markov: predict next likely calls and pre-warm in a daemon thread."""
    try:
        preds = s._prefetcher.predict_next(name, record_symbol or "", top_k=3)
        if preds:
            _warm_cache_async(
                preds, slot, tool_name=name, symbol_name=record_symbol or "",
            )
    except Exception:
        pass


@server.call_tool()
async def call_tool(name: str, arguments: dict[str, Any]) -> list[types.TextContent]:

    record_symbol = _track_call(name, arguments)
    try:
        meta_handler = _META_HANDLERS.get(name)
        if meta_handler is not None:
            return meta_handler(arguments)

        mem_handler = _MEMORY_HANDLERS.get(name)
        if mem_handler is not None:
            return [TextContent(type="text", text=mem_handler(arguments))]

        slot, err = s._slot_mgr.resolve(arguments.get("project"))
        if err:
            return [TextContent(type="text", text=f"Error: {err}")]

        handler = _SLOT_HANDLERS.get(name)
        if handler is not None:
            return _count_and_wrap_result(slot, name, arguments, handler(slot, arguments))

        qfn_handler = _QFN_HANDLERS.get(name)
        if qfn_handler is not None:
            _prep(slot)
            if slot.query_fns is None:
                return [TextContent(
                    type="text",
                    text=f"Error: index not built for '{slot.root}'. Call reindex first.",
                )]
            result = qfn_handler(slot.query_fns, arguments)
            result = _maybe_compress(name, arguments, result)
            _prefetch_next(name, record_symbol, slot)
            return _count_and_wrap_result(slot, name, arguments, result)

        return [TextContent(type="text", text=f"Error: unknown tool '{name}'")]

    except Exception as e:
        print(f"[token-savior] Error in {name}: {traceback.format_exc()}", file=sys.stderr)
        return [TextContent(type="text", text=f"Error: {e}")]


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


async def main():
    memory_db.run_migrations()
    async with stdio_server() as (read_stream, write_stream):
        await server.run(
            read_stream,
            write_stream,
            server.create_initialization_options(),
        )


def main_sync():
    """Synchronous entry point for console_scripts."""
    import asyncio

    asyncio.run(main())


if __name__ == "__main__":
    main_sync()
