from __future__ import annotations

import pytest

from token_savior.cross_project import (
    find_cross_project_deps,
    _get_project_packages,
    _get_all_imports,
    _is_stdlib,
)
from token_savior.models import ImportInfo, ProjectIndex, StructuralMetadata


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_import(module: str, names: list[str] | None = None, line: int = 1, is_from: bool = True) -> ImportInfo:
    return ImportInfo(
        module=module,
        names=names or [],
        alias=None,
        line_number=line,
        is_from_import=is_from,
    )


def _make_meta(source_name: str, imports: list[ImportInfo]) -> StructuralMetadata:
    return StructuralMetadata(
        source_name=source_name,
        total_lines=1,
        total_chars=0,
        lines=[""],
        line_char_offsets=[0],
        imports=imports,
    )


def _make_index(root_path: str, files: dict[str, list[ImportInfo]]) -> ProjectIndex:
    return ProjectIndex(
        root_path=root_path,
        files={path: _make_meta(path, imps) for path, imps in files.items()},
    )


# ---------------------------------------------------------------------------
# Tests for helpers
# ---------------------------------------------------------------------------

class TestIsStdlib:
    def test_stdlib_modules_excluded(self):
        assert _is_stdlib("os") is True
        assert _is_stdlib("sys") is True
        assert _is_stdlib("pathlib") is True
        assert _is_stdlib("collections") is True
        assert _is_stdlib("json") is True

    def test_third_party_not_stdlib(self):
        assert _is_stdlib("fastapi") is False
        assert _is_stdlib("pydantic") is False
        assert _is_stdlib("requests") is False
        assert _is_stdlib("hermes_pc") is False


class TestGetAllImports:
    def test_extracts_top_level_module_names(self):
        index = _make_index("/proj/a", {
            "a/core.py": [
                _make_import("fastapi.routing"),
                _make_import("os.path"),
                _make_import("pydantic"),
            ],
        })
        result = _get_all_imports(index)
        assert "fastapi" in result
        assert "pydantic" in result
        # os is stdlib, may or may not be filtered here — _get_all_imports is raw
        assert "os" in result

    def test_deduplicates_across_files(self):
        index = _make_index("/proj/a", {
            "a/x.py": [_make_import("requests")],
            "a/y.py": [_make_import("requests")],
        })
        result = _get_all_imports(index)
        assert result == {"requests"}

    def test_empty_index_returns_empty_set(self):
        index = _make_index("/proj/a", {})
        assert _get_all_imports(index) == set()


class TestGetProjectPackages:
    def test_infers_package_from_src_layout(self):
        index = _make_index("/proj/a", {
            "src/hermes_pc/client.py": [],
            "src/hermes_pc/server.py": [],
        })
        result = _get_project_packages(index)
        assert "hermes_pc" in result

    def test_infers_package_from_flat_layout(self):
        index = _make_index("/proj/a", {
            "hermes_agent/core.py": [],
            "hermes_agent/utils.py": [],
        })
        result = _get_project_packages(index)
        assert "hermes_agent" in result

    def test_excludes_non_package_top_dirs(self):
        # files without a subdir (flat scripts) should not produce a package name
        index = _make_index("/proj/a", {
            "main.py": [],
        })
        result = _get_project_packages(index)
        assert "main" not in result

    def test_multiple_packages(self):
        index = _make_index("/proj/a", {
            "src/pkg_a/mod.py": [],
            "src/pkg_b/mod.py": [],
        })
        result = _get_project_packages(index)
        assert "pkg_a" in result
        assert "pkg_b" in result


# ---------------------------------------------------------------------------
# Tests for find_cross_project_deps
# ---------------------------------------------------------------------------

class TestFindCrossProjectDeps:
    def test_single_project_no_cross_deps(self):
        index_a = _make_index("/proj/a", {
            "pkg_a/core.py": [_make_import("fastapi")],
        })
        result = find_cross_project_deps({"proj-a": index_a})
        assert "no cross-project dependencies found" in result

    def test_direct_dependency_detected(self):
        """Project A imports a module provided by project B."""
        index_a = _make_index("/proj/a", {
            "pkg_a/core.py": [_make_import("hermes_pc.client")],
        })
        index_b = _make_index("/proj/b", {
            "src/hermes_pc/server.py": [],
        })
        result = find_cross_project_deps({"hermes-agent": index_a, "hermes-pc-mcp": index_b})

        assert "hermes-agent → hermes-pc-mcp" in result
        assert "hermes_pc" in result

    def test_shared_external_dependency_detected(self):
        """Both projects import the same third-party module."""
        index_a = _make_index("/proj/a", {
            "pkg_a/core.py": [_make_import("requests")],
        })
        index_b = _make_index("/proj/b", {
            "pkg_b/core.py": [_make_import("requests")],
        })
        result = find_cross_project_deps({"proj-a": index_a, "proj-b": index_b})

        assert "requests" in result
        assert "proj-a" in result
        assert "proj-b" in result

    def test_stdlib_imports_excluded_from_shared(self):
        """os, sys etc. should not appear as shared external deps."""
        index_a = _make_index("/proj/a", {
            "pkg_a/core.py": [_make_import("os"), _make_import("sys")],
        })
        index_b = _make_index("/proj/b", {
            "pkg_b/core.py": [_make_import("os"), _make_import("sys")],
        })
        result = find_cross_project_deps({"proj-a": index_a, "proj-b": index_b})

        # stdlib modules must not appear as shared dependencies
        assert "Shared external dependencies:" not in result
        assert "no cross-project dependencies found" in result

    def test_header_shows_project_count(self):
        index_a = _make_index("/proj/a", {"pkg_a/x.py": [_make_import("requests")]})
        index_b = _make_index("/proj/b", {"pkg_b/x.py": [_make_import("fastapi")]})
        index_c = _make_index("/proj/c", {"pkg_c/x.py": [_make_import("pydantic")]})
        result = find_cross_project_deps({"a": index_a, "b": index_b, "c": index_c})
        assert "3 projects analyzed" in result

    def test_full_scenario_matches_expected_output(self):
        """End-to-end: A depends on B, both use fastapi and pydantic."""
        index_a = _make_index("/proj/hermes-agent", {
            "src/hermes_agent/main.py": [
                _make_import("hermes_pc.client"),
                _make_import("fastapi"),
                _make_import("pydantic"),
            ],
        })
        index_b = _make_index("/proj/hermes-pc-mcp", {
            "src/hermes_pc/server.py": [
                _make_import("fastapi"),
                _make_import("pydantic"),
            ],
        })

        result = find_cross_project_deps({"hermes-agent": index_a, "hermes-pc-mcp": index_b})

        assert "Cross-Project Dependencies" in result
        assert "2 projects analyzed" in result
        assert "hermes-agent → hermes-pc-mcp" in result
        assert "hermes_pc" in result
        assert "fastapi" in result
        assert "pydantic" in result

    def test_no_shared_deps_section_when_only_direct(self):
        """When only direct deps exist and no shared third-party deps, no shared section."""
        index_a = _make_index("/proj/a", {
            "pkg_a/core.py": [_make_import("pkg_b.stuff")],
        })
        index_b = _make_index("/proj/b", {
            "src/pkg_b/mod.py": [],
        })
        result = find_cross_project_deps({"proj-a": index_a, "proj-b": index_b})

        assert "proj-a → proj-b" in result
        # No shared external deps because only pkg_b imported and it's provided by proj-b
        assert "Shared external" not in result or "pkg_b" not in result

    def test_empty_indices(self):
        result = find_cross_project_deps({})
        assert "no cross-project dependencies found" in result
