"""Compact checkpoint and rollback helpers for structural workflows."""

from __future__ import annotations

import hashlib
import os
import shutil
import time

from token_savior.annotator import annotate
from token_savior.models import ProjectIndex


def create_checkpoint(index: ProjectIndex, file_paths: list[str]) -> dict:
    """Create a compact checkpoint for a bounded set of project files."""
    seed = f"{index.root_path}|{'|'.join(sorted(file_paths))}|{time.time_ns()}"
    checkpoint_id = hashlib.md5(seed.encode()).hexdigest()[:12]
    checkpoint_dir = _checkpoint_dir(index.root_path, checkpoint_id)
    os.makedirs(checkpoint_dir, exist_ok=True)

    saved_files: list[str] = []
    for file_path in sorted(set(file_paths)):
        abs_path = os.path.join(index.root_path, file_path)
        if not os.path.exists(abs_path):
            continue
        dst = os.path.join(checkpoint_dir, file_path)
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        shutil.copy2(abs_path, dst)
        saved_files.append(file_path)

    return {
        "ok": True,
        "checkpoint_id": checkpoint_id,
        "saved_files": saved_files,
    }


def list_checkpoints(index: ProjectIndex) -> dict:
    """List available checkpoints for a project."""
    base_dir = _checkpoint_base_dir(index.root_path)
    if not os.path.isdir(base_dir):
        return {"ok": True, "checkpoints": []}

    checkpoints = []
    for checkpoint_id in sorted(os.listdir(base_dir)):
        checkpoint_dir = os.path.join(base_dir, checkpoint_id)
        if not os.path.isdir(checkpoint_dir):
            continue
        file_count = sum(len(files) for _, _, files in os.walk(checkpoint_dir))
        checkpoints.append(
            {
                "checkpoint_id": checkpoint_id,
                "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(os.path.getmtime(checkpoint_dir))),
                "file_count": file_count,
            }
        )
    return {"ok": True, "checkpoints": checkpoints}


def delete_checkpoint(index: ProjectIndex, checkpoint_id: str) -> dict:
    """Delete a checkpoint directory."""
    checkpoint_dir = _checkpoint_dir(index.root_path, checkpoint_id)
    if not os.path.isdir(checkpoint_dir):
        return {"error": f"Checkpoint '{checkpoint_id}' not found"}
    shutil.rmtree(checkpoint_dir)
    return {"ok": True, "checkpoint_id": checkpoint_id}


def prune_checkpoints(index: ProjectIndex, keep_last: int = 10) -> dict:
    """Keep only the newest N checkpoints."""
    listing = list_checkpoints(index)
    checkpoints = listing.get("checkpoints", [])
    if len(checkpoints) <= keep_last:
        return {"ok": True, "deleted": [], "kept": len(checkpoints)}

    ordered = sorted(checkpoints, key=lambda item: item["created_at"], reverse=True)
    to_delete = ordered[keep_last:]
    deleted = []
    for entry in to_delete:
        result = delete_checkpoint(index, entry["checkpoint_id"])
        if result.get("ok"):
            deleted.append(entry["checkpoint_id"])
    return {"ok": True, "deleted": deleted, "kept": min(keep_last, len(ordered))}


def restore_checkpoint(index: ProjectIndex, checkpoint_id: str) -> dict:
    """Restore files from a previously created checkpoint."""
    checkpoint_dir = _checkpoint_dir(index.root_path, checkpoint_id)
    if not os.path.isdir(checkpoint_dir):
        return {"error": f"Checkpoint '{checkpoint_id}' not found"}

    root_norm = os.path.normpath(index.root_path)
    restored_files: list[str] = []
    for root, _, files in os.walk(checkpoint_dir):
        for name in files:
            src = os.path.join(root, name)
            rel = os.path.relpath(src, checkpoint_dir)
            dst = os.path.normpath(os.path.join(index.root_path, rel))
            # Guard against path traversal (e.g. "../../../etc/passwd" in checkpoint)
            if os.path.commonpath([dst, root_norm]) != root_norm:
                return {"error": f"Checkpoint contains unsafe path: {rel}"}
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            shutil.copy2(src, dst)
            restored_files.append(rel)

    return {
        "ok": True,
        "checkpoint_id": checkpoint_id,
        "restored_files": sorted(restored_files),
    }


def compare_checkpoint_by_symbol(
    index: ProjectIndex,
    checkpoint_id: str,
    max_files: int = 20,
) -> dict:
    """Compare checkpointed files against current files at symbol level."""
    checkpoint_dir = _checkpoint_dir(index.root_path, checkpoint_id)
    if not os.path.isdir(checkpoint_dir):
        return {"error": f"Checkpoint '{checkpoint_id}' not found"}

    files: list[dict] = []
    for root, _, names in os.walk(checkpoint_dir):
        for name in sorted(names):
            if len(files) >= max_files:
                break
            before_path = os.path.join(root, name)
            rel = os.path.relpath(before_path, checkpoint_dir)
            after_path = os.path.join(index.root_path, rel)
            before_meta = _read_metadata(before_path, rel)
            after_meta = _read_metadata(after_path, rel) if os.path.exists(after_path) else None
            files.append(
                {
                    "file": rel,
                    "symbols": _compare_metadata(before_meta, after_meta),
                }
            )

    return {
        "ok": True,
        "checkpoint_id": checkpoint_id,
        "reported_files": len(files),
        "files": files,
    }


def _checkpoint_dir(root_path: str, checkpoint_id: str) -> str:
    """Path for a checkpoint under the project-local cache area."""
    return os.path.join(_checkpoint_base_dir(root_path), checkpoint_id)


def _checkpoint_base_dir(root_path: str) -> str:
    """Base directory for checkpoints."""
    return os.path.join(root_path, ".token-savior-checkpoints")


def _read_metadata(file_path: str, source_name: str):
    """Annotate a file from disk."""
    with open(file_path, encoding="utf-8") as f:
        return annotate(f.read(), source_name=source_name)


def _compare_metadata(before_meta, after_meta) -> dict:
    """Compare before/after metadata as symbol sets."""
    before = _symbol_map(before_meta)
    after = _symbol_map(after_meta) if after_meta is not None else {}
    before_names = set(before)
    after_names = set(after)
    added = sorted(after_names - before_names)
    removed = sorted(before_names - after_names)
    changed = sorted(
        name for name in before_names & after_names
        if before[name] != after[name]
    )
    return {"added": added, "removed": removed, "changed": changed}


def _symbol_map(meta) -> dict[str, str]:
    """Map symbols to their extracted source blocks."""
    if meta is None:
        return {}
    symbols: dict[str, str] = {}
    for func in meta.functions:
        symbols[func.qualified_name] = "\n".join(meta.lines[func.line_range.start - 1 : func.line_range.end])
    for cls in meta.classes:
        symbols[cls.name] = "\n".join(meta.lines[cls.line_range.start - 1 : cls.line_range.end])
    for sec in meta.sections:
        symbols[sec.title] = "\n".join(meta.lines[sec.line_range.start - 1 : sec.line_range.end])
    return symbols
