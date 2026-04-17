#!/usr/bin/env python3
"""Import Token Savior Memory Engine data from a JSON backup.

Usage:
    python3 scripts/import_memory.py --input backup.json [--remap-project /root/new] [--dry-run]

Dedup is native via observation.content_hash (same project_root + title + content -> skipped).
Sessions are matched by created_at_epoch + project_root.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from token_savior import memory_db  # noqa: E402


def _remap(row: dict, new_root: str | None) -> dict:
    if new_root and "project_root" in row:
        row = dict(row)
        row["project_root"] = new_root
    return row


def import_memory(input_path: Path, remap_project: str | None, dry_run: bool) -> dict:
    data = json.loads(input_path.read_text(encoding="utf-8"))

    stats = {"obs_new": 0, "obs_dup": 0, "sessions_new": 0, "sessions_dup": 0,
             "summaries_new": 0, "prompts_new": 0}

    conn = memory_db.get_db()

    # Sessions: map old_id -> new_id
    session_map: dict[int, int] = {}
    for s in data.get("sessions", []):
        s = _remap(s, remap_project)
        epoch = s.get("created_at_epoch")
        existing = conn.execute(
            "SELECT id FROM sessions WHERE project_root=? AND created_at_epoch=?",
            (s["project_root"], epoch),
        ).fetchone()
        if existing:
            session_map[s["id"]] = existing[0]
            stats["sessions_dup"] += 1
            continue
        if dry_run:
            stats["sessions_new"] += 1
            continue
        cur = conn.execute(
            "INSERT INTO sessions (project_root, status, summary, symbols_changed, "
            "files_changed, events_count, created_at, created_at_epoch, completed_at, "
            "completed_at_epoch) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                s["project_root"], s.get("status", "completed"), s.get("summary"),
                s.get("symbols_changed"), s.get("files_changed"),
                s.get("events_count") or 0, s["created_at"], epoch,
                s.get("completed_at"), s.get("completed_at_epoch"),
            ),
        )
        session_map[s["id"]] = cur.lastrowid
        stats["sessions_new"] += 1

    # Observations: dedup via content_hash
    for o in data.get("observations", []):
        o = _remap(o, remap_project)
        chash = memory_db.content_hash(o["content"])
        existing = conn.execute(
            "SELECT id FROM observations WHERE content_hash=? AND project_root=? AND archived=0",
            (chash, o["project_root"]),
        ).fetchone()
        if existing:
            stats["obs_dup"] += 1
            continue
        if dry_run:
            stats["obs_new"] += 1
            continue
        new_sid = session_map.get(o.get("session_id")) if o.get("session_id") else None
        conn.execute(
            "INSERT INTO observations (session_id, project_root, type, title, content, "
            "why, how_to_apply, symbol, file_path, tags, private, importance, "
            "relevance_score, access_count, content_hash, last_accessed_at, "
            "created_at, created_at_epoch, updated_at, archived) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                new_sid, o["project_root"], o["type"], o["title"], o["content"],
                o.get("why"), o.get("how_to_apply"), o.get("symbol"),
                o.get("file_path"), o.get("tags"), o.get("private") or 0,
                o.get("importance") or 5, o.get("relevance_score") or 1.0,
                o.get("access_count") or 0, chash, o.get("last_accessed_at"),
                o["created_at"], o["created_at_epoch"], o["updated_at"], 0,
            ),
        )
        stats["obs_new"] += 1

    # Summaries
    for s in data.get("summaries", []):
        s = _remap(s, remap_project)
        if dry_run:
            stats["summaries_new"] += 1
            continue
        new_sid = session_map.get(s.get("session_id")) if s.get("session_id") else None
        conn.execute(
            "INSERT INTO summaries (session_id, project_root, content, observation_ids, "
            "covers_until_epoch, created_at, created_at_epoch) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                new_sid, s["project_root"], s["content"], s.get("observation_ids"),
                s.get("covers_until_epoch"), s["created_at"], s["created_at_epoch"],
            ),
        )
        stats["summaries_new"] += 1

    # Prompts
    for p in data.get("user_prompts", []):
        p = _remap(p, remap_project)
        if dry_run:
            stats["prompts_new"] += 1
            continue
        new_sid = session_map.get(p.get("session_id")) if p.get("session_id") else None
        conn.execute(
            "INSERT INTO user_prompts (session_id, project_root, prompt_text, "
            "prompt_number, created_at, created_at_epoch) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (
                new_sid, p.get("project_root"), p["prompt_text"],
                p.get("prompt_number"), p["created_at"], p["created_at_epoch"],
            ),
        )
        stats["prompts_new"] += 1

    if not dry_run:
        conn.commit()
    conn.close()
    return stats


def main() -> None:
    parser = argparse.ArgumentParser(description="Import Memory Engine JSON backup.")
    parser.add_argument("--input", required=True, help="Input JSON path")
    parser.add_argument("--remap-project", default=None, help="Rewrite project_root to this absolute path")
    parser.add_argument("--dry-run", action="store_true", help="Report counts without writing")
    args = parser.parse_args()

    input_path = Path(args.input)
    if not input_path.exists():
        print(f"ERR: input file not found: {input_path}", file=sys.stderr)
        sys.exit(1)

    stats = import_memory(input_path, args.remap_project, args.dry_run)
    tag = " (DRY RUN)" if args.dry_run else ""
    print(f"Imported{tag}:")
    print(f"  observations : {stats['obs_new']} new / {stats['obs_dup']} duplicates skipped")
    print(f"  sessions     : {stats['sessions_new']} new / {stats['sessions_dup']} duplicates skipped")
    print(f"  summaries    : {stats['summaries_new']} new")
    print(f"  prompts      : {stats['prompts_new']} new")


if __name__ == "__main__":
    main()
