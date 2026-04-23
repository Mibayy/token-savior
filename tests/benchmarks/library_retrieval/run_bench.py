"""Library retrieval bench: find_library_symbol_by_description on stdlib.

Evaluates two questions at once:

  1. Is semantic library lookup precise enough to rely on? (MRR/Recall)
  2. Does the 0.75 low-confidence threshold in library_api carry signal,
     or should it go the way of the search_codebase warning? (precision
     / recall of the warning against actually-wrong retrievals).

Uses stdlib packages only (json, pathlib, re) so the bench has no external
dependency beyond the embedding stack. First run warms the LRU cache in
``library_api._cached_doc_embed``; a second pass is measured to verify
the cache kicks in (expected: ~10x speedup).

Runs standalone (not pytest):
    python tests/benchmarks/library_retrieval/run_bench.py
"""
from __future__ import annotations

import json
import statistics
import sys
import tempfile
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO_ROOT = HERE.parents[2]
QUERIES_PATH = HERE / "queries.json"
RESULTS_DIR = HERE / "results"


def _metrics(ranked_names: list[str], gt: list[str]) -> dict:
    gt_set = set(gt)
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
    lat = [r["latency_ms"] for r in per_query]
    return {
        "mrr_10": round(statistics.mean(r["rr"] for r in per_query), 4),
        "recall_3": round(statistics.mean(r["recall_3"] for r in per_query), 4),
        "recall_10": round(statistics.mean(r["recall_10"] for r in per_query), 4),
        "p50_ms": round(statistics.median(lat), 1),
        "p95_ms": round(
            statistics.quantiles(lat, n=20)[18], 1
        ) if len(lat) >= 20 else round(max(lat), 1),
        "low_confidence_rate": round(
            sum(1 for r in per_query if r["low_confidence"]) / len(per_query), 2
        ),
    }


def run() -> dict:
    sys.path.insert(0, str(REPO_ROOT / "src"))
    from token_savior.library_api import (
        _cached_doc_embed, find_library_symbol_by_description,
    )

    qspec = json.loads(QUERIES_PATH.read_text())
    queries = qspec["queries"]

    # Cold run: empty cache, measures real per-package embed cost.
    _cached_doc_embed.cache_clear()

    with tempfile.TemporaryDirectory() as td:
        per_query_cold: list[dict] = []
        for q in queries:
            t = time.perf_counter()
            res = find_library_symbol_by_description(
                q["package"], q["query"],
                project_root=td, limit=10,
            )
            latency_ms = (time.perf_counter() - t) * 1000
            if not res.get("ok"):
                per_query_cold.append({
                    "id": q["id"], "query": q["query"], "package": q["package"],
                    "rr": 0.0, "recall_3": 0.0, "recall_10": 0.0,
                    "latency_ms": latency_ms, "low_confidence": False,
                    "top3": [], "error": res.get("error"),
                })
                continue
            names = [h["name"] for h in res["hits"]]
            m = _metrics(names, q["gt"])
            m.update({
                "id": q["id"], "query": q["query"], "package": q["package"],
                "latency_ms": latency_ms,
                "low_confidence": bool(res.get("warning")),
                "top1_score": res["hits"][0]["score"] if res["hits"] else 0.0,
                "top2_score": res["hits"][1]["score"] if len(res["hits"]) > 1 else 0.0,
                "top3": names[:3],
            })
            per_query_cold.append(m)

        cache_after_cold = _cached_doc_embed.cache_info()

        # Warm run: same queries, cache pre-populated by cold run.
        per_query_warm: list[dict] = []
        for q in queries:
            t = time.perf_counter()
            res = find_library_symbol_by_description(
                q["package"], q["query"],
                project_root=td, limit=10,
            )
            latency_ms = (time.perf_counter() - t) * 1000
            if not res.get("ok"):
                continue
            names = [h["name"] for h in res["hits"]]
            m = _metrics(names, q["gt"])
            m.update({
                "id": q["id"], "latency_ms": latency_ms,
                "low_confidence": bool(res.get("warning")),
            })
            per_query_warm.append(m)

        cache_after_warm = _cached_doc_embed.cache_info()

    return {
        "num_queries": len(queries),
        "cold": {
            "agg": _agg(per_query_cold),
            "per_query": per_query_cold,
            "cache_info": {
                "hits": cache_after_cold.hits,
                "misses": cache_after_cold.misses,
                "size": cache_after_cold.currsize,
            },
        },
        "warm": {
            "agg": _agg(per_query_warm),
            "cache_info": {
                "hits": cache_after_warm.hits,
                "misses": cache_after_warm.misses,
                "size": cache_after_warm.currsize,
            },
        },
    }


