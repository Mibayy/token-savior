"""Impacted test selection and compact execution helpers."""

from __future__ import annotations

import subprocess
import time
from pathlib import PurePosixPath

from token_savior.models import ProjectIndex
from token_savior.output_helpers import truncate_output
from token_savior.project_actions import discover_project_actions
from token_savior.project_actions import summarize_command_output


def find_impacted_test_files(
    index: ProjectIndex,
    changed_files: list[str] | None = None,
    symbol_names: list[str] | None = None,
    max_tests: int = 20,
) -> dict:
    """Infer a compact set of likely impacted test files."""
    changed = _normalize_changed_files(index, changed_files, symbol_names)
    if not changed:
        return {"error": "No changed files or symbols could be resolved"}
    tests = sorted(path for path in index.files if _is_test_file(path))
    impacted: list[str] = []
    reasons: dict[str, list[str]] = {}

    def add_reason(test_file: str, reason: str) -> None:
        if test_file not in reasons:
            reasons[test_file] = []
        if reason not in reasons[test_file]:
            reasons[test_file].append(reason)
        if test_file not in impacted and len(impacted) < max_tests:
            impacted.append(test_file)

    for changed_file in changed:
        if _is_test_file(changed_file):
            add_reason(changed_file, "changed_test_file")

        for dependent in sorted(index.reverse_import_graph.get(changed_file, set())):
            if _is_test_file(dependent):
                add_reason(dependent, f"imports:{changed_file}")

        for candidate in _filename_based_test_candidates(changed_file):
            if candidate in reasons:
                continue
            if candidate in index.files and _is_test_file(candidate):
                add_reason(candidate, f"name_match:{changed_file}")

        changed_stem = PurePosixPath(changed_file).stem
        for test_file in tests:
            if test_file in reasons or len(impacted) >= max_tests:
                continue
            if changed_stem and changed_stem in PurePosixPath(test_file).stem:
                add_reason(test_file, f"stem_match:{changed_file}")

    symbol_seeds = _resolve_changed_symbols(index, changed, symbol_names)
    for test_file, test_reasons in _graph_based_test_candidates(index, symbol_seeds).items():
        for reason in test_reasons:
            add_reason(test_file, reason)

    omitted = max(0, len(reasons) - len(impacted))
    return {
        "changed_files": changed,
        "impacted_tests": impacted,
        "reason_map": {test_file: reasons[test_file] for test_file in impacted},
        "omitted_tests": omitted,
    }


def run_impacted_tests(
    index: ProjectIndex,
    changed_files: list[str] | None = None,
    symbol_names: list[str] | None = None,
    max_tests: int = 20,
    timeout_sec: int = 120,
    max_output_chars: int = 12000,
    include_output: bool = False,
    compact: bool = False,
) -> dict:
    """Run the inferred impacted tests and return a compact summary."""
    selection = find_impacted_test_files(
        index,
        changed_files=changed_files,
        symbol_names=symbol_names,
        max_tests=max_tests,
    )
    if "error" in selection:
        return selection

    command = _select_test_command(index, selection)
    if command is None:
        result = {
            "ok": True,
            "selection": selection,
            "command": None,
            "summary": {
                "action_id": "run_impacted_tests",
                "headline": "No impacted tests found",
                "stdout_lines": 0,
                "stderr_lines": 0,
                "tail": [],
                "exit_code": 0,
            },
        }
        return _compact_workflow_result(result) if compact else result
    try:
        start = time.perf_counter()
        result = subprocess.run(
            command,
            cwd=index.root_path,
            capture_output=True,
            text=True,
            timeout=timeout_sec,
        )
        duration_sec = round(time.perf_counter() - start, 3)
        stdout = truncate_output(result.stdout, max_output_chars)
        stderr = truncate_output(result.stderr, max_output_chars)
        payload = {
            "ok": result.returncode == 0,
            "selection": selection,
            "command": command,
            "duration_sec": duration_sec,
            "summary": summarize_command_output(
                "run_impacted_tests", stdout, stderr, result.returncode
            ),
            "exit_code": result.returncode,
            "timed_out": False,
        }
        if include_output:
            payload["stdout"] = stdout
            payload["stderr"] = stderr
        return _compact_workflow_result(payload) if compact else payload
    except FileNotFoundError:
        payload = {
            "ok": False,
            "selection": selection,
            "error": "test runner not found",
            "command": command,
        }
        return _compact_workflow_result(payload) if compact else payload
    except subprocess.TimeoutExpired as exc:
        stdout = truncate_output(exc.stdout or "", max_output_chars)
        stderr = truncate_output(exc.stderr or "", max_output_chars)
        payload = {
            "ok": False,
            "selection": selection,
            "command": command,
            "error": f"Impacted tests timed out after {timeout_sec}s.",
            "summary": summarize_command_output("run_impacted_tests", stdout, stderr, None),
            "exit_code": None,
            "timed_out": True,
        }
        if include_output:
            payload["stdout"] = stdout
            payload["stderr"] = stderr
        return _compact_workflow_result(payload) if compact else payload


