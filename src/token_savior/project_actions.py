"""Project-level action discovery and execution helpers."""

from __future__ import annotations

import json
import os
import re
import subprocess
import time
import tomllib

from token_savior.output_helpers import truncate_output


def discover_project_actions(root_path: str) -> list[dict]:
    """Discover safe, conventional project actions from common build files."""
    actions: list[dict] = []
    seen_ids: set[str] = set()

    def add_action(
        action_id: str, kind: str, command: list[str], source: str, description: str
    ) -> None:
        if action_id in seen_ids:
            return
        seen_ids.add(action_id)
        actions.append(
            {
                "id": action_id,
                "kind": kind,
                "command": command,
                "source": source,
                "description": description,
            }
        )

    package_json = os.path.join(root_path, "package.json")
    if os.path.exists(package_json):
        try:
            with open(package_json, "r", encoding="utf-8") as f:
                payload = json.load(f)
            scripts = payload.get("scripts", {})
            if isinstance(scripts, dict):
                kind_map = {
                    "test": "test",
                    "lint": "lint",
                    "build": "build",
                    "start": "run",
                    "dev": "run",
                    "typecheck": "check",
                    "check": "check",
                }
                for script_name in sorted(scripts):
                    kind = kind_map.get(script_name, "custom")
                    add_action(
                        f"npm:{script_name}",
                        kind,
                        ["npm", "run", script_name],
                        "package.json",
                        f"Run npm script '{script_name}'.",
                    )
        except (OSError, json.JSONDecodeError):
            pass

    pyproject = os.path.join(root_path, "pyproject.toml")
    if os.path.exists(pyproject):
        try:
            with open(pyproject, "rb") as f:
                payload = tomllib.load(f)
        except (OSError, tomllib.TOMLDecodeError):
            payload = {}

        tool_section = payload.get("tool", {}) if isinstance(payload, dict) else {}
        has_tests_dir = os.path.isdir(os.path.join(root_path, "tests"))
        if "pytest" in tool_section or has_tests_dir:
            test_cmd = ["pytest"]
            if has_tests_dir:
                test_cmd.extend(["tests/", "-v"])
            add_action(
                "python:test",
                "test",
                test_cmd,
                "pyproject.toml",
                "Run the Python test suite with pytest.",
            )

        if "ruff" in tool_section:
            lint_targets = [
                path
                for path in ("src/", "tests/")
                if os.path.exists(os.path.join(root_path, path.rstrip("/")))
            ]
            lint_cmd = ["ruff", "check", *(lint_targets or ["."])]
            add_action(
                "python:lint",
                "lint",
                lint_cmd,
                "pyproject.toml",
                "Run Ruff checks for the Python project.",
            )

    cargo_toml = os.path.join(root_path, "Cargo.toml")
    if os.path.exists(cargo_toml):
        add_action(
            "cargo:test", "test", ["cargo", "test"], "Cargo.toml", "Run the Rust test suite."
        )
        add_action("cargo:check", "check", ["cargo", "check"], "Cargo.toml", "Run cargo check.")
        add_action(
            "cargo:build", "build", ["cargo", "build"], "Cargo.toml", "Build the Rust project."
        )

    go_mod = os.path.join(root_path, "go.mod")
    if os.path.exists(go_mod):
        add_action("go:test", "test", ["go", "test", "./..."], "go.mod", "Run Go tests.")
        add_action("go:build", "build", ["go", "build", "./..."], "go.mod", "Build Go packages.")

    gradle_source = next(
        (
            candidate
            for candidate in (
                "build.gradle.kts",
                "build.gradle",
                "settings.gradle.kts",
                "settings.gradle",
            )
            if os.path.exists(os.path.join(root_path, candidate))
        ),
        None,
    )
    if gradle_source:
        gradle_wrapper = os.path.join(root_path, "gradlew")
        gradle_cmd = ["./gradlew"] if os.path.exists(gradle_wrapper) else ["gradle"]
        add_action(
            "gradle:test",
            "test",
            [*gradle_cmd, "test"],
            gradle_source,
            "Run Gradle tests.",
        )
        add_action(
            "gradle:check",
            "check",
            [*gradle_cmd, "check"],
            gradle_source,
            "Run Gradle checks.",
        )
        add_action(
            "gradle:build",
            "build",
            [*gradle_cmd, "build"],
            gradle_source,
            "Build the Gradle project.",
        )

    pom_xml = os.path.join(root_path, "pom.xml")
    if os.path.exists(pom_xml):
        add_action(
            "maven:test",
            "test",
            ["mvn", "test"],
            "pom.xml",
            "Run Maven tests.",
        )
        add_action(
            "maven:check",
            "check",
            ["mvn", "verify"],
            "pom.xml",
            "Run Maven verification.",
        )
        add_action(
            "maven:build",
            "build",
            ["mvn", "package"],
            "pom.xml",
            "Build the Maven project.",
        )

    makefile = None
    for candidate in ("Makefile", "makefile", "GNUmakefile"):
        candidate_path = os.path.join(root_path, candidate)
        if os.path.exists(candidate_path):
            makefile = candidate_path
            break
    if makefile:
        try:
            with open(makefile, "r", encoding="utf-8") as f:
                contents = f.read()
            target_names = {
                match.group(1)
                for match in re.finditer(r"^([A-Za-z0-9_.-]+)\s*:", contents, flags=re.MULTILINE)
                if not match.group(1).startswith(".")
            }
            for target_name, kind in (
                ("test", "test"),
                ("lint", "lint"),
                ("build", "build"),
                ("run", "run"),
            ):
                if target_name in target_names:
                    add_action(
                        f"make:{target_name}",
                        kind,
                        ["make", target_name],
                        os.path.basename(makefile),
                        f"Run make target '{target_name}'.",
                    )
        except OSError:
            pass

    return sorted(actions, key=lambda action: action["id"])


