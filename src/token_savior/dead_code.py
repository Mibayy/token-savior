"""Dead code detection for Token Savior.

Finds functions and classes in a ProjectIndex that have no known callers and
are not considered entry points (routes, test helpers, __init__, etc.).
"""

from __future__ import annotations

import os
from dataclasses import dataclass

from token_savior.models import ClassInfo, FunctionInfo, ProjectIndex


# ---------------------------------------------------------------------------
# Entry-point detection
# ---------------------------------------------------------------------------

_ENTRY_POINT_NAMES = frozenset({"main", "__init__", "__main__"})

_ENTRY_POINT_DECORATOR_KEYWORDS = frozenset(
    {
        "route",
        "app.",
        "api.",
        "click",
        "command",
        "task",
        "test",
        "fixture",
        "setup",
        "teardown",
    }
)

_ENTRY_POINT_CLASS_DECORATOR_KEYWORDS = frozenset({"dataclass", "model"})


def _is_test_file(file_path: str) -> bool:
    """Return True if the file is a test file (test_*.py or *_test.py)."""
    basename = os.path.basename(file_path)
    return basename.startswith("test_") or basename.endswith("_test.py")


def _is_init_file(file_path: str) -> bool:
    """Return True if the file is a package __init__.py."""
    return os.path.basename(file_path) == "__init__.py"


def _decorator_matches_keywords(decorators: list[str], keywords: frozenset[str]) -> bool:
    """Return True if any decorator string contains any of the given keywords."""
    for dec in decorators:
        dec_lower = dec.lower()
        for kw in keywords:
            if kw in dec_lower:
                return True
    return False


def _is_function_entry_point(func: FunctionInfo, file_path: str) -> bool:
    """Return True if this function should never be reported as dead code."""
    # Test files — all functions are excluded
    if _is_test_file(file_path):
        return True
    # Exports from __init__.py
    if _is_init_file(file_path):
        return True
    # Well-known entry-point names
    if func.name in _ENTRY_POINT_NAMES:
        return True
    # test_ prefix
    if func.name.startswith("test_"):
        return True
    # Decorator-based entry points
    if _decorator_matches_keywords(func.decorators, _ENTRY_POINT_DECORATOR_KEYWORDS):
        return True
    return False


def _is_class_entry_point(cls: ClassInfo, file_path: str) -> bool:
    """Return True if this class should never be reported as dead code."""
    if _is_test_file(file_path):
        return True
    if _is_init_file(file_path):
        return True
    if _decorator_matches_keywords(cls.decorators, _ENTRY_POINT_CLASS_DECORATOR_KEYWORDS):
        return True
    return False


# ---------------------------------------------------------------------------
# Core analysis
# ---------------------------------------------------------------------------


@dataclass
class _DeadSymbol:
    file_path: str
    line: int
    kind: str  # "function" or "class"
    name: str
    signature: str  # e.g. "unused_helper(x, y)" or just "OldProcessor"


def _collect_dead_symbols(index: ProjectIndex) -> list[_DeadSymbol]:
    rdg = index.reverse_dependency_graph
    dead: list[_DeadSymbol] = []

    for file_path, meta in index.files.items():
        # --- Functions (top-level and methods) ---
        for func in meta.functions:
            if _is_function_entry_point(func, file_path):
                continue
            symbol_key = func.qualified_name
            callers = rdg.get(symbol_key) or rdg.get(func.name)
            if callers:
                continue
            params_str = ", ".join(func.parameters)
            signature = f"{func.name}({params_str})"
            dead.append(
                _DeadSymbol(
                    file_path=file_path,
                    line=func.line_range.start,
                    kind="function",
                    name=func.name,
                    signature=signature,
                )
            )

        # --- Classes ---
        for cls in meta.classes:
            if _is_class_entry_point(cls, file_path):
                continue
            callers = rdg.get(cls.name)
            if callers:
                continue
            dead.append(
                _DeadSymbol(
                    file_path=file_path,
                    line=cls.line_range.start,
                    kind="class",
                    name=cls.name,
                    signature=cls.name,
                )
            )

    # Sort: file path first, then line number
    dead.sort(key=lambda s: (s.file_path, s.line))
    return dead


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def find_dead_code(index: ProjectIndex, max_results: int = 50) -> str:
    """Analyse *index* and return a formatted dead-code report.

    Parameters
    ----------
    index:
        The ProjectIndex produced by the project indexer.
    max_results:
        Maximum number of dead symbols to include in the output (default 50).
        The header always shows the true total count.

    Returns
    -------
    str
        Multi-line report string.
    """
    all_dead = _collect_dead_symbols(index)
    total = len(all_dead)
    shown = all_dead[:max_results]

    # Header
    symbol_word = "symbol" if total == 1 else "symbols"
    lines: list[str] = [
        f"Dead Code Analysis -- {total} unreferenced {symbol_word} found",
        "",
    ]

    if not shown:
        return lines[0]

    # Group by file
    current_file: str | None = None
    for sym in shown:
        if sym.file_path != current_file:
            current_file = sym.file_path
            lines.append(f"{sym.file_path}:")
        lines.append(f"  line {sym.line}: {sym.kind} {sym.signature}")

    return "\n".join(lines)