def _normalize_changed_files(
    index: ProjectIndex,
    changed_files: list[str] | None,
    symbol_names: list[str] | None,
) -> list[str]:
    """Resolve changed files directly or from symbol names."""
    files: set[str] = set()
    for file_path in changed_files or []:
        normalized = _resolve_file_path(index, file_path)
        if normalized:
            files.add(normalized)
    for symbol_name in symbol_names or []:
        stored_path = index.symbol_table.get(symbol_name)
        if stored_path:
            files.add(stored_path)
            continue
        for path, meta in index.files.items():
            if any(
                func.name == symbol_name or func.qualified_name == symbol_name
                for func in meta.functions
            ):
                files.add(path)
                break
            if any(cls.name == symbol_name for cls in meta.classes):
                files.add(path)
                break
    return sorted(files)


def _resolve_changed_symbols(
    index: ProjectIndex,
    changed_files: list[str],
    symbol_names: list[str] | None,
) -> set[str]:
    symbols: set[str] = set()
    for symbol_name in symbol_names or []:
        if symbol_name in index.reverse_dependency_graph or symbol_name in index.global_dependency_graph:
            symbols.add(symbol_name)
        for path, meta in index.files.items():
            if any(
                func.name == symbol_name or func.qualified_name == symbol_name
                for func in meta.functions
            ):
                symbols.update(
                    func.qualified_name
                    for func in meta.functions
                    if func.name == symbol_name or func.qualified_name == symbol_name
                )
            for cls in meta.classes:
                qualified_name = cls.qualified_name or cls.name
                if cls.name == symbol_name or qualified_name == symbol_name:
                    symbols.add(qualified_name)
                    symbols.update(method.qualified_name for method in cls.methods)
    for changed_file in changed_files:
        meta = index.files.get(changed_file)
        if meta is None:
            continue
        symbols.update(func.qualified_name for func in meta.functions)
        for cls in meta.classes:
            symbols.add(cls.qualified_name or cls.name)
            symbols.update(method.qualified_name for method in cls.methods)
    return symbols


def _graph_based_test_candidates(index: ProjectIndex, seed_symbols: set[str]) -> dict[str, list[str]]:
    if not seed_symbols:
        return {}
    results: dict[str, list[str]] = {}
    visited: set[str] = set(seed_symbols)
    queue = list(seed_symbols)
    while queue:
        symbol = queue.pop(0)
        for dependent in index.reverse_dependency_graph.get(symbol, set()):
            if dependent in visited:
                continue
            visited.add(dependent)
            queue.append(dependent)
            file_path = index.symbol_table.get(dependent)
            if not file_path:
                continue
            if _is_test_file(file_path):
                results.setdefault(file_path, []).append(f"graph_dep:{symbol}")
    return results


def _select_test_command(index: ProjectIndex, selection: dict) -> list[str] | None:
    """Choose the most appropriate test command for the current project."""
    impacted_tests = selection["impacted_tests"]
    changed_files = selection.get("changed_files", [])
    actions = discover_project_actions(index.root_path)
    action_ids = {action["id"]: action["command"] for action in actions}
    if impacted_tests:
        if all(_is_pytest_file(path) for path in impacted_tests):
            return ["pytest", *impacted_tests, "-q"]
        java_command = _select_java_test_command(index, impacted_tests, action_ids)
        if java_command is not None:
            return java_command

    if any(_is_java_related_file(path) for path in changed_files):
        return action_ids.get("gradle:test") or action_ids.get("maven:test")

    if any(path.endswith((".ts", ".tsx", ".js", ".jsx")) for path in changed_files):
        return action_ids.get("npm:test")
    if any(path.endswith(".rs") for path in changed_files):
        return action_ids.get("cargo:test")
    if any(path.endswith(".go") for path in changed_files):
        return action_ids.get("go:test")

    return (
        action_ids.get("gradle:test")
        or action_ids.get("maven:test")
        or action_ids.get("python:test")
        or action_ids.get("npm:test")
        or action_ids.get("cargo:test")
        or action_ids.get("go:test")
    )


def _resolve_file_path(index: ProjectIndex, file_path: str) -> str | None:
    """Resolve a file path against the indexed project paths."""
    if file_path in index.files:
        return file_path
    for stored_path in index.files:
        if stored_path.endswith(file_path) or file_path.endswith(stored_path):
            return stored_path
    return None


