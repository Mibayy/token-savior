"""Code retrieval bench: keyword baseline vs semantic search_codebase over the
token-savior source.

Builds a one-off symbol_vectors index for ``src/token_savior/``, replays
``queries.json`` against both a naive keyword baseline (token overlap on
symbol name + signature + docstring_head, the info an agent sees when it
greps for candidates) and the real ``search_codebase(semantic=True)`` path
(Nomic embeddings via ``search_symbols_semantic``). Reports MRR@10 /
Recall@3 / Recall@10 / latency for each config plus the low-confidence
warning rate on the semantic path.

This is the empirical "prove the qualitative win" step from
``docs/design-semantic-code-tools.md``. The result lands under
``results/`` for future comparison (re-run after a model change to spot
regressions).

Runs standalone (not pytest):
    python tests/benchmarks/code_retrieval/run_bench.py
"""
from __future__ import annotations

import json
import re
import statistics
import sys
import tempfile
import time
from pathlib import Path

SRC = Path("/root/token-savior/src/token_savior")
HERE = Path(__file__).resolve().parent
QUERIES_PATH = HERE / "queries.json"
RESULTS_DIR = HERE / "results"

# Stopwords kept intentionally tiny — we want the keyword baseline to
# behave like an agent grepping with natural-language keywords, not a
# tuned IR system. Too much stopword removal would make it look smarter
# than it is.
_STOP = frozenset({
    "a", "an", "the", "of", "in", "on", "to", "for", "and", "or", "by",
    "that", "is", "it", "as", "with", "into", "from",
    "le", "la", "les", "un", "une", "des", "de", "du", "et", "ou",
    "avec", "pour", "dans", "sur", "qui", "que",
})
_TOKEN = re.compile(r"[a-zA-Z0-9_]+")


def _tokens(text: str) -> list[str]:
    return [t.lower() for t in _TOKEN.findall(text) if t.lower() not in _STOP]


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


def _keyword_search(corpus: list[dict], query: str, limit: int = 10) -> list[dict]:
    """Naive keyword baseline: score each symbol by count of unique query
    tokens present in ``name + signature + docstring_head``. Ties broken
    by symbol name for reproducibility. This mirrors the signal an agent
    gets when it greps with natural-language terms before running
    anything semantic.
    """
    qtoks = set(_tokens(query))
    if not qtoks:
        return []
    scored: list[tuple[int, str, dict]] = []
    for sym in corpus:
        dtoks = set(sym["_tokens"])
        overlap = len(qtoks & dtoks)
        if overlap > 0:
            scored.append((overlap, sym["symbol_key"], sym))
    scored.sort(key=lambda t: (-t[0], t[1]))
    return [
        {"symbol": s["symbol_key"].split("::", 1)[1], "score": float(ov)}
        for ov, _, s in scored[:limit]
    ]