def _report(result: dict) -> str:
    cold = result["cold"]["agg"]
    warm = result["warm"]["agg"]
    lines = []
    lines.append("# Library retrieval bench")
    lines.append("")
    lines.append("- Packages: json, pathlib, re (stdlib)")
    lines.append(f"- Queries: {result['num_queries']} handcrafted with ground truth")
    lines.append("")
    lines.append("## Cold vs warm (LRU cache)")
    lines.append("")
    lines.append("| Pass | MRR@10 | Recall@3 | Recall@10 | P50 ms | P95 ms | Low-conf rate |")
    lines.append("|---|---|---|---|---|---|---|")
    lines.append(
        f"| cold | {cold['mrr_10']} | {cold['recall_3']} | {cold['recall_10']} | "
        f"{cold['p50_ms']} | {cold['p95_ms']} | {cold['low_confidence_rate']} |"
    )
    lines.append(
        f"| warm | {warm['mrr_10']} | {warm['recall_3']} | {warm['recall_10']} | "
        f"{warm['p50_ms']} | {warm['p95_ms']} | {warm['low_confidence_rate']} |"
    )
    lines.append("")
    speedup = cold["p50_ms"] / max(warm["p50_ms"], 1e-3)
    lines.append(
        f"Cache speedup (P50 cold/warm): {speedup:.1f}x "
        f"(warm P50 {warm['p50_ms']} ms vs cold {cold['p50_ms']} ms)."
    )
    ci_cold = result["cold"]["cache_info"]
    ci_warm = result["warm"]["cache_info"]
    lines.append(
        f"Cache: cold pass {ci_cold['misses']} misses / {ci_cold['hits']} hits "
        f"(filled {ci_cold['size']} entries); warm pass "
        f"{ci_warm['misses'] - ci_cold['misses']} new misses / "
        f"{ci_warm['hits'] - ci_cold['hits']} hits."
    )
    lines.append("")

    # Warning diagnostic: does 0.75/0.02 discriminate correct vs wrong?
    rows = result["cold"]["per_query"]
    correct = [r for r in rows if r.get("rr", 0) > 0]
    wrong = [r for r in rows if r.get("rr", 0) == 0 and "error" not in r]
    flagged = [r for r in rows if r.get("low_confidence")]
    wrong_flagged = [r for r in rows if r.get("low_confidence") and r.get("rr", 0) == 0]
    prec = (
        len(wrong_flagged) / len(flagged) if flagged else None
    )
    rec = (
        len(wrong_flagged) / len(wrong) if wrong else None
    )
    lines.append("## Warning diagnostic (0.75 floor / 0.02 gap)")
    lines.append("")
    lines.append(f"- Correct retrievals (rr > 0): {len(correct)}/{len(rows)}")
    lines.append(f"- Wrong retrievals (rr == 0): {len(wrong)}/{len(rows)}")
    lines.append(f"- Flagged by warning: {len(flagged)}/{len(rows)}")
    lines.append(f"- Wrong AND flagged: {len(wrong_flagged)}")
    if prec is not None:
        lines.append(f"- **Warning precision**: {len(wrong_flagged)}/{len(flagged)} = {prec:.2f}")
    if rec is not None:
        lines.append(f"- **Warning recall**: {len(wrong_flagged)}/{len(wrong)} = {rec:.2f}")
    lines.append("")
    lines.append("## Per-query (cold pass)")
    lines.append("")
    lines.append("| ID | Package | RR | R@3 | Top-1 score | Top-1 name | Query |")
    lines.append("|---|---|---|---|---|---|---|")
    for r in rows:
        if "error" in r:
            lines.append(
                f"| {r['id']} | {r['package']} | — | — | — | "
                f"(error: {r['error']}) | {r['query']} |"
            )
            continue
        flag = " ⚠️" if r["low_confidence"] else ""
        top1 = r["top3"][0] if r["top3"] else "—"
        lines.append(
            f"| {r['id']} | {r['package']} | {r['rr']:.3f} | {r['recall_3']:.3f} | "
            f"{r['top1_score']:.3f}{flag} | `{top1}` | {r['query']} |"
        )
    return "\n".join(lines)


if __name__ == "__main__":
    sys.path.insert(0, str(REPO_ROOT / "src"))
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
