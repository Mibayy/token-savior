"""Best-effort annotator for Gradle Groovy/Kotlin build scripts."""

from __future__ import annotations

import re

from token_savior.generic_annotator import annotate_generic
from token_savior.models import LineRange, SectionInfo, StructuralMetadata, build_line_char_offsets

_MAX_DEPTH = 4
_ASSIGNMENT_RE = re.compile(r"^\s*([A-Za-z_][\w.]*)\s*=\s*(.+)$")
_SETTER_RE = re.compile(r"^\s*([A-Za-z_][\w.]*)\.set\(")
_CALL_RE = re.compile(r"^\s*([A-Za-z_][\w.]*)\s*(?:<[^>]+>)?\((.*)\)\s*$")
_CONTROL_PREFIXES = ("if ", "for ", "while ", "when ", "else", "try", "catch", "do ")


def _normalize_ws(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def _strip_inline_comment(line: str) -> str:
    in_string = False
    quote_char = ""
    escaped = False

    for idx, ch in enumerate(line):
        if escaped:
            escaped = False
            continue
        if ch == "\\":
            escaped = True
            continue
        if in_string:
            if ch == quote_char:
                in_string = False
            continue
        if ch in {'"', "'"}:
            in_string = True
            quote_char = ch
            continue
        if line.startswith("//", idx):
            return line[:idx]
    return line


def _count_brace_delta(line: str) -> int:
    delta = 0
    in_string = False
    quote_char = ""
    escaped = False

    for ch in line:
        if escaped:
            escaped = False
            continue
        if ch == "\\":
            escaped = True
            continue
        if in_string:
            if ch == quote_char:
                in_string = False
            continue
        if ch in {'"', "'"}:
            in_string = True
            quote_char = ch
            continue
        if ch == "{":
            delta += 1
        elif ch == "}":
            delta -= 1
    return delta


def _first_call_label(args: str) -> str | None:
    string_match = re.search(r'["\']([^"\']+)["\']', args)
    if string_match:
        return string_match.group(1)

    token_match = re.search(r"([A-Za-z_:][\w:.-]*)", args)
    if token_match:
        return token_match.group(1)
    return None


def _collect_sections(lines: list[str]) -> list[SectionInfo]:
    sections: list[SectionInfo] = []
    depth = 0
    in_block_comment = False

    for lineno, raw_line in enumerate(lines, start=1):
        line = raw_line

        if in_block_comment:
            if "*/" not in line:
                continue
            line = line.split("*/", 1)[1]
            in_block_comment = False

        while "/*" in line:
            before, after = line.split("/*", 1)
            if "*/" in after:
                line = before + " " + after.split("*/", 1)[1]
                continue
            line = before
            in_block_comment = True
            break

        stripped = _strip_inline_comment(line).strip()
        if not stripped:
            continue

        level = min(max(depth + 1, 1), _MAX_DEPTH)
        title: str | None = None
        lowered = stripped.lower()

        if not any(lowered.startswith(prefix) for prefix in _CONTROL_PREFIXES):
            if stripped.endswith("{"):
                prelude = _normalize_ws(stripped[:-1])
                if prelude:
                    title = prelude
            else:
                assignment_match = _ASSIGNMENT_RE.match(stripped)
                if assignment_match:
                    title = assignment_match.group(1)
                else:
                    setter_match = _SETTER_RE.match(stripped)
                    if setter_match:
                        title = setter_match.group(1)
                    else:
                        call_match = _CALL_RE.match(stripped)
                        if call_match:
                            label = _first_call_label(call_match.group(2))
                            title = (
                                f"{call_match.group(1)} {label}"
                                if label
                                else call_match.group(1)
                            )

        if title:
            sections.append(
                SectionInfo(
                    title=title,
                    level=level,
                    line_range=LineRange(start=lineno, end=lineno),
                )
            )

        depth = max(0, depth + _count_brace_delta(stripped))

    return sections


def annotate_gradle(text: str, source_name: str = "<gradle>") -> StructuralMetadata:
    """Parse Gradle build scripts and extract block/statement structure."""
    lines = text.split("\n")
    total_lines = len(lines)
    total_chars = len(text)
    line_offsets = build_line_char_offsets(lines)
    sections = _collect_sections(lines)

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
