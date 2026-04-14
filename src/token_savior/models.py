"""Structural metadata models for Token Savior."""

from __future__ import annotations

import os
from dataclasses import dataclass, field


def build_line_char_offsets(lines: list[str]) -> list[int]:
    """Compute character offset of each line start.

    Shared helper used by every language-specific annotator. For input
    ``lines`` (as produced by ``str.split("\\n")``), returns a list where
    ``offsets[i]`` is the character position of the start of line ``i+1``
    in the original text. Assumes a single-byte newline separator.

    Returns an empty list for empty input.
    """
    offsets: list[int] = []
    pos = 0
    for line in lines:
        offsets.append(pos)
        pos += len(line) + 1
    return offsets


@dataclass(frozen=True)
class LineRange:
    """A range of lines (1-indexed, inclusive on both ends)."""

    start: int
    end: int


@dataclass(frozen=True)
class FunctionInfo:
    """Metadata about a function or method."""

    name: str
    qualified_name: str  # e.g., "MyClass.my_method"
    line_range: LineRange
    parameters: list[str]
    decorators: list[str]  # Decorator names (without @)
    docstring: str | None
    is_method: bool
    parent_class: str | None  # None for top-level functions
    # Hashes filled post-annotation by ProjectIndexer.
    # Empty string means "not computed yet" (e.g. legacy cache, non-py annotator).
    signature_hash: str = ""  # SHA-256[:16] of public signature
    body_hash: str = ""  # SHA-256[:16] of normalized body
    decorator_details: dict[str, str] = field(default_factory=dict)
    visibility: str | None = None
    return_type: str | None = None


@dataclass(frozen=True)
class ClassInfo:
    """Metadata about a class."""

    name: str
    line_range: LineRange
    base_classes: list[str]
    methods: list[FunctionInfo]
    decorators: list[str]
    docstring: str | None
    body_hash: str = ""  # SHA-256[:16] of full normalized class body
    qualified_name: str | None = None
    decorator_details: dict[str, str] = field(default_factory=dict)
    visibility: str | None = None


@dataclass(frozen=True)
class ImportInfo:
    """Metadata about an import statement."""

    module: str  # e.g., "os.path"
    names: list[str]  # e.g., ["join", "exists"] for "from os.path import join, exists"
    alias: str | None  # e.g., "np" for "import numpy as np"
    line_number: int
    is_from_import: bool  # True for "from X import Y", False for "import X"


@dataclass(frozen=True)
class SectionInfo:
    """Metadata about a section in a text document."""

    title: str
    level: int  # Heading level (1 = top-level, 2 = subsection, etc.)
    line_range: LineRange


class LazyLines:
    """A list-like object that lazily reads file lines from disk.

    When constructed with ``data``, behaves like a plain list (used during
    fresh indexing).  When constructed with ``root_path`` + ``rel_path`` and
    no data, defers reading until first access — saving ~80 % idle RAM for
    cached indexes.
    """

    __slots__ = ("_data", "_root_path", "_rel_path")

    def __init__(
        self,
        data: list[str] | None = None,
        *,
        root_path: str = "",
        rel_path: str = "",
    ):
        self._data = data
        self._root_path = root_path
        self._rel_path = rel_path

    # -- lazy loading --------------------------------------------------------

    @property
    def _loaded(self) -> list[str]:
        if self._data is None:
            full_path = os.path.join(self._root_path, self._rel_path)
            try:
                with open(full_path, "r", encoding="utf-8", errors="replace") as f:
                    self._data = f.read().splitlines()
            except OSError:
                self._data = []
        return self._data

    # -- sequence protocol ---------------------------------------------------

    def __getitem__(self, key):  # type: ignore[override]
        return self._loaded[key]

    def __iter__(self):
        return iter(self._loaded)

    def __len__(self):
        return len(self._loaded)

    def __contains__(self, item):
        return item in self._loaded

    def __bool__(self):
        return True

    def __repr__(self):
        if self._data is not None:
            return f"LazyLines({len(self._data)} lines, loaded)"
        return f"LazyLines('{self._rel_path}', deferred)"

    # -- mutation helpers (used by extend patterns) --------------------------

    def extend(self, other):
        self._loaded.extend(other)

    def append(self, item):
        self._loaded.append(item)

    # -- cache management ----------------------------------------------------

    def invalidate(self) -> None:
        """Drop cached data so the next access re-reads from disk."""
        self._data = None

    @property
    def is_loaded(self) -> bool:
        return self._data is not None


