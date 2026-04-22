"""Memory retrieval bench: FTS5 vs Vector vs Hybrid (RRF) on the auto-memory corpus.

Builds an isolated SQLite DB from all .md files in
/root/.claude/projects/-root/memory/ (Louis's auto-memory), indexes them as
observations, then replays queries.json and reports MRR@10 / Recall@3 /
Recall@10 / latency per config.

Runs standalone (not pytest):
    python tests/benchmarks/memory_retrieval/run_bench.py
"""
from __future__ import annotations

import json
import statistics
import sys
import tempfile
import time
from pathlib import Path

CORPUS_DIR = Path("/root/.claude/projects/-root/memory")
HERE = Path(__file__).resolve().parent
QUERIES_PATH = HERE / "queries.json"
RESULTS_DIR = HERE / "results"


def _load_corpus() -> list[dict]:
    docs = []
    for md in sorted(CORPUS_DIR.glob("*.md")):
        if md.name == "MEMORY.md":
            continue
        raw = md.read_text(encoding="utf-8", errors="replace")
        title = md.stem
        if raw.startswith("---"):
            end = raw.find("\n---", 3)
            if end > 0:
                fm = raw[3:end]
                body = raw[end + 4:].lstrip()
                for line in fm.splitlines():
                    if line.startswith("name:"):
                        title = line.split(":", 1)[1].strip()
                        break
            else:
                body = raw
        else:
            body = raw
        docs.append({"stem": md.stem, "title": title, "body": body.strip()})
    return docs


def _seed_db(db_path: Path, docs: list[dict]) -> dict[int, str]:
    import token_savior.memory_db as memory_db
    memory_db.MEMORY_DB_PATH = str(db_path)
    from token_savior import memory_db as md
    md.MEMORY_DB_PATH = str(db_path)
    import token_savior.db_core as db_core
    db_core.run_migrations(db_path)

    from token_savior.memory.observations import observation_save

    id_to_stem: dict[int, str] = {}
    for d in docs:
        oid = observation_save(
            session_id=None,
            project_root="/bench",
            type="note",
            title=d["title"],
            content=d["body"],
            importance=5,
        )
        if oid is not None:
            id_to_stem[oid] = d["stem"]
    return id_to_stem


def _fts_search(conn, query, limit=40):
    import token_savior.db_core as db_core
    fts_q = db_core._fts5_safe_query(query)
    if not fts_q:
        return []
    sql = (
        "SELECT o.id, bm25(observations_fts) AS score "
        "FROM observations_fts AS f "
        "JOIN observations AS o ON o.rowid = f.rowid "
        "WHERE observations_fts MATCH ? AND o.archived = 0 "
        "ORDER BY bm25(observations_fts) "
        "LIMIT ?"
    )
    try:
        rows = conn.execute(sql, (fts_q, limit)).fetchall()
    except Exception:
        return []
    return [{"id": r[0]} for r in rows]


def _vec_search(conn, query, limit=40):
    from token_savior.memory.embeddings import embed
    import sqlite_vec
    vec = embed(query, as_query=True)
    if vec is None:
        return []
    blob = sqlite_vec.serialize_float32(vec)
    sql = (
        "SELECT o.id, v.distance "
        "FROM obs_vectors AS v "
        "JOIN observations AS o ON o.id = v.obs_id "
        "WHERE v.embedding MATCH ? AND k = ? AND o.archived = 0 "
        "ORDER BY v.distance"
    )
    try:
        rows = conn.execute(sql, (blob, limit)).fetchall()
    except Exception:
        return []
    return [{"id": r[0]} for r in rows]


def _hybrid(conn, query, limit=40, rrf_k=60):
    from token_savior.memory.search import rrf_merge
    fts = _fts_search(conn, query, limit=limit * 2)
    vec = _vec_search(conn, query, limit=limit * 2)
    if not vec:
        return fts[:limit]
    if not fts:
        return vec[:limit]
    return rrf_merge(fts, vec, limit=limit, k=rrf_k)


def _metrics(ranked_stems: list[str], gt: list[str]) -> dict:
    gt_set = set(gt)
    hits_3 = sum(1 for s in ranked_stems[:3] if s in gt_set)
    hits_10 = sum(1 for s in ranked_stems[:10] if s in gt_set)
    rr = 0.0
    for rank, s in enumerate(ranked_stems[:10], start=1):
        if s in gt_set:
            rr = 1.0 / rank
            break
    return {
        "rr": rr,
        "recall_3": hits_3 / len(gt_set) if gt_set else 0.0,
        "recall_10": hits_10 / len(gt_set) if gt_set else 0.0,
        "found_ranks": [i + 1 for i, s in enumerate(ranked_stems[:10]) if s in gt_set],
    }


def _agg(per_query: list[dict]) -> dict:
    if not per_query:
        return {}
    return {
        "mrr_10": round(statistics.mean(r["rr"] for r in per_query), 4),
        "recall_3": round(statistics.mean(r["recall_3"] for r in per_query), 4),
        "recall_10": round(statistics.mean(r["recall_10"] for r in per_query), 4),
        "p50_ms": round(statistics.median(r["latency_ms"] for r in per_query), 1),
        "p95_ms": round(
            statistics.quantiles(
                [r["latency_ms"] for r in per_query], n=20
            )[18], 1
        ) if len(per_query) >= 20 else round(
            max(r["latency_ms"] for r in per_query), 1
        ),
    }


