"""PostToolUse entrypoint: record satisfied preconditions. Never raises."""
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
        from token_savior.memory import rules
        rules.record_precondition(
            payload,
            session_id=payload.get("session_id"),
            project_root=payload.get("cwd") or payload.get("project_root"))
    except Exception as exc:
        print(f"[token-savior:precondition_hook] {exc}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
