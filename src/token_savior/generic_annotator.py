"""Generic fallback annotator providing line-only metadata."""

from token_savior.models import StructuralMetadata


def annotate_generic(text: str, source_name: str = "<source>") -> StructuralMetadata:
    """Create minimal structural metadata with just line information."""
    lines = text.splitlines()
    offsets: list[int] = []
    offset = 0
    for line in lines:
        offsets.append(offset)
        offset += len(line) + 1  # +1 for newline

    return StructuralMetadata(
        source_name=source_name,
        total_lines=len(lines),
        total_chars=len(text),
        lines=lines,
        line_char_offsets=offsets,
    )
