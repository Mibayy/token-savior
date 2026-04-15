"""Annotator for Prisma schema files (.prisma)."""

import re

from token_savior.models import (
    ClassInfo,
    FunctionInfo,
    LineRange,
    StructuralMetadata,
    build_line_char_offsets,
)


def annotate_prisma(text: str, source_name: str = "<prisma>") -> StructuralMetadata:
    """Parse a Prisma schema file and extract models as classes, fields as methods."""
    lines = text.splitlines()
    total_lines = len(lines)
    total_chars = len(text)
    line_offsets = build_line_char_offsets(lines)

    classes: list[ClassInfo] = []
    block_pattern = re.compile(r"^\s*(model|enum|type)\s+(\w+)\s*\{")
    field_pattern = re.compile(r"^\s+(\w+)\s+(\S+.*?)$")
    enum_member_pattern = re.compile(r"^\s+([A-Z_][A-Z0-9_]*)\s*$")

    i = 0
    while i < total_lines:
        m = block_pattern.match(lines[i])
        if m:
            block_kind = m.group(1)
            block_name = m.group(2)
            block_start = i + 1
            methods: list[FunctionInfo] = []
            brace_depth = 1
            j = i + 1
            while j < total_lines and brace_depth > 0:
                line = lines[j]
                brace_depth += line.count("{") - line.count("}")
                if brace_depth > 0:
                    fm = field_pattern.match(line)
                    em = enum_member_pattern.match(line) if not fm else None
                    if fm:
                        field_name = fm.group(1)
                        field_type = fm.group(2).strip()
                        if field_name not in ("@@", "//"):
                            methods.append(
                                FunctionInfo(
                                    name=field_name,
                                    qualified_name=f"{block_name}.{field_name}",
                                    line_range=LineRange(start=j + 1, end=j + 1),
                                    parameters=[field_type],
                                    decorators=[],
                                    docstring=None,
                                    is_method=True,
                                    parent_class=block_name,
                                )
                            )
                    elif em:
                        methods.append(
                            FunctionInfo(
                                name=em.group(1),
                                qualified_name=f"{block_name}.{em.group(1)}",
                                line_range=LineRange(start=j + 1, end=j + 1),
                                parameters=[],
                                decorators=[],
                                docstring=None,
                                is_method=True,
                                parent_class=block_name,
                            )
                        )
                j += 1
            block_end = j

            classes.append(
                ClassInfo(
                    name=block_name,
                    line_range=LineRange(start=block_start, end=block_end),
                    base_classes=[block_kind],
                    methods=methods,
                    decorators=[],
                    docstring=None,
                )
            )
            i = j
        else:
            i += 1

    return StructuralMetadata(
        source_name=source_name,
        total_lines=total_lines,
        total_chars=total_chars,
        lines=lines,
        line_char_offsets=line_offsets,
        classes=classes,
    )
