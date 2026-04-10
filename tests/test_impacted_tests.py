"""Tests for impacted test discovery and compact execution."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from token_savior.impacted_tests import find_impacted_test_files, run_impacted_tests
from token_savior.project_indexer import ProjectIndexer


def _sample_index(tmp_path):
    src = tmp_path / "src"
    tests = tmp_path / "tests"
    src.mkdir()
    tests.mkdir()
    (src / "core.py").write_text("def add(a, b):\n    return a + b\n", encoding="utf-8")
    (src / "utils.py").write_text("from src.core import add\n", encoding="utf-8")
    (tests / "test_core.py").write_text(
        "from src.core import add\n\n\ndef test_add():\n    assert add(1, 2) == 3\n",
        encoding="utf-8",
    )
    (tests / "test_utils.py").write_text(
        "from src.utils import add\n\n\ndef test_utils():\n    assert add(1, 2) == 3\n",
        encoding="utf-8",
    )
    indexer = ProjectIndexer(str(tmp_path), include_patterns=["**/*.py"])
    return indexer.index()


class TestFindImpactedTestFiles:
    def test_finds_tests_from_changed_source_file(self, tmp_path):
        index = _sample_index(tmp_path)

        result = find_impacted_test_files(index, changed_files=["src/core.py"])

        assert result["changed_files"] == ["src/core.py"]
        assert "tests/test_core.py" in result["impacted_tests"]
        assert result["reason_map"]["tests/test_core.py"] == ["imports:src/core.py"]

    def test_finds_tests_from_symbol_name(self, tmp_path):
        index = _sample_index(tmp_path)

        result = find_impacted_test_files(index, symbol_names=["add"])

        assert result["changed_files"] == ["src/core.py"]
        assert "tests/test_core.py" in result["impacted_tests"]

    def test_uses_filename_heuristic(self, tmp_path):
        index = _sample_index(tmp_path)

        result = find_impacted_test_files(index, changed_files=["src/utils.py"])

        assert "tests/test_utils.py" in result["impacted_tests"]


class TestRunImpactedTests:
    def test_runs_only_impacted_tests(self, tmp_path):
        index = _sample_index(tmp_path)

        with patch("token_savior.impacted_tests.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(
                returncode=0,
                stdout="..                                                                       [100%]\n2 passed in 0.11s\n",
                stderr="",
            )
            result = run_impacted_tests(index, changed_files=["src/core.py"])

        assert result["ok"] is True
        assert result["command"] == ["pytest", "tests/test_core.py", "-q"]
        assert result["summary"]["pytest"]["passed"] == 2
        assert "stdout" not in result

    def test_can_include_output(self, tmp_path):
        index = _sample_index(tmp_path)

        with patch("token_savior.impacted_tests.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(
                returncode=1,
                stdout="F                                                                        [100%]\n1 failed in 0.10s\n",
                stderr="traceback\n",
            )
            result = run_impacted_tests(index, changed_files=["src/core.py"], include_output=True)

        assert result["ok"] is False
        assert result["stdout"].startswith("F")
        assert result["stderr"] == "traceback\n"

    def test_falls_back_to_npm_test_for_js_projects(self, tmp_path):
        (tmp_path / "package.json").write_text('{"scripts":{"test":"vitest"}}', encoding="utf-8")
        (tmp_path / "src").mkdir()
        (tmp_path / "src/app.ts").write_text("export const x = 1;\n", encoding="utf-8")
        indexer = ProjectIndexer(str(tmp_path), include_patterns=["**/*.ts", "**/*.json"])
        index = indexer.index()

        with patch("token_savior.impacted_tests.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout="ok\n", stderr="")
            result = run_impacted_tests(index, changed_files=["src/app.ts"])

        assert result["command"] == ["npm", "run", "test"]

    def test_compact_mode_returns_minimal_shape(self, tmp_path):
        index = _sample_index(tmp_path)

        with patch("token_savior.impacted_tests.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(
                returncode=0, stdout=". [100%]\n1 passed in 0.10s\n", stderr=""
            )
            result = run_impacted_tests(index, changed_files=["src/core.py"], compact=True)

        assert set(result.keys()) == {"ok", "command", "summary", "selection"}
