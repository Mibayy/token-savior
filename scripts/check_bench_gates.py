"""Quality gate for retrieval benches.

Reads the JSON result of a retrieval bench and exits non-zero when a
tracked metric regresses beyond the configured tolerance. Thresholds
are lenient enough to absorb Nomic run-to-run variance (~1-2pp)
without letting a real regression slip through.

Usage::

    python scripts/check_bench_gates.py code tests/benchmarks/code_retrieval/results/LATEST.json
    python scripts/check_bench_gates.py library tests/benchmarks/library_retrieval/results/LATEST.json

Each gate prints its result on one line so CI logs stay scannable.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path


# Thresholds calibrated on the 2026-04-23 baseline (commit bc5fad6).
# Observed numbers: code semantic MRR 0.71 / R@3 0.77, library cold MRR
# 0.84 / R@10 1.00 / warm P95 236 ms. Margins below give 2-3x the
# intrinsic noise band.
GATES = {
    "code": [
        # (path in result, label, comparator, threshold)
        (("agg", "semantic", "mrr_10"),       "code.semantic.mrr_10",     ">=", 0.65),
        (("agg", "semantic", "recall_3"),     "code.semantic.recall_3",   ">=", 0.70),
        (("agg", "semantic", "recall_10"),    "code.semantic.recall_10",  ">=", 0.80),
        (("agg", "semantic", "low_confidence_rate"), "code.low_conf_rate", "==", 0.0),
    ],
    "library": [
        (("cold", "agg", "mrr_10"),           "lib.cold.mrr_10",          ">=", 0.80),
        (("cold", "agg", "recall_3"),         "lib.cold.recall_3",        ">=", 0.90),
        (("cold", "agg", "recall_10"),        "lib.cold.recall_10",       ">=", 0.95),
        (("warm", "agg", "p95_ms"),           "lib.warm.p95_ms",          "<=", 500.0),
        (("cold", "agg", "low_confidence_rate"), "lib.cold.low_conf_rate", "==", 0.0),
    ],
}


def _dig(d: dict, path: tuple) -> object:
    for k in path:
        d = d[k]
    return d


def _check(value: float, op: str, threshold: float) -> bool:
    if op == ">=":
        return value >= threshold
    if op == "<=":
        return value <= threshold
    if op == "==":
        return value == threshold
    raise ValueError(f"unknown op {op}")


def main(argv: list[str]) -> int:
    if len(argv) != 3:
        print("usage: check_bench_gates.py {code|library} result.json", file=sys.stderr)
        return 2
    suite, path = argv[1], argv[2]
    if suite not in GATES:
        print(f"unknown suite: {suite}", file=sys.stderr)
        return 2

    data = json.loads(Path(path).read_text())
    failures = 0
    for keys, label, op, threshold in GATES[suite]:
        try:
            value = float(_dig(data, keys))
        except (KeyError, TypeError, ValueError) as e:
            print(f"FAIL {label}: missing or non-numeric ({e})")
            failures += 1
            continue
        ok = _check(value, op, threshold)
        tag = "PASS" if ok else "FAIL"
        print(f"{tag} {label}: {value} {op} {threshold}")
        if not ok:
            failures += 1
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
