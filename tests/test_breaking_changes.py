"""Tests for breaking change detection module."""

from __future__ import annotations

import os
import subprocess
import tempfile

import pytest

from token_savior.breaking_changes import detect_breaking_changes
from token_savior.models import ProjectIndex


@pytest.fixture
def git_repo():
    """Create a temporary git repo with Python files and an initial commit."""
    with tempfile.TemporaryDirectory() as tmpdir:
        subprocess.run(["git", "init"], cwd=tmpdir, capture_output=True, check=True)
        subprocess.run(
            ["git", "config", "user.email", "test@test.com"],
            cwd=tmpdir,
            capture_output=True,
            check=True,
        )
        subprocess.run(
            ["git", "config", "user.name", "Test"],
            cwd=tmpdir,
            capture_output=True,
            check=True,
        )
        yield tmpdir


def _make_index(root_path: str) -> ProjectIndex:
    return ProjectIndex(root_path=root_path)


def _commit_all(tmpdir: str, message: str) -> None:
    subprocess.run(["git", "add", "."], cwd=tmpdir, capture_output=True, check=True)
    subprocess.run(
        ["git", "commit", "-m", message],
        cwd=tmpdir,
        capture_output=True,
        check=True,
    )


# ---------------------------------------------------------------------------
# Test: parameter removed → BREAKING
# ---------------------------------------------------------------------------


def test_parameter_removed_is_breaking(git_repo):
    api_file = os.path.join(git_repo, "api.py")
    with open(api_file, "w") as f:
        f.write("def process(name, timeout):\n    pass\n")
    _commit_all(git_repo, "initial")

    # Remove the 'timeout' parameter
    with open(api_file, "w") as f:
        f.write("def process(name):\n    pass\n")

    index = _make_index(git_repo)
    result = detect_breaking_changes(index, since_ref="HEAD")

    assert "BREAKING" in result
    assert "process" in result
    assert "timeout" in result


# ---------------------------------------------------------------------------
# Test: function removed entirely → BREAKING
# ---------------------------------------------------------------------------


def test_function_removed_is_breaking(git_repo):
    api_file = os.path.join(git_repo, "api.py")
    with open(api_file, "w") as f:
        f.write("def old_func(x):\n    pass\n\ndef keep_func():\n    pass\n")
    _commit_all(git_repo, "initial")

    # Remove old_func entirely
    with open(api_file, "w") as f:
        f.write("def keep_func():\n    pass\n")

    index = _make_index(git_repo)
    result = detect_breaking_changes(index, since_ref="HEAD")

    assert "BREAKING" in result
    assert "old_func" in result
    # keep_func should not be flagged
    assert "keep_func" not in result


# ---------------------------------------------------------------------------
# Test: parameter added without default → BREAKING
# ---------------------------------------------------------------------------


def test_parameter_added_no_default_is_breaking(git_repo):
    api_file = os.path.join(git_repo, "api.py")
    with open(api_file, "w") as f:
        f.write("def greet(name):\n    pass\n")
    _commit_all(git_repo, "initial")

    # Add required parameter
    with open(api_file, "w") as f:
        f.write("def greet(name, lang):\n    pass\n")

    index = _make_index(git_repo)
    result = detect_breaking_changes(index, since_ref="HEAD")

    assert "BREAKING" in result
    assert "greet" in result
    assert "lang" in result


# ---------------------------------------------------------------------------
# Test: parameter added with default → WARNING (safe)
# ---------------------------------------------------------------------------


def test_parameter_added_with_default_is_warning(git_repo):
    api_file = os.path.join(git_repo, "api.py")
    with open(api_file, "w") as f:
        f.write("def greet(name):\n    pass\n")
    _commit_all(git_repo, "initial")

    # Add optional parameter
    with open(api_file, "w") as f:
        f.write("def greet(name, lang='en'):\n    pass\n")

    index = _make_index(git_repo)
    result = detect_breaking_changes(index, since_ref="HEAD")

    # Should be WARNING, not BREAKING
    assert "WARNING" in result
    assert "lang" in result
    assert "BREAKING" not in result


# ---------------------------------------------------------------------------
# Test: no changes → no issues
# ---------------------------------------------------------------------------