@dataclass
class StructuralMetadata:
    """Complete structural metadata for a single file or text document."""

    # Source
    source_name: str  # Filename or identifier
    total_lines: int
    total_chars: int

    # Line data — may be a LazyLines (deferred disk read) or plain list
    lines: list[str] | LazyLines  # All lines (0-indexed internally, API is 1-indexed)
    line_char_offsets: list[int]  # Character offset of each line start

    # Code structure (populated for code files)
    functions: list[FunctionInfo] = field(default_factory=list)
    classes: list[ClassInfo] = field(default_factory=list)
    imports: list[ImportInfo] = field(default_factory=list)

    # Text structure (populated for text/markdown files)
    sections: list[SectionInfo] = field(default_factory=list)

    # Dependency map (populated for code files)
    # Maps each function/class name to the names it references
    dependency_graph: dict[str, list[str]] = field(default_factory=dict)
    module_name: str | None = None


@dataclass
class ProjectIndex:
    """Structural index for an entire codebase."""

    root_path: str
    files: dict[str, StructuralMetadata] = field(default_factory=dict)

    # Cross-file dependency graphs
    global_dependency_graph: dict[str, set[str]] = field(default_factory=dict)
    reverse_dependency_graph: dict[str, set[str]] = field(default_factory=dict)

    # File-level import graph
    import_graph: dict[str, set[str]] = field(default_factory=dict)
    reverse_import_graph: dict[str, set[str]] = field(default_factory=dict)

    # Global symbol table: symbol_name -> file_path where defined
    symbol_table: dict[str, str] = field(default_factory=dict)
    duplicate_classes: dict[str, list[str]] = field(default_factory=dict)

    # Stats
    total_files: int = 0
    total_lines: int = 0
    total_functions: int = 0
    total_classes: int = 0
    index_build_time_seconds: float = 0.0
    index_memory_bytes: int = 0

    # Git tracking
    last_indexed_git_ref: str | None = None

    # File modification times for change detection (rel_path -> mtime)
    file_mtimes: dict[str, float] = field(default_factory=dict)

    # Per-symbol body hashes for symbol-level reindex.
    # key = f"{rel_path}:{qualified_name}", value = SHA-256[:16] of body.
    # Empty/missing entries are treated as "recompute".
    symbol_hashes: dict[str, str] = field(default_factory=dict)

    # Stats from the last reindex_file (transient, not persisted).
    last_reindex_symbols_checked: int = 0
    last_reindex_symbols_unchanged: int = 0
    last_reindex_symbols_reindexed: int = 0

    # Pre-sorted file paths (stable order for search_codebase and other
    # whole-project scans). Refreshed whenever files dict mutates
    # (index, reindex_file, remove_file). Transient.
    sorted_paths: list[str] = field(default_factory=list)

    # basename -> list of full rel paths. Lets _resolve_file short-circuit
    # the O(n) endswith fallback with an O(1) lookup when the caller passes
    # a bare filename. Refreshed alongside sorted_paths.
    basename_map: dict[str, list[str]] = field(default_factory=dict)


@dataclass
class ConfigIssue:
    """A single issue found by config analysis."""

    file: str
    key: str
    line: int
    severity: str  # "error" | "warning" | "info"
    check: str  # "duplicate" | "secret" | "orphan"
    message: str
    detail: str | None = None


from typing import Protocol, runtime_checkable  # noqa: E402


@runtime_checkable
class AnnotatorProtocol(Protocol):
    def __call__(self, source_text: str, source_name: str) -> StructuralMetadata: ...
