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
from typing import TYPE_CHECKING

from token_savior import telemetry
from token_savior.cache_ops import CacheManager, compute_config_key
from token_savior.git_tracker import get_changed_files, get_head_commit, is_git_repo
from token_savior.project_indexer import ProjectIndexer, _rebuild_path_indexes
from token_savior.query_api import create_project_query_functions
from token_savior.watcher import SlotWatcher
from token_savior.watcher import resolve_mode as resolve_watcher_mode

if TYPE_CHECKING:
    from token_savior.models import ProjectIndex


# ---------------------------------------------------------------------------
# Per-project slot dataclass
# ---------------------------------------------------------------------------

def _stats_dir() -> str:
    """Ou ce module ecrit, demande au moment ou il ecrit (#90).

    `_STATS_DIR` etait fige a l'import, donc `TOKEN_SAVIOR_STATS_DIR` pose
    apres ce premier import ne comptait plus ici alors qu'il comptait dans
    `telemetry` : le meme processus ecrivait a deux endroits.
    """
    return str(telemetry.stats_dir())


@dataclasses.dataclass
class _ProjectSlot:
    root: str
    indexer: ProjectIndexer | None = None
    query_fns: dict | None = None
    is_git: bool = False
    stats_file: str = ""
    cache: CacheManager | None = None
    # Incremental update tracking
    _last_update_check: float = 0.0
    # Cache of directory mtimes for scandir-based optimization
    _dir_mtimes: dict[str, float] = dataclasses.field(default_factory=dict)
    # Monotonic counter bumped on any index mutation (full build or incremental).
    # Used as invalidation key for session-level result caches.
    cache_gen: int = 0
    # Optional file watcher (TOKEN_SAVIOR_WATCHER=on|auto). None when off,
    # when the watchfiles dep is missing, or when the watcher failed to
    # start. In those cases the legacy mtime phase of maybe_update runs.
    watcher: SlotWatcher | None = None


# ---------------------------------------------------------------------------
# Module-level helper
# ---------------------------------------------------------------------------


def _matches_include_patterns(rel_path: str, patterns: list[str]) -> bool:
    """Return True iff ``rel_path`` matches any include glob.

    Python's ``fnmatch`` does NOT implement ``**`` as a recursive globstar —
    it treats ``**`` as a single ``*`` that requires at least one path
    separator. So ``fnmatch("foo.py", "**/*.py")`` is False, which means
    a file created at the project root wouldn't match any default
    ``**/*.ext`` pattern.

    For ``**/`` -prefixed patterns we therefore also try the bare form
    (pattern without the ``**/`` prefix) so root-level files match.
    Seen in practice when the B3 file watcher detects a newly created
    ``foo.py`` at project root — without this fallback the watcher
    would drop the event silently.
    """
    normalized = rel_path.replace(os.sep, "/")
    for pattern in patterns:
        if fnmatch.fnmatch(normalized, pattern):
            return True
        if pattern.startswith("**/") and fnmatch.fnmatch(normalized, pattern[3:]):
            return True
    return False


def _get_stats_file(project_root: str) -> str:
    """Return path to the stats JSON file for this project."""
    slug = hashlib.md5(project_root.encode()).hexdigest()[:8]
    name = os.path.basename(project_root.rstrip("/"))
    return os.path.join(_stats_dir(), f"{name}-{slug}.json")


# --- Registered-project persistence ---------------------------------------
# stdio clients respawn the server every session, so anything registered via
# set_project_root used to vanish on restart -- the agent then re-ran
# set_project_root (a full reindex) every session. Audit 2026-07-04 measured
# 51 set_project_root calls in 5.5 weeks (p95 1.8s, one 14.6s outlier), with
# collector-crypt-scanner reindexed 20x. Persisting the registry here lets
# switch_project reach those projects across restarts (cache-aware, no rebuild).

# Point de surcharge : `test_registered_persistence` le rebinde par cas. Compare
# a sa valeur d'import pour distinguer « personne n'a rien impose, redemande a
# l'environnement » de « quelqu'un a impose un chemin, il gagne » -- meme forme
# que `db_core.chemin_memoire()`.
_REGISTERED_ROOTS_INITIAL = os.path.join(
    os.path.expanduser(
        os.environ.get("TOKEN_SAVIOR_STATS_DIR", "~/.local/share/token-savior")
    ),
    "registered_projects.json",
)
_REGISTERED_ROOTS_FILE = _REGISTERED_ROOTS_INITIAL


