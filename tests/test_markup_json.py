"""Tests for the JSON annotator."""

import json

from token_savior.json_annotator import annotate_json


class TestJsonBasicObject:
    """Tests for simple key-value JSON objects."""

    def test_simple_object(self):
        text = json.dumps({"name": "test", "version": "1.0"}, indent=2)
        meta = annotate_json(text)
        titles = [s.title for s in meta.sections]
        assert "name" in titles
        assert "version" in titles

    def test_all_sections_level_1(self):
        text = json.dumps({"a": 1, "b": 2, "c": 3}, indent=2)
        meta = annotate_json(text)
        assert all(s.level == 1 for s in meta.sections)

    def test_line_numbers_populated(self):
        text = '{\n  "name": "test",\n  "version": "1.0"\n}'
        meta = annotate_json(text)
        name_section = next(s for s in meta.sections if s.title == "name")
        assert name_section.line_range.start == 2  # "name" is on line 2

    def test_source_name_default(self):
        meta = annotate_json("{}")
        assert meta.source_name == "<json>"

    def test_source_name_custom(self):
        meta = annotate_json("{}", source_name="config.json")
        assert meta.source_name == "config.json"


class TestJsonNestedObject:
    """Tests for nested JSON objects producing deeper levels."""

    def test_nested_keys_increase_level(self):
        text = json.dumps({"outer": {"inner": "value"}}, indent=2)
        meta = annotate_json(text)
        outer = next(s for s in meta.sections if s.title == "outer")
        inner = next(s for s in meta.sections if s.title == "inner")
        assert outer.level == 1
        assert inner.level == 2

    def test_three_levels_deep(self):
        text = json.dumps({"a": {"b": {"c": "val"}}}, indent=2)
        meta = annotate_json(text)
        levels = {s.title: s.level for s in meta.sections}
        assert levels["a"] == 1
        assert levels["b"] == 2
        assert levels["c"] == 3


class TestJsonArrays:
    """Tests for arrays of named objects."""

    def test_named_array_elements(self):
        data = {
            "nodes": [
                {"name": "Start", "type": "trigger"},
                {"name": "End", "type": "action"},
            ]
        }
        text = json.dumps(data, indent=2)
        meta = annotate_json(text)
        titles = [s.title for s in meta.sections]
        assert any("Start" in t for t in titles)
        assert any("End" in t for t in titles)

    def test_array_with_id_field(self):
        data = {"items": [{"id": "abc123", "value": 42}]}
        text = json.dumps(data, indent=2)
        meta = annotate_json(text)
        titles = [s.title for s in meta.sections]
        assert any("abc123" in t for t in titles)

    def test_array_with_type_field(self):
        data = {"steps": [{"type": "http_request", "url": "https://example.com"}]}
        text = json.dumps(data, indent=2)
        meta = annotate_json(text)
        titles = [s.title for s in meta.sections]
        assert any("http_request" in t for t in titles)

    def test_array_without_distinguishing_field(self):
        """Arrays of objects without name/id/type should not create section entries."""
        data = {"coords": [{"x": 1, "y": 2}, {"x": 3, "y": 4}]}
        text = json.dumps(data, indent=2)
        meta = annotate_json(text)
        titles = [s.title for s in meta.sections]
        # "coords" should be there, but no array element entries
        assert "coords" in titles
        # x/y keys still appear as nested sections
        assert any(s.title == "x" for s in meta.sections)

    def test_array_of_primitives(self):
        """Arrays of non-objects should not cause errors."""
        data = {"tags": ["a", "b", "c"]}
        text = json.dumps(data, indent=2)
        meta = annotate_json(text)
        titles = [s.title for s in meta.sections]
        assert "tags" in titles


class TestJsonSchemaRefs:
    """Tests for $ref values becoming ImportInfo."""

    def test_ref_captured(self):
        data = {"properties": {"user": {"$ref": "#/definitions/User"}}}
        text = json.dumps(data, indent=2)
        meta = annotate_json(text)
        assert len(meta.imports) == 1
        assert meta.imports[0].module == "#/definitions/User"

    def test_multiple_refs(self):
        data = {
            "allOf": [
                {"$ref": "#/definitions/Base"},
                {"$ref": "#/definitions/Extra"},
            ]
        }
        text = json.dumps(data, indent=2)
        meta = annotate_json(text)
        modules = [imp.module for imp in meta.imports]
        assert "#/definitions/Base" in modules
        assert "#/definitions/Extra" in modules

    def test_ref_not_string_ignored(self):
        """$ref with a non-string value should be treated as a regular key."""
        data = {"$ref": 42}
        text = json.dumps(data, indent=2)
        meta = annotate_json(text)
        assert len(meta.imports) == 0
        # It should appear as a section instead
        titles = [s.title for s in meta.sections]
        assert "$ref" in titles


