#!/root/.local/token-savior-venv/bin/python3
"""ts — standalone CLI for Token Savior memory from any terminal."""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
from token_savior import memory_db  # noqa: E402


def _resolve_project(explicit: str | None) -> str:
    if explicit:
        return explicit
    env = os.environ.get("CLAUDE_PROJECT_ROOT")
    if env:
        return env
    db = memory_db.get_db()
    row = db.execute(
        "SELECT project_root FROM observations GROUP BY project_root "
        "ORDER BY COUNT(*) DESC LIMIT 1"
    ).fetchone()
    db.close()
    return row[0] if row else ""


def _print_obs_line(r: dict) -> None:
    age = memory_db.relative_age(r.get("created_at_epoch")) or "?"
    glob = "🌐 " if r.get("is_global") else ""
    sym = f" [{r['symbol']}]" if r.get("symbol") else ""
    print(f"  #{r['id']}  [{r['type']}]  {glob}{r['title']}{sym}  {age}")


def cmd_status(args) -> int:
    project = _resolve_project(args.project)
    db = memory_db.get_db()
    total = db.execute(
        "SELECT COUNT(*) FROM observations WHERE archived=0"
    ).fetchone()[0]
    proj_count = 0
    if project:
        proj_count = db.execute(
            "SELECT COUNT(*) FROM observations WHERE project_root=? AND archived=0",
            [project],
        ).fetchone()[0]
    by_type = db.execute(
        "SELECT type, COUNT(*) FROM observations WHERE archived=0 "
        "GROUP BY type ORDER BY COUNT(*) DESC"
    ).fetchall()
    db.close()
    mode = memory_db.get_current_mode(project_root=project or None)
    print(f"Project:  {project or '(none)'}")
    print(f"Mode:     {mode.get('name', 'code')} ({mode.get('origin', 'global')})")
    print(f"Obs:      {proj_count} (project) / {total} (total)")
    if project:
        try:
            cs = memory_db.compute_continuity_score(project)
            if cs.get("total", 0) > 0:
                print(
                    f"Continuity: {cs['score']}% ({cs['label']}) — "
                    f"{cs['valid']}/{cs['total']} valid"
                )
        except Exception:
            pass
    print("\nBy type:")
    for t, c in by_type:
        print(f"  {t:15s} {c}")
    return 0


def cmd_list(args) -> int:
    project = _resolve_project(args.project)
    if not project:
        print("No project found.", file=sys.stderr)
        return 1
    rows = memory_db.get_recent_index(
        project, limit=args.limit, type_filter=args.type
    )
    if not rows:
        print("(no observations)")
        return 0
    for r in rows:
        _print_obs_line(r)
    return 0


def cmd_search(args) -> int:
    project = _resolve_project(args.project)
    rows = memory_db.observation_search(
        project or "", args.query, limit=args.limit
    )
    if not rows:
        print("(no matches)")
        return 0
    for r in rows:
        _print_obs_line(r)
    return 0


def cmd_get(args) -> int:
    db = memory_db.get_db()
    row = db.execute(
        "SELECT * FROM observations WHERE id=?", [args.id]
    ).fetchone()
    db.close()
    if not row:
        print(f"Observation #{args.id} not found.", file=sys.stderr)
        return 1
    obs = dict(row)
    age = memory_db.relative_age(obs.get("created_at_epoch")) or "?"
    print(f"## #{obs['id']} — {obs['title']}")
    print(f"**Type:** {obs['type']}  **Created:** {age}")
    if obs.get("symbol"):
        print(f"**Symbol:** {obs['symbol']}")
    if obs.get("file_path"):
        print(f"**File:** {obs['file_path']}")
    if obs.get("context"):
        print(f"**Context:** {obs['context']}")
    if obs.get("is_global"):
        print("🌐 **Global**")
    print()
    content = obs.get("content") or ""
    if not args.full and len(content) > 80:
        content = content[:80] + "... (--full for complete)"
    print(content)
    if obs.get("why"):
        w = obs["why"]
        if not args.full and len(w) > 80:
            w = w[:80] + "..."
        print(f"\n**Why:** {w}")
    if obs.get("how_to_apply"):
        h = obs["how_to_apply"]
        if not args.full and len(h) > 80:
            h = h[:80] + "..."
        print(f"\n**How to apply:** {h}")
    try:
        links = memory_db.get_linked_observations(obs["id"])
    except Exception:
        links = {"related": [], "contradicts": [], "supersedes": []}
    if links.get("related"):
        parts = [f"#{l['id']} [{l['type']}] {l['title']}" for l in links["related"][:5]]
        print("\n🔗 See also: " + " · ".join(parts))
    if links.get("contradicts"):
        parts = [f"#{l['id']} [{l['type']}] {l['title']}" for l in links["contradicts"][:5]]
        print("⚠️ Contradicts: " + " · ".join(parts))
    return 0


