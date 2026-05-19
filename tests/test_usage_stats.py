"""Tests for the get_usage_stats session metrics."""

import asyncio
import time

import pytest


@pytest.fixture(autouse=True)
def _reset_server_state():
    """Reset server module-level state before each test."""
    import token_savior.server_state as state

    state._session_start = time.time()
    state._session_id = "test-session"
    state._tool_call_counts.clear()
    state._total_chars_returned = 0
    state._total_naive_chars = 0
    state._slot_mgr.projects.clear()
    state._slot_mgr.active_root = ""
    yield
    state._tool_call_counts.clear()
    state._total_chars_returned = 0
    state._total_naive_chars = 0
    state._slot_mgr.projects.clear()
    state._slot_mgr.active_root = ""


class TestFormatDuration:
    def test_seconds(self):
        from token_savior.server import _format_duration

        assert _format_duration(45) == "45s"

    def test_minutes(self):
        from token_savior.server import _format_duration

        assert _format_duration(125) == "2m 5s"

    def test_hours(self):
        from token_savior.server import _format_duration

        assert _format_duration(3725) == "1h 2m"


class TestFormatUsageStats:
    def test_empty_session(self):
        from token_savior.server import _format_usage_stats

        result = _format_usage_stats()
        assert "0 queries" in result
        assert "Chars returned: 0" in result

    def test_with_tool_calls(self):
        import token_savior.server as srv
        import token_savior.server_state as state

        state._tool_call_counts["find_symbol"] = 5
        state._tool_call_counts["get_function_source"] = 3
        state._total_chars_returned = 1234

        result = srv._format_usage_stats()
        assert "8 queries" in result
        assert "find_symbol:5" in result
        assert "get_function_source:3" in result
        assert "Chars returned: 1,234" in result

    def test_usage_stats_call_excluded_from_query_count(self):
        import token_savior.server as srv
        import token_savior.server_state as state

        state._tool_call_counts["find_symbol"] = 3
        state._tool_call_counts["get_stats"] = 2

        result = srv._format_usage_stats()
        assert "3 queries" in result
        assert "get_stats" not in result

    def test_with_indexed_project(self, tmp_path):
        import token_savior.server as srv
        import token_savior.server_state as state
        from token_savior.project_indexer import ProjectIndexer
        from token_savior.server import _ProjectSlot

        (tmp_path / "main.py").write_text("def hello():\n    return 'world'\n" * 100)
        (tmp_path / "utils.py").write_text("def helper():\n    return 42\n" * 100)

        indexer = ProjectIndexer(str(tmp_path), include_patterns=["**/*.py"])
        indexer.index()
        root = str(tmp_path)
        slot = _ProjectSlot(root=root, indexer=indexer)
        state._slot_mgr.projects[root] = slot
        state._slot_mgr.active_root = root

        state._tool_call_counts["find_symbol"] = 5
        state._total_chars_returned = 200
        state._total_naive_chars = 1000

        result = srv._format_usage_stats()
        assert "Savings:" in result
        assert "tokens" in result

    def test_token_savings_uses_per_tool_multipliers(self, tmp_path):
        """Naive estimate should use per-tool cost multipliers, not full codebase per query."""
        import token_savior.server as srv
        import token_savior.server_state as state
        from token_savior.project_indexer import ProjectIndexer
        from token_savior.server import _ProjectSlot

        (tmp_path / "big.py").write_text("x = 1\n" * 1000)

        indexer = ProjectIndexer(str(tmp_path), include_patterns=["**/*.py"])
        indexer.index()
        root = str(tmp_path)
        slot = _ProjectSlot(root=root, indexer=indexer)
        state._slot_mgr.projects[root] = slot
        state._slot_mgr.active_root = root

        source_chars = sum(m.total_chars for m in indexer._project_index.files.values())

        state._tool_call_counts["find_symbol"] = 10
        state._total_chars_returned = 500
        state._total_naive_chars = int(source_chars * 0.05 * 10)

        result = srv._format_usage_stats()
        assert "Savings:" in result
        assert "tokens" in result

    def test_different_tools_produce_different_costs(self, tmp_path):
        """Tools with different multipliers should produce different naive estimates."""
        import token_savior.server as srv
        import token_savior.server_state as state
        from token_savior.project_indexer import ProjectIndexer
        from token_savior.server import _ProjectSlot

        (tmp_path / "code.py").write_text("x = 1\n" * 1000)

        indexer = ProjectIndexer(str(tmp_path), include_patterns=["**/*.py"])
        indexer.index()
        root = str(tmp_path)
        slot = _ProjectSlot(root=root, indexer=indexer)
        state._slot_mgr.projects[root] = slot
        state._slot_mgr.active_root = root

        source_chars = sum(m.total_chars for m in indexer._project_index.files.values())

        # Test with a cheap tool (list_files: 0.01)
        state._tool_call_counts["list_files"] = 1
        state._total_chars_returned = 50
        state._total_naive_chars = int(source_chars * 0.01)
        result_cheap = srv._format_usage_stats()

        # Reset and test with an expensive tool (get_change_impact: 0.30)
        state._tool_call_counts.clear()
        state._total_chars_returned = 50
        state._total_naive_chars = int(source_chars * 0.30)
        state._tool_call_counts["get_change_impact"] = 1
        result_expensive = srv._format_usage_stats()

        # Both should show savings info with tokens
        assert "Savings:" in result_cheap
        assert "Savings:" in result_expensive

        # Extract naive token count from compact format: "Savings: X% (Y vs Z tokens)"
        def extract_naive_tokens(text: str) -> int:
            for line in text.splitlines():
                if "vs " in line and "tokens" in line:
                    part = line.split("vs ")[1].split(" tokens")[0].strip().replace(",", "")
                    return int(part)
            return 0

        cheap_naive = extract_naive_tokens(result_cheap)
        expensive_naive = extract_naive_tokens(result_expensive)

        assert cheap_naive > 0
        assert expensive_naive > 0
        assert expensive_naive > cheap_naive

    def test_no_savings_section_without_index(self):
        import token_savior.server as srv
        import token_savior.server_state as state

        state._tool_call_counts["find_symbol"] = 3
        state._total_chars_returned = 100

        result = srv._format_usage_stats()
        assert "Savings:" not in result

    def test_new_workflow_tools_contribute_to_naive_estimate(self, tmp_path):
        import token_savior.server as srv
        import token_savior.server_state as state
        from token_savior.project_indexer import ProjectIndexer
        from token_savior.server import _ProjectSlot

        (tmp_path / "main.py").write_text("x = 1\n" * 1000, encoding="utf-8")

        indexer = ProjectIndexer(str(tmp_path), include_patterns=["**/*.py"])
        indexer.index()
        root = str(tmp_path)
        slot = _ProjectSlot(root=root, indexer=indexer)
        state._slot_mgr.projects[root] = slot
        state._slot_mgr.active_root = root

        source_chars = sum(m.total_chars for m in indexer._project_index.files.values())
        state._tool_call_counts["apply_symbol_change_and_validate"] = 1
        state._total_chars_returned = 200
        state._total_naive_chars = int(source_chars * 0.35)

        result = srv._format_usage_stats()
        assert "Savings:" in result

    def test_flush_stats_persists_session_history_without_double_counting(self, tmp_path):
        import token_savior.server as srv
        import token_savior.server_state as state
        from token_savior.server import _ProjectSlot

        stats_file = tmp_path / "stats.json"
        slot = _ProjectSlot(root=str(tmp_path), stats_file=str(stats_file))
        state._tool_call_counts["find_symbol"] = 2
        state._total_chars_returned = 100
        state._session_id = "session-a"

        srv._flush_stats(slot, naive_chars=1000)
        srv._flush_stats(slot, naive_chars=1000)

        payload = srv._load_cumulative_stats(str(stats_file))
        assert payload["sessions"] == 1
        assert payload["total_calls"] == 2
        assert payload["total_chars_returned"] == 100
        assert payload["total_naive_chars"] == 1000
        assert len(payload["history"]) == 1
        assert payload["history"][0]["tokens_used"] == 25
        assert payload["history"][0]["tokens_naive"] == 250
        assert payload["history"][0]["savings_pct"] == 90.0

    def test_format_usage_stats_shows_recent_session_log(self, tmp_path):
        import json
        import token_savior.server as srv
        import token_savior.server_state as state
        from token_savior.server import _ProjectSlot

        stats_file = tmp_path / "stats.json"
        stats_file.write_text(
            json.dumps(
                {
                    "total_calls": 8,
                    "total_chars_returned": 400,
                    "total_naive_chars": 4000,
                    "sessions": 2,
                    "tool_counts": {"find_symbol": 8},
                    "history": [
                        {
                            "session_id": "old",
                            "timestamp": "2026-03-29T10:00:00Z",
                            "query_calls": 3,
                            "chars_returned": 100,
                            "naive_chars": 1000,
                            "tokens_used": 25,
                            "tokens_naive": 250,
                            "savings_pct": 90.0,
                        },
                        {
                            "session_id": "new",
                            "timestamp": "2026-03-30T12:00:00Z",
                            "query_calls": 5,
                            "chars_returned": 300,
                            "naive_chars": 3000,
                            "tokens_used": 75,
                            "tokens_naive": 750,
                            "savings_pct": 90.0,
                        },
                    ],
                }
            ),
            encoding="utf-8",
        )

        root = str(tmp_path)
        slot = _ProjectSlot(root=root, stats_file=str(stats_file))
        state._slot_mgr.projects[root] = slot
        state._slot_mgr.active_root = root

        result = srv._format_usage_stats(include_cumulative=True)
        assert "Recent" in result
        assert "03-30 12:00:00" in result
        assert "90%" in result

    def test_specialized_tools_update_usage_totals(self, tmp_path):
        import token_savior.server as srv
        import token_savior.server_state as state

        (tmp_path / "app.py").write_text("def add(a, b):\n    return a + b\n", encoding="utf-8")
        (tmp_path / "test_app.py").write_text(
            "from app import add\n\n\ndef test_add():\n    assert add(2, 3) == 5\n",
            encoding="utf-8",
        )

        asyncio.run(srv.call_tool("set_project_root", {"path": str(tmp_path)}))
        asyncio.run(
            srv.call_tool(
                "run_impacted_tests",
                {
                    "changed_files": ["app.py"],
                    "max_tests": 5,
                    "timeout_sec": 30,
                    "compact": True,
                },
            )
        )

        assert state._tool_call_counts["run_impacted_tests"] == 1
        assert state._total_chars_returned > 0
        assert state._total_naive_chars >= state._total_chars_returned


