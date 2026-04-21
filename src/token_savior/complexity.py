"""Complexity hotspot detection for Token Savior."""

from __future__ import annotations

import os

from token_savior.models import ProjectIndex

# Branching keywords to count (must appear as a substring of a stripped line)
_BRANCH_KEYWORDS = (
    "if ",
    "elif ",
    "else:",
    "for ",
    "while ",
    "except",
    "case ",
    "try:",
    "match ",
)
_BRACE_LANGUAGE_BRANCH_KEYWORDS = (
    "if (",
    "if(",
    "else if (",
    "else if(",
    "else {",
    "for (",
    "for(",
    "while (",
    "while(",
    "switch (",
    "switch(",
    "case ",
    "catch (",
    "catch(",
    "try {",
    "do {",
)
_BRACE_LANGUAGE_EXTENSIONS = frozenset(
    {".java", ".js", ".jsx", ".ts", ".tsx", ".go", ".rs", ".cs", ".c", ".h"}
)

def _compute_nesting_depth(lines: list[str], file_path: str | None = None) -> int:
    """Find max indentation depth relative to function's base indentation."""
    if file_path and os.path.splitext(file_path)[1].lower() in _BRACE_LANGUAGE_EXTENSIONS:
        return _compute_brace_nesting_depth(lines)
    if not lines:
        return 0

    # Determine the base indentation from the first non-empty line
    base_indent: int | None = None
    for line in lines:
        if line.strip():
            base_indent = len(line) - len(line.lstrip())
            break

    if base_indent is None:
        return 0

    max_depth = 0
    for line in lines:
        if not line.strip():
            continue
        indent = len(line) - len(line.lstrip())
        relative = indent - base_indent
        if relative < 0:
            relative = 0
        # depth is how many 4-space levels beyond the base
        depth = relative // 4
        if depth > max_depth:
            max_depth = depth

    return max_depth


def _compute_brace_nesting_depth(lines: list[str]) -> int:
    """Estimate nesting depth for brace-delimited languages."""
    current_depth = 0
    max_depth = 0
    for line in lines:
        stripped = line.strip()
        if not stripped:
            continue
        current_depth = max(0, current_depth - stripped.count("}"))
        max_depth = max(max_depth, max(0, current_depth - 1))
        current_depth += stripped.count("{")
        max_depth = max(max_depth, max(0, current_depth - 1))
    return max_depth


def _count_branches(lines: list[str], file_path: str | None = None) -> int:
    """Count branching keywords in lines."""
    keywords = (
        _BRACE_LANGUAGE_BRANCH_KEYWORDS
        if file_path and os.path.splitext(file_path)[1].lower() in _BRACE_LANGUAGE_EXTENSIONS
        else _BRANCH_KEYWORDS
    )
    count = 0
    for line in lines:
        stripped = line.lstrip()
        for kw in keywords:
            if stripped.startswith(kw) or stripped == kw.rstrip():
                count += 1
                break  # at most one keyword match per line
    return count


def _score_function(line_count: int, branch_count: int, nesting: int, param_count: int) -> float:
    """Compute weighted complexity score."""
    return line_count * 0.3 + branch_count * 2.0 + nesting * 1.5 + max(0, param_count - 4) * 1.0


def find_hotspots(
    index: ProjectIndex,
    max_results: int = 20,
    min_score: float = 0.0,
) -> str:
    """Analyse every function in the index and return a formatted complexity report.

    Results are grouped into actionability tiers (0=most actionable):
      T0  high score (>=20) AND body contains TODO/FIXME/HACK/XXX
      T1  high score (>=30)
      T2  medium score (>=15)
      T3  rest

    Args:
        index: The project index to analyse.
        max_results: Maximum number of functions to include in the report.
        min_score: Minimum complexity score to include a function.

    Returns:
        A formatted string report of the top complexity hotspots.
    """
    from token_savior.project_indexer import is_path_excluded_from_scans

    import re as _re
    _MARKER_RE = _re.compile(r"\b(TODO|FIXME|XXX|HACK)\b", _re.IGNORECASE)

    results: list[tuple[int, float, int, int, int, str, int, str, bool]] = []
    # tuple: (tier, score, line_count, branch_count, nesting, func_name, start_line, file_path, has_marker)

    for file_path, meta in index.files.items():
        if is_path_excluded_from_scans(file_path):
            continue
        for func in meta.functions:
            start = func.line_range.start  # 1-indexed
            end = func.line_range.end  # 1-indexed

            func_lines = meta.lines[start - 1 : end]

            line_count = end - start + 1
            branch_count = _count_branches(func_lines, file_path)
            nesting = _compute_nesting_depth(func_lines, file_path)
            param_count = len(func.parameters)

            score = _score_function(line_count, branch_count, nesting, param_count)

            if score < min_score:
                continue

            body_text = "\n".join(func_lines)
            has_marker = bool(_MARKER_RE.search(body_text))

            if score >= 20 and has_marker:
                tier = 0
            elif score >= 30:
                tier = 1
            elif score >= 15:
                tier = 2
            else:
                tier = 3

            results.append(
                (
                    tier,
                    score,
                    line_count,
                    branch_count,
                    nesting,
                    func.qualified_name,
                    start,
                    file_path,
                    has_marker,
                )
            )

    if not results:
        return "No functions found."

    # Primary sort: tier asc (T0 first), then score desc.
    results.sort(key=lambda r: (r[0], -r[1]))
    results = results[:max_results]

    n = len(results)
    lines_out: list[str] = [
        f"Complexity Hotspots -- top {n} function{'s' if n != 1 else ''} (T0=most actionable)",
        "",
        "Tier | Score | Lines | Branches | Depth | Flag | Function",
        "-----+-------+-------+----------+-------+------+---------",
    ]

    for tier, score, line_count, branch_count, nesting, qualified_name, start_line, file_path, has_marker in results:
        flag = "TODO" if has_marker else "    "
        location = f"{file_path}:{start_line} {qualified_name}()"
        lines_out.append(
            f"  T{tier} | {score:5.1f} | {line_count:5d} | {branch_count:8d} | {nesting:5d} | {flag} | {location}"
        )

    return "\n".join(lines_out)
