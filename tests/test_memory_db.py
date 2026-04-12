"""Tests for memory_db — session lifecycle, observations, FTS5, dedup, symbol links, summaries."""

from __future__ import annotations

import sqlite3
from pathlib import Path
from unittest.mock import patch

import pytest

from token_savior import memory_db

PROJECT = "/tmp/test-project"


@pytest.fixture(autouse=True)
def _memory_tmpdb(tmp_path: Path):
    """Redirect memory_db to a temporary SQLite file for each test."""
    db_path = tmp_path / "memory.db"
    with patch.object(memory_db, "MEMORY_DB_PATH", db_path):
        yield db_path


class TestSessionLifecycle:
    def test_start_and_end(self):
        sid = memory_db.session_start(PROJECT)
        assert isinstance(sid, int)

        conn = memory_db.get_db()
        row = conn.execute("SELECT status FROM sessions WHERE id=?", (sid,)).fetchone()
        assert row["status"] == "active"
        conn.close()

        memory_db.session_end(sid, summary="done", symbols_changed=["foo"], files_changed=["a.py"])

        conn = memory_db.get_db()
        row = conn.execute("SELECT status, summary FROM sessions WHERE id=?", (sid,)).fetchone()
        assert row["status"] == "completed"
        assert row["summary"] == "done"
        conn.close()


class TestObservationSaveAndSearch:
    def test_save_and_fts_search(self):
        sid = memory_db.session_start(PROJECT)
        obs_id = memory_db.observation_save(
            sid, PROJECT, "convention", "Token Savior mandatory",
            "Always use Token Savior for code navigation",
            why="Saves tokens", how_to_apply="switch_project first",
        )
        assert obs_id is not None

        results = memory_db.observation_search(PROJECT, "Token Savior")
        assert len(results) >= 1
        assert any(r["id"] == obs_id for r in results)


class TestDedup:
    def test_same_observation_twice(self):
        sid = memory_db.session_start(PROJECT)
        id1 = memory_db.observation_save(sid, PROJECT, "convention", "rule1", "content1")
        id2 = memory_db.observation_save(sid, PROJECT, "convention", "rule1", "content1")
        assert id1 is not None
        assert id2 is None

        conn = memory_db.get_db()
        count = conn.execute(
            "SELECT COUNT(*) FROM observations WHERE project_root=? AND title=?",
            (PROJECT, "rule1"),
        ).fetchone()[0]
        conn.close()
        assert count == 1


class TestSymbolLink:
    def test_get_by_symbol(self):
        sid = memory_db.session_start(PROJECT)
        obs_id = memory_db.observation_save(
            sid, PROJECT, "convention", "my_func perf note",
            "my_func is O(n^2), consider caching",
            symbol="my_func",
        )
        assert obs_id is not None

        results = memory_db.observation_get_by_symbol(PROJECT, "my_func")
        assert len(results) == 1
        assert results[0]["id"] == obs_id
        assert results[0]["title"] == "my_func perf note"


class TestSummaryTimelinePositioning:
    def test_covers_until_epoch(self):
        sid = memory_db.session_start(PROJECT)
        id1 = memory_db.observation_save(sid, PROJECT, "project", "obs1", "first observation")
        id2 = memory_db.observation_save(sid, PROJECT, "project", "obs2", "second observation")
        assert id1 is not None and id2 is not None

        summary_id = memory_db.summary_save(sid, PROJECT, "summary of both", [id1, id2])

        conn = memory_db.get_db()
        summary = conn.execute(
            "SELECT covers_until_epoch FROM summaries WHERE id=?", (summary_id,)
        ).fetchone()

        max_obs_epoch = conn.execute(
            "SELECT MAX(created_at_epoch) FROM observations WHERE id IN (?, ?)", (id1, id2)
        ).fetchone()[0]
        conn.close()

        assert summary["covers_until_epoch"] is not None
        assert summary["covers_until_epoch"] == max_obs_epoch


class TestFTS5Triggers:
    def test_update_syncs_fts(self):
        sid = memory_db.session_start(PROJECT)
        obs_id = memory_db.observation_save(
            sid, PROJECT, "convention", "old title", "old content about widgets",
        )
        assert obs_id is not None

        results_before = memory_db.observation_search(PROJECT, "widgets")
        assert any(r["id"] == obs_id for r in results_before)

        memory_db.observation_update(obs_id, title="new title", content="new content about rockets")

        results_old = memory_db.observation_search(PROJECT, "widgets")
        assert not any(r["id"] == obs_id for r in results_old)

        results_new = memory_db.observation_search(PROJECT, "rockets")
        assert any(r["id"] == obs_id for r in results_new)
