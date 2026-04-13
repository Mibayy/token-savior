"""Text/Markdown annotator using heuristic heading detection."""

import re

from token_savior.models import LineRange, SectionInfo, StructuralMetadata, build_line_char_offsets


def annotate_text(text: str, source_name: str = "<text>") -> StructuralMetadata:
    """Parse text/markdown and extract section structure.

    Detection rules (applied per line, first match wins):
      1. Markdown headings: lines starting with # (level = number of #'s)
      2. Underline headings: a non-empty line followed by a line of === (level 1)
         or --- (level 2), where the underline is at least 3 characters
      3. Numbered section headings: lines matching r'^\\d+(\\.\\d+)*\\s+'
         (level = depth of numbering, e.g. '1.2.3 Foo' is level 3)
      4. ALL-CAPS lines of 4+ words: treated as level-2 headings

    A section's line_range extends from its heading line to the line before
    the next heading of equal or higher (i.e. smaller level number) level,
    or end of document.

    Returns StructuralMetadata with sections populated; functions/classes/imports
    are left empty.
    """
    lines = text.split("\n")
    total_lines = len(lines)
    total_chars = len(text)
    line_offsets = build_line_char_offsets(lines)

    # First pass: detect headings as (line_index_0based, title, level)
    headings: list[tuple[int, str, int]] = []

    # We need to look ahead for underline headings, so iterate by index.
    i = 0
    while i < total_lines:
        line = lines[i]
        stripped = line.strip()

        # Rule 2: check if *next* line is an underline for *this* line.
        # Must check before other rules so the underline line itself isn't
        # misidentified as something else.
        if i + 1 < total_lines and stripped and not stripped.startswith("#"):
            next_stripped = lines[i + 1].strip()
            if len(next_stripped) >= 3 and re.fullmatch(r"=+", next_stripped):
                headings.append((i, stripped, 1))
                i += 2  # skip the underline line
                continue
            if len(next_stripped) >= 3 and re.fullmatch(r"-+", next_stripped):
                headings.append((i, stripped, 2))
                i += 2
                continue

        # Rule 1: Markdown headings
        md_match = re.match(r"^(#{1,6})\s+(.*)", line)
        if md_match:
            level = len(md_match.group(1))
            title = md_match.group(2).strip()
            if title:
                headings.append((i, title, level))
                i += 1
                continue

        # Rule 3: Numbered sections  e.g. "1.2.3 Some Title"
        num_match = re.match(r"^(\d+(?:\.\d+)*)\s+(.*)", stripped)
        if num_match:
            numbering = num_match.group(1)
            title_text = num_match.group(2).strip()
            if title_text:
                level = numbering.count(".") + 1
                headings.append((i, f"{numbering} {title_text}", level))
                i += 1
                continue

        # Rule 4: ALL-CAPS lines of 4+ words
        if stripped:
            words = stripped.split()
            if len(words) >= 4 and stripped == stripped.upper() and re.search(r"[A-Z]", stripped):
                headings.append((i, stripped, 2))
                i += 1
                continue

        i += 1

    # Second pass: compute section line ranges.
    # Each section extends from its heading to just before the next heading
    # of equal or higher (smaller number) level, or end of document.
    sections: list[SectionInfo] = []

    for idx, (line_idx, title, level) in enumerate(headings):
        start = line_idx + 1  # 1-indexed

        # Find end: scan forward for next heading with level <= this level
        end = total_lines  # default: extends to end of document
        for future_idx in range(idx + 1, len(headings)):
            future_line_idx, _, future_level = headings[future_idx]
            if future_level <= level:
                end = future_line_idx  # line before this heading (0-indexed), = 1-indexed value
                break
            # If the next heading is deeper, the outer section still encompasses it,
            # but we keep scanning for one of equal/higher level.

        sections.append(
            SectionInfo(
                title=title,
                level=level,
                line_range=LineRange(start=start, end=end),
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
