"""Tool capture context-savings micro-benchmark.

Simulates a session of N verbose tool calls and measures how many bytes the
agent would have consumed *without* the sandbox versus *with* it (preview only).

The measurement is byte-for-byte conservative: it ignores the small JSON
overhead of the additionalContext note (~200 bytes) and the cost of a
follow-up capture_get when retrieval is actually needed.

Usage:
    python benchmarks/tool_capture_bench.py
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path
from statistics import mean, median

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from token_savior import db_core
from token_savior.memory import tool_capture


# Realistic tool output corpus -- sizes mirror the Context-mode README claims:
# Playwright snapshot 56 KB, GitHub issues 59 KB, access log 45 KB, verbose
# Bash builds, and lighter Read/Grep results in the 4-12 KB band.
SCENARIOS = [
    ("Playwright accessibility snapshot", 56 * 1024),
    ("gh issue list (20 issues)", 59 * 1024),
    ("nginx access.log tail", 45 * 1024),
    ("npm install verbose", 18 * 1024),
    ("pytest -v output", 12 * 1024),
    ("Bash find . -name *.py", 8 * 1024),
    ("WebFetch HTML page", 14 * 1024),
    ("grep -r foo .", 6 * 1024),
    ("Read large source file", 5 * 1024),
    ("Bash short stdout", 800),  # below threshold, not captured
]


def make_payload(name: str, size: int) -> str:
    """Generate a synthetic payload of approximately `size` bytes."""
    base = f"--- {name} synthetic payload ---\n"
    line = "x" * 80 + "\n"
    n = max(1, (size - len(base)) // len(line))
    return base + line * n


def run() -> None:
    db_path = Path("/tmp/ts-tool-capture-bench.sqlite")
    if db_path.exists():
        db_path.unlink()
    db_core._migrated_paths.clear()
    db_core.MEMORY_DB_PATH = db_path
    db_core.run_migrations(db_path)

    THRESHOLD = 4096  # default in the hook
    total_raw = 0
    total_with_capture = 0
    rows = []

    for name, size in SCENARIOS:
        payload = make_payload(name, size)
        raw_bytes = len(payload)
        if raw_bytes < THRESHOLD:
            # Not captured -- both modes pay full price.
            agent_bytes = raw_bytes
            preview_bytes = raw_bytes
            cap_id = None
        else:
            res = tool_capture.capture_put(tool_name="Bash", output=payload)
            cap_id = res["id"]
            preview_bytes = len(res["preview"])
            note_bytes = len(
                f"[token-savior:capture] Bash output {raw_bytes}B sandboxed to "
                f"ts://capture/{cap_id} ({res['lines']} lines). Use capture_search / "
                f"capture_get / capture_aggregate to retrieve this content later."
            )
            agent_bytes = preview_bytes + note_bytes
        total_raw += raw_bytes
        total_with_capture += agent_bytes
        rows.append({
            "scenario": name,
            "raw_bytes": raw_bytes,
            "agent_bytes": agent_bytes,
            "saving_pct": round(100 * (1 - agent_bytes / raw_bytes), 1) if raw_bytes else 0,
            "captured": cap_id is not None,
        })

    print(f"\n{'Scenario':<45}  {'Raw':>9}  {'With TS':>9}  {'Saved':>7}  Captured")
    print("-" * 90)
    for r in rows:
        captured = "yes" if r["captured"] else "no (under threshold)"
        print(
            f"{r['scenario']:<45}  {r['raw_bytes']:>9,}  "
            f"{r['agent_bytes']:>9,}  {r['saving_pct']:>6}%  {captured}"
        )
    print("-" * 90)
    saving_total_pct = round(100 * (1 - total_with_capture / total_raw), 1)
    print(
        f"{'TOTAL':<45}  {total_raw:>9,}  {total_with_capture:>9,}  "
        f"{saving_total_pct:>6}%"
    )

    # Search latency over a populated table
    t0 = time.perf_counter()
    hits = tool_capture.capture_search("Playwright")
    dt_search = (time.perf_counter() - t0) * 1000
    t0 = time.perf_counter()
    got = tool_capture.capture_get(hits[0]["id"], range_spec="head") if hits else None
    dt_get = (time.perf_counter() - t0) * 1000

    print()
    print(f"capture_search latency: {dt_search:.2f} ms ({len(hits)} hits)")
    print(f"capture_get(head) latency: {dt_get:.2f} ms")

    summary = {
        "scenarios": rows,
        "total_raw_bytes": total_raw,
        "total_agent_bytes": total_with_capture,
        "context_saving_pct": saving_total_pct,
        "search_ms": round(dt_search, 2),
        "get_ms": round(dt_get, 2),
    }
    out_path = Path(__file__).parent / "tool_capture_results.json"
    out_path.write_text(json.dumps(summary, indent=2))
    print(f"\nresults written to {out_path}")


if __name__ == "__main__":
    run()