class TestJsonEdgeCases:
    """Edge cases: invalid JSON, empty, minified."""

    def test_invalid_json_fallback(self):
        text = "not valid json {{"
        meta = annotate_json(text)
        # Falls back to generic annotator — no sections, no imports
        assert meta.sections == []
        assert meta.imports == []
        assert meta.total_lines > 0

    def test_empty_object(self):
        meta = annotate_json("{}")
        assert meta.sections == []
        assert meta.imports == []
        assert meta.total_lines == 1

    def test_empty_array(self):
        meta = annotate_json("[]")
        assert meta.sections == []
        assert meta.imports == []

    def test_minified_json(self):
        """Minified JSON (single line) should still extract keys."""
        data = {"name": "test", "version": "1.0", "main": "index.js"}
        text = json.dumps(data)  # no indent — single line
        meta = annotate_json(text)
        titles = [s.title for s in meta.sections]
        assert "name" in titles
        assert "version" in titles
        assert "main" in titles

    def test_total_chars(self):
        text = '{"key": "value"}'
        meta = annotate_json(text)
        assert meta.total_chars == len(text)

    def test_line_char_offsets(self):
        text = '{\n  "a": 1\n}'
        meta = annotate_json(text)
        assert meta.line_char_offsets == [0, 2, 11]

    def test_functions_classes_empty(self):
        text = '{"key": "value"}'
        meta = annotate_json(text)
        assert meta.functions == []
        assert meta.classes == []


class TestJsonDepthCap:
    """Tests that deeply nested structures stop at level 4."""

    def test_depth_capped_at_4(self):
        data = {"l1": {"l2": {"l3": {"l4": {"l5": "deep"}}}}}
        text = json.dumps(data, indent=2)
        meta = annotate_json(text)
        max_level = max(s.level for s in meta.sections)
        assert max_level == 4
        # l5 should NOT appear (would be level 5)
        titles = [s.title for s in meta.sections]
        assert "l1" in titles
        assert "l2" in titles
        assert "l3" in titles
        assert "l4" in titles
        assert "l5" not in titles


class TestJsonN8NWorkflow:
    """Realistic N8N-style workflow JSON."""

    def test_n8n_workflow(self):
        workflow = {
            "name": "My Workflow",
            "nodes": [
                {
                    "name": "Start",
                    "type": "n8n-nodes-base.start",
                    "parameters": {},
                    "position": [250, 300],
                },
                {
                    "name": "HTTP Request",
                    "type": "n8n-nodes-base.httpRequest",
                    "parameters": {
                        "url": "https://api.example.com/data",
                        "method": "GET",
                    },
                    "position": [450, 300],
                },
                {
                    "name": "Set Variable",
                    "type": "n8n-nodes-base.set",
                    "parameters": {"values": {"string": [{"name": "output"}]}},
                    "position": [650, 300],
                },
            ],
            "connections": {
                "Start": {"main": [[{"node": "HTTP Request", "type": "main"}]]},
                "HTTP Request": {"main": [[{"node": "Set Variable", "type": "main"}]]},
            },
            "settings": {"executionOrder": "v1"},
        }
        text = json.dumps(workflow, indent=2)
        meta = annotate_json(text)

        titles = [s.title for s in meta.sections]

        # Top-level keys
        assert "name" in titles
        assert "nodes" in titles
        assert "connections" in titles
        assert "settings" in titles

        # Named nodes should appear
        assert any("Start" in t for t in titles)
        assert any("HTTP Request" in t for t in titles)
        assert any("Set Variable" in t for t in titles)

        # Should have both sections and no crash
        assert len(meta.sections) > 5
        assert meta.imports == []


class TestAnnotatorDispatch:
    """Test that the annotator dispatch routes .json correctly."""

    def test_json_dispatch(self):
        from token_savior.annotator import annotate

        text = '{"key": "value"}'
        meta = annotate(text, source_name="test.json")
        titles = [s.title for s in meta.sections]
        assert "key" in titles

    def test_json_explicit_file_type(self):
        from token_savior.annotator import annotate

        text = '{"key": "value"}'
        meta = annotate(text, file_type="json")
        titles = [s.title for s in meta.sections]
        assert "key" in titles