def test_no_changes_reports_clean(git_repo):
    api_file = os.path.join(git_repo, "api.py")
    with open(api_file, "w") as f:
        f.write("def stable(a, b):\n    pass\n")
    _commit_all(git_repo, "initial")

    # No modifications to working tree
    index = _make_index(git_repo)
    result = detect_breaking_changes(index, since_ref="HEAD")

    assert "no breaking changes" in result.lower()


# ---------------------------------------------------------------------------
# Test: new function added → not flagged
# ---------------------------------------------------------------------------


def test_new_function_not_flagged(git_repo):
    api_file = os.path.join(git_repo, "api.py")
    with open(api_file, "w") as f:
        f.write("def existing():\n    pass\n")
    _commit_all(git_repo, "initial")

    # Add a brand new function
    with open(api_file, "w") as f:
        f.write("def existing():\n    pass\n\ndef brand_new(x, y):\n    pass\n")

    index = _make_index(git_repo)
    result = detect_breaking_changes(index, since_ref="HEAD")

    # New functions surface in the NON-BREAKING section for visibility, but
    # must never land in BREAKING.
    breaking_section = result.split("NON-BREAKING:")[0] if "NON-BREAKING:" in result else result
    assert "brand_new" not in breaking_section
    # Existing unchanged function should not be flagged anywhere.
    assert "existing" not in result


# ---------------------------------------------------------------------------
# Test: class method removed → BREAKING
# ---------------------------------------------------------------------------


def test_class_method_removed_is_breaking(git_repo):
    models_file = os.path.join(git_repo, "models.py")
    with open(models_file, "w") as f:
        f.write(
            "class UserModel:\n"
            "    def validate(self):\n"
            "        pass\n"
            "    def save(self):\n"
            "        pass\n"
        )
    _commit_all(git_repo, "initial")

    # Remove the 'validate' method
    with open(models_file, "w") as f:
        f.write("class UserModel:\n    def save(self):\n        pass\n")

    index = _make_index(git_repo)
    result = detect_breaking_changes(index, since_ref="HEAD")

    assert "BREAKING" in result
    assert "validate" in result
    assert "UserModel" in result


# ---------------------------------------------------------------------------
# Test: entire file deleted → BREAKING for all functions
# ---------------------------------------------------------------------------


def test_file_deleted_reports_breaking(git_repo):
    utils_file = os.path.join(git_repo, "utils.py")
    with open(utils_file, "w") as f:
        f.write("def helper(x):\n    pass\n")
    _commit_all(git_repo, "initial")

    # Delete the file
    os.remove(utils_file)

    index = _make_index(git_repo)
    result = detect_breaking_changes(index, since_ref="HEAD")

    assert "BREAKING" in result
    assert "helper" in result


# ---------------------------------------------------------------------------
# Test: return type annotation changed → WARNING
# ---------------------------------------------------------------------------


def test_return_type_changed_is_warning(git_repo):
    api_file = os.path.join(git_repo, "api.py")
    with open(api_file, "w") as f:
        f.write("def get_count() -> int:\n    return 1\n")
    _commit_all(git_repo, "initial")

    with open(api_file, "w") as f:
        f.write("def get_count() -> str:\n    return '1'\n")

    index = _make_index(git_repo)
    result = detect_breaking_changes(index, since_ref="HEAD")

    assert "WARNING" in result
    assert "get_count" in result


def test_java_method_signature_change_is_breaking(git_repo):
    api_file = os.path.join(git_repo, "PriceEngine.java")
    with open(api_file, "w") as f:
        f.write(
            "package com.acme.pricing;\n\n"
            "public final class PriceEngine {\n"
            "    public int apply(int input) {\n"
            "        return input;\n"
            "    }\n"
            "}\n"
        )
    _commit_all(git_repo, "initial")

    with open(api_file, "w") as f:
        f.write(
            "package com.acme.pricing;\n\n"
            "public final class PriceEngine {\n"
            "    public int apply(int input, int scale) {\n"
            "        return input * scale;\n"
            "    }\n"
            "}\n"
        )

    index = _make_index(git_repo)
    result = detect_breaking_changes(index, since_ref="HEAD")

    assert "BREAKING" in result
    assert "PriceEngine.apply(int)" in result