def cmd_save(args) -> int:
    project = _resolve_project(args.project)
    if not project:
        print("No project. Use --project.", file=sys.stderr)
        return 1
    conflicts = memory_db.detect_contradictions(
        project, args.title, args.content, args.type
    )
    tags = args.tag or []
    if conflicts and "potential-conflict" not in tags:
        tags.append("potential-conflict")
    obs_id = memory_db.observation_save(
        None,
        project_root=project,
        type=args.type,
        title=args.title,
        content=args.content,
        symbol=args.symbol,
        file_path=args.file,
        context=args.context,
        tags=tags,
        is_global=bool(args.global_),
        ttl_days=getattr(args, "ttl_days", None),
    )
    if obs_id is None:
        print("Duplicate observation — not saved.")
        return 0
    try:
        memory_db.auto_link_observation(
            obs_id, project, contradict_ids=[c["id"] for c in conflicts]
        )
    except Exception:
        pass
    print(f"Observation #{obs_id} saved ({args.type}: {args.title}).")
    if conflicts:
        print("⚠️ Potential contradictions:")
        for c in conflicts[:5]:
            print(f"  #{c['id']} [{c['type']}] {c['title']}")
    return 0


def cmd_delete(args) -> int:
    db = memory_db.get_db()
    cur = db.execute(
        "UPDATE observations SET archived=1 WHERE id=?", [args.id]
    )
    db.commit()
    affected = cur.rowcount
    db.close()
    if affected:
        print(f"Observation #{args.id} archived.")
        return 0
    print(f"Observation #{args.id} not found.", file=sys.stderr)
    return 1


def cmd_mode_get(args) -> int:
    project = _resolve_project(args.project)
    mode = memory_db.get_current_mode(project_root=project or None)
    print(f"Mode: {mode.get('name', 'code')} ({mode.get('origin', 'global')})")
    print(f"Auto-capture: {', '.join(mode.get('auto_capture_types', []))}")
    return 0


def cmd_mode_set(args) -> int:
    try:
        memory_db.set_mode(args.mode, source="manual")
    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1
    print(f"Mode set to {args.mode} (manual).")
    return 0


def cmd_top(args) -> int:
    project = _resolve_project(args.project)
    rows = memory_db.get_top_observations(
        project, limit=args.limit, sort_by=args.sort
    )
    print(f"{'#':>5}  {'TYPE':12}  {'SCORE':>5}  {'ACC':>4}  TITLE")
    print("─" * 70)
    for r in rows:
        flags = ""
        if r.get("is_global"):
            flags += "🌐"
        if r.get("decay_immune"):
            flags += "🔒"
        title = (r["title"] or "")[:40]
        print(
            f"#{r['id']:<4} [{r['type']:10s}] {r['score']:5.2f}  "
            f"{(r.get('access_count') or 0):4d}  {title} {flags}"
        )
    return 0


def cmd_why(args) -> int:
    res = memory_db.explain_observation(args.id, args.query)
    if "error" in res:
        print(res["error"], file=sys.stderr)
        return 1
    print("═" * 60)
    print(f"Why #{res['obs_id']} [{res['type']}] '{res['title']}' appears:")
    print("─" * 60)
    for r in res["reasons"]:
        print(f"  {r}")
    return 0


