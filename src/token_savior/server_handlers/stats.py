"""Handlers for stats/observability tools and the _usage_* formatting helpers.

Public tools: get_stats (fused 9 categories), get_related_symbols
(fused 4 clustering methods), get_call_predictions. Internal handlers are
the _hm_get_* functions plus the 18 _usage_* section builders used by
_format_usage_stats.
"""

from __future__ import annotations

import os
import time
from typing import Any

from mcp.types import TextContent
import mcp.types as types

from token_savior import memory_db
from token_savior import server_state as state
from token_savior.server_handlers.memory import _resolve_memory_project
from token_savior.server_runtime import _get_stats_file, _load_cumulative_stats


# ---------------------------------------------------------------------------
# _usage_* section builders (called by _format_usage_stats)
# ---------------------------------------------------------------------------


def _usage_session_header(elapsed: float, query_calls: int) -> list[str]:
    lines = [f"Session: {_format_duration(elapsed)}, {query_calls} queries"]
    if len(state._slot_mgr.projects) > 1:
        loaded = sum(1 for s in state._slot_mgr.projects.values() if s.indexer is not None)
        lines.append(
            f"Projects: {loaded}/{len(state._slot_mgr.projects)} loaded, active: {os.path.basename(state._slot_mgr.active_root)}"
        )
    return lines


def _usage_tool_counts() -> list[str]:
    if not state._tool_call_counts:
        return []
    top_tools = sorted(
        ((t, c) for t, c in state._tool_call_counts.items() if t != "get_stats"),
        key=lambda x: -x[1],
    )
    tool_str = ", ".join(f"{t}:{c}" for t, c in top_tools[:8])
    if len(top_tools) > 8:
        tool_str += f" +{len(top_tools) - 8} more"
    return [f"Tools: {tool_str}"]


def _usage_chars_savings(source_chars: int, query_calls: int) -> list[str]:
    lines = [f"Chars returned: {state._total_chars_returned:,}"]
    if source_chars > 0 and query_calls > 0 and state._total_naive_chars > state._total_chars_returned:
        reduction = (1 - state._total_chars_returned / state._total_naive_chars) * 100
        lines.append(
            f"Savings: {reduction:.1f}% "
            f"({state._total_chars_returned // 4:,} vs {state._total_naive_chars // 4:,} tokens)"
        )
    return lines


def _usage_csc() -> list[str]:
    if state._csc_hits <= 0:
        return []
    return [f"CSC hits this session: {state._csc_hits} ({state._csc_tokens_saved:,} tokens saved)"]


def _usage_markov() -> list[str]:
    mstats = state._prefetcher.get_stats()
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
    with state._prefetch_lock:
        lines.append(f"Prefetch cache: {len(state._prefetch_cache)} warmed entries")
    return lines


def _usage_dcp() -> list[str]:
    if state._dcp_calls == 0 and state._dcp_total_chunks == 0:
        return []
    stable_pct = (
        (state._dcp_stable_chunks / state._dcp_total_chunks * 100)
        if state._dcp_total_chunks else 0.0
    )
    # Each stable chunk ≈ 256B ≈ 64 tokens of cache savings
    benefit_tokens = (state._dcp_stable_chunks * 256) // 4
    return [
        f"DCP: {state._dcp_total_chunks} chunks registered | "
        f"{stable_pct:.0f}% stable | "
        f"est. cache benefit: {benefit_tokens:,}t"
    ]


def _usage_tcs() -> list[str]:
    if state._tcs_calls == 0:
        return []
    tcs_saved = state._tcs_chars_before - state._tcs_chars_after
    tcs_pct = (tcs_saved / state._tcs_chars_before * 100) if state._tcs_chars_before else 0.0
    return [
        f"Schema compression: {state._tcs_calls} calls, "
        f"{state._tcs_chars_before:,} → {state._tcs_chars_after:,} chars "
        f"(-{tcs_pct:.1f}%, ~{tcs_saved // 4:,} tokens saved)"
    ]


