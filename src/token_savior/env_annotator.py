"""Annotator for .env config files (KEY=VALUE format)."""

import re

from token_savior.models import LineRange, SectionInfo, StructuralMetadata

_ENV_LINE_RE = re.compile(r"^(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=(.*)")


def _build_line_offsets(lines: list[str]) -> list[int]:
    offsets: list[int] = []
    pos = 0
    for line in lines:
        offsets.append(pos)
        pos += len(line) + 1
    return offsets


def annotate_env(text: str, source_name: str = "<env>") -> StructuralMetadata:
    """Parse a .env file and return structural metadata.

    Each KEY=VALUE assignment becomes a SectionInfo with:
      - title = key name
      - level = 1
      - line_range = the single line (1-indexed, start == end)

    Lines starting with '#' (comments) and blank lines are skipped.
    The 'export' prefix is handled transparently.
    Quoted values and empty values are accepted without special treatment
    (the regex captures everything after '=' as the value).
    """
    lines = text.splitlines()
    total_lines = len(lines)
    total_chars = len(text)
    line_offsets = _build_line_offsets(lines)

    sections: list[SectionInfo] = []

    for i, line in enumerate(lines):
        stripped = line.strip()

        # Skip comments and empty lines
        if not stripped or stripped.startswith("#"):
            continue

        match = _ENV_LINE_RE.match(stripped)
        if match:
            key = match.group(1)
            lineno = i + 1  # 1-indexed
            sections.append(
                SectionInfo(
                    title=key,
                    level=1,
                    line_range=LineRange(start=lineno, end=lineno),
                )
            )

    return StructuralMetadata(
        source_name=source_name,
        total_lines=total_lines,
        total_chars=total_chars,
        lines=lines,
        line_char_offsets=line_offsets,
        sections=sections,
    )
