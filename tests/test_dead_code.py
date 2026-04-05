"""Tests for the dead_code module."""
from __future__ import annotations

import pytest

from token_savior.models import (
    ClassInfo,
    FunctionInfo,
    LineRange,
    ProjectIndex,
    StructuralMetadata,
)
from token_savior.dead_code import find_dead_code


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_func(
    name: str,
    qualified_name: str | None = None,
    line_start: int = 1,
    line_end: int = 5,
    decorators: list[str] | None = None,
    is_method: bool = False,
    parent_class: str | None = None,
) -> FunctionInfo:
    return FunctionInfo(
        name=name,
        qualified_name=qualified_name or name,
        line_range=LineRange(line_start, line_end),
        parameters=[],
        decorators=decorators or [],
        docstring=None,
        is_method=is_method,
        parent_class=parent_class,
    )


def _make_class(
    name: str,
    line_start: int = 1,
    line_end: int = 10,
    decorators: list[str] | None = None,
    methods: list[FunctionInfo] | None = None,
) -> ClassInfo:
    return ClassInfo(
        name=name,
        line_range=LineRange(line_start, line_end),
        base_classes=[],
        methods=methods or [],
        decorators=decorators or [],
        docstring=None,
    )


def _make_meta(
    source_name: str,
    functions: list[FunctionInfo] | None = None,
    classes: list[ClassInfo] | None = None,
) -> StructuralMetadata:
    return StructuralMetadata(
        source_name=source_name,
        total_lines=50,
        total_chars=500,
        lines=[],
        line_char_offsets=[],
        functions=functions or [],
        classes=classes or [],
    )


