"""Symbol-level vector index for `search_codebase(semantic=True)`.

Mirrors the design of ``memory/embeddings.py`` but scoped to code symbols
parsed from a project tree rather than memory observations.

Schema (created in ``db_core.run_migrations``):

* ``symbols(id, project_root, symbol_key, file_path, lineno, kind,
  signature, docstring_head, content_hash, updated_at_epoch)``
* ``symbol_vectors(symbol_id INTEGER PK, embedding FLOAT[768])``

Flow:

1. ``collect_project_symbols(root)`` walks ``*.py`` files, extracts
   ``FunctionDef`` / ``AsyncFunctionDef`` / ``ClassDef`` via ``ast``, and
   returns descriptors suitable for embedding.
2. ``reindex_project_symbols(root)`` embeds new/changed symbols (batched)
   and upserts rows. Rows whose ``content_hash`` already matches the
   stored hash are skipped — cheap incremental reindex.
3. ``search_symbols_semantic(query, root, limit=10)`` embeds the query
   (``as_query=True`` prefix), runs k-NN against ``symbol_vectors``,
   joins ``symbols`` for metadata, returns hits with scores.

The module is a no-op when sqlite-vec or fastembed isn't loadable — the
caller gets ``status="unavailable"`` and should fall back to the regex
path. Same degradation contract as ``memory/embeddings.py``.
"""
from __future__ import annotations

import ast
import hashlib
import logging
import time
from pathlib import Path
from typing import Any, Iterable

_logger = logging.getLogger(__name__)

_BATCH_SIZE = 32
_MAX_DOC_CHARS = 1200
_EXCLUDE_DIRS = frozenset({
    "__pycache__", ".git", "node_modules", "dist", "build", ".next",
    "venv", ".venv", "env", ".tox",
})


# ---------------------------------------------------------------------------
# AST walker — produces one descriptor per function/class/method
# ---------------------------------------------------------------------------


def _iter_symbols_in_file(py_path: Path, project_root: Path) -> Iterable[dict]:
    try:
        source = py_path.read_text(encoding="utf-8", errors="replace")
        tree = ast.parse(source)
    except (OSError, SyntaxError):
        return
    rel_path = str(py_path.relative_to(project_root))

    def _visit(node: ast.AST, prefix: str = "") -> Iterable[dict]:
        for child in ast.iter_child_nodes(node):
            if not isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                continue
            qname = f"{prefix}{child.name}" if prefix else child.name
            kind = "class" if isinstance(child, ast.ClassDef) else "func"
            doc = (ast.get_docstring(child) or "").strip()
            doc_head = "\n".join(doc.splitlines()[:2])

            try:
                sig_source = ast.unparse(child)
            except Exception:
                sig_source = ""
            sig_lines = sig_source.splitlines()
            signature = sig_lines[0] if sig_lines else f"{kind} {qname}"

            body_head: list[str] = []
            skipped_doc = False
            for line in sig_lines[1:]:
                stripped = line.strip()
                if not stripped:
                    continue
                if not skipped_doc and stripped.startswith(('"""', "'''")):
                    skipped_doc = True
                    continue
                body_head.append(stripped)
                if len(body_head) >= 3:
                    break
            body_excerpt = "\n".join(body_head)

            symbol_key = f"{rel_path}::{qname}"
            # ``embed()`` already prepends "search_document: " — we only
            # supply the raw payload. Double-prefix would confuse the
            # Nomic task router.
            embed_doc = (
                f"{kind} {qname}\n"
                f"{signature}\n"
                f"{doc_head}\n"
                f"{body_excerpt}"
            ).strip()[:_MAX_DOC_CHARS]
            content_hash = hashlib.sha1(
                embed_doc.encode("utf-8", "replace")
            ).hexdigest()[:16]

            yield {
                "project_root": str(project_root),
                "symbol_key": symbol_key,
                "file_path": rel_path,
                "lineno": child.lineno,
                "kind": kind,
                "signature": signature[:400],
                "docstring_head": doc_head[:400],
                "content_hash": content_hash,
                "embed_doc": embed_doc,
            }
            if isinstance(child, ast.ClassDef):
                yield from _visit(child, prefix=f"{qname}.")

    yield from _visit(tree)


def collect_project_symbols(project_root: str | Path) -> list[dict]:
    """Walk project_root for .py files and return symbol descriptors.

    Excludes common noise dirs (``__pycache__``, ``node_modules``, venvs).
    Silently skips files with syntax or encoding errors — collection
    should never fail the whole reindex over one bad file.
    """
    root = Path(project_root).resolve()
    if not root.is_dir():
        return []
    out: list[dict] = []
    for py in root.rglob("*.py"):
        if any(part in _EXCLUDE_DIRS for part in py.parts):
            continue
        out.extend(_iter_symbols_in_file(py, root))
    return out


