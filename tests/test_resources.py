"""MCP resources: observations exposed as ts://obs/{id} (read-only, additive)."""

from __future__ import annotations

from unittest.mock import patch

import pytest

from token_savior import memory_db, server_state
from token_savior.server_handlers import resources


def _seed(db_path, project):
    memory_db.run_migrations()
    sid = memory_db.session_start(project)
    return memory_db.observation_save(
        sid, project, "convention", "My Title", "the body text",
        why="because reasons", how_to_apply="do the thing",
        symbol="my_func", file_path="src/x.py",
    )


def test_read_observation_roundtrip(tmp_path):
    db = tmp_path / "memory.db"
    with patch.object(memory_db, "MEMORY_DB_PATH", db):
        oid = _seed(db, "/proj/x")
        text = resources.read_observation_resource(f"ts://obs/{oid}")
    assert "My Title" in text
    assert "the body text" in text
    assert "because reasons" in text
    assert "my_func" in text


def test_read_rejects_non_obs_uri(tmp_path):
    with patch.object(memory_db, "MEMORY_DB_PATH", tmp_path / "memory.db"):
        with pytest.raises(ValueError):
            resources.read_observation_resource("https://example.com/x")


def test_read_rejects_bad_id(tmp_path):
    with patch.object(memory_db, "MEMORY_DB_PATH", tmp_path / "memory.db"):
        with pytest.raises(ValueError):
            resources.read_observation_resource("ts://obs/not-an-int")


def test_read_missing_observation_raises(tmp_path):
    db = tmp_path / "memory.db"
    with patch.object(memory_db, "MEMORY_DB_PATH", db):
        memory_db.run_migrations()
        with pytest.raises(ValueError):
            resources.read_observation_resource("ts://obs/999999")


def test_list_resources_for_active_project(tmp_path, monkeypatch):
    db = tmp_path / "memory.db"
    with patch.object(memory_db, "MEMORY_DB_PATH", db):
        oid = _seed(db, "/proj/list")
        monkeypatch.setattr(server_state._slot_mgr, "active_root", "/proj/list")
        res = resources.list_observation_resources()
    uris = [str(r.uri) for r in res]
    assert any(f"ts://obs/{oid}" in u for u in uris)


def test_list_resources_no_active_project_returns_empty(tmp_path, monkeypatch):
    with patch.object(memory_db, "MEMORY_DB_PATH", tmp_path / "memory.db"):
        monkeypatch.setattr(server_state._slot_mgr, "active_root", "")
        assert resources.list_observation_resources() == []
