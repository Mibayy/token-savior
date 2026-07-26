"""PreToolUse entrypoint: enforce hard rules via permissionDecision:deny.

Fails OPEN — any error, or the kill-switch TS_RULES_DISABLE=1, yields exit 0
with no output (the tool is allowed). A bug in enforcement must never block
the user; session safety outranks rule safety.
"""
from __future__ import annotations

import json
import os
import re
import sys


def _debraye_dans_la_commande(payload: dict) -> bool:
    """`TS_RULES_DISABLE=1 <commande>` doit debrayer, comme le message le dit.

    Le hook tourne dans son propre processus : un prefixe de commande pose la
    variable pour l'enfant, jamais pour lui. Sa porte de sortie ne fonctionnait
    donc pas comme annonce -- la meilleure facon de faire retirer un garde-fou
    de la configuration pour de bon.
    """
    try:
        commande = str((payload.get("tool_input") or {}).get("command") or "")
    except AttributeError:
        return False
    return bool(re.search(r"\bTS_RULES_DISABLE\s*=\s*['\"]?1\b", commande))


def main() -> int:
    if os.environ.get("TS_RULES_DISABLE") == "1":
        return 0
    try:
        raw = sys.stdin.read()
        payload = json.loads(raw) if raw.strip() else {}
    except Exception:
        return 0
    if _debraye_dans_la_commande(payload):
        return 0
    try:
        from token_savior.memory import rules
        tool_name = payload.get("tool_name", "")
        tool_input = payload.get("tool_input") or {}
        session_id = payload.get("session_id")
        decision = rules.evaluate(tool_name, tool_input, session_id)
        if decision["decision"] == "deny":
            try:
                from token_savior.memory import ledger
                ledger.ledger_put(
                    "hard_block", subject=decision.get("rule_id"),
                    session_id=session_id,
                    meta={"tool": tool_name, "reason": decision.get("reason")})
            except Exception:
                pass
            out = {"hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "deny",
                "permissionDecisionReason": decision.get("reason") or "blocked by rule",
            }}
            sys.stdout.write(json.dumps(out))
            sys.stdout.flush()
    except Exception as exc:  # fail open
        print(f"[token-savior:rules_hook] {exc}", file=sys.stderr)
        return 0
    return 0


if __name__ == "__main__":
    sys.exit(main())
