"""Fleet planner — when and how to fan out to sub-agents (unit F).

The orchestration RUNTIME already exists (the Agent/Workflow tools, the
subagent-driven-development skill, the orchestrator project). What was missing
is the JUDGMENT: given a task's shape, should I go solo, fan out in parallel,
or run a subagent-driven pipeline — with how many agents, which roles, which
model each, and whether to add adversarial verification. This encodes the
patterns proven this session (sonnet implementers, opus final review,
skeptic-per-finding on risky work) as a decision-support planner.

Advice only — it returns a plan; the human/runtime executes it.

Run:  python3 scripts/fleet.py --subtasks 5 --size large --risk high --review
"""
from __future__ import annotations

import sys


def plan_fleet(*, subtasks: int = 1, size: str = "small",
               risk: str = "low", needs_review: bool = False) -> dict:
    """Recommend a fan-out shape for a task. Pure decision logic."""
    large = size == "large"
    high = risk == "high"

    verify = {"adversarial": high, "skeptics": 3 if high else 0}

    # solo: one small, low-risk unit that carries its own test cycle.
    if subtasks <= 1 and not large and not high and not needs_review:
        return {"mode": "solo", "agents": 0, "roles": [], "verify": verify,
                "note": "trivial — pas de flotte, exécution directe"}

    # subagent-driven: large or review-gated work → implementer + reviewer per task.
    if large or needs_review:
        roles = [
            {"role": "implementer", "count": max(1, subtasks), "model": "sonnet",
             "note": "transcription/impl guidée par plan → mid-tier"},
            {"role": "reviewer", "count": max(1, subtasks),
             "model": "opus" if high else "sonnet",
             "note": "revue spec+qualité par tâche"},
            {"role": "final_review", "count": 1, "model": "opus",
             "note": "revue globale de branche → tier le plus capable"},
        ]
        agents = sum(r["count"] for r in roles)
        return {"mode": "subagent_driven", "agents": agents, "roles": roles,
                "verify": verify,
                "note": "pipeline séquentiel, revue entre tâches"}

    # parallel fan-out: independent subtasks, no barrier needed.
    roles = [{"role": "worker", "count": subtasks, "model": "sonnet",
              "note": "sous-tâches indépendantes en parallèle"}]
    return {"mode": "parallel_fanout", "agents": subtasks, "roles": roles,
            "verify": verify, "note": "fan-out concurrent, filtrer les None au retour"}


def _arg(name, default):
    if name in sys.argv:
        i = sys.argv.index(name)
        return sys.argv[i + 1] if i + 1 < len(sys.argv) else default
    return default


def main() -> int:
    plan = plan_fleet(
        subtasks=int(_arg("--subtasks", "1")),
        size=_arg("--size", "small"),
        risk=_arg("--risk", "low"),
        needs_review="--review" in sys.argv,
    )
    print("=== Fleet planner (proposition d'orchestration) ===")
    print(f"mode    : {plan['mode']}")
    print(f"agents  : {plan['agents']}")
    for r in plan["roles"]:
        print(f"   {r['count']}× {r['role']:12} [{r['model']}] — {r['note']}")
    v = plan["verify"]
    if v["adversarial"]:
        print(f"vérif   : {v['skeptics']} sceptiques adversariaux (risque élevé)")
    print(f"note    : {plan['note']}")
    print("\n→ Plan de flotte. Le runtime (Agent/Workflow) l'exécute ; moi je le cadre.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
