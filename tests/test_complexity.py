"""Tests for complexity hotspot detection."""

from __future__ import annotations

from token_savior.complexity import find_hotspots, _compute_nesting_depth, _count_branches, _score_function
from token_savior.models import (
    FunctionInfo,
    LineRange,
    ProjectIndex,
    StructuralMetadata,
)


def _make_index(files: dict[str, tuple[list[str], list[FunctionInfo]]]) -> ProjectIndex:
    """Build a minimal ProjectIndex from file specs."""
    index_files = {}
    for path, (lines, functions) in files.items():
        index_files[path] = StructuralMetadata(
            source_name=path,
            total_lines=len(lines),
            total_chars=sum(len(l) for l in lines),
            lines=lines,
            line_char_offsets=[],
            functions=functions,
        )
    return ProjectIndex(root_path="/fake", files=index_files)


def _make_func(
    name: str,
    start: int,
    end: int,
    params: list[str] | None = None,
    file: str = "src/foo.py",
) -> FunctionInfo:
    return FunctionInfo(
        name=name,
        qualified_name=name,
        line_range=LineRange(start=start, end=end),
        parameters=params or [],
        decorators=[],
        docstring=None,
        is_method=False,
        parent_class=None,
    )


# ---------------------------------------------------------------------------
# Unit tests for helpers
# ---------------------------------------------------------------------------


class TestComputeNestingDepth:
    def test_flat_function(self):
        lines = [
            "def foo():\n",
            "    x = 1\n",
            "    return x\n",
        ]
        assert _compute_nesting_depth(lines) == 1

    def test_nested_if_inside_for(self):
        lines = [
            "def foo():\n",
            "    for i in range(10):\n",
            "        if i > 5:\n",
            "            x = i\n",
        ]
        assert _compute_nesting_depth(lines) == 3

    def test_empty_lines_ignored(self):
        lines = [
            "def foo():\n",
            "\n",
            "    return 1\n",
        ]
        assert _compute_nesting_depth(lines) == 1

    def test_single_line(self):
        lines = ["def foo(): return 1\n"]
        assert _compute_nesting_depth(lines) == 0


class TestCountBranches:
    def test_no_branches(self):
        lines = ["    x = 1\n", "    return x\n"]
        assert _count_branches(lines) == 0

    def test_if_elif_else(self):
        lines = [
            "    if x > 0:\n",
            "        pass\n",
            "    elif x < 0:\n",
            "        pass\n",
            "    else:\n",
            "        pass\n",
        ]
        assert _count_branches(lines) == 3

    def test_for_while(self):
        lines = [
            "    for i in range(10):\n",
            "        while True:\n",
            "            break\n",
        ]
        assert _count_branches(lines) == 2

    def test_try_except(self):
        lines = [
            "    try:\n",
            "        pass\n",
            "    except ValueError:\n",
            "        pass\n",
        ]
        assert _count_branches(lines) == 2

    def test_match_case(self):
        lines = [
            "    match cmd:\n",
            "        case 'a':\n",
            "            pass\n",
            "        case 'b':\n",
            "            pass\n",
        ]
        assert _count_branches(lines) == 3


class TestScoreFunction:
    def test_zero_everything(self):
        assert _score_function(0, 0, 0, 0) == 0.0

    def test_weights(self):
        # line_count * 0.3 + branch_count * 2.0 + nesting * 1.5 + max(0, params-4) * 1.0
        score = _score_function(10, 3, 2, 6)
        expected = 10 * 0.3 + 3 * 2.0 + 2 * 1.5 + (6 - 4) * 1.0
        assert abs(score - expected) < 1e-9

    def test_params_below_4_no_penalty(self):
        score = _score_function(0, 0, 0, 3)
        assert score == 0.0

    def test_params_exactly_4_no_penalty(self):
        score = _score_function(0, 0, 0, 4)
        assert score == 0.0

    def test_params_5_adds_1(self):
        score = _score_function(0, 0, 0, 5)
        assert score == 1.0


# ---------------------------------------------------------------------------
# Integration tests for find_hotspots
# ---------------------------------------------------------------------------


