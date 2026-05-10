"""Vue single-file component annotator.

Extracts JavaScript/TypeScript from ``<script>`` and ``<script setup>`` blocks,
then delegates to the existing TypeScript annotator. Non-script regions are
masked rather than removed so returned line numbers still point at the original
``.vue`` file.
"""

from __future__ import annotations

import re

from token_savior.models import StructuralMetadata, build_line_char_offsets
from token_savior.typescript_annotator import annotate_typescript

_SCRIPT_RE = re.compile(r"<script\b[^>]*>(.*?)</script>", re.IGNORECASE | re.DOTALL)


def _mask_non_script(source: str) -> str:
    masked = ["\n" if ch == "\n" else " " for ch in source]
    for match in _SCRIPT_RE.finditer(source):
        start, end = match.span(1)
        masked[start:end] = source[start:end]
    return "".join(masked)


def annotate_vue(source: str, source_name: str = "<vue>") -> StructuralMetadata:
    lines = source.split("\n")
    script_meta = annotate_typescript(_mask_non_script(source), source_name)

    return StructuralMetadata(
        source_name=source_name,
        total_lines=len(lines),
        total_chars=len(source),
        lines=lines,
        line_char_offsets=build_line_char_offsets(lines),
        functions=script_meta.functions,
        classes=script_meta.classes,
        imports=script_meta.imports,
    )
