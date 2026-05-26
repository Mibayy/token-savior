"""Verify warm_up_async populates the ts_search embedding cache off-thread.

9 days of usage (2026-05-17..26) showed ts_search avg 4867ms. The cold start
is dominated by Nomic model load + 66 tool-description embeddings on first
call. warm_up_async() moves that cost to server startup so the first client
call sees a populated cache.
"""

from __future__ import annotations

import threading
import time

from token_savior.server_handlers import tool_search


def _reset_cache():
    tool_search._TOOL_EMBED_CACHE = None
    tool_search._TOOL_DESCRIPTIONS = None


def test_warm_up_populates_cache_sync():
    _reset_cache()
    tool_search.warm_up()
    # _build_embedding_cache always returns at least the descriptions dict,
    # even when fastembed is missing (embeddings dict ends up empty in that
    # path). Either way the cache must be non-None after warm_up().
    assert tool_search._TOOL_EMBED_CACHE is not None
    assert tool_search._TOOL_DESCRIPTIONS is not None
    assert len(tool_search._TOOL_DESCRIPTIONS) > 0


def test_warm_up_async_runs_off_thread():
    _reset_cache()
    main_thread = threading.current_thread()
    tool_search.warm_up_async()
    # Give the background thread time to finish. The cache build is fast when
    # fastembed isn't available (substring fallback) and < 10s when it is.
    deadline = time.monotonic() + 15.0
    while tool_search._TOOL_EMBED_CACHE is None and time.monotonic() < deadline:
        time.sleep(0.05)
    assert tool_search._TOOL_EMBED_CACHE is not None, "warm thread never populated cache"
    # And it must not have run on the main thread.
    assert threading.current_thread() is main_thread


def test_warm_up_is_idempotent():
    _reset_cache()
    tool_search.warm_up()
    cache_id = id(tool_search._TOOL_EMBED_CACHE)
    tool_search.warm_up()
    # Second call must not rebuild -- _ensure_cache short-circuits.
    assert id(tool_search._TOOL_EMBED_CACHE) == cache_id
