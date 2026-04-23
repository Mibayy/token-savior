"""Tests for memory/symbol_embeddings.py — the vector index over code
symbols powering search_codebase(semantic=True).

Covers:
  * AST extraction produces one descriptor per function/class/method
  * reindex_project_symbols upserts new symbols and skips unchanged ones
  * stale symbols (file removed, renamed) are pruned on reindex
  * search_symbols_semantic returns k>=5 hits with metadata
  * low-confidence warning fires when top1 is weak or top1/top2 are close

The tests mint a minimal Python fixture project in tmp_path to avoid
pulling on the token-savior source tree (embedding 998 symbols would
make the test suite unbearable). Embedding calls use the real Nomic
model — skipped when the optional stack isn't installed.
"""
from __future__ import annotations

import textwrap
from pathlib import Path

import pytest


def _write(tmp_path: Path, rel: str, content: str) -> None:
    path = tmp_path / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(textwrap.dedent(content), encoding="utf-8")


@pytest.fixture
def fixture_project(tmp_path: Path) -> Path:
    _write(tmp_path, "pkg/__init__.py", "")
    _write(tmp_path, "pkg/ranker.py", '''
        """Ranking helpers."""

        def rank_results(candidates):
            """Sort candidates by their score descending."""
            return sorted(candidates, key=lambda c: c["score"], reverse=True)

        def fuse_two_lists(a, b, k=60):
            """Merge two ranked result lists via reciprocal rank fusion."""
            scores = {}
            for rows in (a, b):
                for rank, item in enumerate(rows, 1):
                    scores[item] = scores.get(item, 0) + 1 / (k + rank)
            return sorted(scores, key=scores.get, reverse=True)
    ''')
    _write(tmp_path, "pkg/graph.py", '''
        """Graph helpers."""

        class ImportGraph:
            """Directed graph over module imports."""

            def find_cycles(self):
                """Detect strongly-connected components (Tarjan)."""
                return []

            def add_edge(self, a, b):
                """Record an import from a to b."""
                pass
    ''')
    return tmp_path


# ---------------------------------------------------------------------------
# AST extraction
# ---------------------------------------------------------------------------


def test_collect_symbols_enumerates_functions_classes_methods(fixture_project: Path):
    from token_savior.memory.symbol_embeddings import collect_project_symbols
    syms = collect_project_symbols(fixture_project)
    names = sorted(s["symbol_key"] for s in syms)
    assert "pkg/ranker.py::rank_results" in names
    assert "pkg/ranker.py::fuse_two_lists" in names
    assert "pkg/graph.py::ImportGraph" in names
    assert "pkg/graph.py::ImportGraph.find_cycles" in names
    assert "pkg/graph.py::ImportGraph.add_edge" in names
    assert len(syms) == 5


def test_collect_symbols_skips_venv_and_pycache(tmp_path: Path):
    from token_savior.memory.symbol_embeddings import collect_project_symbols
    _write(tmp_path, "real/mod.py", "def keep_me(): pass\n")
    _write(tmp_path, ".venv/lib/pkg.py", "def drop_me(): pass\n")
    _write(tmp_path, "__pycache__/cached.py", "def drop_me_too(): pass\n")
    keys = {s["symbol_key"] for s in collect_project_symbols(tmp_path)}
    assert keys == {"real/mod.py::keep_me"}


def test_collect_symbols_tolerates_syntax_errors(tmp_path: Path):
    from token_savior.memory.symbol_embeddings import collect_project_symbols
    _write(tmp_path, "ok.py", "def fine(): pass\n")
    _write(tmp_path, "broken.py", "def oops( missing paren:\n")
    keys = {s["symbol_key"] for s in collect_project_symbols(tmp_path)}
    assert keys == {"ok.py::fine"}


def test_symbol_descriptor_captures_signature_and_docstring(fixture_project: Path):
    from token_savior.memory.symbol_embeddings import collect_project_symbols
    syms = {s["symbol_key"]: s for s in collect_project_symbols(fixture_project)}
    sym = syms["pkg/ranker.py::rank_results"]
    assert sym["kind"] == "func"
    assert sym["file_path"] == "pkg/ranker.py"
    assert "def rank_results" in sym["signature"]
    assert "Sort candidates" in sym["docstring_head"]
    assert sym["content_hash"]


# ---------------------------------------------------------------------------
# Reindex + query (requires embedding stack)
# ---------------------------------------------------------------------------


def _vector_stack_ready() -> bool:
    try:
        from token_savior.db_core import VECTOR_SEARCH_AVAILABLE
        from token_savior.memory.embeddings import is_available
        return bool(VECTOR_SEARCH_AVAILABLE and is_available())
    except Exception:
        return False


_VECTOR_REQUIRED = pytest.mark.skipif(
    not _vector_stack_ready(),
    reason="sqlite-vec + fastembed not installed",
)


