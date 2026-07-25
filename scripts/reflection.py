"""Reflection — close the loop (unit A).

Reads the ledger and turns observations into ACTION: it proposes rules from
recurring activation-misses, flags mechanisms the ledger judges
counterproductive, and points at wasteful retrieval. Bounded autonomy: it only
PROPOSES here (Louis stays on the loop); high-risk changes are never
auto-applied. An adversarial gate (min_count) keeps it from acting on thin
signal — the same discipline that stopped us writing noise all session.

Effect is latent until the ledger accumulates data; the mechanism is live now.

Run:  python3 scripts/reflection.py [--min-count N]
"""
from __future__ import annotations

import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))


def analyze(events: list[dict], net_value: dict, *, min_count: int = 3) -> list[dict]:
    """Turn ledger evidence into proposals. Pure — no I/O, no writes."""
    proposals: list[dict] = []

    # 1. Recurring ACTIVATION misses (ignored: the memory was surfaced but not
    #    acted on) → the case a hard rule is meant for.
    ignored: dict[object, int] = defaultdict(int)
    for e in events:
        meta = e.get("meta") or {}
        if e.get("event_type") == "miss" and meta.get("miss_class") == "ignored":
            obs = meta.get("expected_obs")
            if obs is not None:
                ignored[obs] += 1
    for obs, c in sorted(ignored.items(), key=lambda kv: -kv[1]):
        if c >= min_count:  # adversarial gate: don't propose on thin evidence
            proposals.append({
                "kind": "promote_to_rule", "subject": f"obs:{obs}", "count": c,
                "evidence": f"mémoire #{obs} présente mais ignorée {c}× → candidate règle dure",
                "confidence": round(min(1.0, c / (min_count * 2)), 2),
                "risk": "high", "auto_applicable": False,
            })

    # 2. Mechanisms the ledger's honest metric flags as net-negative.
    for subj in net_value.get("counterproductive", []):
        proposals.append({
            "kind": "review_counterproductive", "subject": subj, "count": 0,
            "evidence": f"{subj} : valeur nette < 0 → à resserrer ou désactiver",
            "confidence": 0.7, "risk": "medium", "auto_applicable": False,
        })

    return proposals


def main() -> int:
    from token_savior.memory import ledger
    args = sys.argv[1:]
    min_count = max(1, int(args[args.index("--min-count") + 1])) if "--min-count" in args else 3

    events = ledger.ledger_query(limit=1_000_000)
    nv = ledger.ledger_net_value()
    props = analyze(events, nv, min_count=min_count)

    print("=== Reflection (le cerveau agit — propositions) ===")
    print(f"Events analysés : {len(events)} | seuil adversarial : {min_count}\n")
    if not props:
        print("Aucune proposition : soit tout va bien, soit pas encore assez de signal.")
        print("(Normal tant que le ledger est jeune — le mécanisme est actif et attend la donnée.)")
        return 0
    for p in props:
        auto = "AUTO" if p["auto_applicable"] else "PROPOSE (ton veto)"
        print(f"[{p['risk'].upper():6}] {p['kind']} — {p['subject']}  ({auto})")
        print(f"         {p['evidence']}  · confiance {p['confidence']}")
    print("\n→ Propositions uniquement. Rien n'est appliqué sans toi (autonomie bornée).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
