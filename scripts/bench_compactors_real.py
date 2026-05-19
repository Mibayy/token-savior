"""Retroactive compactor bench against real Claude Code transcripts.

Walks ~/.claude/projects/-root/*.jsonl, pairs each Bash tool_use with its
tool_result, and replays the v4.2.0 compactor registry on the captured
output. Reports per-compactor hit count, mean savings_pct, cumulative
bytes/tokens saved.

Usage:
    python3 scripts/bench_compactors_real.py [--days 7] [--root PATH]
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path


def _pair_bash_calls(jsonl_path: Path, cutoff: datetime | None):
    """Yield (command, output_text) per Bash tool_use+tool_result pair."""
    pending: dict[str, tuple[str, datetime | None]] = {}
    try:
        with jsonl_path.open("r", encoding="utf-8", errors="replace") as f:
            for line in f:
                if '"tool_use"' not in line and '"tool_result"' not in line:
                    continue
                try:
                    ev = json.loads(line)
                except Exception:
                    continue
                ts_raw = ev.get("timestamp")
                ts = None
                if ts_raw:
                    try:
                        ts = datetime.fromisoformat(ts_raw.replace("Z", "+00:00"))
                    except Exception:
                        ts = None
                if cutoff and ts and ts < cutoff:
                    continue
                msg = ev.get("message") or {}
                content = msg.get("content")
                if not isinstance(content, list):
                    continue
                for block in content:
                    btype = block.get("type")
                    if btype == "tool_use" and block.get("name") == "Bash":
                        cmd = (block.get("input") or {}).get("command", "")
                        if cmd:
                            pending[block.get("id", "")] = (cmd, ts)
                    elif btype == "tool_result":
                        tid = block.get("tool_use_id")
                        if tid not in pending:
                            continue
                        cmd, _ = pending.pop(tid)
                        out = block.get("content")
                        if isinstance(out, list):
                            text = "".join(
                                c.get("text", "") for c in out if isinstance(c, dict)
                            )
                        elif isinstance(out, str):
                            text = out
                        else:
                            text = ""
                        if text:
                            yield cmd, text
    except OSError:
        return


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=7)
    ap.add_argument("--root", type=Path,
                    default=Path.home() / ".claude" / "projects" / "-root")
    ap.add_argument("--json", action="store_true", help="machine output")
    args = ap.parse_args()

    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
    from token_savior.compactors import registry, compact

    cutoff = datetime.now(timezone.utc) - timedelta(days=args.days)

    total_outputs = 0
    matched = 0
    total_orig = 0
    total_compact = 0
    per_compactor: dict[str, dict] = defaultdict(
        lambda: {"hits": 0, "orig": 0, "compact": 0, "examples": []}
    )

    jsonls = sorted(args.root.glob("*.jsonl"))
    if not jsonls:
        print(f"no transcripts under {args.root}", file=sys.stderr)
        return 1

    for path in jsonls:
        mtime = datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)
        if mtime < cutoff:
            continue
        for cmd, output in _pair_bash_calls(path, cutoff):
            total_outputs += 1
            orig_bytes = len(output.encode("utf-8"))
            total_orig += orig_bytes
            result = compact(cmd, output)
            if result is None:
                continue
            matched += 1
            total_compact += result.compact_bytes
            for c in registry:
                if c.matches(cmd):
                    cname = type(c).__name__
                    rec = per_compactor[cname]
                    rec["hits"] += 1
                    rec["orig"] += result.original_bytes
                    rec["compact"] += result.compact_bytes
                    if len(rec["examples"]) < 3:
                        rec["examples"].append(cmd[:80])
                    break

    saved = total_orig - (total_compact + (total_orig - sum(
        v["orig"] for v in per_compactor.values()
    )))
    saved_bytes = sum(v["orig"] - v["compact"] for v in per_compactor.values())
    saved_tokens_est = saved_bytes // 4

    if args.json:
        out = {
            "window_days": args.days,
            "sessions_scanned": len([p for p in jsonls if datetime.fromtimestamp(
                p.stat().st_mtime, tz=timezone.utc) >= cutoff]),
            "bash_outputs_total": total_outputs,
            "bash_outputs_matched": matched,
            "match_rate_pct": round(100 * matched / max(1, total_outputs), 1),
            "orig_bytes_total": total_orig,
            "bytes_saved": saved_bytes,
            "tokens_saved_est": saved_tokens_est,
            "overall_savings_pct_on_matched": round(
                100 * saved_bytes / max(1, sum(v["orig"] for v in per_compactor.values())), 1
            ),
            "per_compactor": {
                name: {
                    "hits": v["hits"],
                    "bytes_saved": v["orig"] - v["compact"],
                    "savings_pct": round(100 * (v["orig"] - v["compact"]) / max(1, v["orig"]), 1),
                    "examples": v["examples"],
                }
                for name, v in sorted(per_compactor.items(), key=lambda kv: -kv[1]["hits"])
            },
        }
        print(json.dumps(out, indent=2))
        return 0

    print(f"=== Compactor bench: last {args.days} days under {args.root} ===")
    print(f"Bash outputs scanned     : {total_outputs}")
    print(f"Matched a compactor      : {matched} ({100*matched/max(1,total_outputs):.1f}%)")
    print(f"Total Bash bytes scanned : {total_orig:,}")
    print(f"Bytes saved (matched)    : {saved_bytes:,}")
    print(f"Token saved (est, /4)    : ~{saved_tokens_est:,}")
    if sum(v["orig"] for v in per_compactor.values()) > 0:
        pct = 100 * saved_bytes / sum(v["orig"] for v in per_compactor.values())
        print(f"Overall savings on matched: {pct:.1f}%")
    print()
    print(f"{'Compactor':35s} {'Hits':>6s} {'Saved':>12s} {'Savings':>9s}")
    print("-" * 65)
    for name, v in sorted(per_compactor.items(), key=lambda kv: -kv[1]["hits"]):
        pct = 100 * (v["orig"] - v["compact"]) / max(1, v["orig"])
        print(f"{name:35s} {v['hits']:>6d} {v['orig']-v['compact']:>12,d} {pct:>8.1f}%")
    return 0


if __name__ == "__main__":
    sys.exit(main())
