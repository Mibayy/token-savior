"""PreToolUse entrypoint: fire the pre-flight verification reflex.

For an irreversible/high-consequence action, injects a short checklist into
context (a visible reminder to verify BEFORE executing) and logs a `preflight`
event so the bench can measure self-verification. NON-BLOCKING by design: the
three worst cases are hard-denied by `rules`; this nudges the habit across the
broader irreversible class. If the nudge proves insufficient (measured), it can
escalate to blocking later — measure before over-engineering.

Fails open. Honors TS_RULES_DISABLE=1.
"""
from __future__ import annotations

import json
import os
import sys


def main() -> int:
    if os.environ.get("TS_RULES_DISABLE") == "1":
        return 0
    try:
        raw = sys.stdin.read()
        payload = json.loads(raw) if raw.strip() else {}
    except Exception:
        return 0
    try:
        from token_savior.memory import preflight
        r = preflight.classify_action(payload.get("tool_name", ""),
                                      payload.get("tool_input") or {})
        if r["level"] == "reflex":
            lines = "\n".join(f"  {i}. {q}" for i, q in enumerate(r["checklist"], 1))
            # stdout on PreToolUse = context injection shown before I proceed.
            print(f"✈ PRÉ-VOL [{r['category']}] — vérifie AVANT d'exécuter :\n{lines}")
            try:
                preflight.record_preflight(
                    r["category"], session_id=payload.get("session_id"),
                    project_root=payload.get("cwd") or payload.get("project_root"))
            except Exception:
                pass
    except Exception as exc:  # fail open
        print(f"[token-savior:preflight_hook] {exc}", file=sys.stderr)
        return 0
    return 0


if __name__ == "__main__":
    sys.exit(main())