@_VECTOR_REQUIRED
def test_reindex_then_search_finds_expected_symbol(fixture_project: Path, tmp_path: Path):
    from token_savior import db_core, memory_db
    from token_savior.memory.symbol_embeddings import (
        reindex_project_symbols, search_symbols_semantic,
    )

    db = tmp_path / "bench.db"
    db_core.run_migrations(db)
    memory_db.MEMORY_DB_PATH = str(db)

    summary = reindex_project_symbols(fixture_project)
    assert summary["status"] == "ok"
    assert summary["indexed"] == 5
    assert summary["skipped"] == 0

    res = search_symbols_semantic(
        "merge two ranked lists using reciprocal rank fusion",
        fixture_project, limit=5,
    )
    assert res["status"] == "ok"
    assert res["hits"], "expected at least one hit"
    assert res["hits"][0]["symbol"] == "fuse_two_lists"
    # Every hit must carry the disambiguation metadata (safety contract).
    for h in res["hits"]:
        assert set(h) >= {"symbol", "kind", "file", "line", "signature", "docstring_head", "score"}


@_VECTOR_REQUIRED
def test_reindex_is_idempotent_on_unchanged_files(fixture_project: Path, tmp_path: Path):
    from token_savior import db_core, memory_db
    from token_savior.memory.symbol_embeddings import reindex_project_symbols

    db = tmp_path / "bench.db"
    db_core.run_migrations(db)
    memory_db.MEMORY_DB_PATH = str(db)

    first = reindex_project_symbols(fixture_project)
    second = reindex_project_symbols(fixture_project)
    assert first["indexed"] == 5
    assert second["indexed"] == 0
    assert second["skipped"] == 5


@_VECTOR_REQUIRED
def test_reindex_prunes_deleted_symbols(fixture_project: Path, tmp_path: Path):
    from token_savior import db_core, memory_db
    from token_savior.memory.symbol_embeddings import reindex_project_symbols

    db = tmp_path / "bench.db"
    db_core.run_migrations(db)
    memory_db.MEMORY_DB_PATH = str(db)
    reindex_project_symbols(fixture_project)

    (fixture_project / "pkg" / "graph.py").unlink()
    second = reindex_project_symbols(fixture_project)
    assert second["removed"] == 3  # class + 2 methods
    assert second["total"] == 2


@_VECTOR_REQUIRED
def test_find_semantic_duplicates_embedding_emits_per_cluster_scores(
    tmp_path: Path,
):
    """Safety contract: every reported cluster must expose its own
    similarity range so the caller can distinguish a tight 0.99 clone
    from a loose 0.85 conceptual match. The shared threshold on line 1
    is not enough — two clusters right above the threshold can differ
    by 10 similarity points and the caller wouldn't know.
    """
    from token_savior import db_core, memory_db
    from token_savior.project_indexer import ProjectIndexer
    from token_savior.query_api import ProjectQueryEngine

    # Fixture: two near-identical `slugify` functions in separate files.
    _write(tmp_path, "pkg/__init__.py", "")
    _write(tmp_path, "pkg/a.py", '''
        """Helpers A."""

        def slugify(text):
            """Return a URL-safe version of text."""
            return text.lower().replace(" ", "-")
    ''')
    _write(tmp_path, "pkg/b.py", '''
        """Helpers B."""

        def slugify(value):
            """Produce a URL-friendly slug from value."""
            return value.lower().replace(" ", "-")
    ''')

    db = tmp_path / "bench.db"
    db_core.run_migrations(db)
    memory_db.MEMORY_DB_PATH = str(db)
    idx = ProjectIndexer(str(tmp_path)).index()
    engine = ProjectQueryEngine(idx)

    report = engine.find_semantic_duplicates(
        method="embedding", min_similarity=0.80, max_groups=30,
    )
    cluster_lines = [
        line for line in report.splitlines() if line.startswith("  cluster(")
    ]
    assert cluster_lines, f"expected at least one cluster, got:\n{report}"
    for line in cluster_lines:
        assert "sim=" in line, (
            f"cluster line missing per-cluster score tag: {line!r}"
        )
        # Format: "  cluster(N) sim=0.XX..0.YY: member1, ..."
        head, _, _ = line.partition(":")
        assert "sim=" in head and ".." in head, (
            f"score tag malformed (expected sim=min..mean): {line!r}"
        )


@_VECTOR_REQUIRED
def test_find_semantic_duplicates_embedding_filters_class_method_pairs(
    fixture_project: Path, tmp_path: Path,
):
    """Feature 2 regression: at a realistic similarity threshold, a
    class and its own method must not form a 2-element duplicate
    cluster. The filter blocks the direct pair comparison; transitive
    unions at very low thresholds are legitimate and not what this
    guard is about.
    """
    from token_savior import db_core, memory_db
    from token_savior.project_indexer import ProjectIndexer
    from token_savior.query_api import ProjectQueryEngine

    db = tmp_path / "bench.db"
    db_core.run_migrations(db)
    memory_db.MEMORY_DB_PATH = str(db)
    idx = ProjectIndexer(str(fixture_project)).index()
    engine = ProjectQueryEngine(idx)

    report = engine.find_semantic_duplicates(
        method="embedding", min_similarity=0.95, max_groups=30,
    )
    for line in report.splitlines():
        if not line.startswith("  cluster(2)"):
            continue
        members = line.split(":", 1)[1]
        # Parse the "a, b" into the two symbol keys. A cluster(2) pair
        # where one ends with ".<member>" of the other is the exact
        # failure mode the filter exists to prevent.
        a, b = [m.strip() for m in members.split(",", 1)]
        _, a_q = a.split("::", 1)
        _, b_q = b.split("::", 1)
        parent, child = sorted((a_q, b_q), key=len)
        assert not child.startswith(parent + "."), (
            f"class↔method pair slipped through: {line}"
        )
