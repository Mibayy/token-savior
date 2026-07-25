"""Initiative — proactive project-advancement, guided by the world model (unit P).

READ-ONLY / propose-only. Fuses the world model (project states, deadlines,
dirty trees, failed services) into a ranked list of what deserves attention,
and suggests a concrete next action for each. It does NOT execute anything —
initiative without judgment is zeal, and it must not act blind. Louis (or a
later, trust-earned step) decides.

Run:  python3 scripts/initiative.py
"""
from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT))
sys.path.insert(0, str(_ROOT / "src"))


def days_until(deadline: str, *, now_epoch: int) -> int | None:
    try:
        dt = datetime.strptime(deadline, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    except Exception:
        return None
    return int((int(dt.timestamp()) - now_epoch) // 86400)


def rank_actions(projects: list[dict], *, now_epoch: int) -> list[dict]:
    """Turn world-model project records into ranked, actionable suggestions."""
    actions: list[dict] = []
    for p in projects:
        name = p.get("name", "?")
        if p.get("deadline"):
            d = days_until(p["deadline"], now_epoch=now_epoch)
            if d is not None and d <= 14:
                urgency = max(10, 100 - max(d, 0) * 6)
                actions.append({
                    "project": name, "kind": "deadline_soon",
                    "why": f"deadline {p['deadline']} dans {d}j"
                           + (" (dépassée)" if d < 0 else ""),
                    "urgency": urgency,
                    "suggested": "vérifier l'avancement et préparer le livrable"})
        if p.get("service_status") == "failed":
            actions.append({
                "project": name, "kind": "failed_service",
                "why": "service en échec", "urgency": 85,
                "suggested": f"journalctl -u {p.get('service', name)} -n 50, "
                             f"puis restart si la cause est saine"})
        if p.get("dirty_files", 0) >= 1 and p.get("activity") in ("recent", "stale", "dormant"):
            actions.append({
                "project": name, "kind": "uncommitted_work",
                "why": f"{p['dirty_files']} fichiers non commités, projet {p.get('activity')}",
                "urgency": 40,
                "suggested": "commiter le travail en cours ou nettoyer l'arbre"})
    actions.sort(key=lambda a: -a["urgency"])
    return actions


def main() -> int:
    from scripts import world_model as wm
    now = int(datetime.now(tz=timezone.utc).timestamp())
    projects = [wm._enrich_git(p, now) for p in wm.discover_projects()]
    projects = wm.map_services(projects, wm._services())
    projects = wm.merge_overlay(projects, wm._load_overlay())

    actions = rank_actions(projects, now_epoch=now)
    print("=== Initiative (propositions, ranked — rien n'est exécuté) ===")
    if not actions:
        print("Rien d'urgent : projets propres, pas de deadline proche, pas de service en échec.")
        return 0
    for a in actions:
        print(f"[{a['urgency']:3}] {a['project']:24} {a['kind']}")
        print(f"       pourquoi : {a['why']}")
        print(f"       suggéré  : {a['suggested']}")
    print("\n→ Je propose et je priorise ; tu (ou une étape à confiance méritée) décides d'agir.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
