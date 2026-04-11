"""Token Savior benchmarks -- index & query performance on real-world repos.

Usage:
    python benchmarks/run_benchmarks.py                 # both repos
    python benchmarks/run_benchmarks.py --repos fastapi  # just FastAPI
    python benchmarks/run_benchmarks.py --skip-clone     # skip git clone step
"""

from __future__ import annotations

import argparse
import json
import os
import random
import subprocess
import sys
import time
import tracemalloc
from pathlib import Path

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

REPOS: dict[str, str] = {
    "fastapi": "https://github.com/tiangolo/fastapi.git",
    "cpython": "https://github.com/python/cpython.git",
}

CLONE_DIR = Path("/tmp/token-savior-bench")
RESULTS_DIR = Path(__file__).resolve().parent
RESULTS_JSON = RESULTS_DIR / "results.json"
REPORT_MD = RESULTS_DIR / "report.md"

RANDOM_SEED = 42
NUM_QUERY_SAMPLES = 10


def log(msg: str) -> None:
    print(f"[bench] {msg}", file=sys.stderr, flush=True)


# ---------------------------------------------------------------------------
# Clone helpers
# ---------------------------------------------------------------------------


def clone_repo(name: str, url: str, skip_clone: bool) -> Path:
    """Shallow-clone a repo into CLONE_DIR/<name>. Returns the local path."""
    dest = CLONE_DIR / name
    if dest.exists():
        if skip_clone:
            log(f"{name}: using existing clone at {dest}")
            return dest
        log(f"{name}: directory exists, reusing")
        return dest

    CLONE_DIR.mkdir(parents=True, exist_ok=True)
    log(f"{name}: cloning {url} (shallow, depth=1) ...")
    try:
        subprocess.run(
            ["git", "clone", "--depth=1", "--single-branch", url, str(dest)],
            check=True,
            capture_output=True,
            text=True,
        )
    except subprocess.CalledProcessError as exc:
        log(f"{name}: clone FAILED -- {exc.stderr.strip()}")
        raise
    log(f"{name}: clone OK")
    return dest


# ---------------------------------------------------------------------------
# Naive baseline (sum of source file sizes)
# ---------------------------------------------------------------------------


def naive_source_size(root: str) -> int:
    """Sum of bytes of all .py files under *root*."""
    total = 0
    for dirpath, _dirs, files in os.walk(root):
        for fname in files:
            if fname.endswith(".py"):
                try:
                    total += os.path.getsize(os.path.join(dirpath, fname))
                except OSError:
                    pass
    return total


# ---------------------------------------------------------------------------
# Benchmark a single repo
# ---------------------------------------------------------------------------


def benchmark_repo(name: str, root: Path) -> dict:
    from token_savior.cache_ops import CacheManager
    from token_savior.project_indexer import ProjectIndexer
    from token_savior.query_api import create_project_query_functions

    root_str = str(root)
    result: dict = {"repo": name, "root": root_str}

    # ---- cold index ----
    log(f"{name}: cold index ...")
    tracemalloc.start()
    t0 = time.perf_counter()
    indexer = ProjectIndexer(root_str)
    index = indexer.index()
    cold_time = time.perf_counter() - t0
    peak_mem = tracemalloc.get_traced_memory()[1]
    tracemalloc.stop()

    result["cold_index_seconds"] = round(cold_time, 3)
    result["cold_index_peak_memory_bytes"] = peak_mem
    result["total_files"] = index.total_files
    result["total_lines"] = index.total_lines
    result["total_functions"] = index.total_functions
    result["total_classes"] = index.total_classes
    result["symbol_table_size"] = len(index.symbol_table)
    log(
        f"{name}: cold index done in {cold_time:.2f}s, "
        f"peak mem {peak_mem / 1_048_576:.1f} MiB, "
        f"{index.total_files} files, {index.total_lines} lines"
    )

    # ---- warm index ----
    log(f"{name}: warm index ...")
    t0 = time.perf_counter()
    indexer2 = ProjectIndexer(root_str)
    indexer2.index()
    warm_time = time.perf_counter() - t0
    result["warm_index_seconds"] = round(warm_time, 3)
    log(f"{name}: warm index done in {warm_time:.2f}s")

    # ---- query benchmarks ----
    log(f"{name}: query benchmarks ({NUM_QUERY_SAMPLES} symbols) ...")
    queries = create_project_query_functions(index)
    find_symbol = queries["find_symbol"]
    get_function_source = queries["get_function_source"]
    get_change_impact = queries["get_change_impact"]

    rng = random.Random(RANDOM_SEED)
    symbols = list(index.symbol_table.keys())
    sample = rng.sample(symbols, min(NUM_QUERY_SAMPLES, len(symbols)))

    # find_symbol timings
    find_times: list[float] = []
    for sym in sample:
        t0 = time.perf_counter()
        find_symbol(sym)
        find_times.append(time.perf_counter() - t0)
    result["find_symbol_avg_ms"] = round(sum(find_times) / len(find_times) * 1000, 3)

    # get_function_source timings
    source_times: list[float] = []
    for sym in sample:
        t0 = time.perf_counter()
        get_function_source(sym)
        source_times.append(time.perf_counter() - t0)
    result["get_function_source_avg_ms"] = round(
        sum(source_times) / len(source_times) * 1000, 3
    )

    # get_change_impact timings (only for symbols in reverse dep graph)
    impact_times: list[float] = []
    for sym in sample:
        if sym in index.reverse_dependency_graph:
            t0 = time.perf_counter()
            get_change_impact(sym)
            impact_times.append(time.perf_counter() - t0)
    if impact_times:
        result["get_change_impact_avg_ms"] = round(
            sum(impact_times) / len(impact_times) * 1000, 3
        )
    else:
        result["get_change_impact_avg_ms"] = None

    log(
        f"{name}: queries -- find_symbol {result['find_symbol_avg_ms']}ms, "
        f"get_function_source {result['get_function_source_avg_ms']}ms, "
        f"get_change_impact {result['get_change_impact_avg_ms']}ms"
    )

    # ---- cache size ----
    log(f"{name}: measuring cache size ...")
    cache = CacheManager(root_path=root_str, cache_version=1)
    cache.save(index)
    cache_path = cache.path()
    cache_size = os.path.getsize(cache_path) if os.path.exists(cache_path) else 0
    result["cache_size_bytes"] = cache_size
    # Clean up cache file from the cloned repo
    try:
        os.remove(cache_path)
    except OSError:
        pass
    log(f"{name}: cache size {cache_size / 1_048_576:.1f} MiB")

    # ---- naive baseline ----
    source_size = naive_source_size(root_str)
    result["naive_source_size_bytes"] = source_size
    log(f"{name}: naive source size {source_size / 1_048_576:.1f} MiB")

    return result