def _is_pytest_file(path: str) -> bool:
    """Whether a path looks like a pytest-style Python test file."""
    name = PurePosixPath(path).name
    return path.endswith(".py") and (
        "tests/" in path or name.startswith("test_") or name.endswith("_test.py")
    )

def _is_java_test_file(path: str) -> bool:
    """Whether a path looks like a Java test class."""
    name = PurePosixPath(path).name
    return path.endswith(".java") and (
        "src/test/java/" in path
        or name.endswith(("Test.java", "Tests.java", "IT.java", "ITCase.java"))
    )


def _is_java_related_file(path: str) -> bool:
    """Whether a file path is part of a Java project/test layout."""
    name = PurePosixPath(path).name
    return path.endswith(".java") or name in {
        "build.gradle",
        "build.gradle.kts",
        "settings.gradle",
        "settings.gradle.kts",
        "pom.xml",
    }


def _is_test_file(path: str) -> bool:
    """Whether a path looks like a supported project test file."""
    return _is_pytest_file(path) or _is_java_test_file(path)


def _filename_based_test_candidates(changed_file: str) -> list[str]:
    """Generate common test filename conventions for a source file."""
    pure_path = PurePosixPath(changed_file)
    stem = pure_path.stem
    parent = pure_path.parent
    candidates = {
        str(PurePosixPath("tests") / f"test_{stem}.py"),
        str(PurePosixPath("tests") / f"{stem}_test.py"),
        str(parent / f"test_{stem}.py"),
        str(parent / f"{stem}_test.py"),
    }
    candidates.update(_java_filename_based_test_candidates(changed_file))
    return sorted(candidates)


def _java_filename_based_test_candidates(changed_file: str) -> set[str]:
    """Generate common Java test filename conventions for a source file."""
    pure_path = PurePosixPath(changed_file)
    if pure_path.suffix != ".java":
        return set()

    candidates: set[str] = set()
    stem = pure_path.stem
    suffixes = ("Test.java", "Tests.java", "IT.java", "ITCase.java")

    if "src/main/java/" in changed_file:
        relative = changed_file.split("src/main/java/", 1)[1]
        package_dir = PurePosixPath(relative).parent
        for suffix in suffixes:
            candidates.add(str(PurePosixPath("src/test/java") / package_dir / f"{stem}{suffix}"))
        return candidates

    if "src/test/java/" in changed_file:
        relative = changed_file.split("src/test/java/", 1)[1]
        package_dir = PurePosixPath(relative).parent
        for suffix in suffixes:
            candidates.add(str(PurePosixPath("src/test/java") / package_dir / f"{stem}{suffix}"))

    return candidates


def _select_java_test_command(
    index: ProjectIndex,
    impacted_tests: list[str],
    action_ids: dict[str, list[str]],
) -> list[str] | None:
    java_tests = [path for path in impacted_tests if _is_java_test_file(path)]
    if not java_tests:
        return None

    selectors = _java_test_selectors(index, java_tests)
    gradle_test = action_ids.get("gradle:test")
    if gradle_test:
        command = [*gradle_test]
        for selector in selectors:
            command.extend(["--tests", selector])
        return command

    maven_test = action_ids.get("maven:test")
    if maven_test:
        simple_selectors = [selector.rsplit(".", 1)[-1] for selector in selectors]
        return [maven_test[0], f"-Dtest={','.join(simple_selectors)}", *maven_test[1:]]

    return None


def _java_test_selectors(index: ProjectIndex, impacted_tests: list[str]) -> list[str]:
    selectors: list[str] = []
    seen: set[str] = set()
    for test_file in impacted_tests:
        selector = _java_test_selector(index, test_file)
        if selector and selector not in seen:
            selectors.append(selector)
            seen.add(selector)
    return selectors


def _java_test_selector(index: ProjectIndex, test_file: str) -> str:
    meta = index.files.get(test_file)
    stem = PurePosixPath(test_file).stem
    if meta and meta.module_name:
        return f"{meta.module_name}.{stem}"
    return stem


def _compact_workflow_result(payload: dict) -> dict:
    """Reduce operational payloads to the minimum useful fields."""
    return {
        "ok": payload.get("ok"),
        "command": payload.get("command"),
        "summary": payload.get("summary"),
        "selection": {
            "changed_files": payload.get("selection", {}).get("changed_files", []),
            "impacted_tests": payload.get("selection", {}).get("impacted_tests", []),
        },
        **({"error": payload["error"]} if "error" in payload else {}),
    }