def _make_index(
    files: dict[str, StructuralMetadata],
    reverse_dependency_graph: dict[str, set[str]] | None = None,
) -> ProjectIndex:
    return ProjectIndex(
        root_path="/project",
        files=files,
        reverse_dependency_graph=reverse_dependency_graph or {},
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestBasicDeadCode:
    def test_function_with_no_dependents_is_dead(self):
        func = _make_func("unused_helper", line_start=15)
        meta = _make_meta("src/utils.py", functions=[func])
        index = _make_index({"src/utils.py": meta})
        result = find_dead_code(index)
        assert "unused_helper" in result
        assert "1 unreferenced symbol" in result

    def test_function_with_dependents_is_not_dead(self):
        func = _make_func("used_helper", line_start=10)
        meta = _make_meta("src/utils.py", functions=[func])
        index = _make_index(
            {"src/utils.py": meta},
            reverse_dependency_graph={"used_helper": {"caller"}},
        )
        result = find_dead_code(index)
        assert "used_helper" not in result
        assert "0 unreferenced symbols" in result

    def test_empty_index_returns_zero(self):
        index = _make_index({})
        result = find_dead_code(index)
        assert "0 unreferenced symbols" in result


class TestEntryPoints:
    def test_main_function_not_flagged(self):
        func = _make_func("main", line_start=5)
        meta = _make_meta("src/app.py", functions=[func])
        index = _make_index({"src/app.py": meta})
        result = find_dead_code(index)
        assert "main" not in result
        assert "0 unreferenced symbols" in result

    def test_dunder_init_not_flagged(self):
        func = _make_func("__init__", qualified_name="MyClass.__init__", line_start=5, is_method=True, parent_class="MyClass")
        meta = _make_meta("src/myclass.py", functions=[func])
        index = _make_index({"src/myclass.py": meta})
        result = find_dead_code(index)
        assert "__init__" not in result

    def test_dunder_main_not_flagged(self):
        func = _make_func("__main__", line_start=5)
        meta = _make_meta("src/app.py", functions=[func])
        index = _make_index({"src/app.py": meta})
        result = find_dead_code(index)
        assert "__main__" not in result

    def test_route_decorator_not_flagged(self):
        func = _make_func("my_view", line_start=20, decorators=["app.route('/home')"])
        meta = _make_meta("src/views.py", functions=[func])
        index = _make_index({"src/views.py": meta})
        result = find_dead_code(index)
        assert "my_view" not in result

    def test_click_command_decorator_not_flagged(self):
        func = _make_func("cli_cmd", line_start=20, decorators=["click.command()"])
        meta = _make_meta("src/cli.py", functions=[func])
        index = _make_index({"src/cli.py": meta})
        result = find_dead_code(index)
        assert "cli_cmd" not in result

    def test_task_decorator_not_flagged(self):
        func = _make_func("run_task", line_start=20, decorators=["celery.task"])
        meta = _make_meta("src/tasks.py", functions=[func])
        index = _make_index({"src/tasks.py": meta})
        result = find_dead_code(index)
        assert "run_task" not in result

    def test_test_prefix_function_not_flagged(self):
        func = _make_func("test_something", line_start=10)
        meta = _make_meta("src/logic.py", functions=[func])
        index = _make_index({"src/logic.py": meta})
        result = find_dead_code(index)
        assert "test_something" not in result

    def test_fixture_decorator_not_flagged(self):
        func = _make_func("my_fixture", line_start=5, decorators=["pytest.fixture"])
        meta = _make_meta("src/conftest.py", functions=[func])
        index = _make_index({"src/conftest.py": meta})
        result = find_dead_code(index)
        assert "my_fixture" not in result

    def test_setup_decorator_not_flagged(self):
        func = _make_func("setup_method", line_start=5, decorators=["setup"])
        meta = _make_meta("src/test_x.py", functions=[func])
        index = _make_index({"src/test_x.py": meta})
        result = find_dead_code(index)
        assert "setup_method" not in result

    def test_dataclass_decorator_class_not_flagged(self):
        cls = _make_class("MyData", line_start=1, line_end=10, decorators=["dataclass"])
        meta = _make_meta("src/models.py", classes=[cls])
        index = _make_index({"src/models.py": meta})
        result = find_dead_code(index)
        assert "MyData" not in result

    def test_model_decorator_class_not_flagged(self):
        cls = _make_class("UserModel", line_start=1, line_end=10, decorators=["pydantic.model"])
        meta = _make_meta("src/models.py", classes=[cls])
        index = _make_index({"src/models.py": meta})
        result = find_dead_code(index)
        assert "UserModel" not in result


class TestTestFiles:
    def test_function_in_test_file_prefix_not_flagged(self):
        func = _make_func("helper_in_test_file", line_start=5)
        meta = _make_meta("tests/test_utils.py", functions=[func])
        index = _make_index({"tests/test_utils.py": meta})
        result = find_dead_code(index)
        assert "helper_in_test_file" not in result

    def test_function_in_suffix_test_file_not_flagged(self):
        func = _make_func("helper_in_test_file", line_start=5)
        meta = _make_meta("tests/utils_test.py", functions=[func])
        index = _make_index({"tests/utils_test.py": meta})
        result = find_dead_code(index)
        assert "helper_in_test_file" not in result


class TestInitPy:
    def test_symbol_in_init_py_not_flagged(self):
        func = _make_func("exported_util", line_start=5)
        meta = _make_meta("mypackage/__init__.py", functions=[func])
        index = _make_index({"mypackage/__init__.py": meta})
        result = find_dead_code(index)
        assert "exported_util" not in result


class TestClassDeadCode:
    def test_class_with_no_dependents_is_dead(self):
        cls = _make_class("OldProcessor", line_start=42, line_end=60)
        meta = _make_meta("src/legacy.py", classes=[cls])
        index = _make_index({"src/legacy.py": meta})
        result = find_dead_code(index)
        assert "OldProcessor" in result

    def test_class_with_dependents_not_dead(self):
        cls = _make_class("UsedProcessor", line_start=42, line_end=60)
        meta = _make_meta("src/legacy.py", classes=[cls])
        index = _make_index(
            {"src/legacy.py": meta},
            reverse_dependency_graph={"UsedProcessor": {"main"}},
        )
        result = find_dead_code(index)
        assert "UsedProcessor" not in result


class TestMaxResults:
    def test_max_results_limits_output(self):
        funcs = [_make_func(f"dead_{i}", line_start=i * 10) for i in range(1, 11)]
        meta = _make_meta("src/lots.py", functions=funcs)
        index = _make_index({"src/lots.py": meta})
        result = find_dead_code(index, max_results=3)
        # Should still say total found
        assert "10 unreferenced symbols found" in result
        # But only show 3 entries in the body
        dead_lines = [line for line in result.splitlines() if "function dead_" in line]
        assert len(dead_lines) == 3

    def test_max_results_default_is_50(self):
        funcs = [_make_func(f"dead_{i}", line_start=i * 10) for i in range(1, 60)]
        meta = _make_meta("src/lots.py", functions=funcs)
        index = _make_index({"src/lots.py": meta})
        result = find_dead_code(index)
        dead_lines = [line for line in result.splitlines() if "function dead_" in line]
        assert len(dead_lines) == 50


class TestOutputFormat:
    def test_output_format(self):
        func = _make_func("unused_helper", line_start=15)
        meta = _make_meta("src/utils.py", functions=[func])
        index = _make_index({"src/utils.py": meta})
        result = find_dead_code(index)
        assert "Dead Code Analysis" in result
        assert "src/utils.py:" in result
        assert "line 15:" in result
        assert "function unused_helper" in result

    def test_output_sorted_by_file_then_line(self):
        func_a = _make_func("dead_a", line_start=30)
        func_b = _make_func("dead_b", line_start=5)
        func_z = _make_func("dead_z", line_start=1)
        meta_src = _make_meta("src/z_mod.py", functions=[func_a, func_b])
        meta_aaa = _make_meta("aaa/module.py", functions=[func_z])
        index = _make_index({"src/z_mod.py": meta_src, "aaa/module.py": meta_aaa})
        result = find_dead_code(index)
        pos_aaa = result.index("aaa/module.py")
        pos_src = result.index("src/z_mod.py")
        assert pos_aaa < pos_src, "Files should be sorted alphabetically"
        pos_b = result.index("line 5:")
        pos_a = result.index("line 30:")
        assert pos_b < pos_a, "Within a file, symbols sorted by line number"

    def test_class_output_format(self):
        cls = _make_class("OldProcessor", line_start=42, line_end=60)
        meta = _make_meta("src/legacy.py", classes=[cls])
        index = _make_index({"src/legacy.py": meta})
        result = find_dead_code(index)
        assert "class OldProcessor" in result
        assert "line 42:" in result
