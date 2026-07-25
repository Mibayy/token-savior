"""Tests for find_symbol kind reporting and module/class-level variable indexing.

Two behaviours are covered:

1. A find_symbol miss reports which symbol kinds were actually searched
   (previously it returned ``complete: True``, which claimed an exhaustive
   scan while only functions and classes had ever been indexed).
2. Module- and class-level variables/constants are indexed and reachable via
   ``find_symbol(name, kinds=[...])``, gated by ``TOKEN_SAVIOR_VARIABLES``.
"""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from token_savior.models import variables_mode
from token_savior.project_indexer import ProjectIndexer
from token_savior.python_annotator import annotate_python
from token_savior.query_api import create_project_query_functions

SAMPLE = """\
from typing import Any

pfb: dict[str, Any] = {}
MAX_QUEUE = 5000
_private = None


class Engine:
    retries = 3

    def run(self):
        return pfb


def helper():
    return MAX_QUEUE
"""


@pytest.fixture(autouse=True)
def _default_variables_mode(monkeypatch):
    """Every test states its own mode; default to the shipped default."""
    monkeypatch.delenv("TOKEN_SAVIOR_VARIABLES", raising=False)


def _index(tmp_path: Path, files: dict[str, str]):
    for rel, content in files.items():
        p = tmp_path / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content)
    return ProjectIndexer(str(tmp_path)).index()


@pytest.fixture
def funcs():
    tmp = Path(tempfile.mkdtemp())
    index = _index(tmp, {"engine.py": SAMPLE})
    return create_project_query_functions(index)


# ---------------------------------------------------------------------------
# 1. Honest miss reporting
# ---------------------------------------------------------------------------


class TestMissReportsSearchedKinds:
    def test_miss_lists_the_kinds_actually_searched(self, funcs):
        result = funcs["find_symbol"]("definitely_absent")
        assert "error" in result
        assert result["searched"] == ["function", "class"]

    def test_miss_does_not_claim_an_exhaustive_scan(self, funcs):
        result = funcs["find_symbol"]("definitely_absent")
        assert "complete" not in result

    def test_miss_reports_indexed_kinds_it_skipped(self, funcs):
        # Variables are indexed by default but not searched by default, so a
        # miss must say so — that is the whole point of the report.
        result = funcs["find_symbol"]("definitely_absent")
        assert result["not_searched"] == ["variable"]

    def test_hit_still_reports_complete(self, funcs):
        result = funcs["find_symbol"]("helper")
        assert result["complete"] is True
        assert result["type"] == "function"


# ---------------------------------------------------------------------------
# 2a. Extraction — python annotator
# ---------------------------------------------------------------------------


class TestPythonAnnotatorVariables:
    def test_module_level_variable_extracted(self):
        meta = annotate_python(SAMPLE, "engine.py")
        by_name = {v.name: v for v in meta.variables}
        assert "pfb" in by_name
        var = by_name["pfb"]
        assert var.line_number == 3
        assert var.kind == "variable"
        assert var.scope == "module"
        assert var.qualified_name == "pfb"
        assert var.type_annotation == "dict[str, Any]"

    def test_upper_case_name_is_a_constant(self):
        meta = annotate_python(SAMPLE, "engine.py")
        by_name = {v.name: v for v in meta.variables}
        assert by_name["MAX_QUEUE"].kind == "constant"

    def test_class_level_variable_is_scoped_and_qualified(self):
        meta = annotate_python(SAMPLE, "engine.py")
        by_qname = {v.qualified_name: v for v in meta.variables}
        assert "Engine.retries" in by_qname
        assert by_qname["Engine.retries"].scope == "class"

    def test_function_local_variables_are_not_extracted(self):
        meta = annotate_python(
            "def f():\n    local_only = 1\n    return local_only\n", "f.py"
        )
        assert [v.name for v in meta.variables] == []

    def test_extraction_is_skipped_when_mode_is_off(self, monkeypatch):
        monkeypatch.setenv("TOKEN_SAVIOR_VARIABLES", "off")
        meta = annotate_python(SAMPLE, "engine.py")
        assert meta.variables == []


# ---------------------------------------------------------------------------
# 2b. Lookup — find_symbol(kinds=...)
# ---------------------------------------------------------------------------


