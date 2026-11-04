"""Tests for project action discovery and execution."""

from __future__ import annotations

import json
import os
import sys
from unittest.mock import MagicMock, patch

from token_savior.project_actions import discover_project_actions, run_project_action


class TestDiscoverProjectActions:
    def test_detects_python_actions_from_pyproject(self, tmp_path):
        (tmp_path / "pyproject.toml").write_text(
            "[tool.pytest.ini_options]\ntestpaths = ['tests']\n\n[tool.ruff]\nline-length = 100\n",
            encoding="utf-8",
        )
        (tmp_path / "src").mkdir()
        (tmp_path / "tests").mkdir()

        actions = discover_project_actions(str(tmp_path))
        action_ids = [action["id"] for action in actions]

        assert "python:test" in action_ids
        assert "python:lint" in action_ids
        python_test = next(action for action in actions if action["id"] == "python:test")
        assert python_test["command"] == ["pytest", "tests/", "-v"]

    def test_detects_npm_scripts(self, tmp_path):
        (tmp_path / "package.json").write_text(
            json.dumps(
                {
                    "scripts": {
                        "test": "vitest",
                        "dev": "vite",
                    }
                }
            ),
            encoding="utf-8",
        )

        actions = discover_project_actions(str(tmp_path))

        assert actions == [
            {
                "id": "npm:dev",
                "kind": "run",
                "command": ["npm", "run", "dev"],
                "source": "package.json",
                "description": "Run npm script 'dev'.",
            },
            {
                "id": "npm:test",
                "kind": "test",
                "command": ["npm", "run", "test"],
                "source": "package.json",
                "description": "Run npm script 'test'.",
            },
        ]

    def test_detects_gradle_actions_and_prefers_wrapper(self, tmp_path):
        (tmp_path / "build.gradle.kts").write_text("plugins {}\n", encoding="utf-8")
        (tmp_path / "gradlew").write_text("#!/bin/sh\n", encoding="utf-8")

        actions = discover_project_actions(str(tmp_path))
        by_id = {action["id"]: action for action in actions}

        assert by_id["gradle:test"]["command"] == ["./gradlew", "test"]
        assert by_id["gradle:check"]["command"] == ["./gradlew", "check"]
        assert by_id["gradle:build"]["command"] == ["./gradlew", "build"]

    def test_detects_maven_actions_from_pom(self, tmp_path):
        (tmp_path / "pom.xml").write_text("<project/>", encoding="utf-8")

        actions = discover_project_actions(str(tmp_path))
        by_id = {action["id"]: action for action in actions}

        assert by_id["maven:test"]["command"] == ["mvn", "test"]
        assert by_id["maven:check"]["command"] == ["mvn", "verify"]
        assert by_id["maven:build"]["command"] == ["mvn", "package"]


class TestRunProjectAction:
    def test_runs_discovered_action(self, tmp_path):
        (tmp_path / "package.json").write_text(
            json.dumps({"scripts": {"test": "vitest"}}),
            encoding="utf-8",
        )
        with patch("token_savior.project_actions.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout="ok\n", stderr="")
            result = run_project_action(str(tmp_path), "npm:test")

        assert result["ok"] is True
        assert result["exit_code"] == 0
        assert result["summary"]["headline"] == "ok"
        assert "stdout" not in result
        assert result["timed_out"] is False

    def test_returns_available_actions_for_unknown_action(self, tmp_path):
        (tmp_path / "package.json").write_text(
            json.dumps({"scripts": {"test": "vitest"}}),
            encoding="utf-8",
        )

        result = run_project_action(str(tmp_path), "python:test")

        assert result["ok"] is False
        assert "Unknown action" in result["error"]
        assert result["available_actions"] == ["npm:test"]

    def test_truncates_large_output(self, tmp_path):
        (tmp_path / "package.json").write_text(
            json.dumps({"scripts": {"test": "vitest"}}),
            encoding="utf-8",
        )
        with patch("token_savior.project_actions.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout="x" * 50, stderr="")
            result = run_project_action(
                str(tmp_path), "npm:test", max_output_chars=10, include_output=True
            )

        assert result["ok"] is True
        assert "... [truncated" in result["stdout"]

    def test_extracts_pytest_summary(self, tmp_path):
        (tmp_path / "package.json").write_text(
            json.dumps({"scripts": {"test": "pytest"}}),
            encoding="utf-8",
        )
        with patch("token_savior.project_actions.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(
                returncode=1,
                stdout="tests/test_x.py F\n================== 1 failed, 4 passed in 0.23s ==================\n",
                stderr="",
            )
            result = run_project_action(str(tmp_path), "npm:test")

        assert result["ok"] is False
        assert result["summary"]["pytest"]["failed"] == 1
        assert result["summary"]["pytest"]["passed"] == 4
        assert result["summary"]["pytest"]["duration"] == "0.23s"

    def test_runs_the_program_found_on_path(self, tmp_path):
        """npm, npx and mvn are npm.cmd, npx.cmd and mvn.cmd on Windows."""
        (tmp_path / "package.json").write_text(
            json.dumps({"scripts": {"test": "vitest"}}),
            encoding="utf-8",
        )
        with (
            patch("token_savior.project_actions.shutil.which", return_value="C:/node/npm.cmd"),
            patch("token_savior.project_actions.subprocess.run") as mock_run,
        ):
            mock_run.return_value = MagicMock(returncode=0, stdout="ok\n", stderr="")
            run_project_action(str(tmp_path), "npm:test")

        assert mock_run.call_args.args[0] == ["C:/node/npm.cmd", "run", "test"]

    def test_runs_a_batch_file_npm_end_to_end(self, tmp_path, monkeypatch):
        """A real `npm` on PATH: npm.cmd on Windows, a shell script elsewhere."""
        project = tmp_path / "project"
        project.mkdir()
        (project / "package.json").write_text(
            json.dumps({"scripts": {"test": "vitest"}}),
            encoding="utf-8",
        )
        bin_dir = tmp_path / "bin"
        bin_dir.mkdir()
        if os.name == "nt":
            (bin_dir / "npm.cmd").write_text(
                f'@"{sys.executable}" -c "print(42)"\r\n', encoding="utf-8"
            )
        else:
            npm = bin_dir / "npm"
            npm.write_text(f'#!/bin/sh\nexec "{sys.executable}" -c "print(42)"\n')
            npm.chmod(0o755)
        monkeypatch.setenv("PATH", str(bin_dir))

        result = run_project_action(str(project), "npm:test")

        assert result["ok"] is True, result
        assert result["summary"]["headline"] == "42"

    def test_names_the_missing_program(self, tmp_path, monkeypatch):
        (tmp_path / "package.json").write_text(
            json.dumps({"scripts": {"test": "vitest"}}),
            encoding="utf-8",
        )
        monkeypatch.setenv("PATH", str(tmp_path / "empty"))

        result = run_project_action(str(tmp_path), "npm:test")

        assert result["ok"] is False
        assert result["error"] == "Command not found: npm"
