#!/usr/bin/env python3
"""Export Token Savior Memory Engine data to a portable JSON backup.

Usage:
    python3 scripts/export_memory.py [--project /root/token-savior] [--output backup.json]

If --project is omitted, exports ALL projects.
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from token_savior import memory_db  # noqa: E402


def _rows_to_dicts(rows) -> list[dict]:
    return [dict(r) for r in rows]


def export_memory(project: str | None, output: Path) -> dict:
    conn = memory_db.get_db()
    conn.row_factory = sqlite3.Row

    if project:
        obs = conn.execute(
            "SELECT * FROM observations WHERE project_root=? AND archived=0",
            (project,),
        ).fetchall()
        sessions = conn.execute(
            "SELECT * FROM sessions WHERE project_root=?", (project,)
        ).fetchall()
        summaries = conn.execute(
            "SELECT * FROM summaries WHERE project_root=?", (project,)
        ).fetchall()
        prompts = conn.execute(
            "SELECT * FROM user_prompts WHERE project_root=?", (project,)
        ).fetchall()
    else:
        obs = conn.execute("SELECT * FROM observations WHERE archived=0").fetchall()
        sessions = conn.execute("SELECT * FROM sessions").fetchall()
        summaries = conn.execute("SELECT * FROM summaries").fetchall()
        prompts = conn.execute("SELECT * FROM user_prompts").fetchall()

    conn.close()

    payload = {
        "exported_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "version": "1.0",
        "project_root": project or "all",
        "stats": {
            "observations": len(obs),
            "sessions": len(sessions),
            "summaries": len(summaries),
            "prompts": len(prompts),
        },
        "observations": _rows_to_dicts(obs),
        "sessions": _rows_to_dicts(sessions),
        "summaries": _rows_to_dicts(summaries),
        "user_prompts": _rows_to_dicts(prompts),
    }

    output.write_text(json.dumps(payload, indent=2, ensure_ascii=False))
    return payload["stats"]


def main() -> None:
    parser = argparse.ArgumentParser(description="Export Memory Engine data to JSON.")
    parser.add_argument("--project", default=None, help="Absolute project_root to filter; omit for all")
    parser.add_argument("--output", default="ts-memory-backup.json", help="Output JSON path")
    args = parser.parse_args()

    out = Path(args.output)
    stats = export_memory(args.project, out)
    print(f"Exported to {out.resolve()}")
    print(f"  observations : {stats['observations']}")
    print(f"  sessions     : {stats['sessions']}")
    print(f"  summaries    : {stats['summaries']}")
    print(f"  prompts      : {stats['prompts']}")


if __name__ == "__main__":
    main()
