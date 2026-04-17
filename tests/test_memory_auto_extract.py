"""A3: opt-in LLM auto-extraction.

No real LLM is ever called. Each test either:
  - checks the disabled path is a true zero-overhead no-op, or
  - monkey-patches the API boundary (``_call_api``) and asserts that
    downstream parsing + ``observation_save`` produce the expected rows.
"""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from unittest.mock import patch

import pytest

from token_savior import memory_db
from token_savior.memory import auto_extract

PROJECT = "/tmp/test-project-a3"


@pytest.fixture
def _memory_tmpdb(tmp_path: Path):
    db_path = tmp_path / "memory.db"
    with patch.object(memory_db, "MEMORY_DB_PATH", db_path):
        yield db_path


# ── master switch ────────────────────────────────────────────────────────


class TestDisabledByDefault:
    def test_is_enabled_false_when_env_absent(self, monkeypatch):
        monkeypatch.delenv("TS_AUTO_EXTRACT", raising=False)
        assert auto_extract.is_enabled() is False

    def test_is_enabled_false_for_values_other_than_one(self, monkeypatch):
        for v in ("0", "true", "yes", "on", ""):
            monkeypatch.setenv("TS_AUTO_EXTRACT", v)
            assert auto_extract.is_enabled() is False

    def test_is_enabled_true_when_exactly_one(self, monkeypatch):
        monkeypatch.setenv("TS_AUTO_EXTRACT", "1")
        assert auto_extract.is_enabled() is True

    def test_process_noop_when_disabled(self, monkeypatch):
        monkeypatch.delenv("TS_AUTO_EXTRACT", raising=False)
        called = {"n": 0}

        def should_not_run(*a, **kw):
            called["n"] += 1
            return []

        monkeypatch.setattr(
            auto_extract, "extract_observations", should_not_run,
        )
        out = auto_extract.process_tool_use("Bash", {"cmd": "ls"}, "ok")
        assert out is False
        assert called["n"] == 0


# ── parser ───────────────────────────────────────────────────────────────


class TestParseItems:
    def test_valid_array(self):
        raw = '[{"type":"infra","title":"t","content":"c","why":"w"}]'
        items = auto_extract._parse_items(raw)
        assert len(items) == 1
        assert items[0]["type"] == "infra"
        assert items[0]["why"] == "w"

    def test_code_fence_stripped(self):
        raw = '```json\n[{"type":"bugfix","title":"t","content":"c"}]\n```'
        items = auto_extract._parse_items(raw)
        assert len(items) == 1
        assert items[0]["type"] == "bugfix"

    def test_malformed_json_returns_empty(self):
        assert auto_extract._parse_items("not-json-at-all") == []

    def test_non_list_returns_empty(self):
        assert auto_extract._parse_items('{"type":"infra"}') == []

    def test_invalid_type_filtered(self):
        raw = '[{"type":"spam","title":"t","content":"c"}]'
        assert auto_extract._parse_items(raw) == []

    def test_missing_title_filtered(self):
        raw = '[{"type":"infra","title":"","content":"c"}]'
        assert auto_extract._parse_items(raw) == []

    def test_missing_content_filtered(self):
        raw = '[{"type":"infra","title":"t","content":""}]'
        assert auto_extract._parse_items(raw) == []

    def test_max_three_items(self):
        raw = json.dumps([
            {"type": "infra", "title": f"t{i}", "content": f"c{i}"}
            for i in range(5)
        ])
        items = auto_extract._parse_items(raw)
        assert len(items) == 3

    def test_empty_array(self):
        assert auto_extract._parse_items("[]") == []

    def test_optional_fields_propagate(self):
        raw = json.dumps([{
            "type": "convention",
            "title": "t",
            "content": "c",
            "symbol": "my_func",
            "file_path": "src/x.py",
        }])
        items = auto_extract._parse_items(raw)
        assert items[0]["symbol"] == "my_func"
        assert items[0]["file_path"] == "src/x.py"


# ── extract_observations (network boundary mocked) ───────────────────────


