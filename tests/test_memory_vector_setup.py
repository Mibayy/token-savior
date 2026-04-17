"""A1-1: sqlite-vec + embeddings module wiring tests.

Covers graceful degradation when sqlite-vec / sentence-transformers
are absent, and that nothing in the baseline memory engine breaks.
"""

from __future__ import annotations

import importlib
from pathlib import Path
from unittest.mock import patch

import pytest

from token_savior import db_core, memory_db
from token_savior.memory import embeddings


@pytest.fixture(autouse=True)
def _memory_tmpdb(tmp_path: Path):
    db_path = tmp_path / "memory.db"
    with patch.object(memory_db, "MEMORY_DB_PATH", db_path):
        yield db_path


class TestVectorAvailabilityFlag:
    def test_flag_exists_and_is_boolean(self):
        assert isinstance(db_core.VECTOR_SEARCH_AVAILABLE, bool)

    def test_flag_reflects_sqlite_vec_import(self):
        try:
            import sqlite_vec  # noqa: F401
            expected = True
        except ImportError:
            expected = False
        assert db_core.VECTOR_SEARCH_AVAILABLE is expected

    def test_maybe_load_returns_false_when_unavailable(self, monkeypatch):
        monkeypatch.setattr(db_core, "VECTOR_SEARCH_AVAILABLE", False)
        # Reset warning flag so we can count emissions across the test.
        monkeypatch.setattr(db_core, "_vector_warning_emitted", False)
        conn = db_core.sqlite3.connect(":memory:")
        try:
            assert db_core._maybe_load_sqlite_vec(conn) is False
        finally:
            conn.close()


class TestMigrationsWithoutVec:
    def test_migration_runs_without_vec(self, tmp_path: Path):
        """Migration must not raise when sqlite-vec is unavailable."""
        p = tmp_path / "nv.db"
        db_core.run_migrations(p)
        conn = db_core.sqlite3.connect(str(p))
        names = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()}
        conn.close()
        # Base tables are still there.
        assert "observations" in names

    def test_obs_vectors_absent_when_vec_unavailable(self, tmp_path: Path, monkeypatch):
        monkeypatch.setattr(db_core, "VECTOR_SEARCH_AVAILABLE", False)
        p = tmp_path / "nv2.db"
        # Force-clear memo so migration re-runs.
        db_core._migrated_paths.discard(str(p))
        db_core.run_migrations(p)
        conn = db_core.sqlite3.connect(str(p))
        names = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE name='obs_vectors'"
        ).fetchall()}
        conn.close()
        assert names == set()

    def test_get_db_open_without_vec(self, tmp_path: Path, monkeypatch):
        monkeypatch.setattr(db_core, "VECTOR_SEARCH_AVAILABLE", False)
        p = tmp_path / "nv3.db"
        conn = db_core.get_db(p)
        try:
            # Basic smoke: observations table exists.
            cols = conn.execute("PRAGMA table_info(observations)").fetchall()
            assert len(cols) > 0
        finally:
            conn.close()


class TestEmbeddingsModule:
    def test_embed_none_when_library_missing(self, monkeypatch):
        """If sentence-transformers can't be imported, embed returns None."""
        # Reset lazy state
        monkeypatch.setattr(embeddings, "_model", None)
        monkeypatch.setattr(embeddings, "_model_load_attempted", False)
        monkeypatch.setattr(embeddings, "_warning_emitted", False)

        real_import = __builtins__["__import__"] if isinstance(__builtins__, dict) else __import__

        def fake_import(name, *args, **kwargs):
            if name == "sentence_transformers" or name.startswith("sentence_transformers."):
                raise ImportError("simulated absence")
            return real_import(name, *args, **kwargs)

        monkeypatch.setattr("builtins.__import__", fake_import)
        assert embeddings.embed("hello world") is None
        assert embeddings.is_available() is False

    def test_embed_none_on_blank_input(self):
        assert embeddings.embed("") is None
        assert embeddings.embed(None) is None  # type: ignore[arg-type]
        assert embeddings.embed(123) is None  # type: ignore[arg-type]

    def test_embed_dim_constant(self):
        assert embeddings.EMBED_DIM == 384

    def test_warning_emitted_once(self, monkeypatch, caplog):
        monkeypatch.setattr(embeddings, "_model", None)
        monkeypatch.setattr(embeddings, "_model_load_attempted", False)
        monkeypatch.setattr(embeddings, "_warning_emitted", False)

        def fake_import(name, *args, **kwargs):
            if name == "sentence_transformers":
                raise ImportError("simulated")
            return importlib.__import__(name, *args, **kwargs)

        monkeypatch.setattr("builtins.__import__", fake_import)
        with caplog.at_level("WARNING", logger=embeddings._logger.name):
            embeddings.embed("a")
            embeddings.embed("b")
            embeddings.embed("c")
        hits = [r for r in caplog.records if "sentence-transformers" in r.message]
        assert len(hits) == 1
