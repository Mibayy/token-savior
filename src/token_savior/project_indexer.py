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

_WORD_BOUNDARY_CACHE: dict[str, re.Pattern] = {}


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
            "**/node_modules/**",
            "**/.git/**",
            "**/.venv/**",
            "**/venv/**",
            "**/*.egg-info/**",
            "**/target/**",
            "**/vendor/**",
            "**/package-lock.json",
            "**/.package-lock.json",
            "**/composer.lock",
            # Next.js / build output dirs
            "**/.next/**",
            "**/dist/**",
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
            # Remove old symbols from symbol table
            for func in old_metadata.functions:
                if idx.symbol_table.get(func.qualified_name) == rel_path:
                    del idx.symbol_table[func.qualified_name]
                if idx.symbol_table.get(func.name) == rel_path:
                    del idx.symbol_table[func.name]
            for cls in old_metadata.classes:
                if idx.symbol_table.get(cls.name) == rel_path:
                    del idx.symbol_table[cls.name]

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
        idx.total_lines += metadata.total_lines
        idx.total_functions += len(metadata.functions)
        idx.total_classes += len(metadata.classes)

        # Rebuild symbol table entries for this file
        for func in metadata.functions:
            if func.qualified_name not in idx.symbol_table:
                idx.symbol_table[func.qualified_name] = rel_path
            if func.name not in idx.symbol_table:
                idx.symbol_table[func.name] = rel_path
        for cls in metadata.classes:
            if cls.name not in idx.symbol_table:
                idx.symbol_table[cls.name] = rel_path

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

        # Remove old symbols from symbol table
        for func in old_metadata.functions:
            if idx.symbol_table.get(func.qualified_name) == rel_path:
                del idx.symbol_table[func.qualified_name]
            if idx.symbol_table.get(func.name) == rel_path:
                del idx.symbol_table[func.name]
        for cls in old_metadata.classes:
            if idx.symbol_table.get(cls.name) == rel_path:
                del idx.symbol_table[cls.name]

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

    def rebuild_graphs(self) -> None:
        """Rebuild all cross-file graphs from current file data.

        Call after batching multiple remove_file() / reindex_file(skip_graph_rebuild=True)
        operations.
        """
        if self._project_index is None:
            raise RuntimeError("Cannot rebuild_graphs before initial index() call.")

        idx = self._project_index
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

        for file_path, metadata in files.items():
            for func in metadata.functions:
                # Register by qualified name (e.g., "MyClass.method")
                if func.qualified_name not in symbol_table:
                    symbol_table[func.qualified_name] = file_path
                # Also register by simple name for top-level functions
                if not func.is_method and func.name not in symbol_table:
                    symbol_table[func.name] = file_path

            for cls in metadata.classes:
                if cls.name not in symbol_table:
                    symbol_table[cls.name] = file_path

        return symbol_table

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
            resolved = self._resolve_import(file_path, imp.module, imp.is_from_import, all_file_set)
            if resolved and resolved != file_path:
                targets.add(resolved)

        return targets

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

        for file_path, metadata in files.items():
            # Collect imported names mapping: local_name -> qualified_name (symbol table key)
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
                    dep_qualified = None

                    # Check if it's an imported name
                    if dep in imported_names:
                        dep_qualified = imported_names[dep]

                    # Check if it's a local name in the same file
                    if dep_qualified is None:
                        candidate = self._qualify_name(dep, file_path, symbol_table)
                        if candidate in all_symbols:
                            dep_qualified = candidate

                    # Check if it's a known global symbol
                    if dep_qualified is None and dep in all_symbols:
                        dep_qualified = dep

                    if dep_qualified and dep_qualified != source_qualified:
                        global_graph[source_qualified].add(dep_qualified)

            # Now handle cross-file dependencies by scanning function/class bodies
            # for references to imported names (which the per-file dep graph misses).
            if not imported_names:
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

        return global_graph

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