def _registered_roots_file() -> str:
    if _REGISTERED_ROOTS_FILE != _REGISTERED_ROOTS_INITIAL:
        return _REGISTERED_ROOTS_FILE
    return os.path.join(_stats_dir(), "registered_projects.json")


def _load_registered_roots() -> list[str]:
    """Return persisted project roots that still exist on disk (best-effort)."""
    import json
    try:
        with open(_registered_roots_file(), encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return []
    if not isinstance(data, list):
        return []
    return [p for p in data if isinstance(p, str) and os.path.isdir(p)]


def _persist_registered_root(root: str) -> None:
    """Append ``root`` to the persisted registry (idempotent, atomic, silent)."""
    import json
    root = os.path.abspath(root)
    try:
        existing = _load_registered_roots()
        if root in existing:
            return
        existing.append(root)
        cible = _registered_roots_file()
        os.makedirs(os.path.dirname(cible), exist_ok=True)
        tmp = cible + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(existing, f)
        os.replace(tmp, cible)
    except OSError:
        pass


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
        return CacheManager(root, self._cache_version, config_key=compute_config_key())

    @staticmethod
    def _make_indexer(root: str) -> ProjectIndexer:
        """ProjectIndexer honouring the env pattern overrides.

        Single resolution point for build() AND the cache-hit paths — a
        cache-hit indexer built with defaults would re-index incremental
        changes under the wrong patterns (#61).
        """
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

        return ProjectIndexer(
            root, include_patterns=include_patterns, exclude_patterns=exclude_patterns
        )

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
                slot.indexer = self._make_indexer(root)
                slot.indexer._project_index = cached_index
                if not cached_index.sorted_paths or not cached_index.basename_map:
                    _rebuild_path_indexes(cached_index)
                if not cached_index.normalized_symbol_index:
                    cached_index.normalized_symbol_index = slot.indexer._build_normalized_symbol_index(
                        cached_index.symbol_table
                    )
                slot.query_fns = create_project_query_functions(cached_index)
                slot.cache_gen += 1
                return

            changeset = get_changed_files(root, cached_index.last_indexed_git_ref)
            total_changes = len(changeset.modified) + len(changeset.added) + len(changeset.deleted)
            if not changeset.is_empty and total_changes <= 20:
                print(
                    f"[token-savior] Cache hit with {total_changes} changed files, "
                    f"applying incremental update -- {root}",
                    file=sys.stderr,
                )
                slot.indexer = self._make_indexer(root)
                slot.indexer._project_index = cached_index
                if not cached_index.sorted_paths or not cached_index.basename_map:
                    _rebuild_path_indexes(cached_index)
                if not cached_index.normalized_symbol_index:
                    cached_index.normalized_symbol_index = slot.indexer._build_normalized_symbol_index(
                        cached_index.symbol_table
                    )
                slot.query_fns = create_project_query_functions(cached_index)
                slot.cache_gen += 1
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

    def resolve_path(self, path: str) -> _ProjectSlot | None:
        """Route an absolute filesystem path to the project that OWNS it.

        Nearest marker root wins, so a path inside a worktree nested in a
        registered repo (`repo/.claude/worktrees/wt/src/x.py`) routes to the
        worktree slot — a linked worktree carries a `.git` *file*, which
        counts as a marker — never to the parent checkout that happens to
        contain it. An unregistered root is registered on the spot.

        Deliberately does NOT touch ``active_root``: this is the per-call
        routing signal parallel agents (several worktrees, one server) rely
        on, and mutating the shared default from one agent's call is exactly
        the race this method exists to avoid.
        """
        if not path or not os.path.isabs(path):
            return None
        # Lazy import: server_runtime pulls in server_state, which imports
        # this module — a top-level import would be circular.
        from token_savior.server_runtime import project_root_of
        root = project_root_of(path)
        if not root:
            return None
        if root not in self.projects:
            try:
                self.register_roots([root])
            except Exception:
                return None
        return self.projects.get(root)

    def _promouvoir(self, root: str) -> None:
        """Faire de `root` le defaut partage — sauf si le gel est demande.

        `TS_STICKY_ACTIVE` existe pour qu'un agent parallele ne repointe pas
        le projet actif des autres. Le gel etait applique au niveau du
        dispatch (`server_state.noter_racine_active`), donc contourne ici :
        un `project=<chemin jamais enregistre>` promouvait quand meme, et le
        prochain appel sans `project=` tombait sur le mauvais depot. Mesure
        le 09/08/2026 avec `TS_STICKY_ACTIVE=1` : la garantie ne tenait pas
        sur cette branche precisement.

        On lit le drapeau de `server_state` quand il est importable, pour
        qu'il n'y ait qu'une source de verite ; sinon l'environnement, pour
        que le gestionnaire reste utilisable seul (tests unitaires, CLI).
        """
        try:
            from token_savior import server_state

            gel = bool(server_state._STICKY_ACTIVE)
        except Exception:
            gel = os.environ.get("TS_STICKY_ACTIVE", "0").lower() in ("1", "true", "on")
        if gel:
            return
        self.active_root = root

    def resolve(self, project_hint: str | None = None) -> tuple[_ProjectSlot | None, str]:
        """
        Return (slot, error_message). error_message is empty on success.

        Resolution order:
        1. explicit project_hint (full path — routed to the project that
           owns it, nearest marker root first, worktree-aware — or basename)
        2. active_root
        3. only registered project (if exactly one)
        4. error
        """
        if project_hint:
            # Try exact match first
            hint_abs = os.path.abspath(project_hint)
            if hint_abs in self.projects:
                return self.projects[hint_abs], ""
            # Path-like hint: route to the OWNING project before any name
            # fuzzing. `repo/.claude/worktrees/wt` must reach the worktree
            # slot, and a subdirectory of a registered project must resolve
            # to that project — not get registered as a project of its own.
            if os.sep in project_hint:
                slot = self.resolve_path(hint_abs)
                if slot is not None:
                    return slot, ""
            # Try basename match
            for root, slot in self.projects.items():
                if os.path.basename(root) == project_hint:
                    return slot, ""
            # Case-insensitive basename match (often the agent says
            # "MyProject" or "myproject" with different casing).
            hint_lower = project_hint.lower()
            for root, slot in self.projects.items():
                if os.path.basename(root).lower() == hint_lower:
                    return slot, ""
            # Substring fuzzy match (one unique candidate only — bail if
            # ambiguous so we don't silently switch to the wrong project).
            substring_matches = [
                (root, slot) for root, slot in self.projects.items()
                if hint_lower in os.path.basename(root).lower()
            ]
            if len(substring_matches) == 1:
                return substring_matches[0][1], ""
            # Containment inverse : le hint est plus LONG que le nom du projet.
            # Mesure : `scribe-transcription` ne trouvait jamais `scribe`.
            # Nom le plus long d'abord, pour que `api` ne rafle pas
            # `api-client` ; moins de 4 caracteres ignore, ca matcherait tout.
            reverse = [
                (root, slot) for root, slot in self.projects.items()
                if os.path.basename(root).lower() in hint_lower
                and len(os.path.basename(root)) >= 4
            ]
            if reverse:
                reverse.sort(key=lambda rs: len(os.path.basename(rs[0])), reverse=True)
                best = len(os.path.basename(reverse[0][0]))
                if sum(1 for r, _ in reverse if len(os.path.basename(r)) == best) == 1:
                    return reverse[0][1], ""
            # Un chemin reel que personne n'a enregistre : le refuser envoyait
            # vers set_project_root sans raison. On sait ou il est, il existe.
            # resolve_path d'abord : si le dossier appartient a un projet (ou
            # est un worktree), c'est SA racine qu'on enregistre, pas le
            # sous-dossier. Sans marqueur nulle part, on enregistre tel quel.
            if os.path.isdir(hint_abs):
                slot = self.resolve_path(hint_abs)
                if slot is not None:
                    self._promouvoir(slot.root)
                    return slot, ""
                try:
                    self.register_roots([*self.projects, hint_abs])
                    if hint_abs in self.projects:
                        self._promouvoir(hint_abs)
                        return self.projects[hint_abs], ""
                except Exception:
                    pass
            # Build a "did you mean?" suggestion based on edit distance.
            import difflib
            basenames = [os.path.basename(r) for r in self.projects]
            close = difflib.get_close_matches(project_hint, basenames, n=3, cutoff=0.5)
            suggestion = (
                f" Did you mean: {', '.join(close)}?"
                if close
                else ""
            )
            ambiguous_note = ""
            if len(substring_matches) > 1:
                multi = [os.path.basename(r) for r, _ in substring_matches]
                ambiguous_note = (
                    f" Multiple substring matches: {', '.join(multi)} — "
                    "be more specific."
                )
            # Le nom correspond peut-etre a un dossier existant mais non
            # indexe (sauvegarde, archive). Le dire transforme un cul-de-sac
            # en information.
            ailleurs = ""
            try:
                for parent in sorted({os.path.dirname(r) for r in self.projects}):
                    candidat = os.path.join(parent, project_hint)
                    if os.path.isdir(candidat):
                        ailleurs = (f" A directory '{candidat}' exists but is not "
                                    f"registered: set_project_root('{candidat}').")
                        break
            except OSError:
                ailleurs = ""
            return None, (
                f"Project '{project_hint}' not found.{suggestion}"
                f"{ambiguous_note}{ailleurs} "
                f"Known projects: {', '.join(basenames)}"
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

    def _ensure_watcher(self, slot: _ProjectSlot) -> None:
        """Lazy-start the file watcher for this slot on first update call.

        Honors ``TOKEN_SAVIOR_WATCHER``:
          - ``off`` : never start a watcher; stay on the legacy mtime path.
          - ``on``  : must succeed — on failure, log and propagate (no
                       fallback, the user explicitly asked for it).
          - ``auto`` (default) : try to start; on failure, log and fall
                       back to mtime. Subsequent calls don't retry.
        """
        if slot.watcher is not None:
            return
        mode = resolve_watcher_mode()
        if mode == "off":
            return
        if slot.indexer is None:
            return
        exclude = list(getattr(slot.indexer, "exclude_patterns", []) or [])
        w = SlotWatcher(root=slot.root, exclude_patterns=exclude)
        started = w.start()
        if started:
            slot.watcher = w
            return
        if mode == "on":
            # The user explicitly asked for the watcher — raise so the
            # failure is not silently swept under the rug.
            raise RuntimeError(
                f"TOKEN_SAVIOR_WATCHER=on requested but watcher failed to "
                f"start: {w.failure_reason}"
            )
        # mode == "auto": remember the failure so we don't retry every call,
        # and let the mtime path carry the session.
        slot.watcher = w  # ok is False, maybe_update will skip the watcher path

    def maybe_update(self, slot: _ProjectSlot) -> None:
        """Incrementally update the slot index using mtime detection + periodic git check.

        When a file watcher is active (``TOKEN_SAVIOR_WATCHER`` is ``on``
        or ``auto`` and the thread is healthy), Phase 1 drains events
        from the watcher instead of running mtime stats. The mtime path
        stays as a fallback for ``off`` mode, unavailable ``watchfiles``
        package, inotify overflow, or other runtime watcher failures.
        """
        if slot.indexer is None or slot.indexer._project_index is None:
            return

        self._ensure_watcher(slot)

        idx = slot.indexer._project_index
        now = time.time()
        _dirty = False
        mtime_changed: list[str] = []

        # -- Phase 1: prefer watcher events, fall back to mtime scan -------
        if slot.watcher is not None and slot.watcher.ok:
            dirty, deleted = slot.watcher.drain()
            # Filter to paths the indexer cares about, and apply the same
            # exclude/include gates the indexer uses during its walk.
            for rel_path in deleted:
                if rel_path in idx.files:
                    slot.indexer.remove_file(rel_path)
                    _dirty = True
            any_dirty = False
            for rel_path in dirty:
                abs_path = os.path.join(slot.root, rel_path)
                if not os.path.isfile(abs_path):
                    continue
                if slot.indexer._is_excluded(rel_path):
                    continue
                if not _matches_include_patterns(
                    rel_path, slot.indexer.include_patterns
                ):
                    continue
                slot.indexer.reindex_file(rel_path, skip_graph_rebuild=True)
                any_dirty = True
            if any_dirty or deleted:
                slot.indexer.rebuild_graphs()
                slot.cache_gen += 1
                print(
                    f"[token-savior] Watcher update: "
                    f"{len(dirty)} changed, {len(deleted)} deleted "
                    f"-- {slot.root}",
                    file=sys.stderr,
                )
                _dirty = True
                slot._last_update_check = now
            # Record "already handled" for the git phase below so we
            # don't reindex the same file twice.
            mtime_changed = list(dirty)
        else:
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
