"""Stdin entrypoint for the UserPromptSubmit hook: capture misses.

Reads the hook JSON payload on stdin, records a 'miss' if the user text is a
correction. Never raises: a hook must not break the session.
"""
from __future__ import annotations

import json
import sys


def main() -> int:
    try:
        raw = sys.stdin.read()
        payload = json.loads(raw) if raw.strip() else {}
    except Exception:
        return 0
    try:
        from token_savior.memory import ledger
        ledger.record_from_userprompt(
            payload,
            session_id=payload.get("session_id"),
            project_root=payload.get("cwd") or payload.get("project_root"),
        )
    except Exception as exc:  # never break the session
        print(f"[token-savior:ledger_hook] {exc}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
