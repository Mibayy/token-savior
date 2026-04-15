"""Tests for batch mode (names parameter) on code_nav handlers."""

import json

from token_savior.server_handlers.code_nav import (
    _batch_dispatch,
    _q_find_symbol,
    _q_get_full_context,
    _q_list_files,
    _resolve_batch_names,
)


def test_resolve_batch_names_returns_none_without_names():
    assert _resolve_batch_names({"name": "foo"}) is None


def test_resolve_batch_names_returns_list():
    assert _resolve_batch_names({"names": ["a", "b"]}) == ["a", "b"]


def test_resolve_batch_names_caps_at_10():
    names = [f"sym_{i}" for i in range(15)]
    result = _resolve_batch_names({"names": names})
    assert len(result) == 10


def test_batch_dispatch_returns_none_for_single_mode():
    result = _batch_dispatch({}, {"name": "foo"}, lambda q, a: "x")
    assert result is None


def test_batch_dispatch_returns_json_dict():
    def handler(qfns, args):
        return f"source of {args['name']}"

    result = _batch_dispatch({}, {"names": ["a", "b"]}, handler)
    parsed = json.loads(result)
    assert parsed == {"a": "source of a", "b": "source of b"}


def test_batch_dispatch_catches_errors():
    def handler(qfns, args):
        if args["name"] == "bad":
            raise ValueError("not found")
        return "ok"

    result = _batch_dispatch({}, {"names": ["good", "bad"]}, handler)
    parsed = json.loads(result)
    assert parsed["good"] == "ok"
    assert "Error" in parsed["bad"]


def test_batch_dispatch_disables_hints():
    captured = {}

    def handler(qfns, args):
        captured["hints"] = args.get("hints")
        return "ok"

    _batch_dispatch({}, {"names": ["a"], "hints": True}, handler)
    assert captured["hints"] is False


def test_find_symbol_batch():
    qfns = {
        "find_symbol": lambda name, level=0: {"name": name, "file": f"{name}.py", "line": 1, "type": "function"},
    }
    result = _q_find_symbol(qfns, {"names": ["foo", "bar"], "level": 2})
    parsed = json.loads(result)
    assert "foo" in parsed
    assert "bar" in parsed
    assert parsed["foo"]["file"] == "foo.py"


def test_find_symbol_single_still_works():
    qfns = {
        "find_symbol": lambda name, level=0: {"name": name, "file": f"{name}.py", "line": 1, "type": "function"},
    }
    result = _q_find_symbol(qfns, {"name": "baz", "hints": False})
    assert isinstance(result, dict)
    assert result["name"] == "baz"


def test_get_full_context_batch():
    qfns = {
        "get_full_context": lambda name, depth=1, max_lines=200: {"symbol": {"name": name}, "source": f"def {name}(): ..."},
    }
    result = _q_get_full_context(qfns, {"names": ["x", "y"]})
    parsed = json.loads(result)
    assert "x" in parsed and "y" in parsed
    assert parsed["x"]["symbol"]["name"] == "x"


def test_get_full_context_single_still_works():
    qfns = {
        "get_full_context": lambda name, depth=1, max_lines=200: {"symbol": {"name": name}},
    }
    result = _q_get_full_context(qfns, {"name": "z"})
    assert isinstance(result, dict)
    assert result["symbol"]["name"] == "z"


# ── Empty-result suggestions ──────────────────────────────────────────


def test_list_files_empty_suggests_alternatives():
    qfns = {"list_files": lambda pattern=None, max_results=0: []}
    result = _q_list_files(qfns, {"pattern": "*.xyz"})
    assert isinstance(result, dict)
    assert result["files"] == []
    assert "list_files()" in result["_suggestion"]
    assert "search_codebase" in result["_suggestion"]


def test_list_files_empty_no_pattern_no_suggestion():
    qfns = {"list_files": lambda pattern=None, max_results=0: []}
    result = _q_list_files(qfns, {})
    assert isinstance(result, list)
    assert result == []


def test_list_files_non_empty_returns_list():
    qfns = {"list_files": lambda pattern=None, max_results=0: ["a.py", "b.py"]}
    result = _q_list_files(qfns, {"pattern": "*.py"})
    assert isinstance(result, list)
    assert len(result) == 2


def test_find_symbol_not_found_suggests_alternatives():
    qfns = {"find_symbol": lambda name, level=0: {"error": f"symbol '{name}' not found"}}
    result = _q_find_symbol(qfns, {"name": "missing_func"})
    assert "_suggestion" in result
    assert "search_codebase" in result["_suggestion"]
    assert "get_functions()" in result["_suggestion"]


def test_find_symbol_found_no_suggestion():
    qfns = {"find_symbol": lambda name, level=0: {"name": name, "file": "x.py", "line": 1, "type": "function"}}
    result = _q_find_symbol(qfns, {"name": "exists", "hints": False})
    assert "_suggestion" not in result
