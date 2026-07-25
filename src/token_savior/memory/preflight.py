"""Pre-flight self-verification (le cap).

Before an IRREVERSIBLE / high-consequence action, a reflex fires a short
adversarial checklist — intent match, reversibility, target/clause — so a
mistake is caught BEFORE it lands, not recorded after. It does not block (the
three worst cases are hard-denied by `rules`); it nudges the verification
habit, and logs a `preflight` ledger event so the bench can measure how
reliably I self-verify → the basis for earned autonomy.

The senior's half-second pause, made a standing reflex.
"""
from __future__ import annotations

import re
from typing import Any

# Command must sit at a real command boundary (not inside an echo/string), like
# the rules catalog — so `echo "rm -rf /x"` never triggers the reflex.
_ANCHOR = r"(?:^|[;&|]\s*|&&\s*|\|\|\s*)"

_CATEGORIES: list[tuple[str, str]] = [
    ("destructive-fs",
     _ANCHOR + r"(?:rm\s+-\w*[rf]|chmod\s+-R\b|chown\s+-R\b|shred\b|mkfs|:\s*>\s*/)"),
    ("service",
     _ANCHOR + r"systemctl\s+(?:stop|disable|restart|mask|kill)\b"),
    ("db",
     _ANCHOR + r"(?:psql|mysql|mariadb|sqlite3|mongosh?)\b[^\n]*"
     r"\b(?:drop\s+(?:table|database)|truncate|delete\s+from)\b"),
    ("publish",
     _ANCHOR + r"(?:git\s+push\b|vercel\s+(?:deploy|--prod)|npm\s+publish|\bdeploy\b)"),
]

_HINTS = {
    "destructive-fs": "Cible : le chemin/glob est-il exactement le bon, pas plus large ?",
    "db": "Clause : l'aperçu (SELECT) utilise-t-il la MÊME clause que l'action ?",
    "service": "Impact : qui dépend de ce service ? l'arrêt casse-t-il un flux live ?",
    "publish": "Cible : bonne branche / bon environnement, publication voulue ?",
}


def _checklist(category: str) -> list[str]:
    base = [
        "Intention : cette action fait-elle EXACTEMENT ce que je veux, ni plus large ?",
        "Réversibilité : est-ce réversible ? sinon, ai-je un backup ou un aperçu à jour ?",
    ]
    hint = _HINTS.get(category)
    return base + ([hint] if hint else [])


def classify_action(tool_name: str, tool_input: dict[str, Any] | None) -> dict:
    """Classify a pending action's pre-flight level. Bash-only for v1."""
    if tool_name != "Bash":
        return {"level": "none", "category": None, "checklist": [], "reason": ""}
    cmd = (tool_input or {}).get("command", "") or ""
    for category, pat in _CATEGORIES:
        if re.search(pat, cmd, re.IGNORECASE):
            return {"level": "reflex", "category": category,
                    "checklist": _checklist(category),
                    "reason": f"action {category} irréversible/conséquente"}
    return {"level": "none", "category": None, "checklist": [], "reason": ""}


def record_preflight(category: str, *, session_id: str | None = None,
                     project_root: str | None = None) -> dict:
    """Log that a pre-flight reflex fired (measures self-verification rate)."""
    from token_savior.memory import ledger
    return ledger.ledger_put("preflight", subject=category, session_id=session_id,
                             project_root=project_root, meta={"category": category})
