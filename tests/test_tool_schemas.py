"""Tests for tool schema definitions and deprecation handling."""

from unittest.mock import patch

from token_savior.tool_schemas import TOOL_SCHEMAS, DEPRECATED_TOOLS


class TestToolSchemas:
    def test_all_tools_have_description(self):
        for name, schema in TOOL_SCHEMAS.items():
            assert "description" in schema, f"Tool '{name}' missing description"
            assert isinstance(schema["description"], str)
            assert len(schema["description"]) > 10, f"Tool '{name}' description too short"

    def test_all_tools_have_input_schema(self):
        for name, schema in TOOL_SCHEMAS.items():
            assert "inputSchema" in schema, f"Tool '{name}' missing inputSchema"
            assert isinstance(schema["inputSchema"], dict)
            assert schema["inputSchema"].get("type") == "object", (
                f"Tool '{name}' inputSchema type must be 'object'"
            )

    def test_required_fields_are_in_properties(self):
        for name, schema in TOOL_SCHEMAS.items():
            required = schema["inputSchema"].get("required", [])
            properties = schema["inputSchema"].get("properties", {})
            for req in required:
                assert req in properties, (
                    f"Tool '{name}': required field '{req}' not in properties"
                )

    def test_deprecated_tools_are_marked(self):
        assert "get_changed_symbols_since_ref" in DEPRECATED_TOOLS
        assert "apply_symbol_change_validate_with_rollback" in DEPRECATED_TOOLS

    def test_deprecated_descriptions_mention_deprecated(self):
        for name in DEPRECATED_TOOLS:
            desc = TOOL_SCHEMAS[name]["description"]
            assert "DEPRECATED" in desc.upper(), (
                f"Deprecated tool '{name}' description should mention DEPRECATED"
            )

    def test_tool_count(self):
        assert len(TOOL_SCHEMAS) == 53, f"Expected 53 tools, got {len(TOOL_SCHEMAS)}"

    def test_server_tools_match_schemas(self):
        from token_savior.server import TOOLS
        server_names = {t.name for t in TOOLS}
        schema_names = set(TOOL_SCHEMAS.keys())
        assert server_names == schema_names


class TestDeprecatedHandlers:
    """Verify deprecated tool aliases inject deprecation messages."""

    def test_get_changed_symbols_since_ref_has_deprecation_message(self):
        from token_savior.server import _h_get_changed_symbols_since_ref

        class _FakeSlot:
            root = "/tmp"
            is_git = True
            indexer = None
            query_fns = None
            _last_update_check = 0.0
            _dir_mtimes = {}
            cache = None
            stats_file = ""

        fake_slot = _FakeSlot()
        # Mock to return a simple dict
        with patch(
            "token_savior.server._h_get_changed_symbols",
            return_value={"files": [], "modified_files": 0},
        ):
            result = _h_get_changed_symbols_since_ref(fake_slot, {"since_ref": "HEAD~1"})
        assert "_deprecated" in result
        assert "DEPRECATED" in result["_deprecated"]
        assert "get_changed_symbols" in result["_deprecated"]

    def test_apply_symbol_change_validate_with_rollback_has_deprecation_message(self):
        from token_savior.server import _h_apply_symbol_change_validate_with_rollback

        class _FakeSlot:
            root = "/tmp"
            is_git = False
            indexer = None
            query_fns = None
            _last_update_check = 0.0
            _dir_mtimes = {}
            cache = None
            stats_file = ""

        fake_slot = _FakeSlot()
        with patch(
            "token_savior.server._h_apply_symbol_change_and_validate",
            return_value={"ok": True, "workflow": "test"},
        ):
            result = _h_apply_symbol_change_validate_with_rollback(
                fake_slot, {"symbol_name": "foo", "new_source": "bar"}
            )
        assert "_deprecated" in result
        assert "DEPRECATED" in result["_deprecated"]
        assert "rollback_on_failure" in result["_deprecated"]