def _usage_linucb() -> list[str]:
    try:
        linucb_s = state._linucb.get_stats()
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
        ws_s = state._warm_start.get_stats()
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
        ls = state._leiden.get_stats()
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
    if not (state._spec_branches_explored or state._spec_branches_warmed):
        return []
    hit_rate = (
        (state._spec_branches_hit / state._spec_branches_warmed * 100)
        if state._spec_branches_warmed else 0.0
    )
    return [
        f"Speculative Tree: {state._spec_branches_explored} explored, "
        f"{state._spec_branches_warmed} warmed, {state._spec_branches_hit} hit "
        f"({hit_rate:.1f}%), ~{state._spec_tokens_saved:,} tokens saved"
    ]


def _usage_symbol_reindex() -> list[str]:
    lines: list[str] = []
    for root, slot in state._slot_mgr.projects.items():
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
    for root, slot in state._slot_mgr.projects.items():
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
        active_root = state._slot_mgr.active_root or ""
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
    elapsed = time.time() - state._session_start
    total_calls = sum(state._tool_call_counts.values())
    query_calls = total_calls - state._tool_call_counts.get("get_stats", 0)

    source_chars = 0
    for slot in state._slot_mgr.projects.values():
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
# Stats meta-handlers (arguments → list[TextContent])
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
    co = state._tca_engine.get_coactive_symbols(seed, top_k=top_k)
    if not co:
        return [TextContent(type="text", text=f"No co-activation data yet for '{seed}'.")]
    lines = [f"🔄 Co-active with '{seed}' (top {len(co)}):"]
    for sym, pmi in co:
        lines.append(f"  {pmi:+.3f}  {sym}")
    return [TextContent(type="text", text="\n".join(lines))]


