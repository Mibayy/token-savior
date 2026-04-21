"""Tests for tool schema definitions."""

from token_savior.tool_schemas import TOOL_SCHEMAS


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

    def test_tool_count(self):
        # v2.0.0: 53 core + 16 memory engine = 69 tools.
        # +1 P5 +1 P6 +1 P7 +1 P8 +2 P9 (verify_edit + find_semantic_duplicates) = 75.
        # +1 v2.2 Step B (get_session_budget) = 76.
        # +2 v2.2 Step C (memory_bus_push, memory_bus_list) = 78.
        # +1 v2.2 Step D (get_lattice_stats) = 79.
        # +3 v2.2 Prompt2 Step A (reasoning_save/search/list) = 82.
        # +1 v2.2 Prompt2 Step B (get_speculation_stats) = 83.
        # +1 v2.2 Prompt3 Step A (get_dcp_stats) = 84.
        # +2 v2.2 Prompt3 Step B (get_coactive_symbols, get_tca_stats) = 86.
        # +2 v2.3 Step B (memory_roi_gc, memory_roi_stats) = 88.
        # +2 v2.3 Step C (get_community, get_leiden_stats) = 90.
        # +1 v2.3 Step D (memory_distill) = 91.
        # +1 v2.3 Prompt3 Step A (get_linucb_stats) = 92.
        # +1 v2.3 Prompt3 Step B (get_warmstart_stats) = 93.
        # +2 v2.3 Prompt3 Step C (memory_consistency, memory_quarantine_list) = 95.
        # +2 Java quality tools (find_allocation_hotspots, find_performance_hotspots) = 97.
        # +1 Java duplicate-classes (get_duplicate_classes) = 98.
        # +1 get_full_context (chain collapse from IMPROVEMENT-SIGNALS) = 99.
        # +1 add_field_to_model = 100.
        # +1 move_symbol = 101.
        # +1 apply_refactoring = 102.
        # +1 find_import_cycles = 103.
        # +1 P2 memory_dedup_sweep = 104.
        # +1 P5 memory_session_history = 105.
        # +1 A1-2 memory_vector_reindex = 106.
        # v2.6 fusions: stats 9->1 (-8), checkpoints 6->1 (-5), clustering 4->1 (-3),
        # hotspots 3->1 (-2). Total -18 => 88 tools.
        assert len(TOOL_SCHEMAS) == 88, f"Expected 88 tools, got {len(TOOL_SCHEMAS)}"

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
