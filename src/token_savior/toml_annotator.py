"""TOML config file annotator.

Parses TOML using stdlib tomllib (Python 3.11+) and extracts structural
metadata. Each key at every nesting level becomes a SectionInfo with
title=key and level=depth. Depth is capped at 4. Only nested dicts are
recursed into; scalar values and lists are recorded but not descended.
Invalid TOML falls back to annotate_generic.
"""

import re
import tomllib

from token_savior.generic_annotator import annotate_generic
from token_savior.models import LineRange, SectionInfo, StructuralMetadata, build_line_char_offsets

_MAX_DEPTH = 4


def _find_key_line(lines: list[str], key: str, start_from: int = 0) -> int:
    """Find the 1-indexed line number where a TOML key or table header appears.

    Searches for either:
    - A bare assignment:  key =
    - A table header:     [key] or [something.key]

    Starts searching from start_from (0-indexed). Returns 1 on no match.
    """
    escaped = re.escape(key)
    # Match "key =" (assignment) or "[...key]" / "[key..." style table headers
    pattern = re.compile(rf"(?:(?:^|\s){escaped}\s*=|^\s*\[.*?{escaped}.*?\])")
    for i in range(start_from, len(lines)):
        if pattern.search(lines[i]):
            return i + 1  # 1-indexed
    return 1


def _walk_structure(
    obj: object,
    lines: list[str],
    depth: int,
    sections: list[SectionInfo],
    line_hint: int,
) -> None:
    """Recursively walk parsed TOML dict, emitting SectionInfo entries.

    Only recurses into nested dicts. Arrays and scalars are recorded as
    sections (at the current depth) but not descended into.
    """
    if depth > _MAX_DEPTH:
        return

    if not isinstance(obj, dict):
        return

    for key, value in obj.items():
        key_line = _find_key_line(lines, key, line_hint)
        sections.append(
            SectionInfo(
                title=key,
                level=depth,
                line_range=LineRange(start=key_line, end=key_line),
            )
        )
        if isinstance(value, dict):
            _walk_structure(
                value,
                lines,
                depth + 1,
                sections,
                max(0, key_line - 1),
            )


def annotate_toml(text: str, source_name: str = "<toml>") -> StructuralMetadata:
    """Parse TOML text and extract structural metadata.

    Extraction rules:
    - Every key at each nesting level becomes a SectionInfo(title=key, level=depth)
    - Only nested dicts are recursed into; scalars and arrays are not
    - Depth is capped at 4
    - Invalid TOML falls back to annotate_generic()
    """
    try:
        parsed = tomllib.loads(text)
    except tomllib.TOMLDecodeError:
        return annotate_generic(text, source_name)

    lines = text.split("\n")
    # If text ends with \n, split produces a trailing empty string — drop it
    # but keep total_lines consistent with actual newline count
    total_lines = len(lines) if not text.endswith("\n") else len(lines) - 1
    total_chars = len(text)
    line_offsets = build_line_char_offsets(lines)

    sections: list[SectionInfo] = []
    _walk_structure(parsed, lines, 1, sections, 0)

    return StructuralMetadata(
        source_name=source_name,
        total_lines=total_lines,
        total_chars=total_chars,
        lines=lines,
        line_char_offsets=line_offsets,
        sections=sections,
    )
