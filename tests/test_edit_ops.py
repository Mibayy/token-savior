"""Tests for compact structural edit helpers."""

from __future__ import annotations

from token_savior.edit_ops import (
    add_field_to_model,
    insert_near_symbol,
    move_symbol,
    replace_symbol_source,
    resolve_symbol_location,
)
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


class TestAddFieldToModel:
    def test_prisma_model(self, tmp_path):
        (tmp_path / "schema.prisma").write_text(
            "model Member {\n"
            "  id    Int    @id @default(autoincrement())\n"
            "  name  String\n"
            "}\n",
            encoding="utf-8",
        )
        indexer = ProjectIndexer(str(tmp_path), include_patterns=["**/*.prisma"])
        index = indexer.index()

        result = add_field_to_model(index, "Member", "archivedAt", "DateTime?")

        assert result["ok"] is True
        content = (tmp_path / "schema.prisma").read_text(encoding="utf-8")
        assert "archivedAt  DateTime?" in content
        # Field should be inside the model block
        assert content.index("archivedAt") > content.index("model Member")
        assert content.index("archivedAt") < content.rindex("}")

    def test_python_dataclass(self, tmp_path):
        (tmp_path / "models.py").write_text(
            "from dataclasses import dataclass\n"
            "\n"
            "@dataclass\n"
            "class User:\n"
            "    name: str\n"
            "    email: str\n",
            encoding="utf-8",
        )
        indexer = ProjectIndexer(str(tmp_path), include_patterns=["**/*.py"])
        index = indexer.index()

        result = add_field_to_model(index, "User", "age", "int")

        assert result["ok"] is True
        content = (tmp_path / "models.py").read_text(encoding="utf-8")
        assert "    age: int" in content

    def test_typescript_interface(self, tmp_path):
        (tmp_path / "types.ts").write_text(
            "export interface Member {\n"
            "  id: number;\n"
            "  name: string;\n"
            "}\n",
            encoding="utf-8",
        )
        indexer = ProjectIndexer(str(tmp_path), include_patterns=["**/*.ts"])
        index = indexer.index()

        result = add_field_to_model(index, "Member", "archivedAt", "Date | null")

        assert result["ok"] is True
        content = (tmp_path / "types.ts").read_text(encoding="utf-8")
        assert "  archivedAt: Date | null;" in content

    def test_typescript_optional_field(self, tmp_path):
        (tmp_path / "types.ts").write_text(
            "export interface Member {\n"
            "  id: number;\n"
            "  name: string;\n"
            "}\n",
            encoding="utf-8",
        )
        indexer = ProjectIndexer(str(tmp_path), include_patterns=["**/*.ts"])
        index = indexer.index()

        result = add_field_to_model(index, "Member", "archivedAt", "Date?")

        assert result["ok"] is True
        content = (tmp_path / "types.ts").read_text(encoding="utf-8")
        assert "  archivedAt?: Date;" in content

    def test_after_param(self, tmp_path):
        (tmp_path / "schema.prisma").write_text(
            "model Member {\n"
            "  id    Int    @id @default(autoincrement())\n"
            "  name  String\n"
            "  email String\n"
            "}\n",
            encoding="utf-8",
        )
        indexer = ProjectIndexer(str(tmp_path), include_patterns=["**/*.prisma"])
        index = indexer.index()

        result = add_field_to_model(
            index, "Member", "archivedAt", "DateTime?", after="name"
        )

        assert result["ok"] is True
        content = (tmp_path / "schema.prisma").read_text(encoding="utf-8")
        lines = content.splitlines()
        name_idx = next(i for i, ln in enumerate(lines) if "name" in ln)
        arch_idx = next(i for i, ln in enumerate(lines) if "archivedAt" in ln)
        assert arch_idx == name_idx + 1


class TestMoveSymbol:
    def test_moves_function_to_new_file(self, tmp_path):
        (tmp_path / "utils.py").write_text(
            "def slugify(s):\n"
            "    return s.lower().replace(' ', '-')\n"
            "\n"
            "def truncate(s, n):\n"
            "    return s[:n]\n",
            encoding="utf-8",
        )
        indexer = ProjectIndexer(str(tmp_path), include_patterns=["**/*.py"])
        index = indexer.index()

        result = move_symbol(index, "slugify", "shared/strings.py")

        assert result["ok"] is True
        assert result["from_file"] == "utils.py"
        assert result["to_file"] == "shared/strings.py"
        # Source file should no longer have slugify
        src = (tmp_path / "utils.py").read_text(encoding="utf-8")
        assert "def slugify" not in src
        assert "def truncate" in src
        # Target file should have slugify
        tgt = (tmp_path / "shared" / "strings.py").read_text(encoding="utf-8")
        assert "def slugify" in tgt

    def test_updates_imports(self, tmp_path):
        (tmp_path / "utils.py").write_text(
            "def slugify(s):\n"
            "    return s.lower()\n",
            encoding="utf-8",
        )
        (tmp_path / "views.py").write_text(
            "from utils import slugify\n"
            "\n"
            "def create_article(title):\n"
            "    return slugify(title)\n",
            encoding="utf-8",
        )
        indexer = ProjectIndexer(str(tmp_path), include_patterns=["**/*.py"])
        index = indexer.index()

        result = move_symbol(index, "slugify", "shared/strings.py")

        assert result["ok"] is True
        assert "views.py" in result["updated_imports"]
        views = (tmp_path / "views.py").read_text(encoding="utf-8")
        assert "from shared.strings import slugify" in views
        assert "from utils import slugify" not in views

    def test_create_if_missing_false(self, tmp_path):
        (tmp_path / "utils.py").write_text(
            "def slugify(s):\n"
            "    return s.lower()\n",
            encoding="utf-8",
        )
        indexer = ProjectIndexer(str(tmp_path), include_patterns=["**/*.py"])
        index = indexer.index()

        result = move_symbol(
            index, "slugify", "nonexistent.py", create_if_missing=False
        )

        assert "error" in result