def cmd_distill(args) -> int:
    project = _resolve_project(args.project)
    if not project:
        print("No project.", file=sys.stderr)
        return 1
    res = memory_db.run_mdl_distillation(
        project,
        dry_run=args.dry_run,
        min_cluster_size=args.min_cluster,
        compression_required=args.compression,
    )
    print(f"MDL Distillation{' (dry run)' if args.dry_run else ''}:")
    print(f"  Clusters found: {res['clusters_found']}")
    if not args.dry_run:
        print(f"  Applied: {res['clusters_applied']} clusters")
        print(f"  Abstractions created: {res['abstractions_created']}")
        print(f"  Obs distilled: {res['obs_distilled']}")
    print(f"  Tokens freed (estimate): ~{res['tokens_freed_estimate']:,}t")
    for i, p in enumerate(res.get("preview", [])[:5], 1):
        pct = p["compression_ratio"] * 100
        print(f"\nCluster {i} ({p['size']} obs, -{pct:.0f}% MDL): type={p['dominant_type']}")
        print(f"  Shared: {', '.join(p['shared_tokens'][:5])}")
        print(f"  MDL: {p['mdl_before']:.1f}t → {p['mdl_after']:.1f}t")
    return 0


def cmd_relink(args) -> int:
    project = _resolve_project(args.project)
    if not project:
        print("No project.", file=sys.stderr)
        return 1
    res = memory_db.relink_all(project, dry_run=args.dry_run)
    verb = "Would process" if args.dry_run else "Processed"
    print(
        f"{verb} {res['processed']} obs → {res['links_created']} links created "
        f"(total: {res['total_links_in_db']}, delta: +{res['delta']})"
    )
    return 0


def cmd_doctor(args) -> int:
    project = _resolve_project(args.project)
    if not project:
        print("No project.", file=sys.stderr)
        return 1
    issues = memory_db.run_health_check(project)
    s = issues["summary"]
    print("🏥 Memory Health Check")
    print("══════════════════════")
    if issues["orphan_symbols"]:
        print(f"⚠️  {len(issues['orphan_symbols'])} orphan symbols:")
        for o in issues["orphan_symbols"][:10]:
            print(f"  #{o['id']} {o['symbol']} ({o['file_path']}) — {o['title']}")
    else:
        print("✅ No orphan symbols")
    if issues["near_duplicates"]:
        print(f"⚠️  {len(issues['near_duplicates'])} near-duplicates:")
        for d in issues["near_duplicates"][:10]:
            print(
                f"  #{d['id_a']} ≈ #{d['id_b']} ({d['score']}) "
                f"'{d['title_a']}' ≈ '{d['title_b']}'"
            )
    else:
        print("✅ No near-duplicates")
    if issues["incomplete_obs"]:
        print(f"⚠️  {len(issues['incomplete_obs'])} incomplete obs:")
        for o in issues["incomplete_obs"][:10]:
            print(f"  #{o['id']} [{o['type']}] {o['title']}")
    else:
        print("✅ No incomplete obs")
    print(f"\nTotal issues: {s['total_issues']}")
    return 0


def cmd_bus(args) -> int:
    project = _resolve_project(args.project)
    if not project:
        print("No project found.", file=sys.stderr)
        return 1
    if args.action == "push":
        if not (args.agent and args.title and args.content):
            print("push requires --agent, --title, --content", file=sys.stderr)
            return 1
        oid = memory_db.observation_save_volatile(
            project_root=project,
            agent_id=args.agent,
            title=args.title,
            content=args.content,
            ttl_days=args.ttl_days or memory_db.DEFAULT_VOLATILE_TTL_DAYS,
        )
        if oid is None:
            print("Bus push skipped (duplicate or invalid).")
            return 0
        print(f"🤖 Bus push #{oid} from agent '{args.agent}': {args.title}")
        return 0
    rows = memory_db.memory_bus_list(
        project_root=project,
        agent_id=args.agent,
        limit=args.limit,
        include_expired=args.include_expired,
    )
    if not rows:
        scope = f" (agent '{args.agent}')" if args.agent else ""
        print(f"Bus is quiet{scope}.")
        return 0
    print(f"🤖 Inter-agent bus ({len(rows)} live message(s)):")
    for r in rows:
        print(
            f"  #{r['id']}  [{r.get('agent_id') or '?'}]  "
            f"{r['title']}  —  {r.get('age', '?')}"
        )
    return 0


def cmd_budget(args) -> int:
    project = _resolve_project(args.project)
    if not project:
        print("No project found.", file=sys.stderr)
        return 1
    budget = args.budget or memory_db.DEFAULT_SESSION_BUDGET_TOKENS
    stats = memory_db.get_session_budget_stats(project, budget_tokens=budget)
    print(memory_db.format_session_budget_box(stats))
    return 0


