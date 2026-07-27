"""A1-2: vector indexation on save + reindex tool + doctor/status hooks.

The VPS that runs these tests typically does not have sqlite-vec or
fastembed installed. Each test below either exercises the
unavailable-path explicitly or monkey-patches the embedding pipeline to
simulate an available one while still using a plain SQLite ``obs_vectors``
table (no vec0 extension required).
"""

from __future__ import annotations

import itertools
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
        monkeypatch.setattr(embeddings, "embed", lambda text, **kw: [0.0] * 768)
        monkeypatch.setattr(embeddings, "_serialize_vec", lambda vec: b"\x00" * 3072)
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
        assert row["n"] == 3072

    def test_embed_receives_title_then_narrative_preferred_over_content(self, monkeypatch):
        """The title is part of the indexed text; narrative still wins over content.

        Indexing the body alone leaves the short formulation out of the vector,
        so a query phrased like a title is compared against a text that does
        not contain what it asks for (see tests/test_vector_distance_floor.py).
        """
        monkeypatch.setattr(db_core, "VECTOR_SEARCH_AVAILABLE", True)
        captured: dict[str, str] = {}

        def fake_embed(text: str):
            captured["text"] = text or ""
            return [0.0] * 768

        monkeypatch.setattr(embeddings, "embed", fake_embed)
        monkeypatch.setattr(embeddings, "_serialize_vec", lambda vec: b"\x00" * 3072)
        _create_plain_obs_vectors_table()

        _save(title="t", content="CONTENT", narrative="NARRATIVE")
        assert captured["text"] == "t\nNARRATIVE"

        _save(title="t2", content="CONTENT2", narrative=None)
        assert captured["text"] == "t2\nCONTENT2"

    def test_texte_indexable_tolerates_missing_parts(self):
        assert embeddings.texte_indexable("titre", "corps") == "titre\ncorps"
        assert embeddings.texte_indexable(None, "corps") == "corps"
        assert embeddings.texte_indexable("titre", None) == "titre"
        assert embeddings.texte_indexable("  ", "") is None


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
        assert "fastembed" in res["reason"]

    def test_unavailable_when_obs_vectors_table_missing(self, monkeypatch):
        monkeypatch.setattr(db_core, "VECTOR_SEARCH_AVAILABLE", True)
        monkeypatch.setattr(embeddings, "_load_model", lambda: object())
        # Create some obs, then drop obs_vectors to simulate a DB where the
        # vec0 extension could not be loaded on this connection.
        _save(title="solo", content="body")
        conn = memory_db.get_db()
        try:
            conn.execute("DROP TABLE IF EXISTS obs_vectors")
            conn.commit()
        finally:
            conn.close()
        res = embeddings.backfill_obs_vectors(project_root=PROJECT, limit=10)
        assert res["status"] == "unavailable"
        assert "obs_vectors" in res["reason"]

    def test_backfill_ok_indexes_missing_rows(self, monkeypatch):
        monkeypatch.setattr(db_core, "VECTOR_SEARCH_AVAILABLE", True)
        monkeypatch.setattr(embeddings, "_load_model", lambda: object())
        monkeypatch.setattr(embeddings, "embed", lambda text, **kw: [0.0] * 768)
        monkeypatch.setattr(embeddings, "_serialize_vec", lambda vec: b"\x00" * 3072)
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

    def test_rebuild_reindexes_rows_that_already_have_a_vector(self, monkeypatch):
        """Without rebuild, a row indexed under an older text recipe is skipped.

        Nothing in the schema records which recipe produced a stored vector, so
        the only way to bring old rows onto the current one is to re-embed them
        on demand.
        """
        monkeypatch.setattr(db_core, "VECTOR_SEARCH_AVAILABLE", True)
        monkeypatch.setattr(embeddings, "_load_model", lambda: object())
        seen: list[str] = []
        monkeypatch.setattr(
            embeddings, "embed", lambda text, **kw: seen.append(text or "") or [0.0] * 768
        )
        monkeypatch.setattr(embeddings, "_serialize_vec", lambda vec: b"\x00" * 3072)
        _create_plain_obs_vectors_table()

        _save(title="a", content="aa")
        _save(title="b", content="bb")

        seen.clear()
        res = embeddings.backfill_obs_vectors(project_root=PROJECT, limit=10)
        assert res["indexed"] == 0, "les deux lignes ont deja un vecteur"
        assert seen == []

        res = embeddings.backfill_obs_vectors(project_root=PROJECT, limit=10, rebuild=True)
        assert res["status"] == "ok"
        assert res["indexed"] == 2
        assert res["pending"] == 0
        assert sorted(seen) == ["a\naa", "b\nbb"]


