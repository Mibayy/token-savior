"""Multi-model comparison bench.

Runs the memory retrieval bench across N FastEmbed built-in models,
measuring MRR/Recall and peak RSS per model via isolated subprocesses.
Outputs a comparison markdown table.

    python tests/benchmarks/memory_retrieval/compare_models.py
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
RESULTS_DIR = HERE / "results"

CANDIDATES = [
    "nomic-ai/nomic-embed-text-v1.5-Q",
    "BAAI/bge-small-en-v1.5",
    "BAAI/bge-base-en-v1.5",
    "snowflake/snowflake-arctic-embed-m-long",
    "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2",
    "mixedbread-ai/mxbai-embed-large-v1",
    "sentence-transformers/paraphrase-multilingual-mpnet-base-v2",
    "snowflake/snowflake-arctic-embed-l",
    "intfloat/multilingual-e5-large",
]


def run_one(model_name: str, timeout_s: int = 900) -> dict:
    env = dict(os.environ)
    env["TS_EMBED_MODEL"] = model_name
    cmd = [
        "/root/.local/token-savior-venv/bin/python",
        str(HERE / "run_bench.py"),
        "--model", model_name,
        "--quiet-output",
    ]
    t0 = time.perf_counter()
    try:
        proc = subprocess.run(
            cmd, env=env, capture_output=True, text=True,
            timeout=timeout_s,
        )
    except subprocess.TimeoutExpired:
        return {"model": model_name, "status": "timeout", "elapsed_s": timeout_s}
    elapsed = time.perf_counter() - t0
    if proc.returncode != 0:
        return {
            "model": model_name, "status": "error",
            "elapsed_s": round(elapsed, 1),
            "stderr": proc.stderr[-2000:],
        }
    try:
        payload = json.loads(proc.stdout.strip().splitlines()[-1])
    except Exception as exc:
        return {
            "model": model_name, "status": "parse_error",
            "elapsed_s": round(elapsed, 1),
            "raw_tail": proc.stdout[-1500:], "exc": str(exc),
        }
    payload["status"] = "ok"
    payload["elapsed_s"] = round(elapsed, 1)
    return payload


def _rank(results: list[dict]) -> list[dict]:
    ok = [r for r in results if r.get("status") == "ok"]
    ok.sort(key=lambda r: r.get("mrr_10", 0), reverse=True)
    return ok


def report(results: list[dict]) -> str:
    lines = []
    lines.append("# Multi-model memory retrieval comparison")
    lines.append("")
    lines.append("| Rank | Model | MRR@10 | R@3 | R@10 | P50 ms | P95 ms | Peak RSS MB | Total run s |")
    lines.append("|---|---|---|---|---|---|---|---|---|")
    for i, r in enumerate(_rank(results), 1):
        short = r["model"].split("/")[-1][:38]
        lines.append(
            f"| {i} | {short} | {r.get('mrr_10', 0):.4f} | {r.get('recall_3', 0):.4f} | "
            f"{r.get('recall_10', 0):.4f} | {r.get('p50_ms', 0):.1f} | "
            f"{r.get('p95_ms', 0):.1f} | {r.get('peak_rss_mb', 0):.0f} | "
            f"{r.get('elapsed_s', 0):.0f} |"
        )
    failures = [r for r in results if r.get("status") != "ok"]
    if failures:
        lines.append("")
        lines.append("## Failures / timeouts")
        for f in failures:
            lines.append(f"- `{f['model']}` → {f['status']}")
            if "stderr" in f:
                tail = f["stderr"].strip().splitlines()[-3:]
                for t in tail:
                    lines.append(f"    {t}")
    return "\n".join(lines)


def main() -> None:
    print(f"[compare] running {len(CANDIDATES)} models sequentially")
    results = []
    for i, model in enumerate(CANDIDATES, 1):
        print(f"[compare] [{i}/{len(CANDIDATES)}] {model}")
        t0 = time.perf_counter()
        r = run_one(model)
        results.append(r)
        print(f"    → status={r['status']} mrr={r.get('mrr_10', 'n/a')} "
              f"rss={r.get('peak_rss_mb', 'n/a')} in {time.perf_counter()-t0:.0f}s")

    RESULTS_DIR.mkdir(exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    out_md = RESULTS_DIR / f"compare-{stamp}.md"
    out_json = RESULTS_DIR / f"compare-{stamp}.json"
    md = report(results)
    out_md.write_text(md, encoding="utf-8")
    out_json.write_text(json.dumps(results, indent=2), encoding="utf-8")
    print()
    print(md)
    print()
    print(f"[compare] wrote {out_md}")
    print(f"[compare] wrote {out_json}")


if __name__ == "__main__":
    sys.path.insert(0, "/root/token-savior/src")
    main()
