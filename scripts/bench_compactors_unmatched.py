"""Inspect the 88% of Bash calls that don't match any compactor.

Helps decide where to invest next: more compactors, or accept the limit.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=7)
    args = ap.parse_args()

    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
    from token_savior.compactors import compact

    root = Path.home() / ".claude" / "projects" / "-root"
    cutoff = datetime.now(timezone.utc) - timedelta(days=args.days)

    verbs: Counter[str] = Counter()
    total_unmatched_bytes = 0
    big_unmatched: list[tuple[int, str]] = []

    for path in sorted(root.glob("*.jsonl")):
        mtime = datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)
        if mtime < cutoff:
            continue
        pending = {}
        with path.open("r", encoding="utf-8", errors="replace") as f:
            for line in f:
                if '"tool_use"' not in line and '"tool_result"' not in line:
                    continue
                try:
                    ev = json.loads(line)
                except Exception:
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
                            pending[block.get("id", "")] = cmd
                    elif btype == "tool_result":
                        tid = block.get("tool_use_id")
                        if tid not in pending:
                            continue
                        cmd = pending.pop(tid)
                        out = block.get("content")
                        if isinstance(out, list):
                            text = "".join(
                                c.get("text", "") for c in out if isinstance(c, dict)
                            )
                        elif isinstance(out, str):
                            text = out
                        else:
                            text = ""
                        if not text:
                            continue
                        if compact(cmd, text) is None:
                            verb = re.split(r"[\s|;&]", cmd.strip(), 1)[0]
                            verb = verb.replace("/usr/bin/", "").replace("/bin/", "")
                            verbs[verb] += 1
                            b = len(text.encode("utf-8"))
                            total_unmatched_bytes += b
                            if b >= 500:
                                big_unmatched.append((b, cmd[:100]))

    print(f"=== Unmatched Bash calls, last {args.days} days ===")
    print(f"Total unmatched bytes : {total_unmatched_bytes:,}")
    print(f"Distinct verbs        : {len(verbs)}")
    print()
    print("Top 20 unmatched verbs:")
    for verb, n in verbs.most_common(20):
        print(f"  {n:>4d}  {verb}")
    print()
    big_unmatched.sort(reverse=True)
    print(f"Top 10 biggest unmatched outputs (>=500B):")
    for b, cmd in big_unmatched[:10]:
        print(f"  {b:>7,d}B  {cmd}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
