"""Economics — cost & model-routing self-management (unit É).

Closes the loop with where this whole session started (token limits). Encodes
Louis's routing policy as logic, reports token spend from the ledger, and flags
mechanisms the honest metric judges wasteful. Advice only — it recommends a
cheaper tier or a restructure; it never silently changes how work runs.

Routing policy (from the model-routing memory):
  batch / cron / bulk        → haiku   (cheapest tier)
  debug / archi / design     → opus    (most capable)
  dev when Token-Savior-fed  → sonnet  (mid-tier suffices)
  review / judge             → sonnet
  unknown                    → opus    (default when unsure)

Run:  python3 scripts/economics.py
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

_BATCH = {"cron", "batch", "bulk", "scheduled"}
_HARD = {"debug", "archi", "architecture", "design", "root-cause"}
_DEV = {"dev", "code", "edit", "refactor", "implement"}
_MID = {"review", "judge", "verify", "summarize"}


def recommend_model(kind: str, *, ts_fed: bool = False) -> dict:
    k = (kind or "").lower().strip()
    if k in _BATCH:
        return {"model": "haiku", "reason": "tâche batch/cron → tier le moins cher"}
    if k in _HARD:
        return {"model": "opus", "reason": "raisonnement/architecture → tier le plus capable"}
    if k in _DEV:
        if ts_fed:
            return {"model": "sonnet", "reason": "dev alimenté par Token Savior → mid-tier suffit"}
        return {"model": "opus", "reason": "dev sans contexte TS → ne pas sous-router à l'aveugle"}
    if k in _MID:
        return {"model": "sonnet", "reason": "revue/jugement → mid-tier"}
    return {"model": "opus", "reason": "type inconnu → défaut capable"}


def token_spend(events: list[dict]) -> dict:
    inj = sum(int(e.get("cost_tokens") or 0)
              for e in events if e.get("event_type") == "injection")
    return {"injection_tokens": inj, "events": len(events)}


def flag_waste(net_value: dict) -> list[str]:
    """Subjects the ledger's honest metric judges net-negative = wasteful."""
    return list(net_value.get("counterproductive", []))


def main() -> int:
    from token_savior.memory import ledger
    events = ledger.ledger_query(limit=1_000_000)
    nv = ledger.ledger_net_value()
    spend = token_spend(events)
    waste = flag_waste(nv)

    print("=== Économie (routage modèle + coût) ===\n")
    print("## Politique de routage (rappel actionnable)")
    for kind in ("cron", "debug", "dev", "review"):
        for tf in ((False,) if kind != "dev" else (True, False)):
            r = recommend_model(kind, ts_fed=tf)
            label = f"{kind}{' +TS' if (kind=='dev' and tf) else ''}"
            print(f"   {label:12} → {r['model']:7} ({r['reason']})")
    print("\n## Coût observé (ledger)")
    print(f"   injections : {spend['injection_tokens']} tokens estimés "
          f"sur {spend['events']} events")
    print("\n## Gaspillage signalé")
    print(f"   mécanismes net<0 : {waste or 'aucun'}")
    tot = nv.get("totals", {})
    print(f"   coût-tokens total (par sujet) : {tot.get('token_cost', 0)}")
    print("\n→ Conseil uniquement. Délègue le batch à Haiku, garde Opus pour l'archi/debug.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
