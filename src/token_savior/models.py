"""Structural metadata models for Token Savior."""

from dataclasses import dataclass, field


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


@dataclass(frozen=True)
class ClassInfo:
    """Metadata about a class."""

    name: str
    line_range: LineRange
    base_classes: list[str]
    methods: list[FunctionInfo]
    decorators: list[str]
    docstring: str | None


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


@dataclass
class StructuralMetadata:
    """Complete structural metadata for a single file or text document."""

    # Source
    source_name: str  # Filename or identifier
    total_lines: int
    total_chars: int

    # Line data (always populated)
    lines: list[str]  # All lines (0-indexed internally, but API uses 1-indexed)
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

    # Stats
    total_files: int = 0
    total_lines: int = 0
    total_functions: int = 0
    total_classes: int = 0
    index_build_time_seconds: float = 0.0
    index_memory_bytes: int = 0

    # Git tracking
    last_indexed_git_ref: str | None = None


@dataclass
class ConfigIssue:
    """A single issue found by config analysis."""

    file: str
    key: str
    line: int
    severity: str   # "error" | "warning" | "info"
    check: str      # "duplicate" | "secret" | "orphan"
    message: str
    detail: str | None = None
