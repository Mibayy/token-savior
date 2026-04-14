"""Manages project slots: lazy loading, caching, incremental updates.

Extracted from server.py to reduce its size and isolate slot lifecycle logic.
"""

from __future__ import annotations

import dataclasses
import fnmatch
import hashlib
import os
import sys
import time
from typing import Optional, TYPE_CHECKING

from token_savior.cache_ops import CacheManager
from token_savior.git_tracker import is_git_repo, get_head_commit, get_changed_files
from token_savior.project_indexer import ProjectIndexer
from token_savior.query_api import create_project_query_functions

if TYPE_CHECKING:
    from token_savior.models import ProjectIndex


# ---------------------------------------------------------------------------
# Per-project slot dataclass
# ---------------------------------------------------------------------------

_STATS_DIR = os.path.expanduser(
    os.environ.get("TOKEN_SAVIOR_STATS_DIR", "~/.local/share/token-savior")
)


@dataclasses.dataclass
class _ProjectSlot:
    root: str
    indexer: Optional[ProjectIndexer] = None
    query_fns: Optional[dict] = None
    is_git: bool = False
    stats_file: str = ""
    cache: Optional[CacheManager] = None
    # Incremental update tracking
    _last_update_check: float = 0.0
    # Cache of directory mtimes for scandir-based optimization
    _dir_mtimes: dict[str, float] = dataclasses.field(default_factory=dict)
    # Monotonic counter bumped on any index mutation (full build or incremental).
    # Used as invalidation key for session-level result caches.
    cache_gen: int = 0


# ---------------------------------------------------------------------------
# Module-level helper
# ---------------------------------------------------------------------------


def _matches_include_patterns(rel_path: str, patterns: list[str]) -> bool:
    normalized = rel_path.replace(os.sep, "/")
    for pattern in patterns:
        if fnmatch.fnmatch(normalized, pattern):
            return True
    return False


def _get_stats_file(project_root: str) -> str:
    """Return path to the stats JSON file for this project."""
    slug = hashlib.md5(project_root.encode()).hexdigest()[:8]
    name = os.path.basename(project_root.rstrip("/"))
    return os.path.join(_STATS_DIR, f"{name}-{slug}.json")


# ---------------------------------------------------------------------------
# SlotManager
# ---------------------------------------------------------------------------


