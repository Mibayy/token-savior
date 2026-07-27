#!/usr/bin/env python3
"""ts_audit — automate the usage audit that used to be a manual dig through
memory.db + tool-calls.json.

Surfaces, over a window:
  * per-tool volume (tool-calls.json, cumulative) and latency p50/p95
    (memory.db `tool_latency`, timestamped)
  * wasteful tool chains (tool-level, within 60s) the chain-nudges target
  * adoption gaps (edits without a preceding get_edit_context; nav bursts
    that never used ts_execute)
  * nudge fire counts (nudge-stats.json) so effectiveness can be tracked
    against adoption over successive runs
  * ML-machinery liveness (prefetch hit-rate, empty co-activation, etc.)

Reads only the persisted stats dir -- no MCP server needed. Re-run it after a
deploy to see whether the nudges moved behaviour.

Usage:
  python3 scripts/ts_audit.py [--since-days 30] [--stats-dir PATH] [--json]
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import statistics
import time
from collections import Counter, defaultdict

_DEFAULT_STATS_DIR = os.path.expanduser(
    os.environ.get("TOKEN_SAVIOR_STATS_DIR", "~/.local/share/token-savior")
)
_CHAIN_WINDOW = 60.0

_EDIT_TOOLS = {"replace_symbol_source", "insert_near_symbol", "add_field_to_model", "move_symbol"}
_NAV_TOOLS = {
    "find_symbol", "get_function_source", "get_class_source", "get_full_context",
    "get_dependents", "get_dependencies", "search_codebase", "get_structure_summary",
}


def _load_json(path: str) -> dict:
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def _tool_counts(stats_dir: str) -> dict[str, int]:
    data = _load_json(os.path.join(stats_dir, "tool-calls.json"))
    agg: dict[str, int] = {}
    for bucket in (data.get("counts") or {}).values():
        if isinstance(bucket, dict):
            for tool, n in bucket.items():
                if isinstance(n, int):
                    agg[tool] = agg.get(tool, 0) + n
    return agg


def _latency_rows(stats_dir: str, since_epoch: int) -> list[tuple[int, str, int, str]]:
    db = os.path.join(stats_dir, "memory.db")
    if not os.path.exists(db):
        return []
    try:
        con = sqlite3.connect(db)
        rows = con.execute(
            "SELECT ts, tool, duration_ms, COALESCE(status,'ok') "
            "FROM tool_latency WHERE ts >= ? ORDER BY ts",
            (since_epoch,),
        ).fetchall()
        con.close()
        return rows
    except sqlite3.Error:
        return []


def _latency_table(rows) -> list[tuple[str, int, float, float, int, int]]:
    by = defaultdict(list)
    err = Counter()
    for _ts, tool, dur, status in rows:
        by[tool].append(dur)
        if status and status != "ok":
            err[tool] += 1
    out = []
    for tool, vals in by.items():
        vals.sort()
        n = len(vals)
        p50 = statistics.median(vals)
        p95 = vals[min(n - 1, int(0.95 * n))]
        out.append((tool, n, p50, p95, max(vals), err.get(tool, 0)))
    out.sort(key=lambda r: -(r[3] * r[1]))  # p95 * volume ~ total pain
    return out


def _chains(rows) -> dict[str, int]:
    """Tool-level chain detection within the 60s window (no symbol args in
    tool_latency, so this is an approximate trend signal, not exact)."""
    counts = Counter()
    seq = [(ts, tool) for ts, tool, _d, _s in rows]
    for i, (ts, tool) in enumerate(seq):
        # look back within window
        j = i - 1
        recent = []
        while j >= 0 and ts - seq[j][0] <= _CHAIN_WINDOW:
            recent.append(seq[j][1])
            j -= 1
        if tool in {"get_function_source", "get_class_source"} and "find_symbol" in recent:
            counts["find_symbol->read"] += 1
        if tool == "get_full_context" and (
            "get_function_source" in recent or "get_class_source" in recent
        ):
            counts["read->get_full_context"] += 1
        if tool in _EDIT_TOOLS and "get_edit_context" not in recent:
            counts["edit_without_context"] += 1
        if tool in _NAV_TOOLS:
            nav_recent = sum(1 for t in recent if t in _NAV_TOOLS) + 1
            if nav_recent == 5:
                counts["nav_burst_5plus"] += 1
    return dict(counts)


def _ml_liveness(stats_dir: str) -> dict:
    out = {}
    tca = _load_json(os.path.join(stats_dir, "tca_coactivation.json"))
    out["tca_sessions"] = tca.get("session_count", 0)
    out["tca_pairs"] = len(tca.get("coactivation", {}) or {})
    markov = _load_json(os.path.join(stats_dir, "markov_model.json"))
    out["markov_keys"] = len(markov)
    out["markov_bogus_tool_keys"] = sum(
        1 for k in markov if k.split(":")[0] not in _NAV_TOOLS
        and k.split(":")[0] not in _EDIT_TOOLS
        and ":" in k and k.split(":")[0].startswith(("nonexistent", "get_community"))
    )
    return out


def audit(stats_dir: str, since_days: int) -> dict:
    since = int(time.time()) - since_days * 86400
    counts = _tool_counts(stats_dir)
    rows = _latency_rows(stats_dir, since)
    nudges = _load_json(os.path.join(stats_dir, "nudge-stats.json")).get("counts", {})
    return {
        "stats_dir": stats_dir,
        "since_days": since_days,
        "total_calls_cumulative": sum(counts.values()),
        "top_tools": sorted(counts.items(), key=lambda kv: -kv[1])[:15],
        "latency_window": _latency_table(rows),
        "latency_sample_size": len(rows),
        "chains": _chains(rows),
        "nudge_fires": nudges,
        "adoption": {
            "get_edit_context": counts.get("get_edit_context", 0),
            "edits_total": sum(counts.get(t, 0) for t in _EDIT_TOOLS),
            "ts_execute": counts.get("ts_execute", 0),
            "nav_total": sum(counts.get(t, 0) for t in _NAV_TOOLS),
            "set_project_root": counts.get("set_project_root", 0),
            "switch_project": counts.get("switch_project", 0),
        },
        "ml": _ml_liveness(stats_dir),
    }


def render(a: dict) -> str:
    L = [f"# TS usage audit — window {a['since_days']}d ({a['stats_dir']})", ""]
    L.append(f"Cumulative tool calls: {a['total_calls_cumulative']:,} | "
             f"latency sample (windowed): {a['latency_sample_size']}")
    L.append("")
    L.append("## Latency (windowed, sorted by p95*volume)")
    L.append("| tool | n | p50 | p95 | max | err |")
    L.append("|---|--:|--:|--:|--:|--:|")
    for tool, n, p50, p95, mx, err in a["latency_window"][:15]:
        L.append(f"| {tool} | {n} | {p50:.0f} | {p95:.0f} | {mx:.0f} | {err} |")
    L.append("")
    L.append("## Wasteful chains (tool-level, 60s window)")
    for k, v in sorted(a["chains"].items(), key=lambda kv: -kv[1]):
        L.append(f"- {k}: {v}")
    L.append("")
    ad = a["adoption"]
    L.append("## Adoption gaps")
    L.append(f"- get_edit_context: {ad['get_edit_context']} vs {ad['edits_total']} edits "
             f"({'GAP' if ad['edits_total'] and ad['get_edit_context'] < ad['edits_total'] * 0.2 else 'ok'})")
    L.append(f"- ts_execute: {ad['ts_execute']} vs {ad['nav_total']} nav calls")
    L.append(f"- set_project_root: {ad['set_project_root']} vs switch_project: {ad['switch_project']} "
             f"({'CHURN' if ad['set_project_root'] > ad['switch_project'] * 0.5 else 'ok'})")
    L.append("")
    L.append("## Nudge fires (compare vs adoption over successive runs)")
    if a["nudge_fires"]:
        for k, v in sorted(a["nudge_fires"].items(), key=lambda kv: -kv[1]):
            L.append(f"- {k}: {v}")
    else:
        L.append("- (none recorded yet -- baseline)")
    L.append("")
    ml = a["ml"]
    L.append("## ML liveness")
    L.append(f"- TCA co-activation: {ml['tca_sessions']} sessions, {ml['tca_pairs']} pairs "
             f"({'DEAD' if ml['tca_sessions'] == 0 else 'live'})")
    L.append(f"- Markov transition keys: {ml['markov_keys']} "
             f"(bogus tool-prefix keys: {ml['markov_bogus_tool_keys']})")
    return "\n".join(L)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--since-days", type=int, default=30)
    ap.add_argument("--stats-dir", default=_DEFAULT_STATS_DIR)
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()
    a = audit(args.stats_dir, args.since_days)
    print(json.dumps(a, indent=2, default=str) if args.json else render(a))


if __name__ == "__main__":
    main()
