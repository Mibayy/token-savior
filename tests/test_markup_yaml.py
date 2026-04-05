"""Tests for the YAML annotator."""

import yaml

from token_savior.yaml_annotator import annotate_yaml


class TestYamlBasicObject:
    """Tests for simple key-value YAML objects."""

    def test_simple_keys(self):
        text = "name: myapp\nversion: 1.0\n"
        meta = annotate_yaml(text)
        titles = [s.title for s in meta.sections]
        assert "name" in titles
        assert "version" in titles

    def test_all_sections_level_1(self):
        text = "a: 1\nb: 2\nc: 3\n"
        meta = annotate_yaml(text)
        assert all(s.level == 1 for s in meta.sections)

    def test_source_name_default(self):
        meta = annotate_yaml("")
        assert meta.source_name == "<yaml>"

    def test_source_name_custom(self):
        meta = annotate_yaml("key: value\n", source_name="config.yaml")
        assert meta.source_name == "config.yaml"

    def test_line_numbers_populated(self):
        text = "name: myapp\nversion: 1.0\n"
        meta = annotate_yaml(text)
        name_section = next(s for s in meta.sections if s.title == "name")
        assert name_section.line_range.start == 1  # "name" is on line 1
        version_section = next(s for s in meta.sections if s.title == "version")
        assert version_section.line_range.start == 2  # "version" is on line 2


class TestYamlNestedObject:
    """Tests for nested YAML objects producing deeper levels."""

    def test_nested_keys_increase_level(self):
        text = "outer:\n  inner: value\n"
        meta = annotate_yaml(text)
        outer = next(s for s in meta.sections if s.title == "outer")
        inner = next(s for s in meta.sections if s.title == "inner")
        assert outer.level == 1
        assert inner.level == 2

    def test_three_levels_deep(self):
        text = "a:\n  b:\n    c: val\n"
        meta = annotate_yaml(text)
        levels = {s.title: s.level for s in meta.sections}
        assert levels["a"] == 1
        assert levels["b"] == 2
        assert levels["c"] == 3

    def test_depth_capped_at_4(self):
        text = "l1:\n  l2:\n    l3:\n      l4:\n        l5: deep\n"
        meta = annotate_yaml(text)
        max_level = max(s.level for s in meta.sections)
        assert max_level == 4
        titles = [s.title for s in meta.sections]
        assert "l1" in titles
        assert "l2" in titles
        assert "l3" in titles
        assert "l4" in titles
        assert "l5" not in titles


class TestYamlArrays:
    """Tests for arrays of named objects."""

    def test_named_array_items(self):
        text = (
            "services:\n"
            "  - name: web\n"
            "    port: 80\n"
            "  - name: db\n"
            "    port: 5432\n"
        )
        meta = annotate_yaml(text)
        titles = [s.title for s in meta.sections]
        assert any("web" in t for t in titles)
        assert any("db" in t for t in titles)

    def test_named_array_items_id_field(self):
        text = (
            "items:\n"
            "  - id: abc123\n"
            "    value: 42\n"
        )
        meta = annotate_yaml(text)
        titles = [s.title for s in meta.sections]
        assert any("abc123" in t for t in titles)

    def test_array_without_distinguishing_field(self):
        text = (
            "coords:\n"
            "  - x: 1\n"
            "    y: 2\n"
        )
        meta = annotate_yaml(text)
        titles = [s.title for s in meta.sections]
        assert "coords" in titles

    def test_array_of_primitives_no_crash(self):
        text = "tags:\n  - alpha\n  - beta\n  - gamma\n"
        meta = annotate_yaml(text)
        titles = [s.title for s in meta.sections]
        assert "tags" in titles


class TestYamlEdgeCases:
    """Edge cases: invalid YAML, empty, non-mapping result."""

    def test_invalid_yaml_fallback(self):
        text = "key: [\ninvalid yaml {{"
        meta = annotate_yaml(text)
        # Falls back to generic annotator — no sections
        assert meta.sections == []
        assert meta.total_lines > 0

    def test_empty_string_fallback(self):
        # yaml.safe_load("") returns None, not a dict
        meta = annotate_yaml("")
        assert meta.sections == []

    def test_non_dict_result_fallback(self):
        # A plain list at the top level
        text = "- a\n- b\n- c\n"
        meta = annotate_yaml(text)
        # Top-level list without dicts should still not crash
        assert meta.total_lines > 0

    def test_total_chars(self):
        text = "key: value\n"
        meta = annotate_yaml(text)
        assert meta.total_chars == len(text)

    def test_functions_classes_empty(self):
        text = "key: value\n"
        meta = annotate_yaml(text)
        assert meta.functions == []
        assert meta.classes == []

    def test_imports_empty(self):
        text = "key: value\n"
        meta = annotate_yaml(text)
        assert meta.imports == []
