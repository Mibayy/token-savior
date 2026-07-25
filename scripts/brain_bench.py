"""Brain bench — the tsbench of the memory brain.

Reads the ledger and reports whether the brain is actually helping: the miss
rate over time (should trend down), the miss-class mix (invisible → retrieval
gap, ignored → activation gap), rule firings, injection cost, and net value.
Pure aggregators are unit-tested; the CLI pulls live ledger events.

Run:  python3 scripts/brain_bench.py [--days N]
"""
from __future__ import annotations

import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))


def miss_class_breakdown(events: list[dict]) -> dict[str, int]:
    out: dict[str, int] = defaultdict(int)
    for e in events:
        if e.get("event_type") == "miss":
            out[(e.get("meta") or {}).get("miss_class", "unknown")] += 1
    return dict(out)


def rule_firings(events: list[dict]) -> dict[str, int]:
    out: dict[str, int] = defaultdict(int)
    for e in events:
        if e.get("event_type") == "hard_block":
            out[e.get("subject") or "(none)"] += 1
    return dict(out)


def injection_stats(events: list[dict]) -> dict[str, int]:
    n = cost = 0
    for e in events:
        if e.get("event_type") == "injection":
            n += 1
            cost += int(e.get("cost_tokens") or 0)
    return {"count": n, "total_token_cost": cost}


def misses_per_day(events: list[dict], day_of) -> dict[str, int]:
    """`day_of(epoch) -> label`; injected so the aggregation is testable."""
    out: dict[str, int] = defaultdict(int)
    for e in events:
        if e.get("event_type") == "miss":
            out[day_of(e.get("ts_epoch") or 0)] += 1
    return dict(out)


def health_summary(events: list[dict]) -> dict:
    return {
        "total_events": len(events),
        "miss_classes": miss_class_breakdown(events),
        "rule_firings": rule_firings(events),
        "injections": injection_stats(events),
    }


def _day(epoch: int) -> str:
    return datetime.fromtimestamp(epoch or 0, tz=timezone.utc).strftime("%Y-%m-%d")


def main() -> int:
    from token_savior.memory import ledger
    args = sys.argv[1:]
    since = None
    if "--days" in args:
        n = int(args[args.index("--days") + 1])
        since = int(datetime.now(tz=timezone.utc).timestamp()) - n * 86400

    events = ledger.ledger_query(since_epoch=since, limit=1_000_000)
    h = health_summary(events)
    nv = ledger.ledger_net_value(since_epoch=since)

    print("=== Brain bench (santé du cerveau) ===")
    print(f"Events ledger analysés : {h['total_events']}\n")

    print("## Ratés par classe")
    if h["miss_classes"]:
        tot = sum(h["miss_classes"].values())
        for cls, c in sorted(h["miss_classes"].items(), key=lambda kv: -kv[1]):
            print(f"   {c:4}  {cls:12} ({100*c/tot:.0f}%)")
        inv = h["miss_classes"].get("invisible", 0)
        ign = h["miss_classes"].get("ignored", 0)
        if inv or ign:
            lead = "retrieval" if inv >= ign else "rules"
            print(f"   → panne dominante : {'invisible' if inv>=ign else 'ignored'} "
                  f"→ prochaine unité utile = {lead}")
    else:
        print("   (aucun raté enregistré encore)")

    print("\n## Ratés par jour")
    per = misses_per_day(events, _day)
    for d, c in sorted(per.items()):
        print(f"   {d}  {'█'*min(c,40)} {c}")
    if not per:
        print("   (pas encore de données)")

    print("\n## Déclenchements de règles")
    for rid, c in sorted(h["rule_firings"].items(), key=lambda kv: -kv[1]):
        print(f"   {c:4}  {rid}")
    if not h["rule_firings"]:
        print("   (aucune règle déclenchée — normal, gardes dormants)")

    print("\n## Injections mémoire")
    print(f"   {h['injections']['count']} injections, "
          f"{h['injections']['total_token_cost']} tokens estimés")

    print("\n## Valeur nette (par sujet)")
    cp = nv.get("counterproductive", [])
    print(f"   contre-productifs : {cp or 'aucun'}")
    print(f"   totaux : {nv.get('totals')}")
    print("\nRelance régulièrement : si les ratés/jour baissent, le cerveau progresse.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
