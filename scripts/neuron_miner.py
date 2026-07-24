"""Neuron miner — READ-ONLY bootstrap of the memory brain from VPS history.

Scans what already exists (prompt archive, tool captures, optionally service
logs) and PROPOSES candidate "neurons" as a report, and can BACKFILL the ledger
with historically-detected misses (additive + marked + idempotent).

It never deletes anything. The one write path (--backfill) only INSERTs ledger
events tagged source=backfill, and is idempotent on prompt id. (Cf. the 25/07
blanket-delete incident: operations on real data are backup-first, and any
write is additive, marked, and reversible.)

Run:  python3 scripts/neuron_miner.py            # read-only report
      python3 scripts/neuron_miner.py --distribution   # historical miss buckets
      python3 scripts/neuron_miner.py --backfill  # write backfilled misses
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

# Commands whose PURPOSE is to view/inspect errors — their output looks like an
# error but the command didn't fail. Excluded from failure mining.
_VIEWER_RE = re.compile(
    r"--log-failed|gh\s+run\s+view|journalctl|hook-errors|grep\b[^|]*error|"
    r"\b(?:cat|tail|head|less|bat)\b[^|]*\.log",
    re.IGNORECASE,
)

# Implicit correction markers — HIGH-PRECISION only. Deliberately excludes
# generic phrasings ("tu n'as pas", "au lieu de") that flood normal
# instructions with false positives (measured: 106 + 78 noise hits on the real
# archive). For human-reviewed historical mining; the live ledger stays
# conservative via ledger.detect_correction.
_IMPLICIT = re.compile(
    r"t'?as oubli|tu as oubli|il (?:te )?manque\b|je t'?avais (?:demand|dit)|"
    r"c'?est pas (?:ce que|ce qu|ça)|combien de fois|"
    r"(?:check|regarde|va voir)(?: dans)? (?:tes|les) logs",
    re.IGNORECASE,
)


def looks_like_error(output: str) -> bool:
    return bool(output) and bool(_ERROR_MARKERS.search(output))


def detect_correction_loose(text: str) -> str | None:
    """Explicit correction (live detector) OR an implicit marker."""
    if not text:
        return None
    hit = ledger.detect_correction(text)
    if hit:
        return hit
    m = _IMPLICIT.search(text)
    return m.group(0).lower() if m else None


def cluster_corrections(prompts: list[str]) -> list[dict]:
    groups: dict[str, list[str]] = defaultdict(list)
    for p in prompts:
        phrase = detect_correction_loose(p)
        if phrase:
            groups[phrase].append(p)
    out = [{"phrase": ph, "count": len(exs), "examples": exs[:5]}
           for ph, exs in groups.items()]
    out.sort(key=lambda c: c["count"], reverse=True)
    return out


def top_error_commands(captures: list[dict]) -> list[dict]:
    """Failing Bash commands, EXCLUDING error-viewers (whose output only
    reports on failures)."""
    groups: dict[str, list[dict]] = defaultdict(list)
    for c in captures:
        cmd = (c.get("command") or "").strip()
        out = c.get("output") or ""
        if not cmd or not looks_like_error(out):
            continue
        if _VIEWER_RE.search(cmd):
            continue
        key = cmd.splitlines()[0][:80]
        groups[key].append({"command": cmd, "output": out[:200]})
    res = [{"command": k, "count": len(v), "examples": v[:3]}
           for k, v in groups.items()]
    res.sort(key=lambda c: c["count"], reverse=True)
    return res


# --- read-only data pulls ---------------------------------------------------

def _pull_prompt_rows() -> list[dict]:
    from token_savior import memory_db
    conn = memory_db.get_db()
    rows = conn.execute(
        "SELECT id, session_id, project_root, prompt_text, created_at_epoch "
        "FROM user_prompts WHERE prompt_text IS NOT NULL").fetchall()
    conn.close()
    return [{"id": r[0], "session_id": r[1], "project_root": r[2],
             "text": r[3], "epoch": r[4]} for r in rows]


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
        try:
            d = _json.loads(arg) if arg else {}
        except Exception:
            d = {}
        if isinstance(d, dict) and d.get("command"):
            caps.append({"command": d["command"], "output": out or ""})
    return caps


def _service_errors(services: list[str], lines: int = 200) -> list[dict]:
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


# --- backfill (the one additive write path) ---------------------------------

def _already_backfilled_ids() -> set[int]:
    ids: set[int] = set()
    for ev in ledger.ledger_query(event_type="miss", limit=1_000_000):
        meta = ev.get("meta") or {}
        if meta.get("source") == "backfill" and meta.get("prompt_id") is not None:
            ids.add(meta["prompt_id"])
    return ids


def backfill_misses(*, write: bool = False) -> dict:
    """Classify every historically-detected miss and (optionally) persist it.

    Returns the 4-bucket distribution. Historical misses have no session
    injection record, so `ignored` cannot be inferred — expect the mix to be
    unrecorded/invisible/uncertain only. Idempotent on prompt id; additive-only.
    """
    rows = _pull_prompt_rows()
    done = _already_backfilled_ids() if write else set()
    dist: dict[str, int] = defaultdict(int)
    written = 0
    # Historical classification does one search per miss; force FTS-only (fast,
    # deterministic) — vector fusion would make backfilling hundreds of misses
    # take minutes and carries cross-call embedding state.
    from token_savior import db_core as _dc
    _prev_vec = _dc.VECTOR_SEARCH_AVAILABLE
    _dc.VECTOR_SEARCH_AVAILABLE = False
    project = ledger._active_project_root()
    try:
        for r in rows:
            phrase = detect_correction_loose(r["text"])
            if not phrase:
                continue
            if write and r["id"] in done:
                continue
            cls = ledger.classify_miss(r["text"], [], project)
            mc = cls["miss_class"]
            dist[mc] += 1
            if write:
                ledger.ledger_put(
                    "miss", session_id=r["session_id"], project_root=r["project_root"],
                    outcome={"was_visible": 0 if mc == "invisible" else None},
                    meta={"source": "backfill", "prompt_id": r["id"], "phrase": phrase,
                          "miss_class": mc, "expected_obs": cls["expected_obs"],
                          "overlap": cls["overlap"], "original_epoch": r["epoch"]})
                written += 1
    finally:
        _dc.VECTOR_SEARCH_AVAILABLE = _prev_vec
    return {"distribution": dict(dist), "written": written,
            "total_detected": sum(dist.values())}


def main() -> int:
    args = set(sys.argv[1:])

    if "--backfill" in args or "--distribution" in args:
        res = backfill_misses(write="--backfill" in args)
        mode = "ÉCRIT" if "--backfill" in args else "DRY-RUN (lecture seule)"
        print(f"=== Backfill des ratés historiques [{mode}] ===")
        print(f"Ratés détectés : {res['total_detected']}")
        print("Distribution des buckets (historique) :")
        for k, v in sorted(res["distribution"].items(), key=lambda kv: -kv[1]):
            print(f"   {v:4}  {k}")
        if "--backfill" in args:
            print(f"Events miss écrits (additifs, source=backfill) : {res['written']}")
        else:
            print("→ Rien écrit. Relance avec --backfill pour persister.")
        print("Note : 'ignored' impossible en historique (pas de trace d'injection par session).")
        return 0

    print("=== Neuron miner (READ-ONLY — rien n'est écrit) ===\n")
    prompts = [r["text"] for r in _pull_prompt_rows()]
    corr = cluster_corrections(prompts)
    print(f"## Corrections récurrentes ({sum(c['count'] for c in corr)}, explicites+implicites)")
    for c in corr[:15]:
        print(f"  {c['count']:3}×  « {c['phrase']} »")
    print()
    errs = top_error_commands(_pull_captures())
    print(f"## Vraies commandes en échec ({len(errs)} motifs, viewers exclus)")
    for e in errs[:15]:
        print(f"  {e['count']:3}×  {e['command']}")
    print()
    svc = _service_errors(["intel-api", "gw2cc", "claude-telegram", "scribe"])
    print("## Erreurs de services (journalctl, best-effort)")
    for s in svc:
        print(f"  {s['count']:3} warn+  {s['service']}")
    print("\n→ Propositions uniquement. Rien n'a été écrit ni supprimé.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