def run_project_action(
    root_path: str,
    action_id: str,
    timeout_sec: int = 120,
    max_output_chars: int = 12000,
    include_output: bool = False,
) -> dict:
    """Run a previously discovered project action."""
    actions = discover_project_actions(root_path)
    action = next((item for item in actions if item["id"] == action_id), None)
    if action is None:
        return {
            "ok": False,
            "error": f"Unknown action '{action_id}'.",
            "available_actions": [item["id"] for item in actions],
        }

    try:
        start = time.perf_counter()
        result = subprocess.run(
            action["command"],
            cwd=root_path,
            capture_output=True,
            text=True,
            timeout=timeout_sec,
        )
        duration_sec = round(time.perf_counter() - start, 3)
        stdout = truncate_output(result.stdout, max_output_chars)
        stderr = truncate_output(result.stderr, max_output_chars)
        payload = {
            "ok": result.returncode == 0,
            "action": action,
            "exit_code": result.returncode,
            "duration_sec": duration_sec,
            "summary": summarize_command_output(action_id, stdout, stderr, result.returncode),
            "timed_out": False,
        }
        if include_output:
            payload["stdout"] = stdout
            payload["stderr"] = stderr
        return payload
    except FileNotFoundError as exc:
        return {
            "ok": False,
            "action": action,
            "error": f"Command not found: {exc.filename}",
            "timed_out": False,
        }
    except subprocess.TimeoutExpired as exc:
        stdout = truncate_output(exc.stdout or "", max_output_chars)
        stderr = truncate_output(exc.stderr or "", max_output_chars)
        return {
            "ok": False,
            "action": action,
            "exit_code": None,
            "timed_out": True,
            "error": f"Action '{action_id}' timed out after {timeout_sec}s.",
            "summary": summarize_command_output(action_id, stdout, stderr, None),
            **({"stdout": stdout, "stderr": stderr} if include_output else {}),
        }


def summarize_command_output(
    action_id: str, stdout: str, stderr: str, exit_code: int | None
) -> dict:
    """Build a compact result summary suitable for token-efficient agent loops."""
    stdout_lines = [line for line in stdout.splitlines() if line.strip()]
    stderr_lines = [line for line in stderr.splitlines() if line.strip()]
    summary = {
        "action_id": action_id,
        "exit_code": exit_code,
        "stdout_lines": len(stdout_lines),
        "stderr_lines": len(stderr_lines),
        "headline": _select_headline(stdout_lines, stderr_lines, exit_code),
        "tail": (stdout_lines + stderr_lines)[-5:],
    }

    pytest_summary = _parse_pytest_summary(stdout_lines + stderr_lines)
    if pytest_summary:
        summary["pytest"] = pytest_summary

    return summary


def _select_headline(
    stdout_lines: list[str], stderr_lines: list[str], exit_code: int | None
) -> str:
    """Pick a single-line headline for the command result."""
    for line in reversed(stdout_lines):
        if line.strip():
            return line.strip()
    for line in reversed(stderr_lines):
        if line.strip():
            return line.strip()
    if exit_code is None:
        return "Command timed out"
    return f"Command exited with code {exit_code}"


def _parse_pytest_summary(lines: list[str]) -> dict | None:
    """Extract a compact pytest summary when present."""
    for line in reversed(lines):
        if " passed" not in line and " failed" not in line and " error" not in line:
            continue
        if " in " not in line:
            continue
        summary: dict[str, int | str] = {"raw": line.strip()}
        for label in ("passed", "failed", "errors", "error", "skipped", "xfailed", "xpassed"):
            match = re.search(rf"(\d+)\s+{label}", line)
            if match:
                key = "errors" if label == "error" else label
                summary[key] = int(match.group(1))
        duration_match = re.search(r"in\s+([0-9.]+s)", line)
        if duration_match:
            summary["duration"] = duration_match.group(1)
        if len(summary) > 1:
            return summary
    return None
