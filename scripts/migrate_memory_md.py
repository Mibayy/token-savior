#!/usr/bin/env python3
"""Migrate .md memory files (with optional YAML frontmatter) into Token Savior's SQLite memory DB."""

from __future__ import annotations

import argparse
import hashlib
import re
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

MEMORY_DB_PATH = Path.home() / ".local" / "share" / "token-savior" / "memory.db"
SCHEMA_PATH = Path(__file__).resolve().parent.parent / "src" / "token_savior" / "memory_schema.sql"

MEMORY_DIR = Path.home() / ".claude" / "projects" / "-root" / "memory"

FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n", re.DOTALL)

TYPE_MAP = {
    "feedback": "convention",
    "guardrail": "guardrail",
    "project": "project",
    "reference": "reference",
    "user": "user",
    "decision": "decision",
    "error_pattern": "error_pattern",
    "convention": "convention",
    "note": "project",
}

SKIP_FILES = {"MEMORY.md"}


def parse_frontmatter(text: str) -> tuple[dict[str, str], str]:
    m = FRONTMATTER_RE.match(text)
    if not m:
        return {}, text

    raw = m.group(1)
    meta: dict[str, str] = {}
    for line in raw.splitlines():
        if ":" in line:
            key, _, val = line.partition(":")
            meta[key.strip()] = val.strip().strip("\"'")
    body = text[m.end():]
    return meta, body


def map_type(raw_type: str) -> str:
    return TYPE_MAP.get(raw_type.lower(), "project")


def content_hash(project_root: str, title: str, content: str) -> str:
    raw = f"{project_root}:{title}:{content}"
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


def get_db() -> sqlite3.Connection:
    MEMORY_DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(MEMORY_DB_PATH))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA synchronous = NORMAL")
    if SCHEMA_PATH.exists():
        conn.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))
    return conn


def infer_type_from_filename(filename: str) -> str:
    name = filename.lower()
    if name.startswith("feedback"):
        return "convention"
    if name.startswith("error"):
        return "error_pattern"
    if name.startswith("guardrail"):
        return "guardrail"
    if "decision" in name:
        return "decision"
    if name.startswith("session-"):
        return "project"
    if name.startswith("hosting") or name.startswith("project-convention"):
        return "reference"
    return "project"


def extract_why_and_how(body: str) -> tuple[str | None, str | None, str]:
    why = None
    how = None

    why_match = re.search(r"\*\*Why:\*\*\s*(.*?)(?=\n\*\*|\n##|\Z)", body, re.DOTALL)
    if why_match:
        why = why_match.group(1).strip()

    how_match = re.search(r"\*\*How to apply:\*\*\s*(.*?)(?=\n\*\*|\n##|\Z)", body, re.DOTALL)
    if how_match:
        how = how_match.group(1).strip()

    return why, how, body


def migrate(project_root: str, memory_dir: Path, dry_run: bool = False) -> None:
    md_files = sorted(f for f in memory_dir.glob("*.md") if f.name not in SKIP_FILES)

    if not md_files:
        print(f"No .md files found in {memory_dir}")
        return

    total = len(md_files)
    migrated = 0
    duplicates = 0
    errors = 0

    conn = None if dry_run else get_db()
    now = datetime.now(timezone.utc).isoformat()
    epoch = int(datetime.now(timezone.utc).timestamp())

    for md_file in md_files:
        try:
            text = md_file.read_text(encoding="utf-8")
        except Exception as exc:
            print(f"  SKIP {md_file.name}: read error ({exc})")
            errors += 1
            continue

        meta, body = parse_frontmatter(text)
        body = body.strip()

        title = meta.get("name") or md_file.stem
        description = meta.get("description", "")
        raw_type = meta.get("type", "")

        filename_type = infer_type_from_filename(md_file.name)
        if raw_type:
            obs_type = map_type(raw_type)
            if obs_type in ("convention", "project") and filename_type in ("error_pattern", "guardrail", "decision", "reference"):
                obs_type = filename_type
        else:
            obs_type = filename_type

        if description and body:
            full_content = f"{description}\n\n{body}"
        elif description:
            full_content = description
        else:
            full_content = body or "(empty)"

        why, how, _ = extract_why_and_how(body)

        chash = content_hash(project_root, title, full_content)

        if dry_run:
            print(f"  [DRY] {md_file.name} -> type={obs_type}, title=\"{title}\"")
            migrated += 1
            continue

        row = conn.execute(
            "SELECT id FROM observations WHERE content_hash=? AND project_root=? AND archived=0",
            (chash, project_root),
        ).fetchone()

        if row is not None:
            print(f"  SKIP {md_file.name}: duplicate (id={row['id']})")
            duplicates += 1
            continue

        conn.execute(
            "INSERT INTO observations "
            "(session_id, project_root, type, title, content, why, how_to_apply, "
            " symbol, file_path, tags, private, importance, content_hash, "
            " created_at, created_at_epoch, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                None,
                project_root,
                obs_type,
                title,
                full_content,
                why,
                how,
                None,
                None,
                None,
                0,
                5,
                chash,
                now,
                epoch,
                now,
            ),
        )
        print(f"  OK   {md_file.name} -> type={obs_type}, title=\"{title}\"")
        migrated += 1

    if conn:
        conn.commit()
        conn.close()

    label = "Would migrate" if dry_run else "Migrated"
    print(f"\n{label} {migrated} / {total} files ({duplicates} duplicates skipped, {errors} errors)")


def main() -> None:
    parser = argparse.ArgumentParser(description="Migrate .md memory files to Token Savior SQLite DB")
    parser.add_argument("--project", required=True, help="Project root path (e.g. /root/token-savior)")
    parser.add_argument("--memory-dir", default=str(MEMORY_DIR), help="Memory .md directory")
    parser.add_argument("--dry-run", action="store_true", help="Preview without inserting")
    args = parser.parse_args()

    memory_dir = Path(args.memory_dir)
    if not memory_dir.is_dir():
        print(f"Error: {memory_dir} is not a directory", file=sys.stderr)
        sys.exit(1)

    print(f"Migrating .md files from {memory_dir}")
    print(f"Target project: {args.project}")
    print(f"DB: {MEMORY_DB_PATH}")
    if args.dry_run:
        print("MODE: dry-run\n")
    else:
        print()

    migrate(args.project, memory_dir, dry_run=args.dry_run)


if __name__ == "__main__":
    main()
