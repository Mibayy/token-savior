"""Neuron miner — READ-ONLY bootstrap of the memory brain from VPS history.

Scans what already exists (prompt archive, tool captures, optionally service
logs) and PROPOSES candidate "neurons" (things worth remembering) as a report.
It NEVER writes to or deletes from any store. Approved proposals get written by
a separate, deliberate step — after a human looks. (Cf. the 25/07 blanket-delete
incident: bulk operations on real data are read-only + backup-first, always.)

Run:  python3 scripts/neuron_miner.py
"""
from __future__ import annotations

import re
import subprocess
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from token_savior.memory import ledger  # noqa: E402

_ERROR_MARKERS = re.compile(
    r"traceback|not found|no such file|not a git repository|\bfatal:|"
    r"\berror\b|\berr!|permission denied|cannot |could not |failed\b|exit code [1-9]",
    re.IGNORECASE,
)


def looks_like_error(output: str) -> bool:
    """Heuristic: does this tool output look like a failure?"""
    return bool(output) and bool(_ERROR_MARKERS.search(output))


def cluster_corrections(prompts: list[str]) -> list[dict]:
    """Group detected corrections by their trigger phrase, with counts/examples."""
    groups: dict[str, list[str]] = defaultdict(list)
    for p in prompts:
        phrase = ledger.detect_correction(p)
        if phrase:
            groups[phrase].append(p)
    out = [{"phrase": ph, "count": len(exs), "examples": exs[:5]}
           for ph, exs in groups.items()]
    out.sort(key=lambda c: c["count"], reverse=True)
    return out


def top_error_commands(captures: list[dict]) -> list[dict]:
    """Group failing Bash commands (by first line) with counts/examples."""
    groups: dict[str, list[dict]] = defaultdict(list)
    for c in captures:
        cmd = (c.get("command") or "").strip()
        out = c.get("output") or ""
        if cmd and looks_like_error(out):
            key = cmd.splitlines()[0][:80]
            groups[key].append({"command": cmd, "output": out[:200]})
    res = [{"command": k, "count": len(v), "examples": v[:3]}
           for k, v in groups.items()]
    res.sort(key=lambda c: c["count"], reverse=True)
    return res


# --- read-only data pulls ---------------------------------------------------

def _pull_prompts() -> list[str]:
    from token_savior import memory_db
    conn = memory_db.get_db()
    rows = conn.execute(
        "SELECT prompt_text FROM user_prompts WHERE prompt_text IS NOT NULL").fetchall()
    conn.close()
    return [r[0] for r in rows]


def _pull_captures() -> list[dict]:
    from token_savior import memory_db
    import json as _json
    conn = memory_db.get_db()
    rows = conn.execute(
        "SELECT args_summary, output_preview FROM tool_captures "
        "WHERE tool_name='Bash'").fetchall()
    conn.close()
    caps: list[dict] = []
    for arg, out in rows:
        cmd = None
        try:
            d = _json.loads(arg) if arg else {}
            cmd = d.get("command") if isinstance(d, dict) else None
        except Exception:
            cmd = None
        if cmd:
            caps.append({"command": cmd, "output": out or ""})
    return caps


def _service_errors(services: list[str], lines: int = 200) -> list[dict]:
    """Best-effort read-only journalctl error scan. Silent on failure."""
    hits = []
    for svc in services:
        try:
            out = subprocess.run(
                ["journalctl", "-u", svc, "-n", str(lines), "--no-pager", "-p", "warning"],
                capture_output=True, text=True, timeout=10).stdout
        except Exception:
            continue
        errs = [ln for ln in out.splitlines() if looks_like_error(ln)]
        if errs:
            hits.append({"service": svc, "count": len(errs), "examples": errs[-3:]})
    return hits


def main() -> int:
    print("=== Neuron miner (READ-ONLY — rien n'est écrit) ===\n")

    corr = cluster_corrections(_pull_prompts())
    print(f"## Corrections récurrentes ({sum(c['count'] for c in corr)} au total)")
    for c in corr:
        print(f"  {c['count']:3}×  « {c['phrase']} »")
        for ex in c["examples"][:2]:
            print(f"        - {ex[:90].strip()}")
    print()

    errs = top_error_commands(_pull_captures())
    print(f"## Commandes qui ont échoué ({len(errs)} motifs)")
    for e in errs[:15]:
        print(f"  {e['count']:3}×  {e['command']}")
    print()

    svc = _service_errors(["intel-api", "gw2cc", "claude-telegram", "scribe"])
    print(f"## Erreurs de services (journalctl, best-effort)")
    for s in svc:
        print(f"  {s['count']:3} warn+  {s['service']}")
    print("\n→ Propositions uniquement. Rien n'a été écrit ni supprimé.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
