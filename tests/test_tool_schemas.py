"""Tests for tool schema definitions."""

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

    def test_deprecated_tools_removed_in_v2(self):
        # v2.0.0: deprecated aliases from v1 were removed entirely.
        assert DEPRECATED_TOOLS == set() or DEPRECATED_TOOLS == frozenset()

    def test_deprecated_descriptions_mention_deprecated(self):
        for name in DEPRECATED_TOOLS:
            desc = TOOL_SCHEMAS[name]["description"]
            assert "DEPRECATED" in desc.upper(), (
                f"Deprecated tool '{name}' description should mention DEPRECATED"
            )

    def test_tool_count(self):
        # v2.0.0: 53 core + 16 memory engine = 69 tools.
        assert len(TOOL_SCHEMAS) == 69, f"Expected 69 tools, got {len(TOOL_SCHEMAS)}"

    def test_server_tools_match_schemas(self):
        from token_savior.server import TOOLS
        server_names = {t.name for t in TOOLS}
        schema_names = set(TOOL_SCHEMAS.keys())
        assert server_names == schema_names


class TestV2HandlersRemoved:
    """Verify v1 deprecated handlers were fully removed in v2.0.0."""

    def test_get_changed_symbols_since_ref_handler_removed(self):
        import token_savior.server as srv
        assert not hasattr(srv, "_h_get_changed_symbols_since_ref")

    def test_apply_symbol_change_validate_with_rollback_handler_removed(self):
        import token_savior.server as srv
        assert not hasattr(srv, "_h_apply_symbol_change_validate_with_rollback")
