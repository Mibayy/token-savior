"""A5: narrative/facts/concepts fields on observations."""

from __future__ import annotations

import sqlite3
from pathlib import Path
from unittest.mock import patch

import pytest

from token_savior import memory_db
from token_savior.server_handlers.memory import (
    _mh_memory_get,
    _mh_memory_save,
)

PROJECT = "/tmp/test-project-a5"


@pytest.fixture(autouse=True)
def _memory_tmpdb(tmp_path: Path):
    db_path = tmp_path / "memory.db"
    with patch.object(memory_db, "MEMORY_DB_PATH", db_path):
        yield db_path


def _save(**kwargs) -> int:
    sid = memory_db.session_start(PROJECT)
    oid = memory_db.observation_save(
        sid, PROJECT, "convention",
        kwargs.pop("title", "seed"),
        kwargs.pop("content", "seed content"),
        **kwargs,
    )
    assert oid is not None, "save failed"
    return oid


class TestSchemaMigration:
    def test_fresh_db_has_three_new_columns(self):
        memory_db.session_start(PROJECT)  # triggers migrations
        conn = memory_db.get_db()
        cols = {r[1] for r in conn.execute("PRAGMA table_info(observations)").fetchall()}
        conn.close()
        assert "narrative" in cols
        assert "facts" in cols
        assert "concepts" in cols

    def test_fts_table_includes_new_columns(self):
        memory_db.session_start(PROJECT)
        conn = memory_db.get_db()
        row = conn.execute(
            "SELECT sql FROM sqlite_master "
            "WHERE type='table' AND name='observations_fts'"
        ).fetchone()
        conn.close()
        sql = row[0] or ""
        assert "narrative" in sql
        assert "facts" in sql
        assert "concepts" in sql

    def test_pre_a5_fts_shape_gets_rebuilt(self, tmp_path: Path):
        """Run full migration once, hand-roll obs_fts back to the pre-A5 shape,
        then re-run migrations and verify FTS is rebuilt with the new columns.
        """
        legacy = tmp_path / "legacy.db"
        memory_db.run_migrations(legacy)

        conn = sqlite3.connect(str(legacy))
        conn.row_factory = sqlite3.Row
        # Remove the new columns from the FTS side (keep base table as-is)
        # by dropping + recreating to the pre-A5 shape.
        for trig in ("obs_fts_insert", "obs_fts_delete", "obs_fts_update"):
            conn.execute(f"DROP TRIGGER IF EXISTS {trig}")
        conn.execute("DROP TABLE IF EXISTS observations_fts")
        conn.execute(
            "CREATE VIRTUAL TABLE observations_fts USING fts5("
            " title, content, why, how_to_apply, tags,"
            " content='observations', content_rowid='id')"
        )
        conn.commit()
        conn.close()

        # Re-run migrations — should detect the missing FTS columns + rebuild.
        memory_db.db_core._migrated_paths.discard(str(legacy))
        memory_db.run_migrations(legacy)

        c = sqlite3.connect(str(legacy))
        fts_sql = c.execute(
            "SELECT sql FROM sqlite_master WHERE name='observations_fts'"
        ).fetchone()[0]
        c.close()
        assert "narrative" in fts_sql
        assert "facts" in fts_sql
        assert "concepts" in fts_sql


class TestObservationSavePersists:
    def test_save_without_new_fields_leaves_them_null(self):
        oid = _save(title="plain", content="body")
        conn = memory_db.get_db()
        row = conn.execute(
            "SELECT narrative, facts, concepts FROM observations WHERE id=?", (oid,)
        ).fetchone()
        conn.close()
        assert row["narrative"] is None
        assert row["facts"] is None
        assert row["concepts"] is None

    def test_save_with_new_fields_persists(self):
        oid = _save(
            title="rich obs", content="body",
            narrative="Long prose explanation of the observation.",
            facts='["atomic fact one", "atomic fact two"]',
            concepts="caching, invalidation",
        )
        conn = memory_db.get_db()
        row = conn.execute(
            "SELECT narrative, facts, concepts FROM observations WHERE id=?", (oid,)
        ).fetchone()
        conn.close()
        assert row["narrative"].startswith("Long prose")
        assert "atomic fact one" in row["facts"]
        assert row["concepts"] == "caching, invalidation"


class TestHandlerPassesFields:
    def test_memory_save_handler_persists_new_fields(self):
        out = _mh_memory_save({
            "type": "convention", "title": "handler obs", "content": "c",
            "narrative": "handler narrative",
            "facts": "fact A",
            "concepts": "concept A",
            "project": PROJECT,
        })
        assert "saved" in out
        conn = memory_db.get_db()
        row = conn.execute(
            "SELECT narrative, facts, concepts FROM observations "
            "WHERE title='handler obs'"
        ).fetchone()
        conn.close()
        assert row["narrative"] == "handler narrative"
        assert row["facts"] == "fact A"
        assert row["concepts"] == "concept A"


class TestMemoryGetRendersSections:
    def test_sections_absent_when_fields_null(self):
        oid = _save(title="bare", content="body")
        out = _mh_memory_get({"ids": [oid], "project": PROJECT, "full": True})
        assert "Narrative" not in out
        assert "Facts" not in out
        assert "Concepts" not in out

    def test_sections_present_when_fields_set(self):
        oid = _save(
            title="rendered", content="body",
            narrative="story here",
            facts="one; two",
            concepts="alpha,beta",
        )
        out = _mh_memory_get({"ids": [oid], "project": PROJECT, "full": True})
        assert "**Narrative:**" in out and "story here" in out
        assert "**Facts:**" in out and "one; two" in out
        assert "**Concepts:**" in out and "alpha,beta" in out

    def test_sections_individually_optional(self):
        oid = _save(title="facts only", content="body", facts="lonely fact")
        out = _mh_memory_get({"ids": [oid], "project": PROJECT, "full": True})
        assert "**Facts:**" in out
        assert "**Narrative:**" not in out
        assert "**Concepts:**" not in out


class TestFTSIndexesNewFields:
    def test_query_matches_narrative_content(self):
        _save(
            title="ordinary title", content="ordinary body",
            narrative="only uniquewordfoo lives in the narrative field",
        )
        hits = memory_db.observation_search(
            project_root=PROJECT, query="uniquewordfoo", limit=10,
        )
        assert len(hits) == 1
        assert hits[0]["title"] == "ordinary title"

    def test_query_matches_concepts(self):
        _save(title="cached", content="body", concepts="uniqueconcepttoken")
        hits = memory_db.observation_search(
            project_root=PROJECT, query="uniqueconcepttoken", limit=10,
        )
        assert len(hits) == 1