# ---------------------------------------------------------------------------
# Report generation
# ---------------------------------------------------------------------------


def _fmt_bytes(b: int | None) -> str:
    if b is None:
        return "N/A"
    if b >= 1_048_576:
        return f"{b / 1_048_576:.1f} MiB"
    if b >= 1024:
        return f"{b / 1024:.1f} KiB"
    return f"{b} B"


def _fmt_ms(v: float | None) -> str:
    if v is None:
        return "N/A"
    return f"{v:.3f} ms"


def generate_report(results: list[dict]) -> str:
    lines = [
        "# Token Savior Benchmarks",
        "",
        f"Generated: {time.strftime('%Y-%m-%d %H:%M:%S UTC', time.gmtime())}",
        "",
    ]

    for r in results:
        lines.extend([
            f"## {r['repo']}",
            "",
            "| Metric | Value |",
            "|--------|-------|",
            f"| Files | {r['total_files']:,} |",
            f"| Lines | {r['total_lines']:,} |",
            f"| Functions | {r['total_functions']:,} |",
            f"| Classes | {r['total_classes']:,} |",
            f"| Symbol table entries | {r['symbol_table_size']:,} |",
            f"| Cold index time | {r['cold_index_seconds']:.3f}s |",
            f"| Warm index time | {r['warm_index_seconds']:.3f}s |",
            f"| Peak memory (cold) | {_fmt_bytes(r['cold_index_peak_memory_bytes'])} |",
            f"| Cache size | {_fmt_bytes(r['cache_size_bytes'])} |",
            f"| Naive source size (.py) | {_fmt_bytes(r['naive_source_size_bytes'])} |",
            f"| find_symbol avg | {_fmt_ms(r['find_symbol_avg_ms'])} |",
            f"| get_function_source avg | {_fmt_ms(r['get_function_source_avg_ms'])} |",
            f"| get_change_impact avg | {_fmt_ms(r.get('get_change_impact_avg_ms'))} |",
            "",
        ])

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Run Token Savior benchmarks on real-world repos"
    )
    parser.add_argument(
        "--repos",
        nargs="+",
        choices=list(REPOS.keys()),
        default=list(REPOS.keys()),
        help="Which repos to benchmark (default: all)",
    )
    parser.add_argument(
        "--skip-clone",
        action="store_true",
        help="Skip git clone if directory already exists",
    )
    args = parser.parse_args()

    all_results: list[dict] = []

    for repo_name in args.repos:
        url = REPOS[repo_name]
        try:
            repo_path = clone_repo(repo_name, url, skip_clone=args.skip_clone)
        except (subprocess.CalledProcessError, OSError) as exc:
            log(f"SKIP {repo_name}: could not clone -- {exc}")
            continue
        result = benchmark_repo(repo_name, repo_path)
        all_results.append(result)

    if not all_results:
        log("No repos benchmarked, exiting.")
        sys.exit(1)

    # Write results
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    with open(RESULTS_JSON, "w") as f:
        json.dump(all_results, f, indent=2)
    log(f"Results written to {RESULTS_JSON}")

    report = generate_report(all_results)
    with open(REPORT_MD, "w") as f:
        f.write(report)
    log(f"Report written to {REPORT_MD}")

    # Also print report to stdout
    print(report)


if __name__ == "__main__":
    main()