def cmd_export(args) -> int:
    from export_markdown import export_all
    res = export_all(args.output_dir)
    print(
        f"Exported {res['observations']} obs across {res['projects']} projects "
        f"→ {res['output_dir']}"
        + (" (committed)" if res["committed"] else " (no changes)")
    )
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="ts", description="Token Savior CLI")
    sub = p.add_subparsers(dest="cmd", required=True)

    mem = sub.add_parser("memory", help="Memory operations")
    msub = mem.add_subparsers(dest="mcmd", required=True)

    s = msub.add_parser("status")
    s.add_argument("--project")
    s.set_defaults(func=cmd_status)

    s = msub.add_parser("list")
    s.add_argument("--project")
    s.add_argument("--limit", type=int, default=20)
    s.add_argument("--type")
    s.set_defaults(func=cmd_list)

    s = msub.add_parser("search")
    s.add_argument("query")
    s.add_argument("--project")
    s.add_argument("--limit", type=int, default=20)
    s.set_defaults(func=cmd_search)

    s = msub.add_parser("get")
    s.add_argument("id", type=int)
    s.add_argument("--full", action="store_true")
    s.set_defaults(func=cmd_get)

    s = msub.add_parser("save")
    s.add_argument("--project")
    s.add_argument("--type", required=True)
    s.add_argument("--title", required=True)
    s.add_argument("--content", required=True)
    s.add_argument("--symbol")
    s.add_argument("--file")
    s.add_argument("--context")
    s.add_argument("--tag", action="append")
    s.add_argument("--global", dest="global_", action="store_true")
    s.add_argument("--ttl", type=int, dest="ttl_days", help="TTL in days")
    s.set_defaults(func=cmd_save)

    s = msub.add_parser("delete")
    s.add_argument("id", type=int)
    s.set_defaults(func=cmd_delete)

    mode = msub.add_parser("mode")
    modesub = mode.add_subparsers(dest="modecmd", required=True)
    mg = modesub.add_parser("get")
    mg.add_argument("--project")
    mg.set_defaults(func=cmd_mode_get)
    ms = modesub.add_parser("set")
    ms.add_argument("mode")
    ms.set_defaults(func=cmd_mode_set)

    s = msub.add_parser("export")
    s.add_argument("--output-dir", default="/root/memory-backup")
    s.set_defaults(func=cmd_export)

    s = msub.add_parser("distill", help="MDL distillation (crystallize similar obs)")
    s.add_argument("--project")
    s.add_argument("--dry-run", action="store_true", default=False)
    s.add_argument("--min-cluster", type=int, default=3)
    s.add_argument("--compression", type=float, default=0.2)
    s.set_defaults(func=cmd_distill)

    s = msub.add_parser("relink")
    s.add_argument("--project")
    s.add_argument("--dry-run", action="store_true")
    s.set_defaults(func=cmd_relink)

    s = msub.add_parser("doctor")
    s.add_argument("--project")
    s.set_defaults(func=cmd_doctor)

    s = msub.add_parser("why")
    s.add_argument("id", type=int)
    s.add_argument("--query")
    s.set_defaults(func=cmd_why)

    s = msub.add_parser("budget")
    s.add_argument("--project")
    s.add_argument("--budget", type=int, help="Soft budget cap in tokens (default 200000)")
    s.set_defaults(func=cmd_budget)

    s = msub.add_parser("bus", help="Inter-agent memory bus")
    s.add_argument("action", choices=["list", "push"], default="list", nargs="?")
    s.add_argument("--project")
    s.add_argument("--agent", help="Subagent id (filter for list, required for push)")
    s.add_argument("--title")
    s.add_argument("--content")
    s.add_argument("--ttl-days", type=int, dest="ttl_days")
    s.add_argument("--limit", type=int, default=20)
    s.add_argument("--include-expired", action="store_true", dest="include_expired")
    s.set_defaults(func=cmd_bus)

    s = msub.add_parser("top")
    s.add_argument("--project")
    s.add_argument("--limit", type=int, default=20)
    s.add_argument("--sort", choices=["score", "access_count", "age"], default="score")
    s.set_defaults(func=cmd_top)

    return p


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
