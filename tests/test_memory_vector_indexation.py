"""A1-2: vector indexation on save + reindex tool + doctor/status hooks.

The VPS that runs these tests typically does not have sqlite-vec or
sentence-transformers installed. Each test below either exercises the
unavailable-path explicitly or monkey-patches the embedding pipeline to
simulate an available one while still using a plain SQLite ``obs_vectors``
table (no vec0 extension required).
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from token_savior import db_core, memory_db
from token_savior.memory import embeddings
from token_savior.server_handlers.memory import (
    _mh_memory_doctor,
    _mh_memory_status,
    _mh_memory_vector_reindex,
)

PROJECT = "/tmp/test-project-a1-2"


@pytest.fixture(autouse=True)
def _memory_tmpdb(tmp_path: Path):
    db_path = tmp_path / "memory.db"
    with patch.object(memory_db, "MEMORY_DB_PATH", db_path):
        yield db_path


def _create_plain_obs_vectors_table() -> None:
    """Create obs_vectors as a regular table (no vec0) for testing."""
    conn = memory_db.get_db()
    try:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS obs_vectors "
            "(obs_id INTEGER PRIMARY KEY, embedding BLOB)"
        )
        conn.commit()
    finally:
        conn.close()


def _save(narrative: str | None = None, **kw) -> int:
    sid = memory_db.session_start(PROJECT)
    oid = memory_db.observation_save(
        sid, PROJECT, "convention",
        kw.pop("title", "seed"),
        kw.pop("content", "seed content"),
        narrative=narrative,
        **kw,
    )
    assert oid is not None
    return oid


class TestObservationSaveVectorHookUnavailable:
    def test_no_crash_when_vector_unavailable(self, monkeypatch):
        monkeypatch.setattr(db_core, "VECTOR_SEARCH_AVAILABLE", False)
        oid = _save(title="plain", content="body")
        # No obs_vectors table exists in unavailable mode — still works.
        conn = memory_db.get_db()
        try:
            row = conn.execute(
                "SELECT id FROM observations WHERE id=?", (oid,)
            ).fetchone()
        finally:
            conn.close()
        assert row is not None

    def test_maybe_index_obs_returns_false_when_unavailable(self, monkeypatch):
        monkeypatch.setattr(db_core, "VECTOR_SEARCH_AVAILABLE", False)
        conn = memory_db.get_db()
        try:
            assert embeddings.maybe_index_obs(1, "some text", conn) is False
        finally:
            conn.close()


class TestObservationSaveVectorHookSimulatedAvailable:
    def test_vector_row_created_on_save(self, monkeypatch):
        """Simulate sqlite-vec + embedding availability, verify row insertion."""
        monkeypatch.setattr(db_core, "VECTOR_SEARCH_AVAILABLE", True)
        # Skip the real model + serializer.
        monkeypatch.setattr(embeddings, "embed", lambda text: [0.0] * 384)
        monkeypatch.setattr(embeddings, "_serialize_vec", lambda vec: b"\x00" * 1536)
        _create_plain_obs_vectors_table()

        oid = _save(title="with-vec", content="hello world", narrative="narrative txt")

        conn = memory_db.get_db()
        try:
            row = conn.execute(
                "SELECT obs_id, LENGTH(embedding) AS n FROM obs_vectors WHERE obs_id=?",
                (oid,),
            ).fetchone()
        finally:
            conn.close()
        assert row is not None
        assert row["obs_id"] == oid
        assert row["n"] == 1536

    def test_embed_receives_narrative_preferred_over_content(self, monkeypatch):
        monkeypatch.setattr(db_core, "VECTOR_SEARCH_AVAILABLE", True)
        captured: dict[str, str] = {}

        def fake_embed(text: str):
            captured["text"] = text or ""
            return [0.0] * 384

        monkeypatch.setattr(embeddings, "embed", fake_embed)
        monkeypatch.setattr(embeddings, "_serialize_vec", lambda vec: b"\x00" * 4)
        _create_plain_obs_vectors_table()

        _save(title="t", content="CONTENT", narrative="NARRATIVE")
        assert captured["text"] == "NARRATIVE"

        _save(title="t2", content="CONTENT2", narrative=None)
        assert captured["text"] == "CONTENT2"


class TestBackfillFunction:
    def test_unavailable_when_sqlite_vec_missing(self, monkeypatch):
        monkeypatch.setattr(db_core, "VECTOR_SEARCH_AVAILABLE", False)
        res = embeddings.backfill_obs_vectors(project_root=PROJECT, limit=10)
        assert res["status"] == "unavailable"
        assert "sqlite-vec" in res["reason"]

    def test_unavailable_when_model_missing(self, monkeypatch):
        monkeypatch.setattr(db_core, "VECTOR_SEARCH_AVAILABLE", True)
        monkeypatch.setattr(embeddings, "_load_model", lambda: None)
        res = embeddings.backfill_obs_vectors(project_root=PROJECT, limit=10)
        assert res["status"] == "unavailable"
        assert "sentence-transformers" in res["reason"]

    def test_unavailable_when_obs_vectors_table_missing(self, monkeypatch):
        monkeypatch.setattr(db_core, "VECTOR_SEARCH_AVAILABLE", True)
        monkeypatch.setattr(embeddings, "_load_model", lambda: object())
        # Create some obs but no obs_vectors table.
        _save(title="solo", content="body")
        res = embeddings.backfill_obs_vectors(project_root=PROJECT, limit=10)
        assert res["status"] == "unavailable"
        assert "obs_vectors" in res["reason"]

    def test_backfill_ok_indexes_missing_rows(self, monkeypatch):
        monkeypatch.setattr(db_core, "VECTOR_SEARCH_AVAILABLE", True)
        monkeypatch.setattr(embeddings, "_load_model", lambda: object())
        monkeypatch.setattr(embeddings, "embed", lambda text: [0.0] * 384)
        monkeypatch.setattr(embeddings, "_serialize_vec", lambda vec: b"\x00" * 8)
        _create_plain_obs_vectors_table()

        # Save 3 obs BEFORE enabling embed (simulate legacy rows without vec).
        # Inline: since VECTOR_SEARCH_AVAILABLE=True, maybe_index_obs will run
        # for each save. To simulate "missing vectors", delete them after save.
        o1 = _save(title="a", content="aa")
        o2 = _save(title="b", content="bb")
        o3 = _save(title="c", content="cc")
        conn = memory_db.get_db()
        try:
            conn.execute("DELETE FROM obs_vectors")
            conn.commit()
        finally:
            conn.close()

        res = embeddings.backfill_obs_vectors(project_root=PROJECT, limit=10)
        assert res["status"] == "ok"
        assert res["total"] == 3
        assert res["indexed"] == 3
        assert res["pending"] == 0

        conn = memory_db.get_db()
        try:
            n = conn.execute("SELECT COUNT(*) FROM obs_vectors").fetchone()[0]
        finally:
            conn.close()
        assert n == 3
        # Sanity: the ids match.
        assert {o1, o2, o3} == set(range(min(o1, o2, o3), max(o1, o2, o3) + 1))


class TestVectorReindexHandler:
    def test_unavailable_path_renders_warning(self, monkeypatch):
        monkeypatch.setattr(db_core, "VECTOR_SEARCH_AVAILABLE", False)
        out = _mh_memory_vector_reindex({"project": PROJECT, "limit": 50})
        assert out.startswith("⚠️") and "vector reindex" in out

    def test_ok_path_renders_coverage(self, monkeypatch):
        monkeypatch.setattr(db_core, "VECTOR_SEARCH_AVAILABLE", True)
        monkeypatch.setattr(embeddings, "_load_model", lambda: object())
        monkeypatch.setattr(embeddings, "embed", lambda text: [0.0] * 384)
        monkeypatch.setattr(embeddings, "_serialize_vec", lambda vec: b"\x00" * 4)
        _create_plain_obs_vectors_table()

        _save(title="x", content="xx")
        _save(title="y", content="yy")
        conn = memory_db.get_db()
        try:
            conn.execute("DELETE FROM obs_vectors")
            conn.commit()
        finally:
            conn.close()

        out = _mh_memory_vector_reindex({"project": PROJECT, "limit": 100})
        assert "Vector reindex" in out
        assert "indexed this run : 2" in out
        assert "total active     : 2" in out
        assert "coverage         : 2/2" in out


class TestDoctorVectorLine:
    def test_disabled_line_when_unavailable(self, monkeypatch):
        monkeypatch.setattr(db_core, "VECTOR_SEARCH_AVAILABLE", False)
        _save(title="d", content="dd")
        out = _mh_memory_doctor({"project": PROJECT})
        assert "Vector coverage: disabled" in out

    def test_enabled_line_when_available(self, monkeypatch):
        monkeypatch.setattr(db_core, "VECTOR_SEARCH_AVAILABLE", True)
        monkeypatch.setattr(embeddings, "embed", lambda text: [0.0] * 384)
        monkeypatch.setattr(embeddings, "_serialize_vec", lambda vec: b"\x00" * 4)
        _create_plain_obs_vectors_table()

        _save(title="one", content="aa")
        _save(title="two", content="bb")
        out = _mh_memory_doctor({"project": PROJECT})
        assert "Vector coverage:" in out
        assert "2/2" in out


class TestStatusVectorLine:
    def test_disabled_row(self, monkeypatch):
        monkeypatch.setattr(db_core, "VECTOR_SEARCH_AVAILABLE", False)
        _save(title="d", content="dd")
        out = _mh_memory_status({"project": PROJECT})
        assert "Vectors" in out
        assert "disabled" in out

    def test_enabled_row_renders_dim_and_coverage(self, monkeypatch):
        monkeypatch.setattr(db_core, "VECTOR_SEARCH_AVAILABLE", True)
        monkeypatch.setattr(embeddings, "embed", lambda text: [0.0] * 384)
        monkeypatch.setattr(embeddings, "_serialize_vec", lambda vec: b"\x00" * 4)
        _create_plain_obs_vectors_table()

        _save(title="z", content="zz")
        out = _mh_memory_status({"project": PROJECT})
        assert "Vectors" in out
        assert "enabled (384d)" in out
        assert "1/1" in out
