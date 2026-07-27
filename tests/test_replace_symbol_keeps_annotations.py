"""`replace_symbol_source` must not eat what sits above the definition.

v4.20.0 stopped Python decorators from being deleted, but keyed the fix to
Python definition heads:

    if depouillee.startswith(("def ", "async def ", "class ")):

The Java annotator puts `@Override` inside the symbol's line range, no head
matches, and the scan falls through to the unchanged start -- so the
replacement begins *at the annotation line* and overwrites it. Same failure
the Python fix describes, in the one other language whose range convention
includes the attribute: what disappears is above the definition, so re-reading
the replaced symbol does not show it. An `@Override` or a `@Test` silently
leaving a method is a semantic change that still compiles.
"""
from __future__ import annotations

from token_savior.edit_ops import replace_symbol_source
from token_savior.project_indexer import ProjectIndexer


def _projet(tmp_path, nom: str, source: str):
    (tmp_path / nom).write_text(source, encoding="utf-8")
    return ProjectIndexer(str(tmp_path)).index()


def test_java_annotation_survives_the_replacement(tmp_path) -> None:
    index = _projet(tmp_path, "Api.java", (
        "public class Api {\n"
        "    @Override\n"
        "    public String toString() {\n"
        '        return "api";\n'
        "    }\n"
        "}\n"
    ))
    resultat = replace_symbol_source(index, "toString", (
        "    public String toString() {\n"
        '        return "API";\n'
        "    }"
    ))
    assert not resultat.get("error"), resultat
    source = (tmp_path / "Api.java").read_text(encoding="utf-8")
    assert "@Override" in source, source
    assert '"API"' in source, source


def test_java_annotation_block_survives(tmp_path) -> None:
    """Several annotations, one of them carrying arguments across lines."""
    index = _projet(tmp_path, "Svc.java", (
        "public class Svc {\n"
        "    @Deprecated\n"
        "    @SuppressWarnings({\n"
        '        "unchecked",\n'
        '        "rawtypes"\n'
        "    })\n"
        "    public int total() {\n"
        "        return 1;\n"
        "    }\n"
        "}\n"
    ))
    resultat = replace_symbol_source(index, "total", (
        "    public int total() {\n"
        "        return 2;\n"
        "    }"
    ))
    assert not resultat.get("error"), resultat
    source = (tmp_path / "Svc.java").read_text(encoding="utf-8")
    assert "@Deprecated" in source, source
    assert "@SuppressWarnings" in source, source
    assert '"rawtypes"' in source, source
    assert "return 2;" in source, source


def test_python_multiline_decorator_still_survives(tmp_path) -> None:
    """The Python case the v4.20.0 fix covers, kept honest.

    A decorator whose arguments span lines is where a naive "skip lines
    starting with @" would stop one line too early and delete the rest.
    """
    index = _projet(tmp_path, "mod.py", (
        "import pytest\n"
        "\n"
        "@pytest.mark.parametrize(\n"
        '    "x",\n'
        "    [1, 2],\n"
        ")\n"
        "def cible(x):\n"
        "    return x\n"
    ))
    resultat = replace_symbol_source(index, "cible", (
        "def cible(x):\n"
        "    return x * 2"
    ))
    assert not resultat.get("error"), resultat
    source = (tmp_path / "mod.py").read_text(encoding="utf-8")
    assert "@pytest.mark.parametrize(" in source, source
    assert "[1, 2]," in source, source
    assert "return x * 2" in source, source
    compile(source, "mod.py", "exec")


def test_caller_supplied_annotations_replace_the_existing_ones(tmp_path) -> None:
    """New source bringing its own attribute decides for itself."""
    index = _projet(tmp_path, "Old.java", (
        "public class Old {\n"
        "    @Deprecated\n"
        "    public int n() {\n"
        "        return 1;\n"
        "    }\n"
        "}\n"
    ))
    resultat = replace_symbol_source(index, "n", (
        "    @Override\n"
        "    public int n() {\n"
        "        return 2;\n"
        "    }"
    ))
    assert not resultat.get("error"), resultat
    source = (tmp_path / "Old.java").read_text(encoding="utf-8")
    assert "@Override" in source, source
    assert "@Deprecated" not in source, source
