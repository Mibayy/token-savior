"""A1-3: hybrid search (FTS5 + vec) with RRF fusion.

These tests run on a VPS that typically doesn't have sqlite-vec /
sentence-transformers installed. They exercise both the graceful
fallback path (pure FTS5) and a monkey-patched simulated-available path
that stubs ``embed`` and injects synthetic vec rows — no real model is
ever downloaded.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from token_savior import db_core, memory_db
from token_savior.memory import search as search_mod
from token_savior.memory.search import hybrid_search, rrf_merge

PROJECT = "/tmp/test-project-a1-3"


@pytest.fixture(autouse=True)
def _memory_tmpdb(tmp_path: Path):
    db_path = tmp_path / "memory.db"
    with patch.object(memory_db, "MEMORY_DB_PATH", db_path):
        yield db_path


def _save(title: str, content: str, **kw) -> int:
    sid = memory_db.session_start(PROJECT)
    oid = memory_db.observation_save(
        sid, PROJECT, "convention", title, content, **kw,
    )
    assert oid is not None
    return oid


class TestRrfMerge:
    def test_single_list_preserves_order(self):
        rows = [{"id": 1}, {"id": 2}, {"id": 3}]
        out = rrf_merge(rows, limit=10)
        assert [r["id"] for r in out] == [1, 2, 3]

    def test_rrf_boost_for_shared_ids(self):
        """Items appearing in BOTH lists must outrank singletons."""
        fts = [{"id": 1}, {"id": 5}, {"id": 6}, {"id": 7}, {"id": 8}]
        vec = [{"id": 9}, {"id": 1}, {"id": 10}, {"id": 11}, {"id": 12}]
        # id=1: 1/(60+1) + 1/(60+2) ≈ 0.0325
        # Any singleton: at most 1/61 ≈ 0.0164.
        out = rrf_merge(fts, vec, limit=6)
        assert out[0]["id"] == 1
        score_top = out[0]["_rrf_score"]
        score_next = out[1]["_rrf_score"]
        assert score_top > score_next

    def test_rrf_scores_are_attached(self):
        fts = [{"id": 10}]
        out = rrf_merge(fts, limit=10)
        assert "_rrf_score" in out[0]
        # Only in rank 1 of a single list → 1/(60+1) = 0.016393...
        assert abs(out[0]["_rrf_score"] - 1 / 61) < 1e-5

    def test_rrf_limit_truncates(self):
        rows = [{"id": i} for i in range(100)]
        out = rrf_merge(rows, limit=5)
        assert len(out) == 5

    def test_rrf_empty_input(self):
        assert rrf_merge(limit=10) == []
        assert rrf_merge([], [], limit=10) == []


class TestHybridSearchFallback:
    def test_unavailable_returns_fts_verbatim(self, monkeypatch):
        """VECTOR_SEARCH_AVAILABLE=False → FTS rank order preserved."""
        monkeypatch.setattr(db_core, "VECTOR_SEARCH_AVAILABLE", False)
        fts = [{"id": i} for i in range(5)]
        out = hybrid_search(None, fts, "q", PROJECT, limit=3)
        assert [r["id"] for r in out] == [0, 1, 2]

    def test_embed_none_returns_fts_verbatim(self, monkeypatch):
        """embed() returning None (model missing) → FTS preserved."""
        monkeypatch.setattr(db_core, "VECTOR_SEARCH_AVAILABLE", True)
        from token_savior.memory import embeddings
        monkeypatch.setattr(embeddings, "embed", lambda text: None)
        fts = [{"id": 1}, {"id": 2}, {"id": 3}]
        out = hybrid_search(None, fts, "q", PROJECT, limit=10)
        assert [r["id"] for r in out] == [1, 2, 3]

    def test_vec_rows_empty_returns_fts_verbatim(self, monkeypatch):
        """No vec rows returned (missing table) → FTS preserved."""
        monkeypatch.setattr(db_core, "VECTOR_SEARCH_AVAILABLE", True)
        from token_savior.memory import embeddings
        monkeypatch.setattr(embeddings, "embed", lambda text: [0.0] * 384)
        monkeypatch.setattr(search_mod, "vec_search_rows",
                            lambda *a, **kw: [])
        fts = [{"id": 1}, {"id": 2}]
        out = hybrid_search(None, fts, "q", PROJECT, limit=10)
        assert [r["id"] for r in out] == [1, 2]

    def test_ci_never_downloads_model(self, monkeypatch):
        """Fallback path must not trigger any model load in CI."""
        monkeypatch.setattr(db_core, "VECTOR_SEARCH_AVAILABLE", False)
        from token_savior.memory import embeddings

        def explode():
            raise AssertionError("model should not be loaded when unavailable")
        monkeypatch.setattr(embeddings, "_load_model", explode)
        out = hybrid_search(None, [{"id": 1}], "q", PROJECT, limit=5)
        assert [r["id"] for r in out] == [1]


class TestHybridSearchFused:
    def test_rrf_reorders_when_vec_available(self, monkeypatch):
        """Simulate vec availability and verify RRF fusion promotes overlap."""
        monkeypatch.setattr(db_core, "VECTOR_SEARCH_AVAILABLE", True)
        from token_savior.memory import embeddings
        monkeypatch.setattr(embeddings, "embed", lambda text: [0.1] * 384)

        # #7 is mid-rank in FTS but also mid-rank in vec → highest fused.
        fts = [{"id": 1}, {"id": 2}, {"id": 7}, {"id": 3}, {"id": 4}]
        vec = [{"id": 9}, {"id": 10}, {"id": 7}, {"id": 11}, {"id": 12}]
        monkeypatch.setattr(search_mod, "vec_search_rows",
                            lambda *a, **kw: vec)

        out = hybrid_search(None, fts, "q", PROJECT, limit=5)
        ids = [r["id"] for r in out]
        assert ids[0] == 7
        # Singletons from both lists also appear in top-5.
        assert 1 in ids or 9 in ids


class TestObservationSearchIntegration:
    def test_fts_behaviour_identical_when_vec_unavailable(self, monkeypatch):
        """Backwards-compat: pure-FTS path matches the pre-A1-3 shape."""
        monkeypatch.setattr(db_core, "VECTOR_SEARCH_AVAILABLE", False)
        _save("alpha title", "body one uniquewordAAA")
        _save("beta title", "body two uniquewordAAA")
        hits = memory_db.observation_search(
            project_root=PROJECT, query="uniquewordAAA", limit=10,
        )
        assert len(hits) == 2
        assert {h["title"] for h in hits} == {"alpha title", "beta title"}

    def test_type_filter_still_works(self, monkeypatch):
        monkeypatch.setattr(db_core, "VECTOR_SEARCH_AVAILABLE", False)
        sid = memory_db.session_start(PROJECT)
        memory_db.observation_save(
            sid, PROJECT, "convention", "conv a", "uniqwordBBB body conv",
        )
        memory_db.observation_save(
            sid, PROJECT, "pattern", "pat a", "uniqwordBBB body pat",
        )
        hits = memory_db.observation_search(
            project_root=PROJECT, query="uniqwordBBB",
            type_filter="pattern", limit=10,
        )
        assert len(hits) == 1
        assert hits[0]["type"] == "pattern"
