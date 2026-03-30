"""Tests for compact structural edit helpers."""

from __future__ import annotations

from token_savior.edit_ops import insert_near_symbol, replace_symbol_source, resolve_symbol_location
from token_savior.project_indexer import ProjectIndexer


def _build_index(tmp_path):
    (tmp_path / "main.py").write_text(
        "def hello():\n"
        "    return 'hello'\n"
        "\n"
        "class Greeter:\n"
        "    def wave(self):\n"
        "        return 'wave'\n",
        encoding="utf-8",
    )
    indexer = ProjectIndexer(str(tmp_path), include_patterns=["**/*.py"])
    return indexer, indexer.index()


class TestResolveSymbolLocation:
    def test_resolves_function_and_class(self, tmp_path):
        _, index = _build_index(tmp_path)

        hello = resolve_symbol_location(index, "hello")
        greeter = resolve_symbol_location(index, "Greeter")

        assert hello["file"] == "main.py"
        assert hello["line"] == 1
        assert hello["type"] == "function"
        assert greeter["line"] == 4
        assert greeter["type"] == "class"


class TestReplaceSymbolSource:
    def test_replaces_function_block(self, tmp_path):
        _, index = _build_index(tmp_path)

        result = replace_symbol_source(
            index,
            "hello",
            "def hello():\n    return 'goodbye'",
        )

        assert result["ok"] is True
        assert result["delta_lines"] == 0
        updated = (tmp_path / "main.py").read_text(encoding="utf-8")
        assert "return 'goodbye'" in updated
        assert "return 'hello'" not in updated


class TestInsertNearSymbol:
    def test_inserts_after_symbol(self, tmp_path):
        _, index = _build_index(tmp_path)

        result = insert_near_symbol(
            index,
            "hello",
            "\n\ndef helper():\n    return 42".strip("\n"),
            position="after",
        )

        assert result["ok"] is True
        updated = (tmp_path / "main.py").read_text(encoding="utf-8")
        assert "def helper()" in updated
        assert updated.index("def helper()") > updated.index("def hello()")

    def test_inserts_before_symbol(self, tmp_path):
        _, index = _build_index(tmp_path)

        result = insert_near_symbol(
            index,
            "Greeter",
            "CONSTANT = 1\n",
            position="before",
        )

        assert result["ok"] is True
        updated = (tmp_path / "main.py").read_text(encoding="utf-8")
        assert updated.index("CONSTANT = 1") < updated.index("class Greeter")