class SlotManager:
    """Manages project slots: lazy loading, caching, incremental updates."""

    def __init__(self, cache_version: int):
        self.projects: dict[str, _ProjectSlot] = {}
        self.active_root: str = ""
        self._cache_version = cache_version

    # -- helpers ----------------------------------------------------------

    def _cache_mgr(self, root: str) -> CacheManager:
        """Return a CacheManager for the given project root."""
        return CacheManager(root, self._cache_version)

    def _save_cache(self, index: ProjectIndex) -> None:
        """Persist the project index to JSON cache."""
        self._cache_mgr(index.root_path).save(index)

    # -- public API -------------------------------------------------------

    def register_roots(self, roots: list[str]) -> None:
        """Create slots for each root. Index is built lazily on first use."""
        for root in roots:
            if root not in self.projects:
                self.projects[root] = _ProjectSlot(root=root)
        if roots and not self.active_root:
            self.active_root = roots[0]

    def ensure(self, slot: _ProjectSlot) -> None:
        """Lazily initialize a project slot if not yet indexed."""
        if slot.indexer is not None:
            return

        root = slot.root
        slot.is_git = is_git_repo(root)
        slot.cache = self._cache_mgr(root)
        if not slot.stats_file:
            slot.stats_file = _get_stats_file(root)

        cached_index = slot.cache.load()
        if cached_index is not None and slot.is_git and cached_index.last_indexed_git_ref:
            current_head = get_head_commit(root)
            if current_head == cached_index.last_indexed_git_ref:
                print(f"[token-savior] Cache hit (git ref matches) -- {root}", file=sys.stderr)
                slot.indexer = ProjectIndexer(root)
                slot.indexer._project_index = cached_index
                slot.query_fns = create_project_query_functions(cached_index)
                return

            changeset = get_changed_files(root, cached_index.last_indexed_git_ref)
            total_changes = len(changeset.modified) + len(changeset.added) + len(changeset.deleted)
            if not changeset.is_empty and total_changes <= 20:
                print(
                    f"[token-savior] Cache hit with {total_changes} changed files, "
                    f"applying incremental update -- {root}",
                    file=sys.stderr,
                )
                slot.indexer = ProjectIndexer(root)
                slot.indexer._project_index = cached_index
                slot.query_fns = create_project_query_functions(cached_index)
                return

            print(
                f"[token-savior] Cache stale ({total_changes} changes), full rebuild -- {root}",
                file=sys.stderr,
            )

        self.build(slot)

    def build(self, slot: _ProjectSlot) -> None:
        """Full index build for a project slot."""
        root = slot.root
        if not slot.stats_file:
            slot.stats_file = _get_stats_file(root)

        print(f"[token-savior] Indexing project: {root}", file=sys.stderr)

        extra_excludes_raw = os.environ.get("EXCLUDE_EXTRA", "")
        exclude_override_raw = os.environ.get("EXCLUDE_PATTERNS", "")
        include_override_raw = os.environ.get("INCLUDE_PATTERNS", "")

        exclude_patterns = None
        include_patterns = None

        if exclude_override_raw:
            exclude_patterns = [p.strip() for p in exclude_override_raw.split(":") if p.strip()]
        elif extra_excludes_raw:
            tmp = ProjectIndexer(root)
            exclude_patterns = tmp.exclude_patterns + [
                p.strip() for p in extra_excludes_raw.split(":") if p.strip()
            ]

        if include_override_raw:
            include_patterns = [p.strip() for p in include_override_raw.split(":") if p.strip()]

        slot.indexer = ProjectIndexer(
            root, include_patterns=include_patterns, exclude_patterns=exclude_patterns
        )
        index = slot.indexer.index()
        slot.query_fns = create_project_query_functions(index)
        slot.cache_gen += 1

        if not slot.is_git:
            slot.is_git = is_git_repo(root)
        if slot.is_git:
            index.last_indexed_git_ref = get_head_commit(root)
            self._save_cache(index)

        print(
            f"[token-savior] Indexed {index.total_files} files, "
            f"{index.total_lines} lines, "
            f"{index.total_functions} functions, "
            f"{index.total_classes} classes "
            f"in {index.index_build_time_seconds:.2f}s -- {root}",
            file=sys.stderr,
        )

    def resolve(self, project_hint: str | None = None) -> tuple[_ProjectSlot | None, str]:
        """
        Return (slot, error_message). error_message is empty on success.

        Resolution order:
        1. explicit project_hint (basename or full path)
        2. active_root
        3. only registered project (if exactly one)
        4. error
        """
        if project_hint:
            # Try exact match first
            hint_abs = os.path.abspath(project_hint)
            if hint_abs in self.projects:
                return self.projects[hint_abs], ""
            # Try basename match
            for root, slot in self.projects.items():
                if os.path.basename(root) == project_hint:
                    return slot, ""
            return None, (
                f"Project '{project_hint}' not found. "
                f"Known projects: {', '.join(os.path.basename(r) for r in self.projects)}"
            )

        if self.active_root and self.active_root in self.projects:
            return self.projects[self.active_root], ""

        if len(self.projects) == 1:
            root = next(iter(self.projects))
            self.active_root = root
            return self.projects[root], ""

        if not self.projects:
            return None, "No projects registered. Call set_project_root('/path') first."

        return None, (
            "Multiple projects loaded but no active project set. "
            f"Call switch_project(name) with one of: "
            f"{', '.join(os.path.basename(r) for r in self.projects)}"
        )

    def maybe_update(self, slot: _ProjectSlot) -> None:
        """Incrementally update the slot index using mtime detection + periodic git check."""
        if slot.indexer is None or slot.indexer._project_index is None:
            return

        idx = slot.indexer._project_index
        now = time.time()
        _dirty = False

        # -- Phase 1: mtime check (near-zero cost, every call) -------------
        mtime_changed = self.check_mtime_changes(slot)
        if mtime_changed:
            for rel_path in mtime_changed:
                abs_path = os.path.join(slot.root, rel_path)
                if not os.path.isfile(abs_path):
                    continue
                slot.indexer.reindex_file(rel_path, skip_graph_rebuild=True)

            slot.indexer.rebuild_graphs()
            slot.cache_gen += 1
            print(
                f"[token-savior] Mtime update: {len(mtime_changed)} file(s) -- {slot.root}",
                file=sys.stderr,
            )
            _dirty = True
            # Reset the git throttle so we don't double-detect these
            slot._last_update_check = now

        # -- Phase 2: git check for new/deleted/branch changes (throttled) -
        if not slot.is_git:
            if _dirty:
                self._save_cache(idx)
            return

        if now - slot._last_update_check < 30:
            if _dirty:
                self._save_cache(idx)
            return
        slot._last_update_check = now

        if idx.last_indexed_git_ref is None:
            head = get_head_commit(slot.root)
            if head is not None:
                idx.last_indexed_git_ref = head
                _dirty = True
            if _dirty:
                self._save_cache(idx)
            return

        changeset = get_changed_files(slot.root, idx.last_indexed_git_ref)
        if changeset.is_empty:
            if _dirty:
                self._save_cache(idx)
            return

        total_changes = len(changeset.modified) + len(changeset.added) + len(changeset.deleted)

        if total_changes > 20 and total_changes > idx.total_files * 0.5:
            print(
                f"[token-savior] Large changeset ({total_changes} files), "
                f"doing full rebuild -- {slot.root}",
                file=sys.stderr,
            )
            self.build(slot)
            return

        # Filter out files already handled by mtime phase
        already_handled = set(mtime_changed) if mtime_changed else set()

        for path in changeset.deleted:
            if path in idx.files:
                slot.indexer.remove_file(path)

        needs_rebuild = False
        for path in changeset.modified + changeset.added:
            if path in already_handled:
                continue
            if slot.indexer._is_excluded(path):
                continue
            if not _matches_include_patterns(path, slot.indexer.include_patterns):
                continue
            abs_path = os.path.join(slot.root, path)
            if not os.path.isfile(abs_path):
                continue
            slot.indexer.reindex_file(path, skip_graph_rebuild=True)
            needs_rebuild = True

        if needs_rebuild or changeset.deleted:
            slot.indexer.rebuild_graphs()
            slot.cache_gen += 1

        idx.last_indexed_git_ref = get_head_commit(slot.root)

        n_mod = len(changeset.modified)
        n_add = len(changeset.added)
        n_del = len(changeset.deleted)
        print(
            f"[token-savior] Incremental update: "
            f"{n_mod} modified, {n_add} added, {n_del} deleted -- {slot.root}",
            file=sys.stderr,
        )
        self._save_cache(idx)

    def check_mtime_changes(self, slot: _ProjectSlot) -> list[str]:
        """Fast mtime scan using os.scandir() to batch stat calls per directory.

        Instead of calling os.path.getmtime() once per indexed file (N syscalls),
        we group files by parent directory and use os.scandir() per directory
        (D syscalls where D << N). Each scandir() yields DirEntry objects whose
        .stat() result is often cached from the single getdents+fstat batch on Linux.
        """
        idx = slot.indexer._project_index
        if not idx.file_mtimes:
            return []

        # Group indexed files by parent directory
        dir_files: dict[str, list[str]] = {}
        for rel_path in idx.file_mtimes:
            parent = os.path.dirname(rel_path)
            dir_files.setdefault(parent, []).append(rel_path)

        changed = []
        for dir_rel, rel_paths in dir_files.items():
            abs_dir = os.path.join(slot.root, dir_rel) if dir_rel else slot.root
            try:
                # Single syscall to get all entries + their stat info
                entry_mtimes: dict[str, float] = {}
                with os.scandir(abs_dir) as it:
                    for entry in it:
                        try:
                            st = entry.stat(follow_symlinks=False)
                            entry_mtimes[entry.name] = st.st_mtime
                        except OSError:
                            continue
            except OSError:
                # Directory removed or inaccessible -- skip all its files
                continue

            for rel_path in rel_paths:
                fname = os.path.basename(rel_path)
                current_mtime = entry_mtimes.get(fname)
                if current_mtime is None:
                    # File deleted -- will be caught by git check
                    continue
                if current_mtime != idx.file_mtimes[rel_path]:
                    changed.append(rel_path)

        return changed
