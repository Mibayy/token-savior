"""A1-1: embedding helper for memory observations (vector search prep).

Lazy-loads ``SentenceTransformer("all-MiniLM-L6-v2")`` on first use. If the
``sentence-transformers`` package is missing or the model fails to
materialize, :func:`embed` returns ``None`` silently — callers degrade to
keyword-only search. One warning is emitted per process when the library
or the model cannot be loaded; subsequent calls are cheap no-ops.

Install the optional stack with::

    pip install 'token-savior-recall[memory-vector]'

The chosen model outputs 384-dim vectors to match the
``obs_vectors USING vec0(embedding FLOAT[384])`` table declared in
:mod:`token_savior.db_core`.
"""

from __future__ import annotations

import logging
from typing import Any

_logger = logging.getLogger(__name__)

_MODEL_NAME = "all-MiniLM-L6-v2"
EMBED_DIM = 384
_MAX_INPUT_CHARS = 512

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
        from sentence_transformers import SentenceTransformer  # type: ignore[import-not-found]
    except ImportError:
        _emit_warning_once(
            "[token-savior:memory] sentence-transformers not installed; "
            "embedding disabled. Install with: "
            "pip install 'token-savior-recall[memory-vector]'",
        )
        return None
    try:
        _model = SentenceTransformer(_MODEL_NAME)
    except Exception as exc:
        _emit_warning_once(
            "[token-savior:memory] failed to load %s (%s); embedding disabled.",
            _MODEL_NAME, exc,
        )
        _model = None
    return _model


def embed(text: str | None) -> list[float] | None:
    """Embed ``text`` into a 384-dim vector.

    Returns ``None`` when:
      * ``text`` is empty / not a string
      * ``sentence-transformers`` is not installed
      * model load or encode fails at runtime

    Input is truncated to 512 characters — callers typically pass
    ``COALESCE(narrative, content)`` and the first 512 chars carry
    the semantic payload for observations.
    """
    if not text or not isinstance(text, str):
        return None
    model = _load_model()
    if model is None:
        return None
    try:
        truncated = text[:_MAX_INPUT_CHARS]
        vec = model.encode(truncated, show_progress_bar=False)
    except Exception as exc:
        _logger.debug("[token-savior:memory] embed failed: %s", exc)
        return None
    try:
        return [float(x) for x in vec]
    except (TypeError, ValueError):
        return None


def is_available() -> bool:
    """True if the embedding model can be loaded. Triggers lazy load."""
    return _load_model() is not None
