"""Tests for the get_usage_stats session metrics."""

import time

import pytest


@pytest.fixture(autouse=True)
def _reset_server_state():
    """Reset server module-level state before each test."""
    import mcp_codebase_index.server as srv

    srv._session_start = time.time()
    srv._tool_call_counts.clear()
    srv._total_chars_returned = 0
    srv._projects.clear()
    srv._active_root = ""
    yield
    srv._tool_call_counts.clear()
    srv._total_chars_returned = 0
    srv._projects.clear()
    srv._active_root = ""


class TestFormatDuration:
    def test_seconds(self):
        from mcp_codebase_index.server import _format_duration

        assert _format_duration(45) == "45s"

    def test_minutes(self):
        from mcp_codebase_index.server import _format_duration

        assert _format_duration(125) == "2m 5s"

    def test_hours(self):
        from mcp_codebase_index.server import _format_duration

        assert _format_duration(3725) == "1h 2m"


class TestFormatUsageStats:
    def test_empty_session(self):
        from mcp_codebase_index.server import _format_usage_stats

        result = _format_usage_stats()
        assert "Total queries: 0" in result
        assert "Total chars returned: 0" in result

    def test_with_tool_calls(self):
        import mcp_codebase_index.server as srv

        srv._tool_call_counts["find_symbol"] = 5
        srv._tool_call_counts["get_function_source"] = 3
        srv._total_chars_returned = 1234

        result = srv._format_usage_stats()
        assert "Total queries: 8" in result
        assert "find_symbol: 5" in result
        assert "get_function_source: 3" in result
        assert "Total chars returned: 1,234" in result

    def test_usage_stats_call_excluded_from_query_count(self):
        import mcp_codebase_index.server as srv

        srv._tool_call_counts["find_symbol"] = 3
        srv._tool_call_counts["get_usage_stats"] = 2

        result = srv._format_usage_stats()
        assert "Total queries: 3" in result
        # get_usage_stats should not appear in the per-tool breakdown
        assert "get_usage_stats" not in result

    def test_with_indexed_project(self, tmp_path):
        import mcp_codebase_index.server as srv
        from mcp_codebase_index.project_indexer import ProjectIndexer
        from mcp_codebase_index.server import _ProjectSlot

        # Create a project with enough source to exceed returned chars
        (tmp_path / "main.py").write_text("def hello():\n    return 'world'\n" * 100)
        (tmp_path / "utils.py").write_text("def helper():\n    return 42\n" * 100)

        indexer = ProjectIndexer(str(tmp_path), include_patterns=["**/*.py"])
        indexer.index()
        root = str(tmp_path)
        slot = _ProjectSlot(root=root, indexer=indexer)
        srv._projects[root] = slot
        srv._active_root = root

        srv._tool_call_counts["find_symbol"] = 5
        srv._total_chars_returned = 200

        result = srv._format_usage_stats()
        assert "Total source in index:" in result
        assert "Estimated token savings:" in result

    def test_token_savings_uses_per_tool_multipliers(self, tmp_path):
        """Naive estimate should use per-tool cost multipliers, not full codebase per query."""
        import mcp_codebase_index.server as srv
        from mcp_codebase_index.project_indexer import ProjectIndexer
        from mcp_codebase_index.server import _ProjectSlot

        # Create a project with known size
        (tmp_path / "big.py").write_text("x = 1\n" * 1000)  # ~6000 chars

        indexer = ProjectIndexer(str(tmp_path), include_patterns=["**/*.py"])
        indexer.index()
        root = str(tmp_path)
        slot = _ProjectSlot(root=root, indexer=indexer)
        srv._projects[root] = slot
        srv._active_root = root

        source_chars = sum(m.total_chars for m in indexer._project_index.files.values())

        # find_symbol has multiplier 0.05, so 10 calls = source_chars * 0.05 * 10
        srv._tool_call_counts["find_symbol"] = 10
        srv._total_chars_returned = 500

        result = srv._format_usage_stats()
        assert "Estimated without indexer:" in result
        assert "Estimated with indexer:" in result
        assert "tokens" in result

        # The naive estimate should be source_chars * 0.05 * 10, NOT source_chars * 10
        expected_naive = int(source_chars * 0.05 * 10)
        assert f"{expected_naive:,} chars" in result

    def test_different_tools_produce_different_costs(self, tmp_path):
        """Tools with different multipliers should produce different naive estimates."""
        import mcp_codebase_index.server as srv
        from mcp_codebase_index.project_indexer import ProjectIndexer
        from mcp_codebase_index.server import _ProjectSlot

        (tmp_path / "code.py").write_text("x = 1\n" * 1000)

        indexer = ProjectIndexer(str(tmp_path), include_patterns=["**/*.py"])
        indexer.index()
        root = str(tmp_path)
        slot = _ProjectSlot(root=root, indexer=indexer)
        srv._projects[root] = slot
        srv._active_root = root

        source_chars = sum(m.total_chars for m in indexer._project_index.files.values())

        # Test with a cheap tool (list_files: 0.01)
        srv._tool_call_counts["list_files"] = 1
        srv._total_chars_returned = 50
        result_cheap = srv._format_usage_stats()

        # Reset and test with an expensive tool (get_change_impact: 0.30)
        srv._tool_call_counts.clear()
        srv._total_chars_returned = 50
        srv._tool_call_counts["get_change_impact"] = 1
        result_expensive = srv._format_usage_stats()

        # Extract the "Estimated without indexer" numbers
        def extract_naive(text: str) -> int:
            for line in text.splitlines():
                if "Estimated without indexer:" in line:
                    # Format: "Estimated without indexer: N chars (M tokens) over Q queries"
                    num_str = line.split(":")[1].split("chars")[0].strip().replace(",", "")
                    return int(num_str)
            return 0

        cheap_naive = extract_naive(result_cheap)
        expensive_naive = extract_naive(result_expensive)

        assert cheap_naive > 0
        assert expensive_naive > 0
        assert expensive_naive > cheap_naive
        # Verify exact values based on multipliers
        assert cheap_naive == int(source_chars * 0.01)
        assert expensive_naive == int(source_chars * 0.30)

    def test_no_savings_section_without_index(self):
        import mcp_codebase_index.server as srv

        srv._tool_call_counts["find_symbol"] = 3
        srv._total_chars_returned = 100

        result = srv._format_usage_stats()
        assert "Estimated token savings:" not in result
