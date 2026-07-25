"""Brain cycle — bounded self-improvement loop (unit D).

Runs the brain's self-review on a cadence: pulls the ledger, runs reflection
(unit A), passes every proposal through an ADVERSARIAL SKEPTIC that tries to
refute it, and appends a metacognitive record to a journal. That journal is the
ONLY thing it writes, and it is append-only — the loop never modifies
enforcement rules on its own. High-risk changes always route to a human
(bounded autonomy: Louis stays on the loop).

Run:  python3 scripts/brain_cycle.py            # run a cycle, append journal
      python3 scripts/brain_cycle.py --dry-run  # print, write nothing
"""
from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT / "src"))
sys.path.insert(0, str(_ROOT))  # make `scripts.*` importable when run directly

JOURNAL = Path.home() / ".local" / "state" / "token-savior" / "brain-journal.jsonl"


def skeptical_review(proposals: list[dict]) -> list[dict]:
    """Try to REFUTE each proposal before trusting it. A high-risk change never
    passes automatically; a thin one is refuted outright."""
    reviewed: list[dict] = []
    for p in proposals:
        conf = p.get("confidence", 0.0)
        if p.get("kind") == "promote_to_rule" and conf < 0.75:
            verdict = "insufficient_evidence"      # skeptic refutes thin signal
        elif p.get("risk") == "high":
            verdict = "needs_human"                # bounded autonomy
        elif conf >= 0.6:
            verdict = "worth_review"
        else:
            verdict = "insufficient_evidence"
        reviewed.append({**p, "skeptic_verdict": verdict})
    return reviewed


def build_cycle_record(ts: int, health: dict, reviewed: list[dict]) -> dict:
    return {
        "ts": ts,
        "total_events": health.get("total_events", 0),
        "miss_classes": health.get("miss_classes", {}),
        "proposals": reviewed,
        "actionable": sum(1 for r in reviewed if r["skeptic_verdict"] == "worth_review"),
        "needs_human": sum(1 for r in reviewed if r["skeptic_verdict"] == "needs_human"),
        "refuted": sum(1 for r in reviewed if r["skeptic_verdict"] == "insufficient_evidence"),
    }


def main() -> int:
    from token_savior.memory import ledger
    from scripts.reflection import analyze  # sibling import works via repo-root on path
    from scripts.brain_bench import health_summary

    dry = "--dry-run" in sys.argv
    # Fail-soft: a locked/corrupt ledger or a bad row must degrade to a no-op
    # cycle, never crash the unattended nightly unit.
    try:
        events = ledger.ledger_query(limit=1_000_000)
        nv = ledger.ledger_net_value()
        health = health_summary(events)
        reviewed = skeptical_review(analyze(events, nv))
    except Exception as exc:
        print(f"(cycle read/analyze dégradé en no-op : {exc})", file=sys.stderr)
        events, reviewed = [], []
        health = {"total_events": 0, "miss_classes": {}}
    ts = int(datetime.now(tz=timezone.utc).timestamp())
    rec = build_cycle_record(ts, health, reviewed)

    print("=== Brain cycle (auto-évaluation bornée) ===")
    print(f"events={rec['total_events']} · actionnables={rec['actionable']} · "
          f"pour toi={rec['needs_human']} · réfutés={rec['refuted']}")
    for r in reviewed:
        print(f"   [{r['skeptic_verdict']}] {r.get('kind')} {r.get('subject','')}")
    if not reviewed:
        print("   (rien à réviser — ledger jeune ; le cycle tournera et s'enrichira)")

    if dry:
        print("\n--dry-run : journal NON écrit.")
        return 0
    try:
        JOURNAL.parent.mkdir(parents=True, exist_ok=True)
        with JOURNAL.open("a") as f:  # append-only, the sole write
            f.write(json.dumps(rec) + "\n")
        print(f"\n✓ Record ajouté au journal métacognitif : {JOURNAL}")
    except Exception as exc:
        print(f"\n(journal non écrit : {exc})", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
