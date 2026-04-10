"""Tests for compact, token-efficient operational helpers."""

from __future__ import annotations

from unittest.mock import patch

from token_savior.compact_ops import get_changed_symbols
from token_savior.models import (
    ClassInfo,
    FunctionInfo,
    LineRange,
    ProjectIndex,
    SectionInfo,
    StructuralMetadata,
)
from token_savior.git_tracker import GitChangeSet


def _metadata() -> StructuralMetadata:
    return StructuralMetadata(
        source_name="main.py",
        total_lines=10,
        total_chars=100,
        lines=[""] * 10,
        line_char_offsets=[0] * 10,
        functions=[
            FunctionInfo(
                name="hello",
                qualified_name="hello",
                line_range=LineRange(1, 2),
                parameters=[],
                decorators=[],
                docstring=None,
                is_method=False,
                parent_class=None,
            )
        ],
        classes=[
            ClassInfo(
                name="Greeter",
                line_range=LineRange(4, 8),
                base_classes=[],
                methods=[],
                decorators=[],
                docstring=None,
            )
        ],
        sections=[SectionInfo(title="Overview", level=1, line_range=LineRange(1, 3))],
    )


class TestGetChangedSymbols:
    def test_returns_compact_symbol_summary(self):
        index = ProjectIndex(
            root_path="/repo",
            files={
                "main.py": _metadata(),
                "notes.md": StructuralMetadata(
                    source_name="notes.md",
                    total_lines=3,
                    total_chars=20,
                    lines=["# Notes", "", "body"],
                    line_char_offsets=[0, 7, 8],
                    sections=[SectionInfo(title="Notes", level=1, line_range=LineRange(1, 3))],
                ),
            },
        )

        with (
            patch("token_savior.compact_ops.get_head_commit", return_value="abc123"),
            patch(
                "token_savior.compact_ops.get_changed_files",
                return_value=GitChangeSet(
                    modified=["main.py"], added=["notes.md"], deleted=["gone.py"]
                ),
            ),
        ):
            result = get_changed_symbols(index)

        assert result["modified_files"] == 1
        assert result["added_files"] == 1
        assert result["deleted_files"] == 1
        assert result["reported_files"] == 3
        assert result["files"][0]["file"] == "main.py"
        assert result["files"][0]["symbols"][0]["name"] == "hello"
        assert result["files"][1]["file"] == "notes.md"
        assert result["files"][2] == {"file": "gone.py", "status": "deleted", "symbols": []}

    def test_respects_file_and_symbol_limits(self):
        index = ProjectIndex(
            root_path="/repo", files={"main.py": _metadata(), "other.py": _metadata()}
        )

        with (
            patch("token_savior.compact_ops.get_head_commit", return_value="abc123"),
            patch(
                "token_savior.compact_ops.get_changed_files",
                return_value=GitChangeSet(modified=["main.py", "other.py"]),
            ),
        ):
            result = get_changed_symbols(index, max_files=1, max_symbols_per_file=1)

        assert result["reported_files"] == 1
        assert result["remaining_files"] == 1
        assert result["files"][0]["symbols"] == [{"name": "hello", "type": "function", "line": 1}]
