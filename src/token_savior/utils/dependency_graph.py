"""Shared regex-based intra-file dependency graph builder.

Used by language annotators that have no dedicated AST/CST and rely on
identifier scanning of raw source text (currently C and Go).

Other annotators (Python, Java, ...) build their dependency graphs from
their respective parse trees and intentionally do **not** use this helper —
their analyses (overload disambiguation, type resolution, AST visitors)
have no equivalent in plain regex scanning.
"""

from __future__ import annotations

import re
from typing import Iterable

from token_savior.models import ClassInfo, FunctionInfo

# Matches a single identifier; shared between C and Go annotators since both
# languages use the same identifier grammar `[A-Za-z_][A-Za-z0-9_]*`.
_IDENT_RE = re.compile(r"\b([A-Za-z_]\w*)\b")


def build_dependency_graph(
    functions: Iterable[FunctionInfo],
    classes: Iterable[ClassInfo],
    lines: list[str],
    defined_names: set[str],
    keywords: frozenset[str],
) -> dict[str, list[str]]:
    """Build an intra-file name → dependencies graph by regex scanning bodies.

    For each function and class, the body text (delimited by ``line_range``)
    is scanned for identifiers. Identifiers are kept as dependencies when
    they are also in ``defined_names``, are not the symbol itself, and are
    not language keywords/built-ins.

    Parameters
    ----------
    functions:
        Function definitions to include as graph nodes.
    classes:
        Class / struct / type definitions to include as graph nodes.
    lines:
        Raw source split into lines. ``line_range`` is 1-based inclusive
        on both ends, matching the ``models`` convention.
    defined_names:
        The set of names that exist in the file. Anything outside this
        set is treated as external and dropped.
    keywords:
        Language keywords / built-ins to exclude from dependencies.

    Returns
    -------
    dict[str, list[str]]
        Mapping from each function/class name to a sorted list of the
        defined names it references. Self-references are removed.
    """
    graph: dict[str, list[str]] = {}

    for symbol in (*functions, *classes):
        start = symbol.line_range.start - 1  # 0-indexed
        end = symbol.line_range.end  # exclusive in the slice
        body_text = "\n".join(lines[start:end])
        refs = set(_IDENT_RE.findall(body_text))
        deps = sorted((refs & defined_names) - {symbol.name} - keywords)
        graph[symbol.name] = deps

    return graph