# ---------------------------------------------------------------------------
# Indexing — batched embed + upsert into symbols / symbol_vectors
# ---------------------------------------------------------------------------


def _embed_batch(docs: list[str]) -> list[list[float] | None]:
    """Embed documents sequentially via the shared ``embed()`` helper.

    Sequential calls are used instead of ``model.embed(docs)`` because
    FastEmbed's ONNX backend holds onto intermediate buffers across a
    materialised batch — a single ``list(model.embed(100_docs))`` spiked
    this process past 3 GB RSS on 4 GB-available VPS and tripped the OOM
    killer. One-at-a-time keeps the peak at ~500 MB (matches the
    memory_retrieval bench measurements) at the cost of ~100 ms per
    symbol on CPU, which is fine for a one-off reindex.
    """
    from token_savior.memory.embeddings import embed
    return [embed(doc) for doc in docs]


def _serialize_vec(vec: list[float]) -> Any | None:
    try:
        import sqlite_vec
        return sqlite_vec.serialize_float32(vec)
    except Exception:
        return None


def reindex_project_symbols(
    project_root: str | Path,
    *,
    db_path: str | Path | None = None,
    batch_size: int = _BATCH_SIZE,
) -> dict[str, Any]:
    """Collect, embed, and upsert all symbols in ``project_root``.

    Fast path: a symbol whose ``content_hash`` already matches the stored
    row is skipped (no re-embed). This makes re-running the indexer cheap
    when only a handful of files changed.

    Returns a summary dict:
      * status     : "ok" | "unavailable"
      * total      : symbols seen
      * indexed    : symbols newly embedded and written
      * skipped    : symbols whose hash was unchanged
      * elapsed_s  : wall clock
      * reason     : filled when status != "ok"
    """
    from token_savior import memory_db
    from token_savior.db_core import VECTOR_SEARCH_AVAILABLE
    from token_savior.memory.embeddings import is_available

    if not VECTOR_SEARCH_AVAILABLE:
        return {"status": "unavailable", "reason": "sqlite-vec not loadable"}
    if not is_available():
        return {"status": "unavailable", "reason": "embedding model not loadable"}

    t0 = time.perf_counter()
    root_str = str(Path(project_root).resolve())
    symbols = collect_project_symbols(root_str)
    now_epoch = int(time.time())

    indexed = 0
    skipped = 0
    to_embed: list[dict] = []

    def conn_factory():
        return memory_db.db_session(db_path) if db_path else memory_db.db_session()
    with conn_factory() as conn:
        existing = dict(conn.execute(
            "SELECT symbol_key, content_hash FROM symbols WHERE project_root=?",
            (root_str,),
        ).fetchall())

        for sym in symbols:
            if existing.get(sym["symbol_key"]) == sym["content_hash"]:
                skipped += 1
                continue
            to_embed.append(sym)

        for i in range(0, len(to_embed), batch_size):
            chunk = to_embed[i:i + batch_size]
            vecs = _embed_batch([s["embed_doc"] for s in chunk])
            for sym, vec in zip(chunk, vecs, strict=False):
                if vec is None:
                    continue
                blob = _serialize_vec(vec)
                if blob is None:
                    continue
                cur = conn.execute(
                    "INSERT INTO symbols("
                    "  project_root, symbol_key, file_path, lineno, kind,"
                    "  signature, docstring_head, content_hash, updated_at_epoch"
                    ") VALUES (?,?,?,?,?,?,?,?,?) "
                    "ON CONFLICT(project_root, symbol_key) DO UPDATE SET "
                    "  file_path=excluded.file_path,"
                    "  lineno=excluded.lineno,"
                    "  kind=excluded.kind,"
                    "  signature=excluded.signature,"
                    "  docstring_head=excluded.docstring_head,"
                    "  content_hash=excluded.content_hash,"
                    "  updated_at_epoch=excluded.updated_at_epoch "
                    "RETURNING id",
                    (
                        sym["project_root"], sym["symbol_key"],
                        sym["file_path"], sym["lineno"], sym["kind"],
                        sym["signature"], sym["docstring_head"],
                        sym["content_hash"], now_epoch,
                    ),
                )
                row = cur.fetchone()
                if row is None:
                    continue
                symbol_id = row[0]
                conn.execute(
                    "INSERT OR REPLACE INTO symbol_vectors(symbol_id, embedding) "
                    "VALUES (?, ?)",
                    (symbol_id, blob),
                )
                indexed += 1
        # Prune rows for symbols that vanished from the project tree.
        live_keys = {s["symbol_key"] for s in symbols}
        stale_keys = [k for k in existing if k not in live_keys]
        removed = 0
        for key in stale_keys:
            cur = conn.execute(
                "DELETE FROM symbols WHERE project_root=? AND symbol_key=? "
                "RETURNING id",
                (root_str, key),
            )
            row = cur.fetchone()
            if row is not None:
                conn.execute(
                    "DELETE FROM symbol_vectors WHERE symbol_id=?", (row[0],),
                )
                removed += 1
        conn.commit()

    return {
        "status": "ok",
        "total": len(symbols),
        "indexed": indexed,
        "skipped": skipped,
        "removed": removed,
        "elapsed_s": round(time.perf_counter() - t0, 2),
    }