def test_java_return_type_change_is_warning(git_repo):
    api_file = os.path.join(git_repo, "PriceEngine.java")
    with open(api_file, "w") as f:
        f.write(
            "package com.acme.pricing;\n\n"
            "public final class PriceEngine {\n"
            "    public int apply(int input) {\n"
            "        return input;\n"
            "    }\n"
            "}\n"
        )
    _commit_all(git_repo, "initial")

    with open(api_file, "w") as f:
        f.write(
            "package com.acme.pricing;\n\n"
            "public final class PriceEngine {\n"
            "    public long apply(int input) {\n"
            "        return input;\n"
            "    }\n"
            "}\n"
        )

    index = _make_index(git_repo)
    result = detect_breaking_changes(index, since_ref="HEAD")

    assert "WARNING" in result
    assert "return type changed" in result


def test_deleted_java_file_reports_breaking(git_repo):
    api_file = os.path.join(git_repo, "PriceEngine.java")
    with open(api_file, "w") as f:
        f.write(
            "package com.acme.pricing;\n\n"
            "public final class PriceEngine {\n"
            "    public int apply(int input) {\n"
            "        return input;\n"
            "    }\n"
            "}\n"
        )
    _commit_all(git_repo, "initial")

    os.remove(api_file)

    index = _make_index(git_repo)
    result = detect_breaking_changes(index, since_ref="HEAD")

    assert "BREAKING" in result
    assert "com.acme.pricing.PriceEngine" in result


def test_private_java_method_not_reported_as_breaking(git_repo):
    api_file = os.path.join(git_repo, "PriceEngine.java")
    with open(api_file, "w") as f:
        f.write(
            "package com.acme.pricing;\n\n"
            "public final class PriceEngine {\n"
            "    private int normalize(int input) {\n"
            "        return input;\n"
            "    }\n"
            "}\n"
        )
    _commit_all(git_repo, "initial")

    with open(api_file, "w") as f:
        f.write(
            "package com.acme.pricing;\n\n"
            "public final class PriceEngine {\n"
            "    private int normalize(int input, int scale) {\n"
            "        return input * scale;\n"
            "    }\n"
            "}\n"
        )

    index = _make_index(git_repo)
    result = detect_breaking_changes(index, since_ref="HEAD")

    assert "normalize" not in result


def test_package_private_java_method_not_reported_as_breaking(git_repo):
    api_file = os.path.join(git_repo, "PriceEngine.java")
    with open(api_file, "w") as f:
        f.write(
            "package com.acme.pricing;\n\n"
            "public final class PriceEngine {\n"
            "    int normalize(int input) {\n"
            "        return input;\n"
            "    }\n"
            "}\n"
        )
    _commit_all(git_repo, "initial")

    with open(api_file, "w") as f:
        f.write(
            "package com.acme.pricing;\n\n"
            "public final class PriceEngine {\n"
            "    int normalize(int input, int scale) {\n"
            "        return input * scale;\n"
            "    }\n"
            "}\n"
        )

    index = _make_index(git_repo)
    result = detect_breaking_changes(index, since_ref="HEAD")

    assert "normalize" not in result


def test_java_method_not_flagged_when_only_lines_shift(git_repo):
    api_file = os.path.join(git_repo, "StatusController.java")
    with open(api_file, "w") as f:
        f.write(
            "package com.acme.pricing;\n\n"
            "public final class StatusController {\n"
            "    public String health(String env) {\n"
            "        return env;\n"
            "    }\n"
            "}\n"
        )
    _commit_all(git_repo, "initial")

    with open(api_file, "w") as f:
        f.write(
            "package com.acme.pricing;\n\n"
            "public final class StatusController {\n"
            "    public String status() {\n"
            "        return \"ok\";\n"
            "    }\n\n"
            "    public String health(String env) {\n"
            "        return env;\n"
            "    }\n"
            "}\n"
        )

    index = _make_index(git_repo)
    result = detect_breaking_changes(index, since_ref="HEAD")

    assert "health" not in result
