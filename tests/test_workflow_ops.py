"""Tests for compact workflow helpers."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from token_savior.project_indexer import ProjectIndexer
from token_savior.workflow_ops import (
    apply_symbol_change_and_validate,
    apply_symbol_change_validate_with_rollback,
)


def _build_project(tmp_path):
    src = tmp_path / "src"
    tests = tmp_path / "tests"
    src.mkdir()
    tests.mkdir()
    (src / "core.py").write_text("def add(a, b):\n    return a + b\n", encoding="utf-8")
    (tests / "test_core.py").write_text(
        "from src.core import add\n\n\ndef test_add():\n    assert add(1, 2) == 3\n",
        encoding="utf-8",
    )
    indexer = ProjectIndexer(str(tmp_path), include_patterns=["**/*.py"])
    indexer.index()
    return indexer


class TestApplySymbolChangeAndValidate:
    def test_replaces_symbol_and_runs_impacted_tests(self, tmp_path):
        indexer = _build_project(tmp_path)

        with patch("token_savior.impacted_tests.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(
                returncode=0,
                stdout=".                                                                        [100%]\n1 passed in 0.08s\n",
                stderr="",
            )
            result = apply_symbol_change_and_validate(
                indexer,
                "add",
                "def add(a, b):\n    return a - b",
            )

        assert result["ok"] is True
        assert result["edit"]["file"] == "src/core.py"
        assert result["validation"]["command"] == ["pytest", "tests/test_core.py", "-q"]
        assert result["summary"]["tests_run"] == 1
        assert "return a - b" in (tmp_path / "src/core.py").read_text(encoding="utf-8")

    def test_propagates_validation_failure(self, tmp_path):
        indexer = _build_project(tmp_path)

        with patch("token_savior.impacted_tests.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(
                returncode=1,
                stdout="F                                                                        [100%]\n1 failed in 0.08s\n",
                stderr="",
            )
            result = apply_symbol_change_and_validate(
                indexer,
                "add",
                "def add(a, b):\n    return a - b",
            )

        assert result["ok"] is False
        assert result["validation"]["summary"]["pytest"]["failed"] == 1
        assert result["summary"]["validation_ok"] is False

    def test_compact_mode_returns_minimal_shape(self, tmp_path):
        indexer = _build_project(tmp_path)

        with patch("token_savior.impacted_tests.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(
                returncode=0,
                stdout=".                                                                        [100%]\n1 passed in 0.08s\n",
                stderr="",
            )
            result = apply_symbol_change_and_validate(
                indexer,
                "add",
                "def add(a, b):\n    return a - b",
                compact=True,
            )

        assert set(result.keys()) == {"ok", "summary", "validation"}


class TestApplySymbolChangeValidateWithRollback:
    def test_rolls_back_on_validation_failure(self, tmp_path):
        indexer = _build_project(tmp_path)
        original = (tmp_path / "src/core.py").read_text(encoding="utf-8")

        with patch("token_savior.impacted_tests.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(
                returncode=1,
                stdout="F                                                                        [100%]\n1 failed in 0.08s\n",
                stderr="",
            )
            result = apply_symbol_change_validate_with_rollback(
                indexer,
                "add",
                "def add(a, b):\n    return a - b",
            )

        assert result["ok"] is False
        assert result["rollback"]["ok"] is True
        assert result["commit_summary"]["headline"] == "1 file(s), 1 symbol(s) affected"
        assert (tmp_path / "src/core.py").read_text(encoding="utf-8") == original

    def test_compact_mode_rollback_shape(self, tmp_path):
        indexer = _build_project(tmp_path)

        with patch("token_savior.impacted_tests.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(
                returncode=1,
                stdout="F                                                                        [100%]\n1 failed in 0.08s\n",
                stderr="",
            )
            result = apply_symbol_change_validate_with_rollback(
                indexer,
                "add",
                "def add(a, b):\n    return a - b",
                compact=True,
            )

        assert set(result.keys()) == {
            "ok",
            "summary",
            "checkpoint_id",
            "rollback_ok",
            "commit_summary",
        }
