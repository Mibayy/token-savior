"""XML/plist config annotator using stdlib xml.etree.ElementTree."""

import xml.etree.ElementTree as ET

from token_savior.generic_annotator import annotate_generic
from token_savior.models import LineRange, SectionInfo, StructuralMetadata, build_line_char_offsets

_MAX_DEPTH = 4
_DISTINGUISHING_ATTRS = ("name", "id", "type", "key", "title")


def _find_tag_line(lines: list[str], tag: str, start_from: int) -> int:
    """Search for '<tag' in lines starting from start_from (0-indexed).

    Returns 1-indexed line number of the first match, or start_from+1 as fallback.
    """
    needle = f"<{tag}"
    for i in range(start_from, len(lines)):
        if needle in lines[i]:
            return i + 1  # 1-indexed
    # Fallback: return the hint line (1-indexed)
    return max(1, start_from + 1)


def _strip_namespace(tag: str) -> str:
    """Strip Clark-notation XML namespace from tag name.

    Clark notation: {http://namespace.uri}localname  ->  localname
    """
    if "}" in tag:
        return tag.split("}", 1)[1]
    return tag


def _walk_element(
    elem: ET.Element,
    lines: list[str],
    depth: int,
    sections: list[SectionInfo],
    line_hint: int,
) -> None:
    """Recursively walk an element tree, emitting SectionInfo for each element."""
    if depth > _MAX_DEPTH:
        return

    tag = _strip_namespace(elem.tag)

    # Check for a distinguishing attribute to enrich the title
    label = None
    for attr in _DISTINGUISHING_ATTRS:
        if attr in elem.attrib:
            label = elem.attrib[attr]
            break

    title = f"{tag} {label}" if label is not None else tag

    # Find the line where this tag appears
    tag_line = _find_tag_line(lines, tag, line_hint)

    sections.append(
        SectionInfo(
            title=title,
            level=depth,
            line_range=LineRange(start=tag_line, end=tag_line),
        )
    )

    # Recurse into children (hint advances past current tag line)
    child_hint = tag_line  # 0-indexed start for next search = tag_line (already 1-indexed)
    for child in elem:
        _walk_element(child, lines, depth + 1, sections, child_hint)
        # Update hint so siblings search after previously found lines
        if sections:
            child_hint = sections[-1].line_range.start  # keep searching forward


def annotate_xml(text: str, source_name: str = "<xml>") -> StructuralMetadata:
    """Parse XML text and extract structural metadata.

    Extraction rules:
    - Each element becomes SectionInfo(title=tag_name, level=depth)
    - XML namespaces are stripped from tag names (Clark notation {uri}name -> name)
    - Elements with distinguishing attributes (name, id, type, key, title) get
      the attribute value appended to the title: "tag label"
    - Depth capped at 4 to avoid noise
    - Invalid XML falls back to annotate_generic()
    """
    try:
        root = ET.fromstring(text)
    except ET.ParseError:
        return annotate_generic(text, source_name)

    lines = text.split("\n")
    total_lines = len(lines)
    total_chars = len(text)
    line_offsets = build_line_char_offsets(lines)

    sections: list[SectionInfo] = []
    _walk_element(root, lines, 1, sections, 0)

    return StructuralMetadata(
        source_name=source_name,
        total_lines=total_lines,
        total_chars=total_chars,
        lines=lines,
        line_char_offsets=line_offsets,
        sections=sections,
    )