class TestFindHotspots:
    def test_empty_index_returns_no_functions_found(self):
        index = _make_index({})
        result = find_hotspots(index)
        assert "no functions found" in result.lower()

    def test_complex_ranks_higher_than_simple(self):
        # Simple: 3 lines, 0 branches, depth 1, 0 params
        simple_lines = [
            "def simple():\n",           # line 1
            "    x = 1\n",               # line 2
            "    return x\n",            # line 3
        ]
        simple_func = _make_func("simple", start=1, end=3, params=[])

        # Complex: 10 lines, 4 branches, deeper nesting, 6 params
        complex_lines = [
            "def complex(a, b, c, d, e, f):\n",   # line 1
            "    if a:\n",                          # line 2
            "        for i in range(b):\n",         # line 3
            "            if i > c:\n",              # line 4
            "                while d:\n",           # line 5
            "                    x = i\n",          # line 6
            "    elif b:\n",                        # line 7
            "        pass\n",                       # line 8
            "    else:\n",                          # line 9
            "        return f\n",                   # line 10
        ]
        complex_func = _make_func("complex_fn", start=1, end=10, params=["a", "b", "c", "d", "e", "f"])

        index = _make_index({
            "src/simple.py": (simple_lines, [simple_func]),
            "src/complex.py": (complex_lines, [complex_func]),
        })
        result = find_hotspots(index)
        # complex_fn should appear before simple
        assert result.index("complex_fn") < result.index("simple")

    def test_max_results_limits_output(self):
        lines = ["def f():\n", "    return 1\n"]
        funcs = [_make_func(f"func_{i}", 1, 2) for i in range(10)]
        index = _make_index({"src/foo.py": (lines, funcs)})
        result = find_hotspots(index, max_results=3)
        # Count data rows (lines with | that aren't the header separator)
        data_rows = [
            line for line in result.splitlines()
            if "|" in line and "---" not in line and "Score" not in line
        ]
        assert len(data_rows) == 3

    def test_min_score_filters_low_complexity(self):
        # A very simple function: 2 lines, 0 branches, minimal depth, 0 params
        # score = 2*0.3 + 0 + 1*1.5 + 0 = 2.1
        lines = ["def tiny():\n", "    pass\n"]
        func = _make_func("tiny", 1, 2)
        index = _make_index({"src/foo.py": (lines, [func])})

        # With a high min_score, the function should be filtered out
        result = find_hotspots(index, min_score=100.0)
        assert "no functions found" in result.lower()

    def test_min_score_zero_includes_all(self):
        lines = ["def tiny():\n", "    pass\n"]
        func = _make_func("tiny", 1, 2)
        index = _make_index({"src/foo.py": (lines, [func])})
        result = find_hotspots(index, min_score=0.0)
        assert "tiny" in result

    def test_many_params_increases_score(self):
        # func_many: 2 lines, 0 branches, 8 params → param penalty = (8-4)*1.0 = 4
        # func_few:  2 lines, 0 branches, 1 param  → param penalty = 0
        lines = ["def f(a, b, c, d, e, f, g, h):\n", "    pass\n"]
        func_many = _make_func("func_many", 1, 2, params=["a", "b", "c", "d", "e", "f", "g", "h"])
        func_few = _make_func("func_few", 1, 2, params=["x"])

        index = _make_index({
            "src/many.py": (lines, [func_many]),
            "src/few.py": (lines, [func_few]),
        })
        result = find_hotspots(index)
        assert result.index("func_many") < result.index("func_few")

    def test_header_format_present(self):
        lines = ["def foo():\n", "    return 1\n"]
        func = _make_func("foo", 1, 2)
        index = _make_index({"src/foo.py": (lines, [func])})
        result = find_hotspots(index)
        assert "Score" in result
        assert "Lines" in result
        assert "Branches" in result
        assert "Depth" in result
        assert "Function" in result

    def test_output_contains_file_and_line(self):
        lines = ["def bar():\n", "    return 2\n"]
        func = _make_func("bar", 1, 2)
        index = _make_index({"src/bar.py": (lines, [func])})
        result = find_hotspots(index)
        assert "src/bar.py" in result
        assert "bar" in result

    def test_nesting_depth_affects_score(self):
        # deep: deeply nested → higher nesting score
        deep_lines = [
            "def deep():\n",                    # line 1
            "    if True:\n",                   # line 2
            "        if True:\n",               # line 3
            "            if True:\n",           # line 4
            "                if True:\n",       # line 5
            "                    pass\n",       # line 6
        ]
        deep_func = _make_func("deep_fn", 1, 6)

        flat_lines = [
            "def flat():\n",  # line 1
            "    pass\n",     # line 2
        ]
        flat_func = _make_func("flat_fn", 1, 2)

        index = _make_index({
            "src/deep.py": (deep_lines, [deep_func]),
            "src/flat.py": (flat_lines, [flat_func]),
        })
        result = find_hotspots(index)
        assert result.index("deep_fn") < result.index("flat_fn")
