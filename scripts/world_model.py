"""World model — a living view of Louis's operation (unit W).

READ-ONLY. Discovers his projects (git repos + services), enriches each with
live signals (last commit, dirty tree, service status), classifies activity,
and overlays maintained business context (client, deadline, priority). The
output is a chief-of-staff view: what's active, what's stale, what needs
attention, what has a deadline — so I reason about WHY work matters, not just
how to do it.

The business overlay lives at world/overlay.json (maintained, versioned).

Run:  python3 scripts/world_model.py
"""
from __future__ import annotations

import json
import re
import subprocess
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
OVERLAY_PATH = _ROOT / "world" / "overlay.json"
SCAN_ROOTS = ["/root", "/root/projects", "/var/www"]

_ACTIVE_D, _RECENT_D, _STALE_D = 7, 30, 90


# --- pure core (testable) ---------------------------------------------------

def classify_activity(last_commit_epoch: int | None, *, now_epoch: int) -> str:
    if not last_commit_epoch:
        return "dormant"
    days = (now_epoch - last_commit_epoch) / 86400
    if days <= _ACTIVE_D:
        return "active"
    if days <= _RECENT_D:
        return "recent"
    if days <= _STALE_D:
        return "stale"
    return "dormant"


def merge_overlay(projects: list[dict], overlay: dict) -> list[dict]:
    out = []
    for p in projects:
        extra = overlay.get(p["name"], {})
        out.append({**p, **extra})
    return out


def map_services(projects: list[dict], services: dict[str, str]) -> list[dict]:
    """Attach the systemd service whose name is DELIMITER-anchored to the
    project name (exact, or name-prefixed), picking the most specific. Anchored
    matching avoids 'a' → 'intel-api.service' false maps."""
    out = []
    for p in projects:
        name = p["name"]
        candidates = []
        for svc, status in services.items():
            base = svc.replace(".service", "")
            if (base == name or base.startswith(name + "-") or base.startswith(name + ".")
                    or name.startswith(base + "-")):
                candidates.append((svc, status, len(base)))
        q = dict(p)
        if candidates:
            svc, status, _ = max(candidates, key=lambda c: c[2])  # most specific
            q["service"], q["service_status"] = svc, status
        out.append(q)
    return out


def world_summary(projects: list[dict]) -> dict:
    by_activity: dict[str, int] = defaultdict(int)
    needs = []
    deadlines = []
    for p in projects:
        by_activity[p.get("activity", "unknown")] += 1
        if p.get("service_status") == "failed":
            needs.append(p["name"])
        if p.get("deadline"):
            deadlines.append({"name": p["name"], "deadline": p["deadline"]})
    return {"total": len(projects), "by_activity": dict(by_activity),
            "needs_attention": needs, "deadlines": deadlines}


# --- live collectors (read-only) --------------------------------------------

def _run(cmd: list[str], cwd: str | None = None, timeout: int = 8) -> str:
    try:
        return subprocess.run(cmd, capture_output=True, text=True,
                              timeout=timeout, cwd=cwd).stdout.strip()
    except Exception:
        return ""


def discover_projects() -> list[dict]:
    seen: dict[str, dict] = {}  # keyed by PATH, so same-name repos in different
    for root in SCAN_ROOTS:      # roots aren't silently dropped
        out = _run(["find", root, "-maxdepth", "2", "-name", ".git", "-type", "d"])
        for gitdir in out.splitlines():
            path = str(Path(gitdir).parent)
            if path not in seen:
                seen[path] = {"name": Path(path).name, "path": path}
    return list(seen.values())


def _enrich_git(p: dict, now: int) -> dict:
    last = _run(["git", "log", "-1", "--format=%ct"], cwd=p["path"])
    epoch = int(last) if last.isdigit() else None
    dirty = _run(["git", "status", "--porcelain"], cwd=p["path"])
    q = dict(p)
    q["last_commit_epoch"] = epoch
    q["dirty_files"] = len([l for l in dirty.splitlines() if l.strip()])
    q["activity"] = classify_activity(epoch, now_epoch=now)
    return q


def _services() -> dict[str, str]:
    out = _run(["systemctl", "list-units", "--type=service", "--all",
                "--no-legend", "--plain"])
    svc: dict[str, str] = {}
    for ln in out.splitlines():
        m = re.match(r"([\w@.\-]+\.service)\s+\S+\s+(\S+)\s+(\S+)", ln)
        if m:
            svc[m.group(1)] = "failed" if m.group(3) == "failed" else m.group(2)
    return svc


def _load_overlay() -> dict:
    try:
        return json.loads(OVERLAY_PATH.read_text())
    except Exception:
        return {}


def main() -> int:
    now = int(datetime.now(tz=timezone.utc).timestamp())
    projects = [_enrich_git(p, now) for p in discover_projects()]
    projects = map_services(projects, _services())
    projects = merge_overlay(projects, _load_overlay())
    s = world_summary(projects)

    print("=== Modèle du monde (read-only) ===")
    print(f"{s['total']} projets · " +
          " · ".join(f"{k}:{v}" for k, v in sorted(s['by_activity'].items())))
    if s["needs_attention"]:
        print(f"\n⚠ À traiter (service en échec) : {s['needs_attention']}")
    if s["deadlines"]:
        print("\n📅 Deadlines :")
        for d in sorted(s["deadlines"], key=lambda x: x["deadline"]):
            print(f"   {d['deadline']}  {d['name']}")
    print("\n## Projets actifs / récents")
    for p in sorted(projects, key=lambda x: -(x.get("last_commit_epoch") or 0)):
        if p.get("activity") in ("active", "recent"):
            tag = f" [{p['priority']}]" if p.get("priority") else ""
            svc = f" · {p['service_status']}" if p.get("service_status") else ""
            dirty = f" · {p['dirty_files']} modifs" if p.get("dirty_files") else ""
            print(f"   {p['activity']:7} {p['name']}{tag}{svc}{dirty}")
    dormant = [p["name"] for p in projects if p.get("activity") == "dormant"]
    print(f"\n## Dormants ({len(dormant)}) : {', '.join(dormant[:20])}")
    print("\n→ Vue read-only. Overlay business maintenu dans world/overlay.json.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
