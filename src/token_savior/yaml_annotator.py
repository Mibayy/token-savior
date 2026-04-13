"""YAML annotator that extracts structural metadata from YAML files.

Maps YAML's nested key structure to SectionInfo entries (title=key, level=depth).
Mirrors the json_annotator pattern: depth-capped recursive walk with line search.
"""

import re

import yaml

from token_savior.generic_annotator import annotate_generic
from token_savior.models import (
    LineRange,
    SectionInfo,
    StructuralMetadata,
    build_line_char_offsets,
)

_MAX_DEPTH = 4
_DISTINGUISHING_FIELDS = ("name", "id", "type", "key", "title")


def _find_key_line(lines: list[str], key: str, start_from: int = 0) -> int:
    """Find the 1-indexed line number where a YAML key appears.

    Searches for the pattern "key:" in the raw lines, starting from start_from
    (0-indexed). Returns 1 if not found (safe fallback).
    """
    escaped_key = re.escape(key)
    pattern = re.compile(rf"^\s*{escaped_key}\s*:")
    for i in range(start_from, len(lines)):
        if pattern.search(lines[i]):
            return i + 1  # 1-indexed
    return 1


def _walk_structure(
    obj: object,
    lines: list[str],
    path: str,
    depth: int,
    sections: list[SectionInfo],
    line_hint: int,
) -> None:
    """Recursively walk parsed YAML, emitting SectionInfo entries."""
    if depth > _MAX_DEPTH:
        return

    if isinstance(obj, dict):
        for key, value in obj.items():
            key_str = str(key)
            key_line = _find_key_line(lines, key_str, line_hint)
            sections.append(
                SectionInfo(
                    title=key_str,
                    level=depth,
                    line_range=LineRange(start=key_line, end=key_line),
                )
            )
            _walk_structure(
                value,
                lines,
                f"{path}.{key_str}",
                depth + 1,
                sections,
                max(0, key_line - 1),
            )

    elif isinstance(obj, list):
        for i, item in enumerate(obj):
            if isinstance(item, dict):
                # Look for a distinguishing field to label the entry
                label = None
                for field in _DISTINGUISHING_FIELDS:
                    if field in item and isinstance(item[field], str):
                        label = item[field]
                        break

                if label is not None:
                    entry_title = f"{path.rsplit('.', 1)[-1]}[{i}] {label}"
                    entry_line = _find_key_line(lines, label, line_hint)
                    sections.append(
                        SectionInfo(
                            title=entry_title,
                            level=depth,
                            line_range=LineRange(start=entry_line, end=entry_line),
                        )
                    )
                    _walk_structure(
                        item,
                        lines,
                        f"{path}[{i}]",
                        depth + 1,
                        sections,
                        max(0, entry_line - 1),
                    )
                else:
                    # No label — still recurse but don't create a section entry
                    _walk_structure(
                        item,
                        lines,
                        f"{path}[{i}]",
                        depth + 1,
                        sections,
                        line_hint,
                    )


def annotate_yaml(text: str, source_name: str = "<yaml>") -> StructuralMetadata:
    """Parse YAML text and extract structural metadata.

    Extraction rules:
    - Mapping keys at each nesting level become SectionInfo(title=key, level=depth)
    - Sequence elements that are mappings with a distinguishing field (name/id/type/key/title)
      become labeled SectionInfo entries
    - Depth capped at 4 to avoid noise
    - Invalid YAML or non-mapping/list result falls back to annotate_generic()
    """
    try:
        parsed = yaml.safe_load(text)
    except yaml.YAMLError:
        return annotate_generic(text, source_name)

    if not isinstance(parsed, (dict, list)):
        return annotate_generic(text, source_name)

    lines = text.split("\n")
    total_lines = len(lines)
    total_chars = len(text)
    line_offsets = build_line_char_offsets(lines)

    sections: list[SectionInfo] = []

    _walk_structure(parsed, lines, "", 1, sections, 0)

    return StructuralMetadata(
        source_name=source_name,
        total_lines=total_lines,
        total_chars=total_chars,
        lines=lines,
        line_char_offsets=line_offsets,
        sections=sections,
    )
