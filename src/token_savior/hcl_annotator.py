"""HCL/Terraform configuration annotator.

Regex-based parser (no external dependencies).

Detects:
  - Block declarations: ``resource "type" "name" {`` -> SectionInfo
  - Key-value pairs: ``key = value`` -> SectionInfo
  - Brace nesting tracked for depth levels (capped at 4)
  - Comments (# and //) skipped
  - Falls back to annotate_generic if no sections found
"""

import re

from token_savior.generic_annotator import annotate_generic
from token_savior.models import LineRange, SectionInfo, StructuralMetadata, build_line_char_offsets

_MAX_DEPTH = 4

# Matches block declarations like: resource "aws_instance" "web" {
# Group 1: indent, Group 2: block type keyword, Group 3: labels (quoted strings)
_BLOCK_RE = re.compile(r'^(\s*)(\w+)\s+((?:"[^"]*"\s*)*)\{')

# Matches key-value pairs like: ami = "value" or count = 3
# Group 1: indent, Group 2: key name
_KV_RE = re.compile(r"^(\s*)(\w[\w-]*)\s*=\s*(.*)")


def annotate_hcl(text: str, source_name: str = "<hcl>") -> StructuralMetadata:
    """Parse HCL/Terraform text and extract structural metadata.

    Extraction rules:
    - Block declarations ``keyword "label1" "label2" {`` become
      SectionInfo(title="keyword label1 label2", level=current_depth+1)
    - Key-value pairs ``key = value`` inside blocks become
      SectionInfo(title=key, level=current_depth+1)
    - Depth tracked via brace nesting; capped at _MAX_DEPTH
    - Comments (# and //) are skipped
    - Falls back to annotate_generic if no sections found
    """
    lines = text.split("\n")
    total_lines = len(lines)
    total_chars = len(text)
    line_offsets = build_line_char_offsets(lines)

    sections: list[SectionInfo] = []
    # depth_stack tracks the depth when each '{' was opened so we can restore
    # on '}'.  current_depth is the nesting level (0 = top-level).
    current_depth = 0
    depth_stack: list[int] = []

    for lineno, raw_line in enumerate(lines, start=1):
        # Strip trailing whitespace but keep leading for indent measurement
        stripped = raw_line.strip()

        # Skip empty lines and comments
        if not stripped:
            continue
        if stripped.startswith("#") or stripped.startswith("//"):
            continue

        # Count braces on this line to update depth correctly.
        # We process the line's semantic content first, then update depth.

        # Check for block declaration: keyword "label1" "label2" {
        block_m = _BLOCK_RE.match(raw_line)

        # Check for key-value (only if no block match and not a bare closing brace)
        kv_m = None
        if not block_m and not stripped.startswith("}"):
            kv_m = _KV_RE.match(raw_line)

        if block_m:
            labels_raw = block_m.group(3).strip()
            # Extract quoted label values
            labels = re.findall(r'"([^"]*)"', labels_raw)
            block_type = block_m.group(2)
            title_parts = [block_type] + labels
            title = " ".join(title_parts)

            level = min(current_depth + 1, _MAX_DEPTH)
            sections.append(
                SectionInfo(
                    title=title,
                    level=level,
                    line_range=LineRange(start=lineno, end=lineno),
                )
            )

            # Opening brace: push current depth and increase
            depth_stack.append(current_depth)
            current_depth += 1

        elif kv_m:
            key = kv_m.group(2)
            level = min(current_depth + 1, _MAX_DEPTH)
            sections.append(
                SectionInfo(
                    title=key,
                    level=level,
                    line_range=LineRange(start=lineno, end=lineno),
                )
            )

        # Handle closing braces (may appear on lines without a block match)
        # Count all '}' on the line that are not inside strings
        brace_closes = _count_close_braces(stripped)
        for _ in range(brace_closes):
            if depth_stack:
                current_depth = depth_stack.pop()
            else:
                current_depth = max(0, current_depth - 1)

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


def _count_close_braces(stripped: str) -> int:
    """Count closing braces on a line, ignoring those inside strings."""
    count = 0
    in_string = False
    escape_next = False
    for ch in stripped:
        if escape_next:
            escape_next = False
            continue
        if ch == "\\":
            escape_next = True
            continue
        if ch == '"':
            in_string = not in_string
        elif ch == "}" and not in_string:
            count += 1
    return count