# ---------------------------------------------------------------------------
# Query — k-NN over symbol_vectors with safety metadata
# ---------------------------------------------------------------------------


def search_symbols_semantic(
    query: str,
    project_root: str | Path,
    *,
    limit: int = 10,
    db_path: str | Path | None = None,
) -> dict[str, Any]:
    """k-NN over symbol_vectors for ``project_root``.

    Returns ``{"status": str, "hits": [...], "warning": Optional[str]}``.
    Each hit carries the disambiguation metadata required by the safety
    contract (see docs/design-semantic-code-tools.md): signature,
    docstring head, file:line, score.

    Warnings:
      * ``"low_confidence"`` when top1 score < 0.60 or when top1-top2
        delta < 0.01 (dense cluster, likely ambiguous). Thresholds are
        CODE-specific — tuned on tests/benchmarks/code_retrieval since
        the memory-era 0.75 floor fired on 93% of code queries.
    """
    from token_savior import memory_db
    from token_savior.db_core import VECTOR_SEARCH_AVAILABLE
    from token_savior.memory.embeddings import embed

    if not VECTOR_SEARCH_AVAILABLE:
        return {"status": "unavailable", "reason": "sqlite-vec not loadable", "hits": []}
    qvec = embed(query, as_query=True)
    if qvec is None:
        return {"status": "unavailable", "reason": "query embed failed", "hits": []}
    blob = _serialize_vec(qvec)
    if blob is None:
        return {"status": "unavailable", "reason": "vec serialize failed", "hits": []}

    root_str = str(Path(project_root).resolve())
    k = max(int(limit), 5)

    sql = (
        "SELECT s.symbol_key, s.file_path, s.lineno, s.kind, s.signature,"
        "       s.docstring_head, v.distance "
        "FROM symbol_vectors v "
        "JOIN symbols s ON s.id = v.symbol_id "
        "WHERE v.embedding MATCH ? AND k = ? AND s.project_root = ? "
        "ORDER BY v.distance"
    )
    def conn_factory():
        return memory_db.db_session(db_path) if db_path else memory_db.db_session()
    with conn_factory() as conn:
        rows = conn.execute(sql, (blob, k, root_str)).fetchall()

    hits: list[dict] = []
    for r in rows:
        # sqlite-vec vec0 FLOAT[N] stores L2 distance by default. Our
        # vectors are L2-normalised upstream, so the identity
        #   |a - b|^2 = 2 - 2·(a·b)
        # gives  cos(a, b) = 1 - L2^2 / 2  on the [-1, 1] interval.
        l2 = float(r["distance"])
        cos_score = max(0.0, 1.0 - (l2 * l2) / 2.0)
        _, qname = r["symbol_key"].split("::", 1) if "::" in r["symbol_key"] else ("", r["symbol_key"])
        hits.append({
            "symbol": qname,
            "kind": r["kind"],
            "file": r["file_path"],
            "line": r["lineno"],
            "signature": r["signature"],
            "docstring_head": r["docstring_head"],
            "score": round(cos_score, 4),
        })

    # Low-confidence thresholds tuned for CODE symbols, not memory
    # observations. tests/benchmarks/code_retrieval (30 queries, 1002
    # symbols, Nomic-embed-text-v1.5-Q) shows correct-hit top1 scores
    # range 0.59-0.78 vs wrong-hit top1 0.63-0.69 — the two distributions
    # overlap, so the memory-era 0.75 absolute floor fired on 93% of
    # queries and carried no signal. The tighter values below catch the
    # genuine failure modes without drowning real results:
    #   - top1 < 0.60 : retrieval is barely above noise floor
    #   - gap  < 0.01 : top1 indistinguishable from top2 (ambiguous)
    # Rate on the same bench with these values: ~20%.
    _CODE_TOP1_FLOOR = 0.60
    _CODE_GAP_MIN = 0.01
    warning: str | None = None
    if hits:
        top1 = hits[0]["score"]
        top2 = hits[1]["score"] if len(hits) > 1 else 0.0
        if top1 < _CODE_TOP1_FLOOR or (top1 - top2) < _CODE_GAP_MIN:
            warning = (
                f"low_confidence: top1={top1:.2f}, top2={top2:.2f}. "
                "Consider refining the query or verifying via find_symbol."
            )

    return {
        "status": "ok",
        "hits": hits,
        "warning": warning,
    }