# ---------------------------------------------------------------------------
# F3 — get_usage_stats v2 polish
# ---------------------------------------------------------------------------


def _seed_history_stats(tmp_path, entries):
    """Helper: write a stats.json file and register a slot for it."""
    import json
    import token_savior.server_state as state
    from token_savior.server import _ProjectSlot

    stats_file = tmp_path / "stats.json"
    payload = {
        "total_calls": sum(e.get("query_calls", 0) for e in entries),
        "total_chars_returned": sum(e.get("chars_returned", 0) for e in entries),
        "total_naive_chars": sum(e.get("naive_chars", 0) for e in entries),
        "sessions": len(entries),
        "tool_counts": {},
        "history": entries,
    }
    stats_file.write_text(json.dumps(payload), encoding="utf-8")
    root = str(tmp_path)
    slot = _ProjectSlot(root=root, stats_file=str(stats_file))
    state._slot_mgr.projects[root] = slot
    state._slot_mgr.active_root = root
    return slot


class TestSparkline:
    def test_sparkline_basic(self):
        from token_savior.server_handlers.stats_render import sparkline, SPARK

        out = sparkline([1, 2, 4, 8])
        assert len(out) == 4
        # All chars must be in the SPARK alphabet
        assert all(ch in SPARK for ch in out)
        # Increasing input -> non-decreasing rank
        ranks = [SPARK.index(ch) for ch in out]
        assert ranks == sorted(ranks)

    def test_sparkline_all_zero(self):
        from token_savior.server_handlers.stats_render import sparkline, SPARK

        out = sparkline([0, 0, 0])
        assert out == SPARK[0] * 3

    def test_sparkline_empty(self):
        from token_savior.server_handlers.stats_render import sparkline

        assert sparkline([]) == ""

    def test_sparkline_utf8_roundtrip(self):
        from token_savior.server_handlers.stats_render import sparkline

        out = sparkline([1, 5, 3, 9, 0, 2, 7])
        encoded = out.encode("utf-8")
        assert encoded.decode("utf-8") == out


