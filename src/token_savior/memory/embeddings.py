"""A1-1: embedding helper for memory observations (vector search prep).

Backend: FastEmbed + ``nomic-ai/nomic-embed-text-v1.5-Q``
  - 768-dim L2-normalized vectors
  - INT8 quantized ONNX, ~210MB RAM, ~160ms/call on a 4-core CPU
  - Context 8192 tokens, Matryoshka truncation supported (64-768)
  - Task prefixes: ``search_document:`` (stored) / ``search_query:`` (user query)

If ``fastembed`` is missing or the model fails to materialize, :func:`embed`
returns ``None`` silently -- callers degrade to keyword-only search. One
warning is emitted per process when the library or the model cannot be
loaded; subsequent calls are cheap no-ops.

Install the optional stack with::

    pip install 'token-savior-recall[memory-vector]'

The chosen model outputs 768-dim vectors to match the
``obs_vectors USING vec0(embedding FLOAT[768])`` table declared in
:mod:`token_savior.db_core`.
"""

from __future__ import annotations

import logging
import math
from typing import Any

_logger = logging.getLogger(__name__)

_MODEL_NAME = "nomic-ai/nomic-embed-text-v1.5-Q"
EMBED_DIM = 768
_MAX_INPUT_CHARS = 2000

PREFIX_DOCUMENT = "search_document: "
PREFIX_QUERY = "search_query: "

_model: Any | None = None
_model_load_attempted = False
_warning_emitted = False


def _emit_warning_once(message: str, *args: object) -> None:
    global _warning_emitted
    if _warning_emitted:
        return
    _logger.warning(message, *args)
    _warning_emitted = True


def _load_model() -> Any | None:
    """Return the singleton model, loading it on first call. ``None`` on failure."""
    global _model, _model_load_attempted
    if _model_load_attempted:
        return _model
    _model_load_attempted = True
    try:
        from fastembed import TextEmbedding  # type: ignore[import-not-found]
    except ImportError:
        _emit_warning_once(
            "[token-savior:memory] fastembed not installed; "
            "embedding disabled. Install with: "
            "pip install 'token-savior-recall[memory-vector]'",
        )
        return None
    try:
        _model = TextEmbedding(model_name=_MODEL_NAME)
    except Exception as exc:
        _emit_warning_once(
            "[token-savior:memory] failed to load %s (%s); embedding disabled.",
            _MODEL_NAME, exc,
        )
        _model = None
    return _model


def _normalize_l2(values: Any) -> list[float] | None:
    try:
        flat = [float(x) for x in values]
    except (TypeError, ValueError):
        return None
    norm_sq = sum(v * v for v in flat)
    if norm_sq <= 0.0:
        return None
    inv = 1.0 / math.sqrt(norm_sq)
    return [v * inv for v in flat]


def embed(text: str | None, *, as_query: bool = False) -> list[float] | None:
    """Embed ``text`` into a 768-dim L2-normalized vector.

    The Nomic v1.5 family is task-tuned: the model expects a prefix that
    describes the role of the text. Stored observations use
    ``search_document:`` (the default); user-facing queries should pass
    ``as_query=True`` to get ``search_query:``.

    Returns ``None`` when:
      * ``text`` is empty / not a string
      * ``fastembed`` is not installed or the model failed to load
      * the embed call or the normalization fails at runtime

    Input is truncated to :data:`_MAX_INPUT_CHARS` to cap CPU latency --
    2000 chars is well within the 8192-token context window and covers the
    narrative payload of an observation or the body of most code symbols.
    """
    if not text or not isinstance(text, str):
        return None
    model = _load_model()
    if model is None:
        return None
    try:
        truncated = text[:_MAX_INPUT_CHARS]
        prefix = PREFIX_QUERY if as_query else PREFIX_DOCUMENT
        vec_iter = model.embed([prefix + truncated])
        raw = next(iter(vec_iter))
    except Exception as exc:
        _logger.debug("[token-savior:memory] embed failed: %s", exc)
        return None
    return _normalize_l2(raw)


def is_available() -> bool:
    """True if the embedding model can be loaded. Triggers lazy load."""
    return _load_model() is not None


# ---------------------------------------------------------------------------
# A1-2: vector row helpers (insert on save + backfill)
# ---------------------------------------------------------------------------


def _serialize_vec(vec: list[float]) -> Any | None:
    """Return the sqlite-vec binary blob for ``vec``, or ``None`` on failure.

    The ``obs_vectors`` vec0 table only exists when sqlite-vec is loaded,
    so importing the package here is expected to succeed whenever this
    path runs. We still guard against ImportError to keep the module
    usable in test environments that monkey-patch the flag.
    """
    try:
        import sqlite_vec  # type: ignore[import-not-found]
    except ImportError:
        return None
    try:
        return sqlite_vec.serialize_float32(vec)
    except Exception as exc:
        _logger.debug("[token-savior:memory] vec serialize failed: %s", exc)
        return None


