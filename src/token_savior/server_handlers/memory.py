"""Handlers for the Memory Engine.

Covers:
- _resolve_memory_project helper
- _invalidate_injection_hash, _linucb_credit_reward, _build_linucb_context helpers
- 26 _mh_* memory tool handlers (memory_bus_*, reasoning_*, memory_save/get/...,
  corpus_*, memory_status/mode/archive/maintain/prompts, ...)
- 2 _hm_* admin handlers (memory_consistency, memory_quarantine_list)

Exports:
- HANDLERS — _mh_* dispatch table (matches former _MEMORY_HANDLERS)
- ADMIN_HANDLERS — _hm_* admin handlers spread into _META_HANDLERS
"""

from __future__ import annotations

import os
import re as _re
import subprocess
import time
from typing import Any

from mcp.types import TextContent
import mcp.types as types

from token_savior import memory_db
from token_savior import server_state as state
from token_savior.server_runtime import _resolve_project_root


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _resolve_memory_project(arguments: dict[str, Any]) -> str:
    """Resolve the project_root for memory tools.

    Falls back from explicit hint → active slot (if it has observations) →
    project with the most observations → active slot → workspace default.
    This lets `memory_index`/`memory_search` work even when the active slot
    is a code project but observations live under a different project_root.
    """
    hint = arguments.get("project")
    if hint:
        slot, _ = state._slot_mgr.resolve(hint)
        if slot:
            return slot.root
        return hint

    active_root = state._slot_mgr.active_root
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


# P3: citation URIs. Injected obs rows are tagged `[ts://obs/{id}]` so agents
# can paste the URI back into memory_get without re-reading the integer. The
# parser below also accepts bare ints and digit strings for back-compat.
_TS_OBS_URI_RE = _re.compile(r"^\s*ts://obs/(\d+)\s*$", _re.IGNORECASE)


