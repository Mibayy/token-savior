"""Token Savior — project-wide indexer.

Walks a project directory, annotates each file using the dispatch annotator,
builds cross-file dependency graphs, import graphs, and a global symbol table.
"""

import fnmatch
import logging
import os
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

from token_savior.annotator import annotate
from token_savior.models import ProjectIndex, StructuralMetadata
from token_savior.symbol_hash import fill_hashes

logger = logging.getLogger(__name__)


def _rebuild_path_indexes(idx: ProjectIndex) -> None:
    """Refresh sorted_paths and basename_map after idx.files mutates."""
    idx.sorted_paths = sorted(idx.files.keys())
    bmap: dict[str, list[str]] = {}
    for path in idx.sorted_paths:
        base = os.path.basename(path)
        bmap.setdefault(base, []).append(path)
    idx.basename_map = bmap


_WORD_BOUNDARY_CACHE: dict[str, re.Pattern] = {}
_JAVA_METHOD_REFERENCE_PATTERN = re.compile(r"(?<![\w$])([A-Za-z_][\w.]*)::([A-Za-z_]\w*)")
_SPRING_CLASS_DECORATORS = frozenset(
    {
        "RestController",
        "Controller",
        "RequestMapping",
        "Configuration",
        "ConfigurationProperties",
        "Service",
        "Component",
        "Repository",
        "SpringBootApplication",
    }
)
_SPRING_METHOD_DECORATORS = frozenset(
    {
        "Bean",
        "GetMapping",
        "PostMapping",
        "PutMapping",
        "PatchMapping",
        "DeleteMapping",
        "RequestMapping",
    }
)
_JAVA_LIFECYCLE_DECORATORS = frozenset({"PreDestroy", "PostConstruct"})
_JAVA_DYNAMIC_DISPATCH_BASES = frozenset({"Runnable", "Thread"})
_LOCAL_SCOPE_PREFIX = "::<local>"


def _parse_gitignore(root_path: str) -> list[str]:
    """Read .gitignore from root_path and convert entries to fnmatch exclude patterns.

    Handles the most common gitignore syntax:
    - blank lines and ``#`` comments are skipped
    - ``!`` negation patterns are skipped (too complex to invert safely)
    - a trailing ``/`` marks a directory-only pattern
    - a leading ``/`` marks a root-relative pattern; stripped before conversion
    - all other patterns are treated as anywhere-in-tree patterns

    Returns a list of patterns compatible with ``_is_excluded`` / fnmatch.
    """
    gitignore = os.path.join(root_path, ".gitignore")
    if not os.path.isfile(gitignore):
        return []

    patterns: list[str] = []
    try:
        with open(gitignore, encoding="utf-8") as fh:
            for raw in fh:
                line = raw.rstrip("\n").rstrip("\r")

                # skip comments and blanks
                if not line or line.startswith("#"):
                    continue
                # skip negation patterns
                if line.startswith("!"):
                    continue

                is_dir_only = line.endswith("/")
                # strip leading/trailing slashes for normalization
                line = line.strip("/")
                if not line:
                    continue

                if is_dir_only:
                    # match the directory itself and anything inside it
                    patterns.append(f"**/{line}/**")
                    patterns.append(f"{line}/**")
                else:
                    # file or generic pattern: match anywhere in tree
                    patterns.append(f"**/{line}")
                    patterns.append(f"**/{line}/**")
    except (OSError, UnicodeDecodeError) as exc:
        logger.debug("Could not read .gitignore at %s: %s", gitignore, exc)

    return patterns


def _word_boundary_re(name: str) -> re.Pattern:
    pat = _WORD_BOUNDARY_CACHE.get(name)
    if pat is None:
        pat = re.compile(r"\b" + re.escape(name) + r"\b")
        _WORD_BOUNDARY_CACHE[name] = pat
    return pat