def maybe_index_obs(obs_id: int, text: str | None, conn: Any) -> bool:
    """Embed ``text`` and upsert an obs_vectors row using ``conn``.

    Returns True on success. Silent False when:
      * vector search is globally unavailable (VECTOR_SEARCH_AVAILABLE=False)
      * embed() returns None (model missing, blank input, encode error)
      * the serializer fails or the table is absent (eg extension not loaded
        on this connection)
    """
    from token_savior.db_core import VECTOR_SEARCH_AVAILABLE
    if not VECTOR_SEARCH_AVAILABLE:
        return False
    vec = embed(text)
    if vec is None:
        return False
    blob = _serialize_vec(vec)
    if blob is None:
        return False
    try:
        conn.execute(
            "INSERT OR REPLACE INTO obs_vectors(obs_id, embedding) VALUES (?, ?)",
            (obs_id, blob),
        )
        return True
    except Exception as exc:
        _logger.debug(
            "[token-savior:memory] obs_vector insert failed (obs=%s): %s",
            obs_id, exc,
        )
        return False


def backfill_obs_vectors(
    project_root: str | None = None,
    *,
    limit: int = 500,
) -> dict[str, Any]:
    """Backfill obs_vectors for observations that lack a vector row.

    Scope: active (non-archived) observations in ``project_root`` when
    given, else all projects. Returns a dict with:
      * status   : "ok" / "unavailable" / "error"
      * indexed  : rows inserted this run
      * total    : total eligible obs
      * pending  : total - (previously_indexed + indexed_this_run)
      * reason   : filled when status != "ok"
    """
    from token_savior import memory_db
    from token_savior.db_core import VECTOR_SEARCH_AVAILABLE

    if not VECTOR_SEARCH_AVAILABLE:
        return {
            "status": "unavailable", "indexed": 0, "total": 0, "pending": 0,
            "reason": "sqlite-vec not installed (pip install "
                      "'token-savior-recall[memory-vector]')",
        }
    if _load_model() is None:
        return {
            "status": "unavailable", "indexed": 0, "total": 0, "pending": 0,
            "reason": "fastembed not installed or model load failed",
        }

    where_proj = "AND project_root=?" if project_root else ""
    base_params: list[Any] = [project_root] if project_root else []

    try:
        with memory_db.db_session() as conn:
            total = conn.execute(
                f"SELECT COUNT(*) FROM observations "
                f"WHERE archived=0 {where_proj}",
                base_params,
            ).fetchone()[0]
            try:
                prev_indexed = conn.execute(
                    f"SELECT COUNT(*) FROM observations o "
                    f"JOIN obs_vectors v ON v.obs_id=o.id "
                    f"WHERE o.archived=0 {('AND o.project_root=?' if project_root else '')}",
                    base_params,
                ).fetchone()[0]
            except Exception:
                # obs_vectors table may not exist if extension couldn't load.
                return {
                    "status": "unavailable", "indexed": 0, "total": total, "pending": 0,
                    "reason": "obs_vectors table missing (extension not loaded)",
                }

            rows = conn.execute(
                f"SELECT id, COALESCE(narrative, content) AS text "
                f"FROM observations WHERE archived=0 {where_proj} "
                f"  AND id NOT IN (SELECT obs_id FROM obs_vectors) "
                f"ORDER BY id DESC LIMIT ?",
                base_params + [int(limit)],
            ).fetchall()

            indexed = 0
            for r in rows:
                if maybe_index_obs(r["id"], r["text"], conn):
                    indexed += 1
            conn.commit()

        pending = max(0, total - prev_indexed - indexed)
        return {
            "status": "ok", "indexed": indexed, "total": total,
            "pending": pending, "previously_indexed": prev_indexed,
        }
    except Exception as exc:
        return {
            "status": "error", "indexed": 0, "total": 0, "pending": 0,
            "reason": str(exc),
        }


def vector_coverage(project_root: str) -> dict[str, Any]:
    """Return {total, indexed, percent, available} for a project."""
    from token_savior import memory_db
    from token_savior.db_core import VECTOR_SEARCH_AVAILABLE

    result: dict[str, Any] = {
        "total": 0, "indexed": 0, "percent": 0.0,
        "available": bool(VECTOR_SEARCH_AVAILABLE),
    }
    try:
        with memory_db.db_session() as conn:
            total = conn.execute(
                "SELECT COUNT(*) FROM observations "
                "WHERE project_root=? AND archived=0",
                [project_root],
            ).fetchone()[0]
            result["total"] = int(total)
            try:
                indexed = conn.execute(
                    "SELECT COUNT(*) FROM observations o "
                    "JOIN obs_vectors v ON v.obs_id=o.id "
                    "WHERE o.project_root=? AND o.archived=0",
                    [project_root],
                ).fetchone()[0]
                result["indexed"] = int(indexed)
            except Exception:
                result["indexed"] = 0
                result["available"] = False
        if result["total"] > 0:
            result["percent"] = round(100.0 * result["indexed"] / result["total"], 1)
    except Exception as exc:
        _logger.debug("[token-savior:memory] vector_coverage failed: %s", exc)
    return result