class TestFindSymbolKinds:
    def test_variable_not_found_under_default_kinds(self, funcs):
        result = funcs["find_symbol"]("pfb")
        assert "error" in result
        assert result["not_searched"] == ["variable"]

    def test_variable_found_when_kind_requested(self, funcs):
        result = funcs["find_symbol"]("pfb", kinds=["variable"])
        assert result["file"] == "engine.py"
        assert result["line"] == 3
        assert result["type"] == "variable"
        assert result["complete"] is True

    def test_constant_reports_its_kind(self, funcs):
        result = funcs["find_symbol"]("MAX_QUEUE", kinds=["variable"])
        assert result["type"] == "constant"

    def test_isolating_a_kind_excludes_the_others(self, funcs):
        result = funcs["find_symbol"]("helper", kinds=["variable"])
        assert "error" in result
        assert result["searched"] == ["variable"]

    def test_class_attribute_found_by_qualified_name(self, funcs):
        result = funcs["find_symbol"]("Engine.retries", kinds=["variable"])
        assert result["line"] == 9
        assert result["type"] == "variable"

    def test_same_variable_in_two_files_is_ambiguous(self):
        tmp = Path(tempfile.mkdtemp())
        index = _index(tmp, {"a.py": "shared = 1\n", "b.py": "shared = 2\n"})
        funcs = create_project_query_functions(index)
        result = funcs["find_symbol"]("shared", kinds=["variable"])
        assert "ambiguous" in result["error"]
        assert sorted(result["candidates"]) == ["a.py", "b.py"]

    def test_unknown_kind_is_rejected(self, funcs):
        result = funcs["find_symbol"]("pfb", kinds=["gadget"])
        assert "error" in result
        assert "gadget" in result["error"]


# ---------------------------------------------------------------------------
# 2c. TOKEN_SAVIOR_VARIABLES gating
# ---------------------------------------------------------------------------


class TestVariablesMode:
    def test_default_mode_is_index(self):
        assert variables_mode() == "index"

    def test_invalid_value_falls_back_to_default(self, monkeypatch):
        monkeypatch.setenv("TOKEN_SAVIOR_VARIABLES", "banana")
        assert variables_mode() == "index"

    def test_search_mode_puts_variables_in_the_default_kinds(self, monkeypatch):
        monkeypatch.setenv("TOKEN_SAVIOR_VARIABLES", "search")
        tmp = Path(tempfile.mkdtemp())
        funcs = create_project_query_functions(_index(tmp, {"engine.py": SAMPLE}))
        result = funcs["find_symbol"]("pfb")
        assert result["type"] == "variable"
        assert result["file"] == "engine.py"

    def test_search_mode_miss_reports_all_three_kinds(self, monkeypatch):
        monkeypatch.setenv("TOKEN_SAVIOR_VARIABLES", "search")
        tmp = Path(tempfile.mkdtemp())
        funcs = create_project_query_functions(_index(tmp, {"engine.py": SAMPLE}))
        result = funcs["find_symbol"]("definitely_absent")
        assert result["searched"] == ["function", "class", "variable"]
        assert "not_searched" not in result

    def test_off_mode_rejects_an_explicit_variable_request(self, monkeypatch):
        monkeypatch.setenv("TOKEN_SAVIOR_VARIABLES", "off")
        tmp = Path(tempfile.mkdtemp())
        funcs = create_project_query_functions(_index(tmp, {"engine.py": SAMPLE}))
        result = funcs["find_symbol"]("pfb", kinds=["variable"])
        assert "error" in result
        assert "TOKEN_SAVIOR_VARIABLES" in result["error"]

    def test_off_mode_keeps_variables_out_of_the_index(self, monkeypatch):
        monkeypatch.setenv("TOKEN_SAVIOR_VARIABLES", "off")
        tmp = Path(tempfile.mkdtemp())
        index = _index(tmp, {"engine.py": SAMPLE})
        assert index.variable_table == {}
        assert index.files["engine.py"].variables == []


# ---------------------------------------------------------------------------
# 2d. Cache round-trip
# ---------------------------------------------------------------------------


class TestVariableCacheRoundTrip:
    def test_variables_survive_serialization(self):
        from token_savior.cache_ops import CacheManager

        tmp = Path(tempfile.mkdtemp())
        index = _index(tmp, {"engine.py": SAMPLE})
        mgr = CacheManager(str(tmp), cache_version=99)
        restored = mgr.index_from_dict(mgr.index_to_dict(index))

        names = {v.name for v in restored.files["engine.py"].variables}
        assert {"pfb", "MAX_QUEUE"} <= names
        assert restored.variable_table["pfb"] == ["engine.py"]

    def test_cache_version_was_bumped_for_the_new_field(self):
        from token_savior.server_state import _CACHE_VERSION

        # A stale v2 cache has no `variables` key; reading it as if it did
        # would silently report every project as variable-free.
        assert _CACHE_VERSION > 2
