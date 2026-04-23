"""Tests for the persistent call counter (A5).

Scoped by (tool_name, client). Writes to
``$TOKEN_SAVIOR_STATS_DIR/tool-calls.json`` via atomic rename.
Failures are swallowed silently and surfaced via ``telemetry_health``.
"""
from __future__ import annotations

import json

import pytest

from token_savior import telemetry


@pytest.fixture(autouse=True)
def _isolated_stats_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("TOKEN_SAVIOR_STATS_DIR", str(tmp_path))
    telemetry.reset_for_tests()
    yield tmp_path
    telemetry.reset_for_tests()


class TestRecordToolCall:
    def test_first_call_creates_file(self, _isolated_stats_dir, monkeypatch):
        monkeypatch.setenv("TOKEN_SAVIOR_CLIENT", "claude-code")
        telemetry.record_tool_call("find_symbol")

        counter = _isolated_stats_dir / "tool-calls.json"
        assert counter.exists()
        data = json.loads(counter.read_text())
        assert data["schema_version"] == 1
        assert data["counts"]["claude-code"]["find_symbol"] == 1

    def test_subsequent_calls_increment(self, _isolated_stats_dir, monkeypatch):
        monkeypatch.setenv("TOKEN_SAVIOR_CLIENT", "claude-code")
        for _ in range(7):
            telemetry.record_tool_call("get_function_source")
        data = json.loads((_isolated_stats_dir / "tool-calls.json").read_text())
        assert data["counts"]["claude-code"]["get_function_source"] == 7

    def test_different_clients_are_separate_buckets(
        self, _isolated_stats_dir, monkeypatch,
    ):
        monkeypatch.setenv("TOKEN_SAVIOR_CLIENT", "claude-code")
        telemetry.record_tool_call("find_symbol")
        telemetry.record_tool_call("find_symbol")
        monkeypatch.setenv("TOKEN_SAVIOR_CLIENT", "cursor")
        telemetry.record_tool_call("find_symbol")

        data = json.loads((_isolated_stats_dir / "tool-calls.json").read_text())
        assert data["counts"]["claude-code"]["find_symbol"] == 2
        assert data["counts"]["cursor"]["find_symbol"] == 1

    def test_empty_client_env_falls_back_to_unknown(
        self, _isolated_stats_dir, monkeypatch,
    ):
        monkeypatch.delenv("TOKEN_SAVIOR_CLIENT", raising=False)
        telemetry.record_tool_call("find_symbol")
        data = json.loads((_isolated_stats_dir / "tool-calls.json").read_text())
        assert "unknown" in data["counts"]
        assert data["counts"]["unknown"]["find_symbol"] == 1

    def test_empty_tool_name_is_noop(self, _isolated_stats_dir):
        telemetry.record_tool_call("")
        # No file written, no crash.
        assert not (_isolated_stats_dir / "tool-calls.json").exists()


class TestSchemaMigration:
    def test_stale_schema_renamed_to_bak_and_reset(
        self, _isolated_stats_dir, monkeypatch,
    ):
        monkeypatch.setenv("TOKEN_SAVIOR_CLIENT", "claude-code")
        counter = _isolated_stats_dir / "tool-calls.json"
        counter.write_text(json.dumps({
            "schema_version": 0,
            "counts": {"x": {"y": 999}},
        }))
        telemetry.record_tool_call("find_symbol")

        # Old file preserved as .bak so humans can inspect.
        assert (_isolated_stats_dir / "tool-calls.json.bak").exists()
        data = json.loads(counter.read_text())
        assert data["schema_version"] == 1
        assert data["counts"]["claude-code"]["find_symbol"] == 1
        assert "x" not in data["counts"]


class TestTelemetryHealth:
    def test_health_before_any_call(self, _isolated_stats_dir):
        h = telemetry.telemetry_health()
        assert h["ok"] is True
        assert h["clients"] == 0
        assert h["distinct_tools"] == 0
        assert h["error"] is None

    def test_health_after_calls(self, _isolated_stats_dir, monkeypatch):
        monkeypatch.setenv("TOKEN_SAVIOR_CLIENT", "claude-code")
        telemetry.record_tool_call("a")
        telemetry.record_tool_call("b")
        monkeypatch.setenv("TOKEN_SAVIOR_CLIENT", "cursor")
        telemetry.record_tool_call("c")
        h = telemetry.telemetry_health()
        assert h["clients"] == 2
        assert h["distinct_tools"] == 3

    def test_health_reports_error_on_unwritable_dir(
        self, tmp_path, monkeypatch,
    ):
        # Point the stats dir at a regular file so mkdir raises.
        blocking = tmp_path / "blocker"
        blocking.write_text("not a directory")
        monkeypatch.setenv("TOKEN_SAVIOR_STATS_DIR", str(blocking))
        telemetry.reset_for_tests()
        telemetry.record_tool_call("find_symbol")

        h = telemetry.telemetry_health()
        assert h["ok"] is False
        assert "save:" in (h["error"] or "")


class TestCorruptFile:
    def test_corrupt_json_falls_back_to_empty_state(
        self, _isolated_stats_dir, monkeypatch,
    ):
        monkeypatch.setenv("TOKEN_SAVIOR_CLIENT", "claude-code")
        counter = _isolated_stats_dir / "tool-calls.json"
        counter.write_text("{this is not json")
        # Must not crash; counter starts fresh.
        telemetry.record_tool_call("find_symbol")
        data = json.loads(counter.read_text())
        assert data["counts"]["claude-code"]["find_symbol"] == 1