class ProjectIndexer:
    """Indexes an entire codebase for structural navigation."""

    def __init__(
        self,
        root_path: str,
        include_patterns: list[str] | None = None,
        exclude_patterns: list[str] | None = None,
        max_file_size_bytes: int = 500_000,
        max_files: int = 10_000,
    ):
        self.root_path = os.path.abspath(root_path)
        self.include_patterns = include_patterns or [
            "**/*.py",
            "**/*.ts",
            "**/*.tsx",
            "**/*.js",
            "**/*.jsx",
            "**/*.go",
            "**/*.rs",
            "**/*.c",
            "**/*.h",
            "**/*.glsl",
            "**/*.vert",
            "**/*.frag",
            "**/*.comp",
            "**/*.cs",
            "**/*.java",
            "**/*.gradle",
            "**/*.gradle.kts",
            "**/*.md",
            "**/*.txt",
            "**/*.json",
            "**/*.yaml",
            "**/*.yml",
            "**/*.toml",
            "**/*.ini",
            "**/*.cfg",
            "**/*.properties",
            "**/.env",
            "**/.env.*",
            "**/*.xml",
            "**/*.plist",
            "**/*.hcl",
            "**/*.tf",
            "**/*.conf",
            "**/Dockerfile",
            "**/Dockerfile.*",
            "**/*.dockerfile",
        ]
        self.exclude_patterns = exclude_patterns or [
            "**/__pycache__/**",
            ".gradle/**",
            "**/.gradle/**",
            ".idea/**",
            "**/.idea/**",
            "**/node_modules/**",
            "**/.git/**",
            "**/.venv/**",
            "**/venv/**",
            "**/*.egg-info/**",
            "target/**",
            "**/target/**",
            "**/vendor/**",
            "**/package-lock.json",
            "**/.package-lock.json",
            "**/composer.lock",
            # Next.js / build output dirs
            "**/.next/**",
            "dist/**",
            "**/dist/**",
            "build/**",
            "**/build/**",
            "**/.turbo/**",
            "**/.vercel/**",
            # Coverage / test artifacts
            "**/coverage/**",
            "**/.nyc_output/**",
            # C/C++ build artifacts
            "**/_deps/**",
            # Claude Code worktrees (duplicates of the project)
            "**/.claude/worktrees/**",
            # git worktrees checked out inside the repo
            "**/.worktrees/**",
        ]
        self.max_file_size_bytes = max_file_size_bytes or int(
            os.environ.get("TOKEN_SAVIOR_MAX_FILE_SIZE", "500000")
        )
        self.max_files = max_files or int(
            os.environ.get("TOKEN_SAVIOR_MAX_FILES", "10000")
        )

        # Append patterns from .gitignore (when caller didn't supply custom excludes)
        if exclude_patterns is None:
            self.exclude_patterns.extend(_parse_gitignore(self.root_path))

        # Append patterns from TOKEN_SAVIOR_EXCLUDE_PATTERNS env var (colon-separated)
        env_excludes = os.environ.get("TOKEN_SAVIOR_EXCLUDE_PATTERNS", "")
        if env_excludes:
            self.exclude_patterns.extend(
                p.strip() for p in env_excludes.split(":") if p.strip()
            )

        self._project_index: ProjectIndex | None = None

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def index(self) -> ProjectIndex:
        """Walk the project, annotate all files, build cross-file graphs.

        Steps:
        1. Discover files using a single os.walk() traversal matching include
           patterns, filtering out exclude patterns
        2. Read and annotate each file using the dispatch annotator
        3. Build global symbol table: for each file's functions and classes,
           map qualified_name -> file_path
        4. Build cross-file import graph: for each file's imports, resolve to
           actual project files using Python module resolution
        5. Build reverse import graph
        6. Build global dependency graph: merge per-file dependency graphs,
           resolve cross-file references via symbol table
        7. Build reverse dependency graph
        8. Record timing and stats

        Returns:
            ProjectIndex with all files indexed and cross-references built.
        """
        start_time = time.monotonic()

        # Step 1: discover files
        file_paths = self._discover_files()
        logger.info("Discovered %d files in %s", len(file_paths), self.root_path)

        # Step 2: annotate each file (parallel I/O + annotation)
        files: dict[str, StructuralMetadata] = {}
        file_mtimes: dict[str, float] = {}
        total_lines = 0
        total_functions = 0
        total_classes = 0

        def _annotate_file(fpath: str) -> tuple[str, StructuralMetadata, float] | None:
            rel_path = os.path.relpath(fpath, self.root_path)
            try:
                mtime = os.path.getmtime(fpath)
                source = self._read_file(fpath)
            except (OSError, UnicodeDecodeError) as e:
                logger.warning("Skipping %s: %s", rel_path, e)
                return None
            metadata = annotate(source, source_name=rel_path)
            fill_hashes(metadata, source.splitlines())
            return rel_path, metadata, mtime

        max_workers = min(32, (os.cpu_count() or 1) * 4)
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            futures = {executor.submit(_annotate_file, fpath): fpath for fpath in file_paths}
            for future in as_completed(futures):
                result = future.result()
                if result is None:
                    continue
                rel_path, metadata, mtime = result
                files[rel_path] = metadata
                file_mtimes[rel_path] = mtime
                total_lines += metadata.total_lines
                total_functions += len(metadata.functions)
                total_classes += len(metadata.classes)

        # Step 3: build global symbol table
        symbol_table = self._build_symbol_table(files)
        duplicate_classes = self._build_duplicate_classes(files)

        # Step 4: build cross-file import graph
        import_graph = self._build_import_graph(files)

        # Step 5: build reverse import graph
        reverse_import_graph = self._build_reverse_graph(import_graph)

        # Step 6: build global dependency graph
        global_dep_graph = self._build_global_dependency_graph(files, symbol_table)

        # Step 7: build reverse dependency graph
        reverse_dep_graph = self._build_reverse_graph(global_dep_graph)

        elapsed = time.monotonic() - start_time

        symbol_hashes: dict[str, str] = {}
        for rel_path, metadata in files.items():
            for func in metadata.functions:
                if func.body_hash:
                    symbol_hashes[f"{rel_path}:{func.qualified_name}"] = func.body_hash
            for cls in metadata.classes:
                if cls.body_hash:
                    symbol_hashes[f"{rel_path}:{cls.name}"] = cls.body_hash
                for method in cls.methods:
                    if method.body_hash:
                        symbol_hashes[f"{rel_path}:{method.qualified_name}"] = method.body_hash

        self._project_index = ProjectIndex(
            root_path=self.root_path,
            files=files,
            global_dependency_graph=global_dep_graph,
            reverse_dependency_graph=reverse_dep_graph,
            import_graph=import_graph,
            reverse_import_graph=reverse_import_graph,
            symbol_table=symbol_table,
            duplicate_classes=duplicate_classes,
            total_files=len(files),
            total_lines=total_lines,
            total_functions=total_functions,
            total_classes=total_classes,
            index_build_time_seconds=elapsed,
            index_memory_bytes=sum(
                sys.getsizeof(m) + sys.getsizeof(m.lines) for m in files.values()
            ),
            file_mtimes=file_mtimes,
            symbol_hashes=symbol_hashes,
        )

        logger.info(
            "Indexed %d files (%d lines, %d functions, %d classes) in %.2fs",
            len(files),
            total_lines,
            total_functions,
            total_classes,
            elapsed,
        )

        _rebuild_path_indexes(self._project_index)
        return self._project_index

    def reindex_file(self, file_path: str, skip_graph_rebuild: bool = False) -> None:
        """Re-index a single file. Updates the existing ProjectIndex in place.

        Args:
            file_path: Path to the file (absolute or relative to root_path).
            skip_graph_rebuild: If True, skip rebuilding cross-file graphs.
                Use when batching multiple reindex calls, then call
                rebuild_graphs() once at the end.
        """
        if self._project_index is None:
            raise RuntimeError("Cannot reindex before initial index() call.")

        # Normalize to relative path
        abs_path = (
            os.path.abspath(file_path)
            if os.path.isabs(file_path)
            else os.path.join(self.root_path, file_path)
        )
        rel_path = os.path.relpath(abs_path, self.root_path)

        idx = self._project_index

        # Remove old data for this file
        old_metadata = idx.files.get(rel_path)
        if old_metadata is not None:

            # Remove old entries from import graphs
            idx.import_graph.pop(rel_path, None)
            for targets in idx.reverse_import_graph.values():
                targets.discard(rel_path)
            # Clean up reverse import graph entries pointing from this file
            for target_file in list(idx.reverse_import_graph.keys()):
                idx.reverse_import_graph[target_file].discard(rel_path)

            # Update stats
            idx.total_lines -= old_metadata.total_lines
            idx.total_functions -= len(old_metadata.functions)
            idx.total_classes -= len(old_metadata.classes)

        # Read and annotate the updated file
        try:
            mtime = os.path.getmtime(abs_path)
            source = self._read_file(abs_path)
        except (OSError, UnicodeDecodeError) as e:
            logger.warning("Cannot reindex %s: %s", rel_path, e)
            if rel_path in idx.files:
                del idx.files[rel_path]
                idx.file_mtimes.pop(rel_path, None)
                idx.total_files = len(idx.files)
                _rebuild_path_indexes(idx)
            return

        metadata = annotate(source, source_name=rel_path)
        fill_hashes(metadata, source.splitlines())

        # Symbol-level diffing: count what actually changed vs the old hashes.
        symbols_checked = 0
        symbols_unchanged = 0
        symbols_reindexed = 0
        changed_symbols: set[str] = set()

        def _check(qname: str, new_hash: str) -> None:
            nonlocal symbols_checked, symbols_unchanged, symbols_reindexed
            symbols_checked += 1
            key = f"{rel_path}:{qname}"
            old = idx.symbol_hashes.get(key, "")
            if old and old == new_hash:
                symbols_unchanged += 1
            else:
                symbols_reindexed += 1
                changed_symbols.add(qname)
                if new_hash:
                    idx.symbol_hashes[key] = new_hash
                else:
                    idx.symbol_hashes.pop(key, None)

        # Drop stale entries for symbols that disappeared from this file.
        new_keys: set[str] = set()
        for func in metadata.functions:
            new_keys.add(f"{rel_path}:{func.qualified_name}")
        for cls in metadata.classes:
            new_keys.add(f"{rel_path}:{cls.name}")
            for method in cls.methods:
                new_keys.add(f"{rel_path}:{method.qualified_name}")
        for key in [k for k in idx.symbol_hashes if k.startswith(f"{rel_path}:")]:
            if key not in new_keys:
                idx.symbol_hashes.pop(key, None)
                # Removed symbols count as "changed" for graph rebuild.
                changed_symbols.add(key.split(":", 1)[1])

        for func in metadata.functions:
            _check(func.qualified_name, func.body_hash)
        for cls in metadata.classes:
            _check(cls.name, cls.body_hash)
            for method in cls.methods:
                _check(method.qualified_name, method.body_hash)

        idx.last_reindex_symbols_checked = symbols_checked
        idx.last_reindex_symbols_unchanged = symbols_unchanged
        idx.last_reindex_symbols_reindexed = symbols_reindexed

        idx.files[rel_path] = metadata
        idx.file_mtimes[rel_path] = mtime
        idx.total_files = len(idx.files)
        _rebuild_path_indexes(idx)
        idx.total_lines += metadata.total_lines
        idx.total_functions += len(metadata.functions)
        idx.total_classes += len(metadata.classes)
        idx.symbol_table = self._build_symbol_table(idx.files)
        idx.duplicate_classes = self._build_duplicate_classes(idx.files)

        # Rebuild import graph for this file
        file_imports = self._resolve_imports_for_file(rel_path, metadata, idx.files)
        if file_imports:
            idx.import_graph[rel_path] = file_imports
        else:
            idx.import_graph.pop(rel_path, None)

        # If every symbol is unchanged AND imports are identical, the existing
        # graphs are still valid and we can skip the full rebuild.
        imports_changed = True
        if old_metadata is not None:
            old_imp_keys = {(i.module, tuple(i.names)) for i in old_metadata.imports}
            new_imp_keys = {(i.module, tuple(i.names)) for i in metadata.imports}
            imports_changed = old_imp_keys != new_imp_keys

        if not skip_graph_rebuild and (changed_symbols or imports_changed or old_metadata is None):
            # Rebuild reverse import graph
            idx.reverse_import_graph = self._build_reverse_graph(idx.import_graph)

            # Rebuild global dependency graphs (full rebuild is simplest for correctness)
            idx.global_dependency_graph = self._build_global_dependency_graph(
                idx.files, idx.symbol_table
            )
            idx.reverse_dependency_graph = self._build_reverse_graph(idx.global_dependency_graph)

    def remove_file(self, file_path: str) -> None:
        """Remove a file from the index. Does NOT rebuild cross-file graphs.

        Call rebuild_graphs() after batching multiple remove/reindex operations.

        Args:
            file_path: Path to the file (absolute or relative to root_path).
        """
        if self._project_index is None:
            raise RuntimeError("Cannot remove_file before initial index() call.")

        # Normalize to relative path
        abs_path = (
            os.path.abspath(file_path)
            if os.path.isabs(file_path)
            else os.path.join(self.root_path, file_path)
        )
        rel_path = os.path.relpath(abs_path, self.root_path)

        idx = self._project_index
        old_metadata = idx.files.get(rel_path)
        if old_metadata is None:
            return

        # Remove from import graphs
        idx.import_graph.pop(rel_path, None)
        for targets in idx.reverse_import_graph.values():
            targets.discard(rel_path)

        # Update stats
        idx.total_lines -= old_metadata.total_lines
        idx.total_functions -= len(old_metadata.functions)
        idx.total_classes -= len(old_metadata.classes)

        # Remove the file entry
        del idx.files[rel_path]
        idx.file_mtimes.pop(rel_path, None)
        idx.total_files = len(idx.files)
        _rebuild_path_indexes(idx)
        idx.symbol_table = self._build_symbol_table(idx.files)
        idx.duplicate_classes = self._build_duplicate_classes(idx.files)

    def rebuild_graphs(self) -> None:
        """Rebuild all cross-file graphs from current file data.

        Call after batching multiple remove_file() / reindex_file(skip_graph_rebuild=True)
        operations.
        """
        if self._project_index is None:
            raise RuntimeError("Cannot rebuild_graphs before initial index() call.")

        idx = self._project_index
        idx.symbol_table = self._build_symbol_table(idx.files)
        idx.duplicate_classes = self._build_duplicate_classes(idx.files)
        idx.import_graph = self._build_import_graph(idx.files)
        idx.reverse_import_graph = self._build_reverse_graph(idx.import_graph)
        idx.global_dependency_graph = self._build_global_dependency_graph(
            idx.files, idx.symbol_table
        )
        idx.reverse_dependency_graph = self._build_reverse_graph(idx.global_dependency_graph)

    # ------------------------------------------------------------------
    # File discovery
    # ------------------------------------------------------------------

    def _discover_files(self) -> list[str]:
        """Discover files using a single os.walk() traversal.

        Previous implementation called Path.glob() once per include pattern,
        producing N full directory traversals (N ≈ 27 by default).  On slow
        or network-backed filesystems (OneDrive, NFS, SMB) each traversal
        can take minutes; a single os.walk() pass reduces that to one trip.

        All include patterns are ``**/<name_pattern>`` style, so recursive
        descent is handled by os.walk and only the filename portion needs
        fnmatch matching.  Excluded directories are pruned in-place so
        os.walk never descends into them.
        """
        matched: set[str] = set()

        # Extract just the filename part of every include pattern
        # e.g. "**/*.py" → "*.py", "**/Dockerfile.*" → "Dockerfile.*"
        filename_patterns = [pat.rsplit("/", 1)[-1] for pat in self.include_patterns]

        for dirpath, dirnames, filenames in os.walk(self.root_path, topdown=True):
            rel_dir = os.path.relpath(dirpath, self.root_path)
            if rel_dir == ".":
                rel_dir = ""

            # Prune excluded directories in-place — os.walk won't descend into them.
            dirnames[:] = [
                d
                for d in dirnames
                if not self._is_excluded(os.path.join(rel_dir, d) if rel_dir else d)
            ]

            for filename in filenames:
                rel_path = os.path.join(rel_dir, filename) if rel_dir else filename

                if self._is_excluded(rel_path):
                    continue

                if not any(fnmatch.fnmatch(filename, fp) for fp in filename_patterns):
                    continue

                abs_path = os.path.join(dirpath, filename)
                try:
                    size = os.path.getsize(abs_path)
                except OSError:
                    continue
                if size > self.max_file_size_bytes:
                    logger.debug(
                        "Skipping %s (size %d > %d)", rel_path, size, self.max_file_size_bytes
                    )
                    continue

                matched.add(abs_path)

                if len(matched) >= self.max_files:
                    logger.warning(
                        "Project has %d+ files, stopping at MAX_FILES=%d. "
                        "Set TOKEN_SAVIOR_MAX_FILES env var to increase.",
                        len(matched), self.max_files,
                    )
                    break

            if len(matched) >= self.max_files:
                break

        return sorted(matched)

    def _is_excluded(self, rel_path: str) -> bool:
        """Check if a relative path matches any exclude pattern."""
        # Normalize separators to forward slashes for matching
        normalized = rel_path.replace(os.sep, "/")
        parts = normalized.split("/")
        for pattern in self.exclude_patterns:
            if fnmatch.fnmatch(normalized, pattern):
                return True
            # For patterns starting with "**/" strip the prefix and retry.
            # fnmatch's "*" matches "/" so "*.log" matches "sub/dir/file.log".
            if pattern.startswith("**/"):
                if fnmatch.fnmatch(normalized, pattern[3:]):
                    return True
            # Also check if any path component matches a simple name
            # e.g., "__pycache__" or ".worktrees" present as a directory segment
            pattern_parts = pattern.replace("**/", "").replace("/**", "").strip("/")
            if "/" not in pattern_parts and pattern_parts in parts:
                return True
        return False

    def _read_file(self, abs_path: str) -> str:
        """Read a file as text, trying UTF-8 first then latin-1 as fallback."""
        try:
            with open(abs_path, "r", encoding="utf-8") as f:
                return f.read()
        except UnicodeDecodeError:
            with open(abs_path, "r", encoding="latin-1") as f:
                return f.read()

    # ------------------------------------------------------------------
    # Symbol table
    # ------------------------------------------------------------------

    def _build_symbol_table(self, files: dict[str, StructuralMetadata]) -> dict[str, str]:
        """Build global symbol table: symbol_name -> file_path where defined.

        For methods, use qualified_name (e.g., "MyClass.run" -> "src/engine.py").
        First-found wins for duplicates.
        """
        symbol_table: dict[str, str] = {}
        alias_counts: dict[str, int] = {}
        alias_targets: dict[str, str] = {}

        for file_path, metadata in files.items():
            for func in metadata.functions:
                if func.qualified_name not in symbol_table:
                    symbol_table[func.qualified_name] = file_path
                for alias in self._function_symbol_aliases(func):
                    alias_counts[alias] = alias_counts.get(alias, 0) + 1
                    alias_targets.setdefault(alias, file_path)

            for cls in metadata.classes:
                qualified_name = getattr(cls, "qualified_name", None)
                if qualified_name and qualified_name not in symbol_table:
                    symbol_table[qualified_name] = file_path
                for alias in self._class_symbol_aliases(cls):
                    alias_counts[alias] = alias_counts.get(alias, 0) + 1
                    alias_targets.setdefault(alias, file_path)

        for alias, count in alias_counts.items():
            if count == 1 and alias not in symbol_table:
                symbol_table[alias] = alias_targets[alias]

        return symbol_table

    def _build_duplicate_classes(self, files: dict[str, StructuralMetadata]) -> dict[str, list[str]]:
        duplicates: dict[str, set[str]] = {}
        for file_path, metadata in files.items():
            if not file_path.endswith(".java"):
                continue
            for cls in metadata.classes:
                qualified_name = getattr(cls, "qualified_name", None)
                if not qualified_name or self._is_local_scoped_symbol(qualified_name):
                    continue
                duplicates.setdefault(qualified_name, set()).add(file_path)
        return {
            qualified_name: sorted(paths)
            for qualified_name, paths in duplicates.items()
            if len(paths) > 1
        }

    @staticmethod
    def _is_local_scoped_symbol(name: str | None) -> bool:
        return bool(name and "::<local>." in name)

    @staticmethod
    def _function_symbol_aliases(func) -> list[str]:
        aliases: list[str] = []
        if not func.is_method:
            aliases.append(func.name)
            return aliases
        if ProjectIndexer._is_local_scoped_symbol(func.qualified_name):
            return aliases

        qualified_name = func.qualified_name
        base_name = qualified_name
        signature_suffix = ""
        if qualified_name.endswith(")") and "(" in qualified_name:
            base_name, _, suffix = qualified_name.rpartition("(")
            signature_suffix = f"({suffix}"
            aliases.append(base_name)

        if func.parent_class:
            aliases.append(f"{func.parent_class}.{func.name}")
            if signature_suffix:
                aliases.append(f"{func.parent_class}.{func.name}{signature_suffix}")

        return aliases

    @staticmethod
    def _class_symbol_aliases(cls) -> list[str]:
        if ProjectIndexer._is_local_scoped_symbol(getattr(cls, "qualified_name", None)):
            return []
        return [cls.name]

    # ------------------------------------------------------------------
    # Import graph
    # ------------------------------------------------------------------

    def _build_import_graph(self, files: dict[str, StructuralMetadata]) -> dict[str, set[str]]:
        """Build file-level import graph: file -> set of files it imports from."""
        import_graph: dict[str, set[str]] = {}

        for file_path, metadata in files.items():
            targets = self._resolve_imports_for_file(file_path, metadata, files)
            if targets:
                import_graph[file_path] = targets

        return import_graph

    def _resolve_imports_for_file(
        self,
        file_path: str,
        metadata: StructuralMetadata,
        all_files: dict[str, StructuralMetadata],
    ) -> set[str]:
        """Resolve a file's imports to other project files."""
        targets: set[str] = set()
        all_file_set = set(all_files.keys())

        for imp in metadata.imports:
            if file_path.endswith(".java"):
                targets.update(self._resolve_java_import(file_path, imp, all_files))
                continue
            resolved = self._resolve_import(file_path, imp.module, imp.is_from_import, all_file_set)
            if resolved and resolved != file_path:
                targets.add(resolved)

        return targets

    def _resolve_java_import(
        self,
        importing_file: str,
        imp,
        all_files: dict[str, StructuralMetadata],
    ) -> set[str]:
        targets: set[str] = set()
        if imp.is_from_import:
            owner_target = self._resolve_java_type_path(imp.module, all_files)
            if owner_target and owner_target != importing_file:
                targets.add(owner_target)
            return targets

        if imp.names == ["*"]:
            for path, metadata in all_files.items():
                if path == importing_file or not path.endswith(".java"):
                    continue
                if metadata.module_name == imp.module:
                    targets.add(path)
            return targets

        target = self._resolve_java_type_path(imp.module, all_files)
        if target and target != importing_file:
            targets.add(target)
        return targets

    @staticmethod
    def _resolve_java_type_path(
        qualified_type: str,
        all_files: dict[str, StructuralMetadata],
    ) -> str | None:
        for path, metadata in all_files.items():
            if not path.endswith(".java"):
                continue
            for cls in metadata.classes:
                if getattr(cls, "qualified_name", None) == qualified_type:
                    return path
        return None

    def _resolve_import(
        self,
        importing_file: str,
        module_path: str,
        is_from_import: bool,
        all_files: set[str],
    ) -> str | None:
        """Resolve an import module path to a project file path.

        For Python files:
        - Convert module path to file path (dots to slashes)
        - Look for module.py or module/__init__.py
        - Search relative to root and common source dirs (src/, lib/)

        For TypeScript/JavaScript files:
        - Resolve relative paths (./foo, ../bar)
        - Try common path aliases (@/ -> src/)
        """
        if not module_path:
            return None

        ext = os.path.splitext(importing_file)[1].lower()

        if ext == ".py":
            return self._resolve_python_import(module_path, all_files)
        elif ext in (".ts", ".tsx", ".js", ".jsx"):
            return self._resolve_ts_import(importing_file, module_path, all_files)
        elif ext == ".rs":
            return self._resolve_rust_import(importing_file, module_path, all_files)
        elif ext == ".go":
            return self._resolve_go_import(module_path, all_files)
        elif ext == ".cs":
            return self._resolve_csharp_import(module_path, all_files)

        return None

    def _resolve_python_import(self, module_path: str, all_files: set[str]) -> str | None:
        """Resolve a Python module path to a project file."""
        # Convert dots to path separators
        rel_module = module_path.replace(".", "/")

        # Search directories: root, src/, lib/
        search_prefixes = ["", "src/", "lib/"]

        for prefix in search_prefixes:
            # Try as a .py file
            candidate = prefix + rel_module + ".py"
            candidate_normalized = candidate.replace(os.sep, "/")
            if candidate_normalized in all_files:
                return candidate_normalized

            # Try as a package (__init__.py)
            candidate = prefix + rel_module + "/__init__.py"
            candidate_normalized = candidate.replace(os.sep, "/")
            if candidate_normalized in all_files:
                return candidate_normalized

        return None

    def _resolve_ts_import(
        self, importing_file: str, module_path: str, all_files: set[str]
    ) -> str | None:
        """Resolve a TypeScript/JavaScript import path to a project file.

        Handles:
        - Relative paths: './utils' -> try utils.ts, utils.tsx, utils/index.ts, etc.
        - Path aliases: '@/lib/utils' -> try src/lib/utils.ts, etc.
        """
        extensions = [".ts", ".tsx", ".js", ".jsx"]

        if module_path.startswith("."):
            # Relative import
            importing_dir = os.path.dirname(importing_file)
            base = os.path.normpath(os.path.join(importing_dir, module_path))
            base = base.replace(os.sep, "/")
        elif module_path.startswith("@/"):
            # Common path alias: @/ -> src/
            base = "src/" + module_path[2:]
        else:
            # Likely an external package (e.g., 'react', 'lodash')
            return None

        # Try exact match first (might already have extension)
        if base in all_files:
            return base

        # Try with extensions
        for ext in extensions:
            candidate = base + ext
            if candidate in all_files:
                return candidate

        # Try as directory with index file
        for ext in extensions:
            candidate = base + "/index" + ext
            if candidate in all_files:
                return candidate

        return None

    def _resolve_rust_import(
        self, importing_file: str, module_path: str, all_files: set[str]
    ) -> str | None:
        """Resolve a Rust use path to a project file.

        Handles:
        - crate::module::Item -> src/module.rs or src/module/mod.rs
        - super::module -> parent dir
        - self::module -> current dir
        """
        if not module_path:
            return None

        # Skip external crates (std, third-party)
        if not module_path.startswith(("crate", "super", "self")):
            return None

        importing_dir = os.path.dirname(importing_file)

        if module_path.startswith("crate::"):
            # crate:: maps to src/
            rel_module = module_path[len("crate::") :].replace("::", "/")
            search_prefixes = ["src/", ""]
        elif module_path.startswith("super::"):
            rel_module = module_path[len("super::") :].replace("::", "/")
            parent = os.path.dirname(importing_dir)
            search_prefixes = [parent + "/" if parent else ""]
        elif module_path.startswith("self::"):
            rel_module = module_path[len("self::") :].replace("::", "/")
            search_prefixes = [importing_dir + "/" if importing_dir else ""]
        else:
            return None

        for prefix in search_prefixes:
            # Try as .rs file
            candidate = (prefix + rel_module + ".rs").replace(os.sep, "/")
            if candidate in all_files:
                return candidate
            # Try as mod.rs
            candidate = (prefix + rel_module + "/mod.rs").replace(os.sep, "/")
            if candidate in all_files:
                return candidate
            # Try parent module (e.g., use crate::module::Item -> src/module.rs)
            parts = rel_module.rsplit("/", 1)
            if len(parts) == 2:
                candidate = (prefix + parts[0] + ".rs").replace(os.sep, "/")
                if candidate in all_files:
                    return candidate
                candidate = (prefix + parts[0] + "/mod.rs").replace(os.sep, "/")
                if candidate in all_files:
                    return candidate

        return None

    def _resolve_go_import(self, module_path: str, all_files: set[str]) -> str | None:
        """Resolve a Go import path to a project directory's .go files.

        Matches import path suffixes against project directories containing .go files.
        """
        if not module_path:
            return None

        # Build a mapping of directory -> any .go file in that dir
        dir_to_file: dict[str, str] = {}
        for f in all_files:
            if f.endswith(".go"):
                d = os.path.dirname(f)
                if d not in dir_to_file:
                    dir_to_file[d] = f

        # Try matching the import path suffix against directory paths
        # e.g., "github.com/user/repo/pkg/utils" -> look for dirs ending with "pkg/utils"
        path_parts = module_path.split("/")
        for length in range(len(path_parts), 0, -1):
            suffix = "/".join(path_parts[-length:])
            for d, f in dir_to_file.items():
                normalized = d.replace(os.sep, "/")
                if normalized == suffix or normalized.endswith("/" + suffix):
                    return f

        return None

    def _resolve_csharp_import(self, module_path: str, all_files: set[str]) -> str | None:
        """Resolve a C# using directive to a project file.

        Best-effort: converts namespace segments to path (Ns.Sub -> Ns/Sub.cs).
        Skips well-known external namespaces (System.*, Microsoft.*).
        """
        if not module_path:
            return None

        # Skip external namespaces
        if module_path.startswith(("System", "Microsoft", "Newtonsoft", "NUnit", "Xunit", "Moq")):
            return None

        # Convert Namespace.Sub to path: Namespace/Sub.cs
        rel_module = module_path.replace(".", "/")

        # Search in common locations
        search_prefixes = ["", "src/", "lib/"]
        for prefix in search_prefixes:
            candidate = (prefix + rel_module + ".cs").replace(os.sep, "/")
            if candidate in all_files:
                return candidate
            # Try as directory with matching file
            # e.g., Models/User.cs for Models.User
            parts = rel_module.rsplit("/", 1)
            if len(parts) == 2:
                candidate = (prefix + parts[0] + "/" + parts[1] + ".cs").replace(os.sep, "/")
                if candidate in all_files:
                    return candidate

        return None

    # ------------------------------------------------------------------
    # Dependency graph
    # ------------------------------------------------------------------

    def _build_global_dependency_graph(
        self,
        files: dict[str, StructuralMetadata],
        symbol_table: dict[str, str],
    ) -> dict[str, set[str]]:
        """Build global dependency graph: qualified_name -> set of qualified_names.

        Merges per-file dependency graphs and resolves cross-file references
        via the symbol table and import information.

        The per-file dependency graph only tracks references to names defined
        in the same file. For cross-file dependencies, we also check each
        function/class body for references to imported names.
        """
        global_graph: dict[str, set[str]] = {}

        # Set of all known qualified names in the project
        all_symbols = set(symbol_table.keys())
        java_package_symbols: dict[str, dict[str, str]] = {}
        java_hierarchy = self._build_java_type_hierarchy(files)
        java_impl_edges = self._build_java_implementation_edges(files, java_hierarchy)
        for metadata in files.values():
            if not metadata.module_name:
                continue
            package_symbols = java_package_symbols.setdefault(metadata.module_name, {})
            for cls in metadata.classes:
                qualified_name = getattr(cls, "qualified_name", None)
                if qualified_name:
                    package_symbols.setdefault(cls.name, qualified_name)

        for file_path, metadata in files.items():
            # Collect imported names mapping: local_name -> qualified_name (symbol table key)
            if file_path.endswith(".java"):
                imported_names = self._build_java_imported_names(
                    metadata,
                    symbol_table,
                    java_package_symbols,
                )
            else:
                imported_names: dict[str, str] = {}
                for imp in metadata.imports:
                    for name in imp.names:
                        # Check if this name is a known symbol
                        if name in symbol_table:
                            imported_names[name] = name

            # Process per-file dependency graph (intra-file deps)
            for source_name, deps in metadata.dependency_graph.items():
                source_qualified = self._qualify_name(source_name, file_path, symbol_table)
                if source_qualified not in global_graph:
                    global_graph[source_qualified] = set()

                for dep in deps:
                    for dep_qualified in self._resolve_java_dependency_symbols(
                        dep,
                        file_path,
                        imported_names,
                        symbol_table,
                        all_symbols,
                    ):
                        if dep_qualified != source_qualified:
                            global_graph[source_qualified].add(dep_qualified)

            # Now handle cross-file dependencies by scanning function/class bodies
            # for references to imported names (which the per-file dep graph misses).
            is_java_file = file_path.endswith(".java")
            if not imported_names and not is_java_file:
                continue

            # Look up cached regex per imported name (shared across all files)
            compiled_patterns = {name: _word_boundary_re(name) for name in imported_names}

            for func in metadata.functions:
                func_qualified = self._qualify_name(func.qualified_name, file_path, symbol_table)
                if func_qualified not in global_graph:
                    global_graph[func_qualified] = set()

                # Scan the function body lines for imported name references
                start_idx = func.line_range.start - 1  # 0-indexed
                end_idx = func.line_range.end  # exclusive
                body_text = " ".join(metadata.lines[start_idx:end_idx])
                for local_name, resolved_name in imported_names.items():
                    # fast path: skip regex if the name isn't even in the text
                    if local_name not in body_text:
                        continue
                    if compiled_patterns[local_name].search(body_text):
                        if resolved_name != func_qualified:
                            global_graph[func_qualified].add(resolved_name)
                if is_java_file:
                    current_owner = func_qualified.rsplit(".", 1)[0] if "." in func_qualified else None
                    global_graph[func_qualified].update(
                        target
                        for target in self._resolve_java_method_reference_targets(
                            body_text,
                            current_owner,
                            imported_names,
                            symbol_table,
                        )
                        if target != func_qualified
                    )
                    global_graph[func_qualified].update(java_impl_edges.get(func_qualified, set()))

            for cls in metadata.classes:
                cls_qualified = self._qualify_name(cls.name, file_path, symbol_table)
                if cls_qualified not in global_graph:
                    global_graph[cls_qualified] = set()

                # Scan the class body lines for imported name references
                start_idx = cls.line_range.start - 1
                end_idx = cls.line_range.end
                body_text = " ".join(metadata.lines[start_idx:end_idx])
                for local_name, resolved_name in imported_names.items():
                    # fast path: skip regex if the name isn't even in the text
                    if local_name not in body_text:
                        continue
                    if compiled_patterns[local_name].search(body_text):
                        if resolved_name != cls_qualified:
                            global_graph[cls_qualified].add(resolved_name)
                if is_java_file:
                    global_graph[cls_qualified].update(
                        target
                        for target in self._resolve_java_method_reference_targets(
                            body_text,
                            cls_qualified,
                            imported_names,
                            symbol_table,
                        )
                        if target != cls_qualified
                    )

        for source, targets in list(global_graph.items()):
            targets.update(
                target for dep in list(targets) for target in java_impl_edges.get(dep, set()) if target != source
            )

        for source, targets in self._build_java_framework_entry_edges(files).items():
            global_graph.setdefault(source, set()).update(targets)

        for source, targets in self._build_java_spring_wiring_edges(
            files,
            symbol_table,
            java_package_symbols,
        ).items():
            global_graph.setdefault(source, set()).update(targets)

        for source, targets in self._build_java_runtime_entry_edges(files, java_hierarchy).items():
            global_graph.setdefault(source, set()).update(targets)

        return global_graph

    @staticmethod
    def _build_java_implementation_edges(
        files: dict[str, StructuralMetadata],
        hierarchy: dict[str, set[str]],
    ) -> dict[str, set[str]]:
        class_index: dict[str, list] = {}
        for metadata in files.values():
            for cls in metadata.classes:
                class_index.setdefault(cls.name, []).append(cls)
                qualified_name = getattr(cls, "qualified_name", None)
                if qualified_name:
                    class_index.setdefault(qualified_name, []).append(cls)

        impl_edges: dict[str, set[str]] = {}
        for metadata in files.values():
            for cls in metadata.classes:
                qualified_name = getattr(cls, "qualified_name", None) or cls.name
                ancestor_names = set(cls.base_classes) | hierarchy.get(qualified_name, set())
                for base_name in ancestor_names:
                    for base_cls in class_index.get(base_name, []):
                        base_methods = {}
                        for method in base_cls.methods:
                            base_methods.setdefault(ProjectIndexer._method_signature_key(method), []).append(
                                method.qualified_name
                            )
                        for method in cls.methods:
                            for base_symbol in base_methods.get(
                                ProjectIndexer._method_signature_key(method),
                                [],
                            ):
                                impl_edges.setdefault(base_symbol, set()).add(method.qualified_name)
        return impl_edges

    @staticmethod
    def _build_java_type_hierarchy(
        files: dict[str, StructuralMetadata]
    ) -> dict[str, set[str]]:
        class_index: dict[str, list] = {}
        by_qualified_name: dict[str, object] = {}
        for metadata in files.values():
            for cls in metadata.classes:
                class_index.setdefault(cls.name, []).append(cls)
                qualified_name = getattr(cls, "qualified_name", None)
                if qualified_name:
                    class_index.setdefault(qualified_name, []).append(cls)
                    by_qualified_name[qualified_name] = cls

        cache: dict[str, set[str]] = {}

        def _collect(qualified_name: str, seen: set[str]) -> set[str]:
            if qualified_name in cache:
                return cache[qualified_name]
            cls = by_qualified_name.get(qualified_name)
            if cls is None:
                return set()
            ancestors: set[str] = set()
            next_seen = set(seen)
            next_seen.add(qualified_name)
            for base_name in cls.base_classes:
                ancestors.add(base_name)
                for base_cls in class_index.get(base_name, []):
                    base_qualified = getattr(base_cls, "qualified_name", None) or base_cls.name
                    ancestors.add(base_qualified)
                    if base_qualified not in next_seen:
                        ancestors.update(_collect(base_qualified, next_seen))
            cache[qualified_name] = ancestors
            return ancestors

        for qualified_name in by_qualified_name:
            _collect(qualified_name, set())
        return cache

    @staticmethod
    def _decorator_names(decorators: list[str]) -> set[str]:
        return {decorator.split(".")[-1] for decorator in decorators}

    def _build_java_framework_entry_edges(
        self,
        files: dict[str, StructuralMetadata],
    ) -> dict[str, set[str]]:
        edges: dict[str, set[str]] = {}
        for file_path, metadata in files.items():
            if not file_path.endswith(".java"):
                continue
            for cls in metadata.classes:
                qualified_name = getattr(cls, "qualified_name", None) or cls.name
                class_decorators = self._decorator_names(getattr(cls, "decorators", []))
                if class_decorators & _SPRING_CLASS_DECORATORS:
                    if class_decorators & {"RestController", "Controller", "RequestMapping"}:
                        class_kind = "controller"
                    elif class_decorators & {"Configuration", "ConfigurationProperties"}:
                        class_kind = "configuration"
                    elif "SpringBootApplication" in class_decorators:
                        class_kind = "application"
                    else:
                        class_kind = "component"
                    source = f"__framework__.spring.class:{class_kind}:{qualified_name}"
                    targets = edges.setdefault(source, set())
                    targets.add(qualified_name)
                    targets.update(method.qualified_name for method in cls.methods)
                for method in cls.methods:
                    method_decorators = self._decorator_names(method.decorators)
                    if method_decorators & (_SPRING_METHOD_DECORATORS | _JAVA_LIFECYCLE_DECORATORS):
                        if method_decorators & _JAVA_LIFECYCLE_DECORATORS:
                            method_kind = "lifecycle"
                        elif "Bean" in method_decorators:
                            method_kind = "bean"
                        else:
                            method_kind = "route"
                        source = f"__framework__.spring.method:{method_kind}:{method.qualified_name}"
                        edges.setdefault(source, set()).add(method.qualified_name)
        return edges

    def _build_java_runtime_entry_edges(
        self,
        files: dict[str, StructuralMetadata],
        hierarchy: dict[str, set[str]],
    ) -> dict[str, set[str]]:
        edges: dict[str, set[str]] = {}
        source = "__runtime__.java.dispatch:Runnable.run"
        for file_path, metadata in files.items():
            if not file_path.endswith(".java"):
                continue
            for cls in metadata.classes:
                qualified_name = getattr(cls, "qualified_name", None) or cls.name
                ancestor_names = set(cls.base_classes) | hierarchy.get(qualified_name, set())
                if not (ancestor_names & _JAVA_DYNAMIC_DISPATCH_BASES):
                    continue
                for method in cls.methods:
                    if method.name == "run":
                        edges.setdefault(source, set()).add(method.qualified_name)
        return edges

    def _build_java_spring_wiring_edges(
        self,
        files: dict[str, StructuralMetadata],
        symbol_table: dict[str, str],
        java_package_symbols: dict[str, dict[str, str]],
    ) -> dict[str, set[str]]:
        edges: dict[str, set[str]] = {}
        managed_classes: dict[str, tuple[object, StructuralMetadata, dict[str, str], set[str]]] = {}
        application_classes: set[str] = set()

        for file_path, metadata in files.items():
            if not file_path.endswith(".java"):
                continue
            imported_names = self._build_java_imported_names(metadata, symbol_table, java_package_symbols)
            for cls in metadata.classes:
                qualified_name = getattr(cls, "qualified_name", None) or cls.name
                decorators = self._decorator_names(getattr(cls, "decorators", []))
                if decorators & _SPRING_CLASS_DECORATORS:
                    managed_classes[qualified_name] = (cls, metadata, imported_names, decorators)
                if "SpringBootApplication" in decorators:
                    application_classes.add(qualified_name)

        preferred_bootstrap_targets = {
            qualified_name
            for qualified_name, (_, _, _, decorators) in managed_classes.items()
            if decorators
            & {
                "Service",
                "Repository",
                "Controller",
                "RestController",
                "Configuration",
                "ConfigurationProperties",
            }
        }
        bootstrap_targets = preferred_bootstrap_targets or set(managed_classes)
        for application_class in application_classes:
            source = f"__framework__.spring.boot:application:{application_class}"
            targets = edges.setdefault(source, set())
            for qualified_name in bootstrap_targets:
                if qualified_name != application_class:
                    targets.add(qualified_name)

        for qualified_name, (cls, metadata, imported_names, _) in managed_classes.items():
            constructors = [method for method in cls.methods if method.name == cls.name]
            autowired_ctors = [
                method
                for method in constructors
                if "Autowired" in self._decorator_names(getattr(method, "decorators", []))
            ]
            injection_points = autowired_ctors or constructors
            if len(constructors) > 1 and not autowired_ctors:
                injection_points = []

            for method in injection_points:
                deps = self._resolve_java_signature_types(
                    method.qualified_name,
                    imported_names,
                    symbol_table,
                )
                if not deps:
                    continue
                edges.setdefault(qualified_name, set()).update(dep for dep in deps if dep != qualified_name)
                edges.setdefault(method.qualified_name, set()).update(
                    dep for dep in deps if dep != method.qualified_name
                )

            for method in cls.methods:
                if "Bean" not in self._decorator_names(getattr(method, "decorators", [])):
                    continue
                bean_deps = metadata.dependency_graph.get(method.qualified_name)
                if bean_deps:
                    edges.setdefault(qualified_name, set()).update(bean_deps)

        return edges

    @staticmethod
    def _method_signature_key(func) -> tuple[str, int]:
        return func.name, len(getattr(func, "parameters", []))

    def _resolve_java_dependency_symbols(
        self,
        dep: str,
        file_path: str,
        imported_names: dict[str, str],
        symbol_table: dict[str, str],
        all_symbols: set[str],
    ) -> set[str]:
        if "." not in dep:
            resolved: set[str] = set()
            if dep in imported_names:
                resolved.add(imported_names[dep])
            candidate = self._qualify_name(dep, file_path, symbol_table)
            if candidate in all_symbols:
                resolved.add(candidate)
            if dep in all_symbols:
                resolved.add(dep)
            return resolved

        owner, _, member = dep.rpartition(".")
        owner_candidates = {owner}
        if owner in imported_names:
            owner_candidates.add(imported_names[owner])
        owner_candidates.add(self._qualify_name(owner, file_path, symbol_table))

        resolved: set[str] = set()
        for owner_candidate in owner_candidates:
            if not owner_candidate:
                continue
            prefix = owner_candidate + "."
            for symbol in all_symbols:
                if not symbol.startswith(prefix):
                    continue
                remainder = symbol[len(prefix) :]
                if remainder == member or remainder.startswith(f"{member}("):
                    resolved.add(symbol)

        if resolved:
            canonical = {symbol for symbol in resolved if symbol.count(".") > dep.count(".")}
            return canonical or resolved

        if dep in imported_names:
            resolved.add(imported_names[dep])
        candidate = self._qualify_name(dep, file_path, symbol_table)
        if candidate in all_symbols:
            resolved.add(candidate)
        if dep in all_symbols:
            resolved.add(dep)
        return resolved

    @staticmethod
    def _split_java_signature_params(signature: str) -> list[str]:
        start = signature.find("(")
        end = signature.rfind(")")
        if start < 0 or end <= start:
            return []
        raw = signature[start + 1 : end]
        if not raw:
            return []

        params: list[str] = []
        current: list[str] = []
        angle_depth = 0
        bracket_depth = 0
        paren_depth = 0
        for ch in raw:
            if ch == "<":
                angle_depth += 1
            elif ch == ">":
                angle_depth = max(0, angle_depth - 1)
            elif ch == "[":
                bracket_depth += 1
            elif ch == "]":
                bracket_depth = max(0, bracket_depth - 1)
            elif ch == "(":
                paren_depth += 1
            elif ch == ")":
                paren_depth = max(0, paren_depth - 1)
            elif ch == "," and angle_depth == 0 and bracket_depth == 0 and paren_depth == 0:
                part = "".join(current).strip()
                if part:
                    params.append(part)
                current = []
                continue
            current.append(ch)
        tail = "".join(current).strip()
        if tail:
            params.append(tail)
        return params

    @staticmethod
    def _extract_java_signature_type_name(type_name: str) -> str | None:
        cleaned = type_name.replace("...", "").replace("[]", "").strip()
        if not cleaned:
            return None
        result: list[str] = []
        generic_depth = 0
        for ch in cleaned:
            if ch == "<":
                generic_depth += 1
                continue
            if ch == ">":
                generic_depth = max(0, generic_depth - 1)
                continue
            if generic_depth == 0:
                result.append(ch)
        base = "".join(result).strip()
        if not base:
            return None
        simple = base.rsplit(".", 1)[-1]
        if not simple or not simple[0].isupper():
            return None
        return base

    def _resolve_java_signature_types(
        self,
        qualified_name: str,
        imported_names: dict[str, str],
        symbol_table: dict[str, str],
    ) -> set[str]:
        deps: set[str] = set()
        for param in self._split_java_signature_params(qualified_name):
            type_name = self._extract_java_signature_type_name(param)
            if not type_name:
                continue
            if type_name in imported_names:
                deps.add(imported_names[type_name])
                continue
            simple_name = type_name.rsplit(".", 1)[-1]
            if simple_name in imported_names:
                deps.add(imported_names[simple_name])
                continue
            if type_name in symbol_table:
                deps.add(type_name)
                continue
        return deps

    def _build_java_imported_names(
        self,
        metadata: StructuralMetadata,
        symbol_table: dict[str, str],
        java_package_symbols: dict[str, dict[str, str]],
    ) -> dict[str, str]:
        imported_names: dict[str, str] = {}

        if metadata.module_name:
            imported_names.update(java_package_symbols.get(metadata.module_name, {}))

        for cls in metadata.classes:
            qualified_name = getattr(cls, "qualified_name", None)
            if qualified_name:
                imported_names.setdefault(cls.name, qualified_name)

        for imp in metadata.imports:
            if imp.is_from_import:
                if imp.module in symbol_table:
                    owner_simple = imp.module.rsplit(".", 1)[-1]
                    imported_names.setdefault(owner_simple, imp.module)
                    for name in imp.names:
                        if name != "*":
                            imported_names.setdefault(name, imp.module)
                continue

            if imp.names == ["*"]:
                imported_names.update(java_package_symbols.get(imp.module, {}))
                continue

            if imp.module in symbol_table and imp.names:
                imported_names.setdefault(imp.names[0], imp.module)

        return imported_names

    @staticmethod
    def _resolve_java_method_reference_owner(
        owner_token: str,
        current_owner: str | None,
        imported_names: dict[str, str],
        symbol_table: dict[str, str],
    ) -> str | None:
        if owner_token in {"this", "super"}:
            return current_owner
        if owner_token in imported_names:
            return imported_names[owner_token]
        if owner_token in symbol_table:
            return owner_token
        return None

    @staticmethod
    def _resolve_java_method_reference_targets(
        body_text: str,
        current_owner: str | None,
        imported_names: dict[str, str],
        symbol_table: dict[str, str],
    ) -> set[str]:
        targets: set[str] = set()
        for owner_token, method_name in _JAVA_METHOD_REFERENCE_PATTERN.findall(body_text):
            owner = ProjectIndexer._resolve_java_method_reference_owner(
                owner_token, current_owner, imported_names, symbol_table
            )
            if not owner:
                continue
            prefix = owner + "."
            for symbol in symbol_table:
                if not symbol.startswith(prefix):
                    continue
                remainder = symbol[len(prefix) :]
                if remainder == method_name or remainder.startswith(f"{method_name}("):
                    targets.add(symbol)
        return targets

    def _qualify_name(self, name: str, file_path: str, symbol_table: dict[str, str]) -> str:
        """Given a local name and file path, find the qualified name in the symbol table."""
        # If the name is already a qualified name in the symbol table, use it
        if name in symbol_table and symbol_table[name] == file_path:
            return name

        # Check if it's a method-style qualified name (Class.method)
        # The symbol table should already have these
        return name

    # ------------------------------------------------------------------
    # Graph utilities
    # ------------------------------------------------------------------

    @staticmethod
    def _build_reverse_graph(
        graph: dict[str, set[str]],
    ) -> dict[str, set[str]]:
        """Build a reverse graph: for each target, collect all sources."""
        reverse: dict[str, set[str]] = {}
        for source, targets in graph.items():
            for target in targets:
                if target not in reverse:
                    reverse[target] = set()
                reverse[target].add(source)
        return reverse