class TestExtractObservations:
    def test_no_api_key_returns_empty_without_calling_api(self, monkeypatch):
        monkeypatch.delenv("TS_API_KEY", raising=False)

        def explode(*a, **kw):
            raise AssertionError("should not call API without key")

        monkeypatch.setattr(auto_extract, "_call_api", explode)
        assert auto_extract.extract_observations("Bash", {}, "") == []

    def test_api_returns_none(self, monkeypatch):
        monkeypatch.setenv("TS_API_KEY", "sk-test")
        monkeypatch.setattr(auto_extract, "_call_api", lambda *a, **kw: None)
        assert auto_extract.extract_observations("Bash", {}, "") == []

    def test_api_success_yields_items(self, monkeypatch):
        monkeypatch.setenv("TS_API_KEY", "sk-test")
        monkeypatch.setattr(
            auto_extract, "_call_api",
            lambda *a, **kw:
                '[{"type":"infra","title":"x","content":"y"}]',
        )
        out = auto_extract.extract_observations(
            "Bash", {"cmd": "systemctl"}, "ok",
        )
        assert len(out) == 1
        assert out[0]["type"] == "infra"
        assert out[0]["title"] == "x"

    def test_uses_default_model_when_unset(self, monkeypatch):
        captured: dict = {}
        monkeypatch.setenv("TS_API_KEY", "sk-test")
        monkeypatch.delenv("TS_MODEL", raising=False)

        def fake_call(system, user, api_key, model):
            captured["model"] = model
            captured["api_key"] = api_key
            return "[]"

        monkeypatch.setattr(auto_extract, "_call_api", fake_call)
        auto_extract.extract_observations("Bash", {}, "")
        assert captured["model"] == auto_extract.DEFAULT_MODEL
        assert captured["api_key"] == "sk-test"

    def test_honours_ts_model_override(self, monkeypatch):
        captured: dict = {}
        monkeypatch.setenv("TS_API_KEY", "sk-test")
        monkeypatch.setenv("TS_MODEL", "claude-opus-4-7")

        def fake_call(system, user, api_key, model):
            captured["model"] = model
            return "[]"

        monkeypatch.setattr(auto_extract, "_call_api", fake_call)
        auto_extract.extract_observations("Bash", {}, "")
        assert captured["model"] == "claude-opus-4-7"


# ── persistence ──────────────────────────────────────────────────────────


class TestSaveExtracted:
    def test_save_persists_obs(self, _memory_tmpdb):
        items = [{
            "type": "infra",
            "title": "hook test",
            "content": "saved from A3",
            "why": "test",
        }]
        saved = auto_extract._save_extracted(items, PROJECT)
        assert saved == 1
        rows = memory_db.get_recent_index(project_root=PROJECT, limit=5)
        titles = [r["title"] for r in rows]
        assert "hook test" in titles

    def test_save_no_items(self, _memory_tmpdb):
        assert auto_extract._save_extracted([], PROJECT) == 0

    def test_save_dedup_via_content_hash(self, _memory_tmpdb):
        """Same item extracted twice → collapsed by P2 content_hash."""
        item = {"type": "convention", "title": "dupe",
                "content": "identical body"}
        first = auto_extract._save_extracted([item], PROJECT)
        second = auto_extract._save_extracted([item], PROJECT)
        assert first == 1
        # P2 dedup: second save returns no obs_id, so count stays 0.
        assert second == 0


# ── end-to-end async dispatch ────────────────────────────────────────────


class TestProcessToolUse:
    def test_dispatches_thread_when_enabled(
        self, _memory_tmpdb, monkeypatch,
    ):
        monkeypatch.setenv("TS_AUTO_EXTRACT", "1")
        monkeypatch.setenv("TS_API_KEY", "sk-test")
        sid = memory_db.session_start(PROJECT)
        memory_db.observation_save(
            sid, PROJECT, "convention", "seed", "body",
        )

        ran = threading.Event()

        def fake_call(*a, **kw):
            ran.set()
            return '[{"type":"infra","title":"auto","content":"from hook"}]'

        monkeypatch.setattr(auto_extract, "_call_api", fake_call)
        out = auto_extract.process_tool_use(
            "Bash", {"cmd": "systemctl restart foo"}, "",
            project_root=PROJECT,
        )
        assert out is True
        assert ran.wait(timeout=3.0) is True

        # Poll until the daemon thread persists the row.
        found = False
        for _ in range(60):
            rows = memory_db.observation_search(
                project_root=PROJECT, query="auto", limit=5,
            )
            if any(r["title"] == "auto" for r in rows):
                found = True
                break
            time.sleep(0.05)
        assert found

    def test_no_project_returns_false(self, _memory_tmpdb, monkeypatch):
        monkeypatch.setenv("TS_AUTO_EXTRACT", "1")
        monkeypatch.setenv("TS_API_KEY", "sk-test")
        # Empty DB → no project resolvable.
        out = auto_extract.process_tool_use("Bash", {}, "")
        assert out is False


# ── status line integration ──────────────────────────────────────────────


class TestStatusLine:
    def test_status_shows_auto_extract_enabled(
        self, _memory_tmpdb, monkeypatch,
    ):
        monkeypatch.setenv("TS_AUTO_EXTRACT", "1")
        from token_savior.server_handlers.memory import _mh_memory_status
        out = _mh_memory_status({"project": PROJECT})
        assert "Auto-extract" in out
        assert "enabled" in out

    def test_status_shows_auto_extract_disabled(
        self, _memory_tmpdb, monkeypatch,
    ):
        monkeypatch.delenv("TS_AUTO_EXTRACT", raising=False)
        from token_savior.server_handlers.memory import _mh_memory_status
        out = _mh_memory_status({"project": PROJECT})
        assert "Auto-extract" in out
        assert "disabled" in out