def run(sweep_rrf: list[int] | None = None) -> dict:
    qspec = json.loads(QUERIES_PATH.read_text())
    queries = qspec["queries"]
    docs = _load_corpus()
    print(f"[bench] corpus: {len(docs)} docs, {len(queries)} queries")

    with tempfile.TemporaryDirectory() as td:
        db_path = Path(td) / "bench.db"
        t0 = time.perf_counter()
        id_to_stem = _seed_db(db_path, docs)
        seed_s = time.perf_counter() - t0
        print(f"[bench] seeded + indexed in {seed_s:.1f}s")

        import token_savior.memory_db as memory_db
        conn = memory_db.get_db(db_path)

        configs: dict = {
            "fts5": _fts_search,
            "vector": _vec_search,
            "hybrid": _hybrid,
        }
        if sweep_rrf:
            for k in sweep_rrf:
                configs[f"hybrid_k{k}"] = (lambda kv: lambda c, q, limit=10: _hybrid(c, q, limit, rrf_k=kv))(k)

        all_results: dict[str, list[dict]] = {k: [] for k in configs}
        per_query_table: list[dict] = []

        for q in queries:
            row = {"id": q["id"], "query": q["query"], "kind": q["kind"]}
            for name, fn in configs.items():
                t0 = time.perf_counter()
                rows = fn(conn, q["query"], limit=10)
                latency_ms = (time.perf_counter() - t0) * 1000
                stems = [id_to_stem.get(r["id"], "?") for r in rows]
                m = _metrics(stems, q["gt"])
                m["latency_ms"] = latency_ms
                m["top3"] = stems[:3]
                all_results[name].append(m)
                row[f"{name}_rr"] = m["rr"]
                row[f"{name}_r3"] = m["recall_3"]
                row[f"{name}_ms"] = round(latency_ms, 1)
            per_query_table.append(row)

        conn.close()

    agg = {name: _agg(rs) for name, rs in all_results.items()}
    return {
        "corpus_size": len(docs),
        "num_queries": len(queries),
        "seed_seconds": round(seed_s, 2),
        "agg": agg,
        "per_query": per_query_table,
    }


def _print_report(result: dict) -> str:
    agg = result["agg"]
    lines = []
    lines.append("# Memory retrieval bench")
    lines.append("")
    lines.append(f"- Corpus: {result['corpus_size']} docs (auto-memory .md)")
    lines.append(f"- Queries: {result['num_queries']} handcrafted with ground truth")
    lines.append(f"- Seed+index time: {result['seed_seconds']}s")
    lines.append("")
    lines.append("## Aggregated metrics")
    lines.append("")
    lines.append("| Config | MRR@10 | Recall@3 | Recall@10 | P50 ms | P95 ms |")
    lines.append("|---|---|---|---|---|---|")
    ordered = ["fts5", "vector", "hybrid"] + [k for k in agg if k.startswith("hybrid_k")]
    for name in ordered:
        a = agg[name]
        lines.append(
            f"| **{name}** | {a['mrr_10']} | {a['recall_3']} | {a['recall_10']} | "
            f"{a['p50_ms']} | {a['p95_ms']} |"
        )
    lines.append("")
    base = agg["fts5"]["mrr_10"] or 1e-9
    lines.append(
        f"Hybrid vs FTS5 (MRR@10): "
        f"{(agg['hybrid']['mrr_10'] - agg['fts5']['mrr_10']):+.4f} "
        f"({(agg['hybrid']['mrr_10'] / base - 1) * 100:+.1f}%)"
    )
    lines.append("")
    lines.append("## Per-query")
    lines.append("")
    lines.append("| ID | Kind | FTS5 RR | Vec RR | Hybrid RR | Query |")
    lines.append("|---|---|---|---|---|---|")
    for r in result["per_query"]:
        lines.append(
            f"| {r['id']} | {r['kind']} | {r['fts5_rr']:.3f} | "
            f"{r['vector_rr']:.3f} | {r['hybrid_rr']:.3f} | {r['query']} |"
        )
    return "\n".join(lines)


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--sweep-rrf", type=str, default="",
                        help="Comma-separated RRF k values, e.g. '5,10,20,30,60'")
    args = parser.parse_args()
    sweep = [int(x) for x in args.sweep_rrf.split(",") if x.strip()] if args.sweep_rrf else None

    sys.path.insert(0, "/root/token-savior/src")
    result = run(sweep_rrf=sweep)
    report = _print_report(result)
    print()
    print(report)
    RESULTS_DIR.mkdir(exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    out_md = RESULTS_DIR / f"{stamp}.md"
    out_json = RESULTS_DIR / f"{stamp}.json"
    out_md.write_text(report, encoding="utf-8")
    out_json.write_text(json.dumps(result, indent=2), encoding="utf-8")
    print()
    print(f"[bench] wrote {out_md}")
    print(f"[bench] wrote {out_json}")
