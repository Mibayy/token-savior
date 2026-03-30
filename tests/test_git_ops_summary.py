"""Tests for compact commit summary helpers."""

from __future__ import annotations

from token_savior.git_ops import build_commit_summary
from token_savior.models import FunctionInfo, LineRange, ProjectIndex, StructuralMetadata


class TestBuildCommitSummary:
    def test_builds_compact_summary(self):
        index = ProjectIndex(
            root_path="/repo",
            files={
                "src/core.py": StructuralMetadata(
                    source_name="src/core.py",
                    total_lines=2,
                    total_chars=20,
                    lines=["def add():", "    pass"],
                    line_char_offsets=[0, 11],
                    functions=[
                        FunctionInfo(
                            name="add",
                            qualified_name="add",
                            line_range=LineRange(1, 2),
                            parameters=[],
                            decorators=[],
                            docstring=None,
                            is_method=False,
                            parent_class=None,
                        )
                    ],
                )
            },
        )

        result = build_commit_summary(index, ["src/core.py"])

        assert result["headline"] == "1 file(s), 1 symbol(s) affected"
        assert result["top_files"] == ["src/core.py"]
        assert result["reported_symbols"] == 1
