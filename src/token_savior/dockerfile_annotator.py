"""Annotator for Dockerfile files."""

from __future__ import annotations

import re

from token_savior.models import LineRange, SectionInfo, StructuralMetadata, build_line_char_offsets

_INSTRUCTION_RE = re.compile(
    r"^(FROM|RUN|COPY|ADD|ENV|EXPOSE|CMD|ENTRYPOINT|ARG|WORKDIR|LABEL|VOLUME|USER|HEALTHCHECK|SHELL|STOPSIGNAL|ONBUILD)\s+(.+)",
    re.IGNORECASE,
)

# Instructions that are treated as stage markers (level 1)
_STAGE_INSTRUCTIONS = {"FROM"}

# Max length for value summary in title (excluding the instruction keyword)
_MAX_VALUE_LEN = 60


def _make_title(instruction: str, value: str) -> str:
    """Build a section title from instruction + value, truncating long values."""
    value = value.strip()
    if len(value) > _MAX_VALUE_LEN:
        value = value[:_MAX_VALUE_LEN] + "..."
    return f"{instruction} {value}"


def annotate_dockerfile(text: str, source_name: str = "<dockerfile>") -> StructuralMetadata:
    """Parse a Dockerfile and extract structural metadata as SectionInfo entries.

    Dispatch rules:
    - FROM instructions become level-1 sections (stage markers)
    - All other instructions become level-2 sections
    - Before the first FROM, instructions are assigned level 2
    - Comments (#) and blank lines are ignored
    - Long RUN/COPY/etc. values are truncated to ~60 chars in the title
    - ENV and ARG entries include the variable name in the title
    """
    lines = text.splitlines()
    total_lines = len(lines)
    total_chars = len(text)
    line_offsets = build_line_char_offsets(lines)

    sections: list[SectionInfo] = []

    for i, line in enumerate(lines):
        stripped = line.strip()

        # Skip blank lines and comments
        if not stripped or stripped.startswith("#"):
            continue

        m = _INSTRUCTION_RE.match(stripped)
        if not m:
            continue

        instruction = m.group(1).upper()
        value = m.group(2).strip()
        lineno = i + 1  # 1-indexed

        if instruction == "FROM":
            title = _make_title(instruction, value)
            sections.append(
                SectionInfo(
                    title=title,
                    level=1,
                    line_range=LineRange(start=lineno, end=lineno),
                )
            )
        else:
            # Build a meaningful title depending on instruction type
            title = _build_instruction_title(instruction, value)
            level = 2  # always level 2 whether or not we've seen FROM
            sections.append(
                SectionInfo(
                    title=title,
                    level=level,
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


def _build_instruction_title(instruction: str, value: str) -> str:
    """Build a human-readable title for an instruction, highlighting key names for ENV/ARG."""
    if instruction in ("ENV", "ARG"):
        # Extract variable name: "VAR=value" or "VAR value" or just "VAR"
        var_name = re.split(r"[=\s]", value, maxsplit=1)[0]
        if len(value) > _MAX_VALUE_LEN:
            return f"{instruction} {var_name} ..."
        return f"{instruction} {value}"
    else:
        return _make_title(instruction, value)
