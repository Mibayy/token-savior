"""Code retrieval bench: semantic search_codebase over the token-savior source.

Builds a one-off symbol_vectors index for ``src/token_savior/``, replays
``queries.json``, and reports MRR@10 / Recall@3 / Recall@10 plus the
low-confidence warning rate. The result lands under ``results/`` for
future comparison (re-run after a model change to spot regressions).

Runs standalone (not pytest):
    python tests/benchmarks/code_retrieval/run_bench.py
"""
from __future__ import annotations

import json
import statistics
import sys
import tempfile
import time
from pathlib import Path

SRC = Path("/root/token-savior/src/token_savior")
HERE = Path(__file__).resolve().parent
QUERIES_PATH = HERE / "queries.json"
RESULTS_DIR = HERE / "results"


def _basename_from_key(key: str) -> str:
    # symbol_key = "path/to/file.py::Qualified.Name"
    if "::" not in key:
        return key
    _, qname = key.split("::", 1)
    return qname.rsplit(".", 1)[-1]


def _metrics(ranked_names: list[str], gt: list[str]) -> dict:
    gt_set = set(gt)
    # Deduplicate via ``set(...) & gt_set`` — when a symbol name exists
    # in several files (eg. ``embed`` in multiple modules) the raw
    # intersection would overcount and push Recall above 1.0.
    hits_3 = len(set(ranked_names[:3]) & gt_set)
    hits_10 = len(set(ranked_names[:10]) & gt_set)
    rr = 0.0
    for rank, n in enumerate(ranked_names[:10], 1):
        if n in gt_set:
            rr = 1.0 / rank
            break
    return {
        "rr": rr,
        "recall_3": hits_3 / len(gt_set) if gt_set else 0.0,
        "recall_10": hits_10 / len(gt_set) if gt_set else 0.0,
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
        "low_confidence_rate": round(
            sum(1 for r in per_query if r["low_confidence"]) / len(per_query), 2
        ),
    }


def run() -> dict:
    sys.path.insert(0, "/root/token-savior/src")
    from token_savior import db_core, memory_db
    from token_savior.memory.symbol_embeddings import (
        reindex_project_symbols, search_symbols_semantic,
    )

    qspec = json.loads(QUERIES_PATH.read_text())
    queries = qspec["queries"]

    with tempfile.TemporaryDirectory() as td:
        db = Path(td) / "bench.db"
        db_core.run_migrations(db)
        memory_db.MEMORY_DB_PATH = str(db)

        t0 = time.perf_counter()
        reindex = reindex_project_symbols(SRC)
        index_s = time.perf_counter() - t0
        print(f"[bench] indexed {reindex['indexed']} symbols in {index_s:.1f}s",
              flush=True)

        per_query: list[dict] = []
        for q in queries:
            t = time.perf_counter()
            res = search_symbols_semantic(q["query"], SRC, limit=10)
            latency_ms = (time.perf_counter() - t) * 1000
            names = [h["symbol"].rsplit(".", 1)[-1] for h in res["hits"]]
            m = _metrics(names, q["gt"])
            m["id"] = q["id"]
            m["query"] = q["query"]
            m["kind"] = q["kind"]
            m["latency_ms"] = latency_ms
            m["low_confidence"] = bool(res.get("warning"))
            m["top3"] = names[:3]
            per_query.append(m)

    return {
        "corpus_source": str(SRC),
        "corpus_symbols": reindex.get("total", 0),
        "indexed": reindex.get("indexed", 0),
        "skipped": reindex.get("skipped", 0),
        "index_seconds": round(index_s, 2),
        "num_queries": len(queries),
        "agg": _agg(per_query),
        "per_query": per_query,
    }


def _report(result: dict) -> str:
    agg = result["agg"]
    lines = []
    lines.append("# Code retrieval bench")
    lines.append("")
    lines.append(f"- Corpus: {result['corpus_source']}")
    lines.append(f"- Symbols: {result['corpus_symbols']} ({result['indexed']} indexed)")
    lines.append(f"- Queries: {result['num_queries']} handcrafted with ground truth")
    lines.append(f"- Index time: {result['index_seconds']}s")
    lines.append("")
    lines.append("## Aggregate")
    lines.append("")
    lines.append("| MRR@10 | Recall@3 | Recall@10 | P50 ms | P95 ms | Low-conf rate |")
    lines.append("|---|---|---|---|---|---|")
    lines.append(
        f"| {agg['mrr_10']} | {agg['recall_3']} | {agg['recall_10']} | "
        f"{agg['p50_ms']} | {agg['p95_ms']} | {agg['low_confidence_rate']} |"
    )
    lines.append("")
    lines.append("## Per-query")
    lines.append("")
    lines.append("| ID | Kind | RR | R@3 | Top-1 | Query |")
    lines.append("|---|---|---|---|---|---|")
    for r in result["per_query"]:
        top1 = r["top3"][0] if r["top3"] else "—"
        flag = " ⚠️" if r["low_confidence"] else ""
        lines.append(
            f"| {r['id']} | {r['kind']} | {r['rr']:.3f} | {r['recall_3']:.3f} | "
            f"`{top1}`{flag} | {r['query']} |"
        )
    return "\n".join(lines)


if __name__ == "__main__":
    result = run()
    md = _report(result)
    print()
    print(md)
    RESULTS_DIR.mkdir(exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    (RESULTS_DIR / f"{stamp}.md").write_text(md, encoding="utf-8")
    (RESULTS_DIR / f"{stamp}.json").write_text(
        json.dumps(result, indent=2), encoding="utf-8",
    )
    print(f"\n[bench] wrote {RESULTS_DIR}/{stamp}.{{md,json}}")
