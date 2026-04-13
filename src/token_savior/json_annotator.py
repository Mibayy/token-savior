"""JSON annotator that extracts structural metadata from JSON files.

Maps JSON's nested key structure to SectionInfo entries (title=key, level=depth).
Captures $ref values as ImportInfo for JSON Schema cross-references.
"""

import json
import re

from token_savior.generic_annotator import annotate_generic
from token_savior.models import (
    ImportInfo,
    LineRange,
    SectionInfo,
    StructuralMetadata,
    build_line_char_offsets,
)

_MAX_DEPTH = 4
_DISTINGUISHING_FIELDS = ("name", "id", "type")


def _find_key_line(lines: list[str], key: str, start_from: int = 0) -> int:
    """Find the 1-indexed line number where a JSON key appears.

    Searches for the pattern "key": in the raw lines, starting from start_from
    (0-indexed). Returns 1 if not found (safe fallback for minified JSON).
    """
    escaped_key = re.escape(key)
    pattern = re.compile(rf'"\s*{escaped_key}\s*"\s*:')
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
    imports: list[ImportInfo],
    line_hint: int,
) -> None:
    """Recursively walk parsed JSON, emitting SectionInfo and ImportInfo entries."""
    if depth > _MAX_DEPTH:
        return

    if isinstance(obj, dict):
        for key, value in obj.items():
            # Capture $ref values as imports
            if key == "$ref" and isinstance(value, str):
                key_line = _find_key_line(lines, "$ref", line_hint)
                imports.append(
                    ImportInfo(
                        module=value,
                        names=[],
                        alias=None,
                        line_number=key_line,
                        is_from_import=False,
                    )
                )
                continue

            key_line = _find_key_line(lines, key, line_hint)
            sections.append(
                SectionInfo(
                    title=key,
                    level=depth,
                    line_range=LineRange(start=key_line, end=key_line),
                )
            )
            _walk_structure(
                value,
                lines,
                f"{path}.{key}",
                depth + 1,
                sections,
                imports,
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
                    # Try to find the distinguishing field's line
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
                        imports,
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
                        imports,
                        line_hint,
                    )


def annotate_json(text: str, source_name: str = "<json>") -> StructuralMetadata:
    """Parse JSON text and extract structural metadata.

    Extraction rules:
    - Object keys at each nesting level become SectionInfo(title=key, level=depth)
    - Array elements that are objects with a distinguishing field (name/id/type)
      become labeled SectionInfo entries
    - $ref values become ImportInfo entries
    - Depth capped at 4 to avoid noise
    - Invalid JSON falls back to annotate_generic()
    """
    try:
        parsed = json.loads(text)
    except (json.JSONDecodeError, ValueError):
        return annotate_generic(text, source_name)

    lines = text.split("\n")
    total_lines = len(lines)
    total_chars = len(text)
    line_offsets = build_line_char_offsets(lines)

    sections: list[SectionInfo] = []
    imports: list[ImportInfo] = []

    _walk_structure(parsed, lines, "", 1, sections, imports, 0)

    return StructuralMetadata(
        source_name=source_name,
        total_lines=total_lines,
        total_chars=total_chars,
        lines=lines,
        line_char_offsets=line_offsets,
        sections=sections,
        imports=imports,
    )
