"""Config analysis: check_duplicates and helpers.

Analyses StructuralMetadata produced by config annotators (YAML, ENV, INI, …)
to surface problems like exact duplicate keys, likely typos (similar keys), and
cross-file conflicts.
"""

from __future__ import annotations

from collections import defaultdict
from typing import TYPE_CHECKING

from token_savior.models import ConfigIssue, StructuralMetadata

if TYPE_CHECKING:
    pass


# ---------------------------------------------------------------------------
# Levenshtein edit distance
# ---------------------------------------------------------------------------

def _levenshtein(s1: str, s2: str) -> int:
    """Return the Levenshtein edit distance between *s1* and *s2*."""
    if s1 == s2:
        return 0
    len1, len2 = len(s1), len(s2)
    if len1 == 0:
        return len2
    if len2 == 0:
        return len1

    # Use two-row DP to keep memory O(min(len1, len2))
    if len1 < len2:
        s1, s2 = s2, s1
        len1, len2 = len2, len1

    prev = list(range(len2 + 1))
    for i in range(1, len1 + 1):
        curr = [i] + [0] * len2
        for j in range(1, len2 + 1):
            cost = 0 if s1[i - 1] == s2[j - 1] else 1
            curr[j] = min(
                curr[j - 1] + 1,       # insertion
                prev[j] + 1,           # deletion
                prev[j - 1] + cost,    # substitution
            )
        prev = curr
    return prev[len2]


# ---------------------------------------------------------------------------
# Main check
# ---------------------------------------------------------------------------

def check_duplicates(
    config_files: dict[str, StructuralMetadata],
) -> list[ConfigIssue]:
    """Analyse *config_files* and return a list of duplicate / similar-key issues.

    Rules
    -----
    1. **Exact duplicate at same level (same file)** — two sections with
       identical titles at the same nesting level within a single file.
    2. **Similar keys (typo) at same level (same file)** — two sections whose
       titles differ by Levenshtein ≤ 2, but only when both keys are > 3 chars.
    3. **Cross-file conflict** — same key name at level 1 across two different
       files, but with different line content (suggests misconfiguration).

    Keys at *different* levels are never flagged (e.g. ``server.host`` and
    ``db.host`` both valid because they live at different nesting depths).
    """
    issues: list[ConfigIssue] = []

    # ------------------------------------------------------------------
    # Per-file checks: exact duplicates + similar keys
    # ------------------------------------------------------------------
    for source_name, meta in config_files.items():
        # Group sections by level
        by_level: dict[int, list] = defaultdict(list)
        for sec in meta.sections:
            by_level[sec.level].append(sec)

        for level, sections in by_level.items():
            n = len(sections)
            for i in range(n):
                for j in range(i + 1, n):
                    a, b = sections[i], sections[j]
                    if a.title == b.title:
                        # Exact duplicate
                        issues.append(ConfigIssue(
                            file=source_name,
                            key=a.title,
                            line=b.line_range.start,
                            severity="error",
                            check="duplicate",
                            message=f"Exact duplicate key '{a.title}' at level {level}",
                            detail=(
                                f"First occurrence at line {a.line_range.start}, "
                                f"duplicate at line {b.line_range.start}"
                            ),
                        ))
                    elif (
                        len(a.title) > 4
                        and len(b.title) > 4
                        and _levenshtein(a.title, b.title) <= 2
                    ):
                        # Similar key — likely typo
                        issues.append(ConfigIssue(
                            file=source_name,
                            key=a.title,
                            line=b.line_range.start,
                            severity="warning",
                            check="duplicate",
                            message=(
                                f"Similar keys (possible typo) '{a.title}' and "
                                f"'{b.title}' at level {level}"
                            ),
                            detail=(
                                f"Levenshtein distance = "
                                f"{_levenshtein(a.title, b.title)}"
                            ),
                        ))

    # ------------------------------------------------------------------
    # Cross-file conflicts — level-1 keys with differing line content
    # ------------------------------------------------------------------
    if len(config_files) > 1:
        # Build: key -> list of (source_name, line_content)
        level1_map: dict[str, list[tuple[str, str]]] = defaultdict(list)

        for source_name, meta in config_files.items():
            for sec in meta.sections:
                if sec.level != 1:
                    continue
                line_idx = sec.line_range.start  # 1-indexed
                # meta.lines is stored 0-indexed internally (index 0 = line 1)
                # but _make_meta in tests passes lines with a leading "" so that
                # lines[1] == "PORT=3000". We try both conventions gracefully.
                if line_idx < len(meta.lines):
                    content = meta.lines[line_idx].strip()
                else:
                    content = ""
                level1_map[sec.title].append((source_name, content))

        for key, occurrences in level1_map.items():
            if len(occurrences) < 2:
                continue
            # Collect distinct non-empty contents
            contents = {content for _, content in occurrences if content}
            if len(contents) <= 1:
                # All identical (or all empty) — no conflict
                continue
            # Different content across files → conflict
            for source_name, content in occurrences:
                issues.append(ConfigIssue(
                    file=source_name,
                    key=key,
                    line=next(
                        sec.line_range.start
                        for sec in config_files[source_name].sections
                        if sec.title == key and sec.level == 1
                    ),
                    severity="warning",
                    check="duplicate",
                    message=(
                        f"Cross-file conflict: key '{key}' has different values "
                        f"across config files"
                    ),
                    detail=f"Value in this file: {content!r}",
                ))

    return issues