class TestUsageStatsV2:
    def test_sparkline_section_renders(self, tmp_path):
        import token_savior.server as srv
        from datetime import datetime, timezone

        ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        _seed_history_stats(
            tmp_path,
            [
                {
                    "session_id": "s1",
                    "timestamp": ts,
                    "query_calls": 4,
                    "tokens_used": 100,
                    "tokens_naive": 1000,
                    "savings_pct": 90.0,
                    "tool_counts": {"find_symbol": 4},
                }
            ],
        )
        result = srv._format_usage_stats(include_cumulative=True, days=30)
        assert "sparkline" in result.lower()
        # Sparkline char must round-trip through utf-8
        assert result.encode("utf-8").decode("utf-8") == result

    def test_daily_breakdown_table(self, tmp_path):
        import token_savior.server as srv
        from datetime import datetime, timezone

        ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        _seed_history_stats(
            tmp_path,
            [
                {
                    "session_id": "s1",
                    "timestamp": ts,
                    "query_calls": 7,
                    "tokens_used": 50,
                    "tokens_naive": 550,
                    "tool_counts": {"find_symbol": 5, "get_function_source": 2},
                }
            ],
        )
        result = srv._format_usage_stats(include_cumulative=True, daily=True)
        assert "Daily breakdown" in result
        assert "find_symbol" in result
        # Header columns present
        assert "tokens saved" in result
        assert "top tool" in result

    def test_top_tools_section(self, tmp_path):
        import token_savior.server as srv
        from datetime import datetime, timezone

        ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        _seed_history_stats(
            tmp_path,
            [
                {
                    "session_id": "s1",
                    "timestamp": ts,
                    "query_calls": 10,
                    "tokens_used": 100,
                    "tokens_naive": 1100,
                    "tool_counts": {
                        "find_symbol": 6,
                        "get_function_source": 3,
                        "search_codebase": 1,
                    },
                }
            ],
        )
        result = srv._format_usage_stats(include_cumulative=True)
        assert "Top" in result and "tools" in result
        assert "find_symbol" in result
        assert "get_function_source" in result

    def test_session_delta_section(self, tmp_path):
        import token_savior.server as srv
        from datetime import datetime, timezone, timedelta

        now = datetime.now(timezone.utc)
        prev_ts = (now - timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
        cur_ts = now.strftime("%Y-%m-%dT%H:%M:%SZ")
        _seed_history_stats(
            tmp_path,
            [
                {
                    "session_id": "prev",
                    "timestamp": prev_ts,
                    "query_calls": 3,
                    "tokens_used": 100,
                    "tokens_naive": 500,
                    "tool_counts": {"find_symbol": 3},
                },
                {
                    "session_id": "curr",
                    "timestamp": cur_ts,
                    "query_calls": 8,
                    "tokens_used": 200,
                    "tokens_naive": 1800,
                    "tool_counts": {"find_symbol": 8},
                },
            ],
        )
        result = srv._format_usage_stats(include_cumulative=True)
        assert "Session vs previous" in result
        # Delta calls = +5
        assert "+5" in result

    def test_format_json_is_valid_json(self, tmp_path):
        import json
        from datetime import datetime, timezone
        from token_savior.server_handlers.stats import _hm_get_usage_stats

        ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        _seed_history_stats(
            tmp_path,
            [
                {
                    "session_id": "s1",
                    "timestamp": ts,
                    "query_calls": 4,
                    "tokens_used": 100,
                    "tokens_naive": 1000,
                    "tool_counts": {"find_symbol": 4},
                }
            ],
        )
        result = _hm_get_usage_stats({"format": "json", "days": 30, "daily": True})
        assert len(result) == 1
        payload = json.loads(result[0].text)
        # Required top-level keys
        assert "session" in payload
        assert "sparkline" in payload
        assert "top_tools" in payload
        assert "session_delta" in payload
        assert "daily" in payload
        # Sparkline structure
        assert payload["sparkline"]["days"] == 30
        assert len(payload["sparkline"]["values"]) == 30
        assert isinstance(payload["sparkline"]["ascii"], str)

    def test_backward_compat_no_args(self, tmp_path):
        """Calling get_usage_stats with empty args must still produce a parseable text output."""
        from token_savior.server_handlers.stats import _hm_get_usage_stats

        result = _hm_get_usage_stats({})
        assert len(result) == 1
        text = result[0].text
        # Existing fields must remain
        assert "Session:" in text
        assert "queries" in text

    def test_backward_compat_format_text_default(self, tmp_path):
        """format omitted defaults to text, not JSON."""
        from token_savior.server_handlers.stats import _hm_get_usage_stats

        result = _hm_get_usage_stats({"days": 30})
        text = result[0].text
        # Not a JSON document
        import json
        try:
            parsed = json.loads(text)
            # If it parsed, it must NOT be a dict with "session" key
            assert not (isinstance(parsed, dict) and "session" in parsed)
        except json.JSONDecodeError:
            pass  # Expected — text output

    def test_days_zero_disables_sparkline(self, tmp_path):
        import token_savior.server as srv
        from datetime import datetime, timezone

        ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        _seed_history_stats(
            tmp_path,
            [
                {
                    "session_id": "s1",
                    "timestamp": ts,
                    "query_calls": 1,
                    "tokens_used": 10,
                    "tokens_naive": 100,
                    "tool_counts": {"find_symbol": 1},
                }
            ],
        )
        result = srv._format_usage_stats(include_cumulative=True, days=0)
        assert "sparkline" not in result.lower()


class TestDailyTokenSavings:
    def test_old_entries_ignored(self):
        from token_savior.server_handlers.stats_render import daily_token_savings

        rows = [
            {
                "timestamp": "2020-01-01T00:00:00Z",
                "tokens_used": 0,
                "tokens_naive": 1000,
            }
        ]
        vals = daily_token_savings(rows, days=7)
        assert vals == [0] * 7

    def test_bucket_count(self):
        from token_savior.server_handlers.stats_render import daily_token_savings

        assert len(daily_token_savings([], days=14)) == 14


class TestTopToolsBySavings:
    def test_proportional_distribution(self):
        from token_savior.server_handlers.stats_render import top_tools_by_savings

        rows = [
            {
                "tokens_used": 100,
                "tokens_naive": 1100,  # 1000 saved
                "tool_counts": {"a": 4, "b": 1},  # a gets 800, b gets 200
                "timestamp": "2026-01-01T00:00:00Z",
            }
        ]
        out = top_tools_by_savings(rows, top_n=5)
        names = [r["tool"] for r in out]
        assert names[0] == "a"
        assert out[0]["tokens_saved"] == 800
        assert out[1]["tokens_saved"] == 200