def _parse_obs_id(value: Any) -> int | None:
    """Coerce an obs identifier from int, digit str, or ``ts://obs/{id}`` URI."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        m = _TS_OBS_URI_RE.match(value)
        if m:
            return int(m.group(1))
        s = value.strip()
        if s.isdigit():
            return int(s)
    return None


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


def _linucb_credit_reward(obs_ids: list[int], reward: float = 1.0) -> None:
    """Apply LinUCB online update for previously-injected obs ids."""
    now = int(time.time())
    for oid in obs_ids:
        slot = state._linucb_pending.pop(oid, None)
        if slot is None:
            continue
        # Only credit if the click happened within ~30 min of injection.
        if now - slot.get("injected_epoch", now) > 1800:
            continue
        try:
            phi = slot["features"]
            for i in range(state._linucb.FEATURE_DIM):
                for j in range(state._linucb.FEATURE_DIM):
                    state._linucb.A[i][j] += phi[i] * phi[j]
                state._linucb.b[i] += reward * phi[i]
            state._linucb.updates += 1
            if state._linucb.updates % 5 == 0:
                state._linucb.save()
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
    if state._prefetcher.call_sequence:
        last = state._prefetcher.call_sequence[-1]
        last_tool = last.split(":", 1)[0] if ":" in last else last
    recent_symbols: list[str] = []
    for st in reversed(state._prefetcher.call_sequence[-12:]):
        if ":" in st:
            _, sym = st.split(":", 1)
            if sym and sym not in recent_symbols:
                recent_symbols.append(sym)
        if len(recent_symbols) >= 8:
            break
    # Tokens-used proxy: cap at 200k ≈ context budget.
    tokens_used = state._total_chars_returned / 4.0
    tokens_used_pct = min(1.0, tokens_used / 200_000.0)
    return {
        "prompt": prompt,
        "auto_capture_types": auto_types,
        "last_tool": last_tool,
        "recent_symbols": tuple(recent_symbols),
        "tokens_used_pct": tokens_used_pct,
        "now_epoch": int(time.time()),
    }


# ---------------------------------------------------------------------------
# _mh_* memory handlers (str-returning)
# ---------------------------------------------------------------------------


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


def _mh_memory_dedup_sweep(args: dict[str, Any]) -> str:
    root = args.get("project_root") or None
    recompute = bool(args.get("recompute") or False)
    batch = int(args.get("batch_size") or 500)
    stats = memory_db.dedup_sweep(
        project_root=root, recompute=recompute, batch_size=batch,
    )
    scope = f"project={root}" if root else "all projects"
    mode = "recompute" if recompute else "backfill NULL only"
    lines = [
        f"Dedup sweep ({scope}, {mode}):",
        "─" * 60,
        f" scanned:           {stats['scanned']}",
        f" updated:           {stats['updated']}",
        f" collisions merged: {stats['collisions_merged']}",
        f" archived:          {stats['archived']}",
    ]
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
    # Accept bare ints, digit strings, or ``ts://obs/{id}`` citation URIs
    # (emitted by memory_index rows). Invalid tokens are rendered as errors
    # instead of silently dropped so the caller sees the bad input.
    raw_ids = args["ids"]
    ids: list[int] = []
    invalid: list[str] = []
    for x in raw_ids:
        parsed = _parse_obs_id(x)
        if parsed is None:
            invalid.append(repr(x))
        else:
            ids.append(parsed)
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
    if invalid:
        blocks.append(
            "## Invalid id(s)\n"
            + ", ".join(invalid)
            + "\nExpected integer, digit string, or `ts://obs/{id}` URI."
        )
    return "\n\n---\n\n".join(blocks)


def _mh_memory_delete(args: dict[str, Any]) -> str:
    obs_list = memory_db.observation_get([args["id"]])
    if not obs_list:
        return f"Observation #{args['id']} not found."
    memory_db.observation_delete(args["id"])
    _invalidate_injection_hash()
    return f"Observation #{args['id']} archived."


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
    ranked = state._linucb.rank_observations(rows, ctx, top_k=desired_limit)
    if not ranked:
        ranked = [(r, float(r.get("relevance_score", 1.0))) for r in rows[:desired_limit]]

    # Track injected obs for reward attribution.
    now = int(time.time())
    for obs, _score in ranked:
        oid = obs.get("id")
        if oid is not None:
            state._linucb_pending[oid] = {
                "features": state._linucb.extract_features(obs, ctx),
                "context": ctx,
                "injected_epoch": now,
                "access_count_at_inject": int(obs.get("access_count") or 0),
            }

    # Each row ends with `[ts://obs/{id}]` so agents can paste the URI back
    # into memory_get without re-reading the integer (P3 citation URIs).
    lines = [
        "| ID | Type | Title | Imp. | Rel. | UCB | Age | Cite |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for obs, ucb in ranked:
        age = obs.get("age") or "?"
        rel = f"{obs.get('relevance_score', 1.0):.2f}"
        glob = "🌐 " if obs.get("is_global") else ""
        lines.append(
            f"| {obs['id']} | {obs['type']} | {glob}{obs['title']} | "
            f"{obs['importance']} | {rel} | {ucb:+.3f} | {age} | "
            f"[ts://obs/{obs['id']}] |"
        )
    lines.append(
        f"\n{len(ranked)} obs (LinUCB-ranked from {len(rows)} candidates). "
        "Use `memory_get` with IDs or `ts://obs/{id}` URIs for full details."
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
# _hm_* admin handlers (list[TextContent]-returning)
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# Dispatch tables
# ---------------------------------------------------------------------------


HANDLERS: dict[str, Any] = {
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
    "memory_dedup_sweep": _mh_memory_dedup_sweep,
    "memory_roi_gc": _mh_memory_roi_gc,
    "memory_roi_stats": _mh_memory_roi_stats,
    "memory_why": _mh_memory_why,
    "memory_top": _mh_memory_top,
    "memory_set_global": _mh_memory_set_global,
}


ADMIN_HANDLERS: dict[str, Any] = {
    "memory_consistency": _hm_memory_consistency,
    "memory_quarantine_list": _hm_memory_quarantine_list,
}