def _load_corpus_for_keyword() -> list[dict]:
    """Collect every symbol as (symbol_key, doc tokens) so the keyword
    baseline matches the embedding input verbatim — the same text Nomic
    sees, just tokenised. Uses the ``embed_doc`` field populated by
    ``collect_project_symbols`` so there is zero drift between the two
    paths.
    """
    from token_savior.memory.symbol_embeddings import collect_project_symbols
    return [
        {"symbol_key": s["symbol_key"], "_tokens": _tokens(s["embed_doc"])}
        for s in collect_project_symbols(SRC)
    ]


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

        # Build the keyword-side corpus from the same AST pass.
        keyword_corpus = _load_corpus_for_keyword()
        print(f"[bench] keyword corpus: {len(keyword_corpus)} symbols", flush=True)

        per_query_semantic: list[dict] = []
        per_query_keyword: list[dict] = []
        per_query_combined: list[dict] = []
        for q in queries:
            # Semantic path
            t = time.perf_counter()
            res = search_symbols_semantic(q["query"], SRC, limit=10)
            sem_ms = (time.perf_counter() - t) * 1000
            sem_names = [h["symbol"].rsplit(".", 1)[-1] for h in res["hits"]]
            sem = _metrics(sem_names, q["gt"])
            sem.update({
                "id": q["id"], "query": q["query"], "kind": q["kind"],
                "latency_ms": sem_ms,
                "low_confidence": bool(res.get("warning")),
                "top3": sem_names[:3],
            })
            per_query_semantic.append(sem)

            # Keyword baseline
            t = time.perf_counter()
            kw_hits = _keyword_search(keyword_corpus, q["query"], limit=10)
            kw_ms = (time.perf_counter() - t) * 1000
            kw_names = [h["symbol"].rsplit(".", 1)[-1] for h in kw_hits]
            kw = _metrics(kw_names, q["gt"])
            kw.update({
                "id": q["id"], "query": q["query"], "kind": q["kind"],
                "latency_ms": kw_ms,
                "low_confidence": False,
                "top3": kw_names[:3],
            })
            per_query_keyword.append(kw)

            # Combined per-query row for the side-by-side table.
            sem_top1_score = res["hits"][0]["score"] if res.get("hits") else 0.0
            sem_top2_score = res["hits"][1]["score"] if res.get("hits") and len(res["hits"]) > 1 else 0.0
            per_query_combined.append({
                "id": q["id"], "kind": q["kind"], "query": q["query"],
                "sem_rr": sem["rr"], "sem_r3": sem["recall_3"],
                "sem_top1": sem_names[0] if sem_names else "",
                "sem_top1_score": sem_top1_score,
                "sem_top2_score": sem_top2_score,
                "sem_ms": round(sem_ms, 1),
                "sem_low_conf": sem["low_confidence"],
                "kw_rr": kw["rr"], "kw_r3": kw["recall_3"],
                "kw_top1": kw_names[0] if kw_names else "",
                "kw_ms": round(kw_ms, 1),
            })

    return {
        "corpus_source": str(SRC),
        "corpus_symbols": reindex.get("total", 0),
        "indexed": reindex.get("indexed", 0),
        "skipped": reindex.get("skipped", 0),
        "index_seconds": round(index_s, 2),
        "num_queries": len(queries),
        "agg": {
            "keyword": _agg(per_query_keyword),
            "semantic": _agg(per_query_semantic),
        },
        "per_query": per_query_combined,
    }


def _report(result: dict) -> str:
    agg = result["agg"]
    kw = agg["keyword"]
    sem = agg["semantic"]
    lines = []
    lines.append("# Code retrieval bench")
    lines.append("")
    lines.append(f"- Corpus: {result['corpus_source']}")
    lines.append(f"- Symbols: {result['corpus_symbols']} ({result['indexed']} indexed)")
    lines.append(f"- Queries: {result['num_queries']} handcrafted with ground truth")
    lines.append(f"- Index time: {result['index_seconds']}s")
    lines.append("")
    lines.append("## Aggregate — keyword baseline vs semantic (Nomic)")
    lines.append("")
    lines.append("| Config | MRR@10 | Recall@3 | Recall@10 | P50 ms | P95 ms |")
    lines.append("|---|---|---|---|---|---|")
    lines.append(
        f"| keyword | {kw['mrr_10']} | {kw['recall_3']} | {kw['recall_10']} | "
        f"{kw['p50_ms']} | {kw['p95_ms']} |"
    )
    lines.append(
        f"| semantic | {sem['mrr_10']} | {sem['recall_3']} | {sem['recall_10']} | "
        f"{sem['p50_ms']} | {sem['p95_ms']} |"
    )
    lines.append("")
    base = kw["mrr_10"] or 1e-9
    lines.append(
        f"Semantic vs keyword (MRR@10): "
        f"{sem['mrr_10'] - kw['mrr_10']:+.4f} "
        f"({(sem['mrr_10'] / base - 1) * 100:+.1f}%). "
        f"Semantic low-confidence rate: {sem['low_confidence_rate']}."
    )
    lines.append("")
    lines.append("## Per-query")
    lines.append("")
    lines.append("| ID | Kind | KW RR | Sem RR | KW Top-1 | Sem Top-1 | Query |")
    lines.append("|---|---|---|---|---|---|---|")
    for r in result["per_query"]:
        flag = " ⚠️" if r.get("sem_low_conf") else ""
        lines.append(
            f"| {r['id']} | {r['kind']} | {r['kw_rr']:.3f} | {r['sem_rr']:.3f} | "
            f"`{r['kw_top1']}` | `{r['sem_top1']}`{flag} | {r['query']} |"
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
