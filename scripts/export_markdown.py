#!/usr/bin/env python3
"""Export observations to versioned markdown in a git repo.

Usage:
    python3 export_markdown.py [--output-dir /root/memory-backup]
"""
from __future__ import annotations

import argparse
import re
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
from token_savior import memory_db  # noqa: E402


TYPE_FILES = {
    "guardrail": "guardrails.md",
    "convention": "conventions.md",
    "decision": "decisions.md",
    "bugfix": "bugfixes.md",
    "command": "commands.md",
    "infra": "infra.md",
    "config": "config.md",
    "research": "research.md",
    "note": "notes.md",
    "warning": "warnings.md",
    "idea": "ideas.md",
    "error_pattern": "error_patterns.md",
}

_SLUG_RE = re.compile(r"[^a-zA-Z0-9_-]+")


def slugify(value: str) -> str:
    base = _SLUG_RE.sub("-", value.strip("/").split("/")[-1] or "unknown").strip("-")
    return base or "unknown"


def looks_private(row: dict) -> bool:
    if row.get("private"):
        return True
    for field in ("title", "content", "why", "how_to_apply"):
        v = row.get(field) or ""
        if "<private>" in v.lower() or "[private]" in v.lower():
            return True
    return False


def fmt_obs(row: dict) -> str:
    age = memory_db.relative_age(row.get("created_at_epoch"))
    header = f"## #{row['id']} {row['title']}"
    meta_parts = [f"**Type:** {row['type']}"]
    if row.get("symbol"):
        meta_parts.append(f"**Symbol:** `{row['symbol']}`")
    if row.get("file_path"):
        meta_parts.append(f"**File:** `{row['file_path']}`")
    if row.get("context"):
        meta_parts.append(f"**Context:** {row['context']}")
    if row.get("is_global"):
        meta_parts.append("🌐 **Global**")
    meta_parts.append(f"**Created:** {age}")
    if row.get("access_count"):
        meta_parts.append(f"**Accesses:** {row['access_count']}")
    meta = " | ".join(meta_parts)

    tags = row.get("tags")
    tag_line = ""
    if tags:
        try:
            import json as _json
            parsed = _json.loads(tags) if isinstance(tags, str) else tags
            if parsed:
                tag_line = f"\n**Tags:** {', '.join(parsed)}"
        except Exception:
            pass

    body_parts = [row.get("content") or ""]
    if row.get("why"):
        body_parts.append(f"\n**Why:** {row['why']}")
    if row.get("how_to_apply"):
        body_parts.append(f"\n**How to apply:** {row['how_to_apply']}")
    body = "\n".join(body_parts).strip()

    return f"{header}\n{meta}{tag_line}\n\n{body}\n"


def write_type_file(dir_path: Path, type_name: str, project_label: str, rows: list[dict]) -> None:
    fname = TYPE_FILES.get(type_name, f"{type_name}.md")
    path = dir_path / fname
    now_iso = time.strftime("%Y-%m-%d %H:%M")
    lines = [
        f"# {type_name.capitalize()} — {project_label}",
        f"*Last updated: {now_iso} · {len(rows)} observations*",
        "",
        "---",
        "",
    ]
    for row in rows:
        lines.append(fmt_obs(row))
        lines.append("---\n")
    path.write_text("\n".join(lines), encoding="utf-8")


def write_readme(dir_path: Path, project_label: str, by_type: dict[str, int]) -> None:
    now_iso = time.strftime("%Y-%m-%d %H:%M")
    total = sum(by_type.values())
    lines = [
        f"# Memory Backup — {project_label}",
        f"*Exported: {now_iso}*",
        "",
        "| Type | Count |",
        "|------|-------|",
    ]
    for t, c in sorted(by_type.items(), key=lambda x: -x[1]):
        lines.append(f"| {t} | {c} |")
    lines.append(f"\n**Total: {total} observations**\n")
    (dir_path / "README.md").write_text("\n".join(lines), encoding="utf-8")


def ensure_git_repo(root: Path) -> None:
    if (root / ".git").is_dir():
        return
    root.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", str(root)], capture_output=True, text=True, check=False)
    subprocess.run(
        ["git", "-C", str(root), "commit", "--allow-empty", "-m", "init memory backup"],
        capture_output=True, text=True, check=False,
    )


def git_commit(root: Path, message: str) -> bool:
    subprocess.run(["git", "-C", str(root), "add", "-A"], capture_output=True, text=True, check=False)
    diff = subprocess.run(
        ["git", "-C", str(root), "diff", "--cached", "--quiet"],
        capture_output=True, text=True, check=False,
    )
    if diff.returncode == 0:
        return False
    subprocess.run(
        ["git", "-C", str(root), "commit", "-m", message],
        capture_output=True, text=True, check=False,
    )
    return True


def export_all(output_dir: str) -> dict:
    root = Path(output_dir).resolve()
    ensure_git_repo(root)

    conn = memory_db.get_db()
    rows = conn.execute(
        "SELECT id, project_root, type, title, content, why, how_to_apply, "
        "  symbol, file_path, context, tags, private, is_global, "
        "  access_count, created_at_epoch "
        "FROM observations WHERE archived=0 "
        "ORDER BY type, created_at_epoch DESC"
    ).fetchall()
    conn.close()

    buckets: dict[str, dict[str, list[dict]]] = {}
    by_project_count: dict[str, dict[str, int]] = {}
    total_kept = 0
    for raw in rows:
        row = dict(raw)
        if looks_private(row):
            continue
        label = "global" if row.get("is_global") else slugify(row.get("project_root") or "unknown")
        t = row.get("type") or "note"
        buckets.setdefault(label, {}).setdefault(t, []).append(row)
        by_project_count.setdefault(label, {})[t] = by_project_count.get(label, {}).get(t, 0) + 1
        total_kept += 1

    for label, by_type in buckets.items():
        dir_path = root / label
        dir_path.mkdir(parents=True, exist_ok=True)
        for t, rows_t in by_type.items():
            write_type_file(dir_path, t, label, rows_t)
        write_readme(dir_path, label, by_project_count[label])

    commit_msg = f"Memory backup {time.strftime('%Y-%m-%d')} — {total_kept} obs"
    committed = git_commit(root, commit_msg)
    return {
        "projects": len(buckets),
        "observations": total_kept,
        "output_dir": str(root),
        "committed": committed,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output-dir", default="/root/memory-backup")
    parser.add_argument("--project", default=None, help="(ignored — exports all)")
    args = parser.parse_args()
    res = export_all(args.output_dir)
    msg = (
        f"Exported {res['observations']} obs across {res['projects']} projects "
        f"→ {res['output_dir']}"
    )
    if res["committed"]:
        msg += " (git committed)"
    else:
        msg += " (no changes)"
    print(msg)
    return 0


if __name__ == "__main__":
    sys.exit(main())
