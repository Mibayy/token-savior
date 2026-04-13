"""Annotator for INI config files and Java .properties files."""

import configparser
import re

from token_savior.generic_annotator import annotate_generic
from token_savior.models import LineRange, SectionInfo, StructuralMetadata, build_line_char_offsets


def _is_properties_file(source_name: str) -> bool:
    """Return True if source_name has a .properties extension."""
    return source_name.lower().endswith(".properties")


# Removed: _find_line was unused dead code


def _parse_properties(text: str, source_name: str) -> StructuralMetadata:
    """Parse a Java .properties file (KEY=VALUE lines, no sections)."""
    lines = text.splitlines()
    total_lines = len(lines)
    total_chars = len(text)
    line_offsets = build_line_char_offsets(lines)

    sections: list[SectionInfo] = []

    for i, line in enumerate(lines):
        stripped = line.strip()
        # Skip blank lines and comments
        if not stripped or stripped.startswith("#") or stripped.startswith("!"):
            continue
        # Match KEY=VALUE or KEY:VALUE
        m = re.match(r"^([^=:#!\s][^=:]*?)\s*[=:](.*)", stripped)
        if m:
            key = m.group(1).strip()
            line_num = i + 1  # 1-indexed
            sections.append(
                SectionInfo(
                    title=key,
                    level=2,
                    line_range=LineRange(start=line_num, end=line_num),
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


def annotate_ini(text: str, source_name: str = "<ini>") -> StructuralMetadata:
    """Parse an INI or .properties file and extract structural metadata.

    Dispatch rules:
    - source_name ending in .properties -> parse as Java properties (KEY=VALUE, no sections)
    - Otherwise -> parse as INI using configparser
      - Section headers become SectionInfo(level=1)
      - Keys under each section become SectionInfo(level=2)
      - DEFAULT section is included if it has keys
    - Falls back to annotate_generic on parse failure
    """
    if _is_properties_file(source_name):
        return _parse_properties(text, source_name)

    lines = text.splitlines()
    total_lines = len(lines)
    total_chars = len(text)
    line_offsets = build_line_char_offsets(lines)

    try:
        parser = configparser.ConfigParser(interpolation=None)
        parser.read_string(text)
    except Exception:
        return annotate_generic(text, source_name)

    sections: list[SectionInfo] = []

    # Helper: find the 1-indexed line number of a section header or key
    def find_section_line(section_name: str) -> int:
        pattern = re.compile(r"^\s*\[" + re.escape(section_name) + r"\]\s*$", re.IGNORECASE)
        for i, line in enumerate(lines):
            if pattern.match(line):
                return i + 1  # 1-indexed
        return 1

    def find_key_line(key: str, after_line: int) -> int:
        """Find 1-indexed line of 'key = ...' at or after after_line (1-indexed)."""
        pattern = re.compile(r"^\s*" + re.escape(key) + r"\s*[=:]", re.IGNORECASE)
        for i in range(after_line - 1, len(lines)):
            if pattern.match(lines[i]):
                return i + 1
        return after_line

    # Handle DEFAULT section explicitly if it has keys
    default_keys = list(parser.defaults().keys())
    if default_keys:
        default_line = find_section_line("DEFAULT")
        sections.append(
            SectionInfo(
                title="DEFAULT",
                level=1,
                line_range=LineRange(start=default_line, end=default_line),
            )
        )
        for key in default_keys:
            key_line = find_key_line(key, default_line)
            sections.append(
                SectionInfo(
                    title=key,
                    level=2,
                    line_range=LineRange(start=key_line, end=key_line),
                )
            )

    # Handle all non-DEFAULT sections
    for section in parser.sections():
        sec_line = find_section_line(section)
        sections.append(
            SectionInfo(
                title=section,
                level=1,
                line_range=LineRange(start=sec_line, end=sec_line),
            )
        )
        # Keys specific to this section (excluding defaults)
        section_keys = [k for k in parser.options(section) if k not in parser.defaults()]
        for key in section_keys:
            key_line = find_key_line(key, sec_line)
            sections.append(
                SectionInfo(
                    title=key,
                    level=2,
                    line_range=LineRange(start=key_line, end=key_line),
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