def _hm_get_tca_stats(arguments: dict[str, Any]) -> list[types.TextContent]:
    stats = state._tca_engine.get_stats()
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
    if state._dcp_calls:
        sess_pct = (state._dcp_stable_chunks / state._dcp_total_chunks * 100) if state._dcp_total_chunks else 0.0
        lines.append(
            f"  Session: {state._dcp_calls} DCP calls, "
            f"{state._dcp_stable_chunks}/{state._dcp_total_chunks} chunks stable ({sess_pct:.1f}%)"
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
    list_all = bool(arguments.get("list_all", False))

    if list_all or (not sym and not cname):
        comms = getattr(state._leiden, "communities", {}) or {}
        if not comms:
            return [TextContent(type="text", text="No communities detected (run reindex or community detection first).")]
        min_size = int(arguments.get("min_size", 2) or 2)
        max_results = int(arguments.get("max_results", 30) or 30)
        max_members_preview = int(arguments.get("max_members_preview", 8) or 8)
        rows = sorted(
            ((name, sorted(members)) for name, members in comms.items() if len(members) >= min_size),
            key=lambda kv: -len(kv[1]),
        )[:max_results]
        if not rows:
            return [TextContent(type="text", text=f"No communities with size>={min_size}.")]
        lines = [f"🏘️  {len(rows)} communities (size>={min_size}, top {max_results})"]
        lines.append("─" * 60)
        for name, members in rows:
            preview = ", ".join(members[:max_members_preview])
            extra = f" (+{len(members) - max_members_preview} more)" if len(members) > max_members_preview else ""
            lines.append(f"• {name} [{len(members)}] {preview}{extra}")
        return [TextContent(type="text", text="\n".join(lines))]

    comm = state._leiden.get_community_for(sym) if sym else state._leiden.get_community(cname)
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
    stats = state._linucb.get_stats()
    lines = ["LinUCB Injection Model:", "  Feature weights (θ):"]
    for i, fw in enumerate(stats["feature_weights"]):
        marker = "  ← top feature" if i == 0 else ""
        lines.append(f"    {fw['name']:<14}: {fw['weight']:+.4f}{marker}")
    lines.append(f"  Updates: {stats['updates']} | Observations scored: {stats['scored']}")
    return [TextContent(type="text", text="\n".join(lines))]


def _hm_get_warmstart_stats(arguments: dict[str, Any]) -> list[types.TextContent]:
    stats = state._warm_start.get_stats()
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


def _hm_get_leiden_stats(arguments: dict[str, Any]) -> list[types.TextContent]:
    stats = state._leiden.get_stats()
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
    with state._prefetch_lock:
        cache_size = len(state._prefetch_cache)
    hit_rate = (state._spec_branches_hit / state._spec_branches_warmed * 100) if state._spec_branches_warmed else 0.0
    lines = [
        "Speculative Tool Tree Execution:",
        f"  Branches explored : {state._spec_branches_explored}",
        f"  Branches warmed   : {state._spec_branches_warmed}",
        f"  Branches hit      : {state._spec_branches_hit}",
        f"  Hit rate          : {hit_rate:.1f}%",
        f"  Tokens saved (est): {state._spec_tokens_saved:,}",
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
    preds = state._prefetcher.predict_next(tool_name, symbol_name, top_k=top_k)
    if not preds:
        return [TextContent(
            type="text",
            text=f"No transitions recorded for state '{tool_name}:{symbol_name}' yet.",
        )]
    lines = [f"Markov predictions after {tool_name}({symbol_name}):"]
    for st, prob in preds:
        lines.append(f"  {prob*100:5.1f}%  {st}")
    return [TextContent(type="text", text="\n".join(lines))]


def _hm_get_related_symbols(arguments: dict[str, Any]) -> list[types.TextContent]:
    """Unified dispatcher: community / rwr / cluster / coactive."""
    method = (arguments.get("method") or "community").strip().lower()

    if method == "coactive":
        return _hm_get_coactive_symbols(arguments)

    if method == "community":
        sym = arguments.get("name") or arguments.get("symbol")
        cname = arguments.get("community_name") or (
            arguments.get("name") if arguments.get("list_all") else None
        )
        list_all = bool(arguments.get("list_all", False))
        if list_all or (not sym and not cname):
            return _hm_get_community({
                "list_all": True,
                "min_size": arguments.get("min_size", 2),
                "max_results": arguments.get("max_results", 30),
                "max_members_preview": arguments.get("max_members_preview", 8),
            })
        return _hm_get_community({"symbol": sym, "name": cname})

    if method in ("rwr", "cluster"):
        import json
        from token_savior import server_state as st_mod
        from token_savior.server_runtime import _prep

        name = (arguments.get("name") or "").strip()
        if not name:
            return [TextContent(
                type="text",
                text=f"Error: 'name' required for method='{method}'.",
            )]
        slot, err = st_mod._slot_mgr.resolve(arguments.get("project"))
        if err or slot is None:
            return [TextContent(type="text", text=f"Error: {err or 'no slot'}")]
        _prep(slot)
        if slot.query_fns is None:
            return [TextContent(
                type="text",
                text=f"Error: index not built for '{slot.root}'. Call reindex first.",
            )]
        qfns = slot.query_fns
        if method == "cluster":
            result = qfns["get_symbol_cluster"](
                name, max_members=int(arguments.get("max_members", 30)),
            )
        else:
            result = qfns["get_relevance_cluster"](
                name,
                budget=int(arguments.get("budget", 10)),
                include_reverse=bool(arguments.get("include_reverse", True)),
            )
        if isinstance(result, str):
            return [TextContent(type="text", text=result)]
        return [TextContent(type="text", text=json.dumps(result, indent=2, default=str))]

    return [TextContent(
        type="text",
        text=f"Unknown method '{method}'. Valid: community, rwr, cluster, coactive.",
    )]


_STATS_CATEGORIES: dict[str, Any] = {
    "usage": _hm_get_usage_stats,
    "session_budget": _hm_get_session_budget,
    "tca": _hm_get_tca_stats,
    "dcp": _hm_get_dcp_stats,
    "linucb": _hm_get_linucb_stats,
    "warmstart": _hm_get_warmstart_stats,
    "leiden": _hm_get_leiden_stats,
    "speculation": _hm_get_speculation_stats,
    "lattice": _hm_get_lattice_stats,
}


def _hm_get_stats(arguments: dict[str, Any]) -> list[types.TextContent]:
    category = (arguments.get("category") or "usage").strip().lower()
    handler = _STATS_CATEGORIES.get(category)
    if handler is None:
        valid = ", ".join(sorted(_STATS_CATEGORIES))
        return [TextContent(
            type="text",
            text=f"Unknown stats category '{category}'. Valid: {valid}.",
        )]
    return handler(arguments)


HANDLERS: dict[str, Any] = {
    "get_stats": _hm_get_stats,
    "get_related_symbols": _hm_get_related_symbols,
    "get_call_predictions": _hm_get_call_predictions,
}
