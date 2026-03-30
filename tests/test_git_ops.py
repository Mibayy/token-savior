"""Tests for compact git-oriented summaries."""

from __future__ import annotations

from unittest.mock import patch

from token_savior.git_ops import get_changed_symbols_since_ref, summarize_patch_by_symbol
from token_savior.git_tracker import GitChangeSet
from token_savior.models import (
    FunctionInfo,
    LineRange,
    ProjectIndex,
    StructuralMetadata,
)


def _index() -> ProjectIndex:
    return ProjectIndex(
        root_path="/repo",
        files={
            "src/core.py": StructuralMetadata(
                source_name="src/core.py",
                total_lines=3,
                total_chars=40,
                lines=["def add(a, b):", "    return a + b", ""],
                line_char_offsets=[0, 15, 33],
                functions=[
                    FunctionInfo(
                        name="add",
                        qualified_name="add",
                        line_range=LineRange(1, 2),
                        parameters=["a", "b"],
                        decorators=[],
                        docstring=None,
                        is_method=False,
                        parent_class=None,
                    )
                ],
            ),
            "tests/test_core.py": StructuralMetadata(
                source_name="tests/test_core.py",
                total_lines=2,
                total_chars=20,
                lines=["def test_add():", "    pass"],
                line_char_offsets=[0, 16],
                functions=[
                    FunctionInfo(
                        name="test_add",
                        qualified_name="test_add",
                        line_range=LineRange(1, 2),
                        parameters=[],
                        decorators=[],
                        docstring=None,
                        is_method=False,
                        parent_class=None,
                    )
                ],
            ),
        },
    )


class TestGetChangedSymbolsSinceRef:
    def test_returns_compact_summary_since_ref(self):
        index = _index()

        with patch(
            "token_savior.git_ops.get_changed_files",
            return_value=GitChangeSet(modified=["src/core.py"], added=["tests/test_core.py"]),
        ):
            result = get_changed_symbols_since_ref(index, "HEAD~1")

        assert result["since_ref"] == "HEAD~1"
        assert result["modified_files"] == 1
        assert result["added_files"] == 1
        assert result["files"][0]["symbols"][0]["name"] == "add"


class TestSummarizePatchBySymbol:
    def test_summarizes_selected_files(self):
        index = _index()

        result = summarize_patch_by_symbol(index, changed_files=["src/core.py"])

        assert result["reported_files"] == 1
        assert result["files"][0]["file"] == "src/core.py"
        assert result["files"][0]["status"] == "changed"
        assert result["files"][0]["symbols"][0]["name"] == "add"
