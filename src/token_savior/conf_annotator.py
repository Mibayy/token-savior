"""Best-effort regex parser for generic .conf files."""

import re
from token_savior.generic_annotator import annotate_generic
from token_savior.models import LineRange, SectionInfo, StructuralMetadata, build_line_char_offsets

_MAX_DEPTH = 4

# key = value  or  key: value  (first non-whitespace token is an identifier)
_KV_RE = re.compile(r"^(\s*)([A-Za-z_][\w.-]*)\s*[=:]\s*(.*)")

# block_name {
_BLOCK_RE = re.compile(r"^(\s*)([A-Za-z_][\w.-]*)\s*\{")

# Comment prefixes
_COMMENT_RE = re.compile(r"^\s*(#|;|//)")


def annotate_conf(text: str, source_name: str = "<conf>") -> StructuralMetadata:
    """Parse a generic .conf file and extract structural metadata.

    Detection rules:
    - key = value and key: value patterns become SectionInfo at the current depth
    - block_name { opens a new depth level; closing } decrements depth
    - Comments (#, ;, //) are skipped
    - Depth is capped at _MAX_DEPTH (4)
    - Falls back to annotate_generic if no sections found
    """
    lines = text.split("\n")
    total_lines = len(lines)
    total_chars = len(text)
    line_offsets = build_line_char_offsets(lines)

    sections: list[SectionInfo] = []
    depth = 0  # current brace nesting depth (0 = top level)

    for i, line in enumerate(lines):
        stripped = line.strip()

        # Skip blank lines
        if not stripped:
            continue

        # Skip comment lines
        if _COMMENT_RE.match(line):
            continue

        # Check for block opening: name {
        bm = _BLOCK_RE.match(stripped)
        if bm:
            block_depth = depth + 1  # depth after entering the block
            if block_depth <= _MAX_DEPTH:
                sections.append(
                    SectionInfo(
                        title=bm.group(2),
                        level=block_depth,
                        line_range=LineRange(start=i + 1, end=i + 1),
                    )
                )
            depth += 1
            continue

        # Check for closing brace
        if stripped == "}":
            if depth > 0:
                depth -= 1
            continue

        # Check for key = value or key: value
        kv = _KV_RE.match(stripped)
        if kv:
            kv_depth = depth + 1
            if kv_depth <= _MAX_DEPTH:
                sections.append(
                    SectionInfo(
                        title=kv.group(2),
                        level=kv_depth,
                        line_range=LineRange(start=i + 1, end=i + 1),
                    )
                )
            continue

    # Fall back to annotate_generic if nothing was detected
    if not sections:
        return annotate_generic(text, source_name)

    return StructuralMetadata(
        source_name=source_name,
        total_lines=total_lines,
        total_chars=total_chars,
        lines=lines,
        line_char_offsets=line_offsets,
        sections=sections,
    )
