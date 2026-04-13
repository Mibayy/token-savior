"""Persistent JSON cache for project indexes.

Handles serialization, deserialization, and legacy filename migration.
"""

from __future__ import annotations

import json
import os
import sys
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from token_savior.models import ProjectIndex


class CacheManager:
    """Manages persistent JSON cache for a project index."""

    FILENAME = ".token-savior-cache.json"
    LEGACY_FILENAME = ".codebase-index-cache.json"

    def __init__(self, root_path: str, cache_version: int):
        self.root_path = root_path
        self.cache_version = cache_version

    def path(self) -> str:
        """Return cache file path, auto-migrating legacy filename."""
        new_path = os.path.join(self.root_path, self.FILENAME)
        if not os.path.exists(new_path):
            legacy = os.path.join(self.root_path, self.LEGACY_FILENAME)
            if os.path.exists(legacy):
                try:
                    os.rename(legacy, new_path)
                    print(
                        f"[token-savior] Migrated cache {self.LEGACY_FILENAME} -> {self.FILENAME}",
                        file=sys.stderr,
                    )
                except OSError:
                    return legacy  # fallback to old name if rename fails
        return new_path

    def save(self, index: ProjectIndex) -> None:
        """Persist the project index to JSON cache."""
        try:
            path = self.path()
            payload = {"version": self.cache_version, "index": self.index_to_dict(index)}
            with open(path, "w", encoding="utf-8") as f:
                json.dump(payload, f, separators=(",", ":"))
            print(f"[token-savior] Cache saved -> {path}", file=sys.stderr)
        except Exception as exc:
            print(f"[token-savior] Cache save failed: {exc}", file=sys.stderr)

    def load(self) -> ProjectIndex | None:
        """Load cached project index, or None if missing/incompatible."""
        path = self.path()
        if not os.path.exists(path):
            return None
        try:
            with open(path, "r", encoding="utf-8") as f:
                payload = json.load(f)
            if not isinstance(payload, dict) or payload.get("version") != self.cache_version:
                print("[token-savior] Cache version mismatch, ignoring", file=sys.stderr)
                return None
            raw_index = payload["index"]
            # Back-compat guard for the symbol-hash feature: pre-hash caches
            # lack symbol_hashes and would produce 0% savings on reindex_file.
            # Invalidate them so the next indexing pass repopulates hashes.
            if "symbol_hashes" not in raw_index:
                print("[token-savior] Cache missing symbol_hashes, invalidating", file=sys.stderr)
                return None
            return self.index_from_dict(raw_index)
        except Exception as exc:
            print(f"[token-savior] Cache load failed: {exc}", file=sys.stderr)
            return None

    @staticmethod
    def index_to_dict(index: ProjectIndex) -> dict:
        """Serialize a ProjectIndex to a JSON-compatible dict (sets become sorted lists).

        Manual serialization to avoid dataclasses.asdict() which deep-copies every
        field -- including large lines[] and line_char_offsets[] arrays -- causing a
        ~2x memory peak.  Primitive lists are referenced directly instead of copied.
        """

        def _lr(lr) -> dict:
            return {"start": lr.start, "end": lr.end}

        def _fi(fi) -> dict:
            return {
                "name": fi.name,
                "qualified_name": fi.qualified_name,
                "line_range": _lr(fi.line_range),
                "parameters": fi.parameters,
                "decorators": fi.decorators,
                "docstring": fi.docstring,
                "is_method": fi.is_method,
                "parent_class": fi.parent_class,
                "signature_hash": fi.signature_hash,
                "body_hash": fi.body_hash,
            }

        def _ci(ci) -> dict:
            return {
                "name": ci.name,
                "line_range": _lr(ci.line_range),
                "base_classes": ci.base_classes,
                "methods": [_fi(m) for m in ci.methods],
                "decorators": ci.decorators,
                "docstring": ci.docstring,
                "body_hash": ci.body_hash,
            }

        def _ii(ii) -> dict:
            return {
                "module": ii.module,
                "names": ii.names,
                "alias": ii.alias,
                "line_number": ii.line_number,
                "is_from_import": ii.is_from_import,
            }

        def _si(si) -> dict:
            return {"title": si.title, "level": si.level, "line_range": _lr(si.line_range)}

        def _sm(sm) -> dict:
            return {
                "source_name": sm.source_name,
                "total_lines": sm.total_lines,
                "total_chars": sm.total_chars,
                # lines[] and line_char_offsets[] are NOT persisted — they are
                # lazy-loaded from disk on demand, saving ~80% cache size and
                # idle RAM.
                "functions": [_fi(f) for f in sm.functions],
                "classes": [_ci(c) for c in sm.classes],
                "imports": [_ii(i) for i in sm.imports],
                "sections": [_si(s) for s in sm.sections],
                "dependency_graph": sm.dependency_graph,    # already dict[str, list[str]]
            }

        def _sets_to_sorted(d: dict) -> dict:
            return {k: sorted(v) for k, v in d.items()}

        return {
            "root_path": index.root_path,
            "files": {k: _sm(v) for k, v in index.files.items()},
            "global_dependency_graph": _sets_to_sorted(index.global_dependency_graph),
            "reverse_dependency_graph": _sets_to_sorted(index.reverse_dependency_graph),
            "import_graph": _sets_to_sorted(index.import_graph),
            "reverse_import_graph": _sets_to_sorted(index.reverse_import_graph),
            "symbol_table": index.symbol_table,
            "total_files": index.total_files,
            "total_lines": index.total_lines,
            "total_functions": index.total_functions,
            "total_classes": index.total_classes,
            "index_build_time_seconds": index.index_build_time_seconds,
            "index_memory_bytes": index.index_memory_bytes,
            "last_indexed_git_ref": index.last_indexed_git_ref,
            "file_mtimes": index.file_mtimes,
            "symbol_hashes": index.symbol_hashes,
        }

    @staticmethod
    def index_from_dict(data: dict) -> ProjectIndex:
        """Deserialize a ProjectIndex from JSON dict, restoring sets where needed."""
        from token_savior.models import (
            ProjectIndex,
            StructuralMetadata,
            FunctionInfo,
            ClassInfo,
            ImportInfo,
            SectionInfo,
            LineRange,
            LazyLines,
        )

        def _lr(d: dict) -> LineRange:
            return LineRange(start=d["start"], end=d["end"])

        def _fi(d: dict) -> FunctionInfo:
            return FunctionInfo(
                name=d["name"],
                qualified_name=d["qualified_name"],
                line_range=_lr(d["line_range"]),
                parameters=d["parameters"],
                decorators=d["decorators"],
                docstring=d.get("docstring"),
                is_method=d["is_method"],
                parent_class=d.get("parent_class"),
                signature_hash=d.get("signature_hash", ""),
                body_hash=d.get("body_hash", ""),
            )

        def _ci(d: dict) -> ClassInfo:
            return ClassInfo(
                name=d["name"],
                line_range=_lr(d["line_range"]),
                base_classes=d["base_classes"],
                methods=[_fi(m) for m in d["methods"]],
                decorators=d["decorators"],
                docstring=d.get("docstring"),
                body_hash=d.get("body_hash", ""),
            )

        def _ii(d: dict) -> ImportInfo:
            return ImportInfo(
                module=d["module"],
                names=d["names"],
                alias=d.get("alias"),
                line_number=d["line_number"],
                is_from_import=d["is_from_import"],
            )

        def _si(d: dict) -> SectionInfo:
            return SectionInfo(title=d["title"], level=d["level"], line_range=_lr(d["line_range"]))

        root_path = data["root_path"]

        def _sm(d: dict, rel_path: str) -> StructuralMetadata:
            return StructuralMetadata(
                source_name=d["source_name"],
                total_lines=d["total_lines"],
                total_chars=d["total_chars"],
                # Lazy-load lines from disk instead of storing in memory.
                # Backward-compat: old caches that still include "lines"
                # are ignored — we always read from the source file.
                lines=LazyLines(root_path=root_path, rel_path=rel_path),
                line_char_offsets=[],  # Not used at query time
                functions=[_fi(f) for f in d.get("functions", [])],
                classes=[_ci(c) for c in d.get("classes", [])],
                imports=[_ii(i) for i in d.get("imports", [])],
                sections=[_si(s) for s in d.get("sections", [])],
                dependency_graph=d.get("dependency_graph", {}),
            )

        def _sets(d: dict) -> dict[str, set[str]]:
            return {k: set(v) for k, v in d.items()}

        return ProjectIndex(
            root_path=root_path,
            files={k: _sm(v, k) for k, v in data["files"].items()},
            global_dependency_graph=_sets(data.get("global_dependency_graph", {})),
            reverse_dependency_graph=_sets(data.get("reverse_dependency_graph", {})),
            import_graph=_sets(data.get("import_graph", {})),
            reverse_import_graph=_sets(data.get("reverse_import_graph", {})),
            symbol_table=data.get("symbol_table", {}),
            total_files=data.get("total_files", 0),
            total_lines=data.get("total_lines", 0),
            total_functions=data.get("total_functions", 0),
            total_classes=data.get("total_classes", 0),
            index_build_time_seconds=data.get("index_build_time_seconds", 0.0),
            index_memory_bytes=data.get("index_memory_bytes", 0),
            last_indexed_git_ref=data.get("last_indexed_git_ref"),
            file_mtimes=data.get("file_mtimes", {}),
            symbol_hashes=data.get("symbol_hashes", {}),
        )