class TestReindexationSurUneVraieTableVec0:
    """Les autres tests de ce fichier utilisent une table obs_vectors ORDINAIRE.

    C'est ce qui rend la suite executable sur un hote sans sqlite-vec, mais une
    table ordinaire accepte `INSERT OR REPLACE` -- vec0 le refuse. Le cas
    « ce vecteur existe deja, remplace-le » n'etait donc couvert nulle part.
    """

    def test_maybe_index_obs_remplace_un_vecteur_existant(self, monkeypatch):
        """Reindexer une observation deja indexee doit reussir, pas echouer.

        observation_update() rafraichit le vecteur quand le contenu change, et
        backfill_obs_vectors(rebuild=True) reindexe tout le monde : les deux
        passent par ce chemin, et tous deux echouaient en silence sur une vraie
        table vec0 (`UNIQUE constraint failed on obs_vectors primary key`),
        maybe_index_obs avalant l'exception pour rendre False.
        """
        pytest.importorskip("sqlite_vec")
        monkeypatch.setattr(db_core, "VECTOR_SEARCH_AVAILABLE", True)
        # Un vecteur distinct par appel : _save() en consomme deja un.
        appels = itertools.count()
        monkeypatch.setattr(
            embeddings, "embed",
            lambda text, **kw: [float(next(appels))] + [0.0] * 767,
        )

        oid = _save(title="cible", content="corps")
        with memory_db.db_session() as conn:
            if not conn.execute(
                "SELECT 1 FROM sqlite_master WHERE name='obs_vectors' AND sql LIKE '%vec0%'"
            ).fetchone():
                pytest.skip("obs_vectors n'est pas une table vec0 sur cet hote")
            assert embeddings.maybe_index_obs(oid, "premiere version", conn) is True
            avant = conn.execute(
                "SELECT embedding FROM obs_vectors WHERE obs_id=?", (oid,)
            ).fetchone()[0]
            assert embeddings.maybe_index_obs(oid, "seconde version", conn) is True
            apres = conn.execute(
                "SELECT embedding FROM obs_vectors WHERE obs_id=?", (oid,)
            ).fetchone()[0]
        assert avant != apres, "le vecteur stocke n'a pas ete remplace"


class TestVectorReindexHandler:
    def test_unavailable_path_renders_warning(self, monkeypatch):
        monkeypatch.setattr(db_core, "VECTOR_SEARCH_AVAILABLE", False)
        out = _mh_memory_vector_reindex({"project": PROJECT, "limit": 50})
        assert out.startswith("⚠️") and "vector reindex" in out

    def test_ok_path_renders_coverage(self, monkeypatch):
        monkeypatch.setattr(db_core, "VECTOR_SEARCH_AVAILABLE", True)
        monkeypatch.setattr(embeddings, "_load_model", lambda: object())
        monkeypatch.setattr(embeddings, "embed", lambda text, **kw: [0.0] * 768)
        monkeypatch.setattr(embeddings, "_serialize_vec", lambda vec: b"\x00" * 3072)
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
        monkeypatch.setattr(embeddings, "embed", lambda text, **kw: [0.0] * 768)
        monkeypatch.setattr(embeddings, "_serialize_vec", lambda vec: b"\x00" * 3072)
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
        monkeypatch.setattr(embeddings, "embed", lambda text, **kw: [0.0] * 768)
        monkeypatch.setattr(embeddings, "_serialize_vec", lambda vec: b"\x00" * 3072)
        _create_plain_obs_vectors_table()

        _save(title="z", content="zz")
        out = _mh_memory_status({"project": PROJECT})
        assert "Vectors" in out
        assert "enabled (768d)" in out
        assert "1/1" in out
