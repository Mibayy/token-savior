"""End-to-end integration tests for server.py call_tool dispatch."""

from __future__ import annotations

import asyncio
import os
import time


from token_savior.server import call_tool
from token_savior.server_state import _slot_mgr
from token_savior.cache_ops import CacheManager


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _run(coro):
    """Run an async coroutine synchronously."""
    return asyncio.new_event_loop().run_until_complete(coro)


def _setup_project(tmp_path, files: dict[str, str] | None = None):
    """Register a temp directory as a project and index it via call_tool."""
    if files is None:
        files = {
            "main.py": (
                "def hello():\n"
                "    return 'world'\n"
                "\n"
                "def greet(name):\n"
                "    return f'Hello {name}'\n"
            ),
        }
    for name, content in files.items():
        fpath = tmp_path / name
        fpath.parent.mkdir(parents=True, exist_ok=True)
        fpath.write_text(content, encoding="utf-8")
    return str(tmp_path)


def _text(result):
    """Extract text from a call_tool result."""
    assert len(result) >= 1
    return result[0].text


def _cleanup_slot(root: str):
    """Remove a slot from the global slot manager after a test."""
    _slot_mgr.projects.pop(root, None)
    if _slot_mgr.active_root == root:
        _slot_mgr.active_root = ""


# ---------------------------------------------------------------------------
# Test 1: Full flow -- set_project_root + get_project_summary
# ---------------------------------------------------------------------------


class TestFullFlow:
    def test_set_project_root_then_get_project_summary(self, tmp_path):
        root = _setup_project(tmp_path)
        try:
            # Register and index
            result = _run(call_tool("set_project_root", {"path": root}))
            text = _text(result)
            assert "Added and indexed" in text

            # Get summary
            result = _run(call_tool("get_project_summary", {}))
            text = _text(result)
            # Should mention the file count (at least 1 file)
            assert "1" in text or "file" in text.lower()
        finally:
            _cleanup_slot(root)


# ---------------------------------------------------------------------------
# Test 2: Incremental update -- modify a file, find the new symbol
# ---------------------------------------------------------------------------


class TestIncrementalUpdate:
    def test_find_symbol_after_file_modification(self, tmp_path):
        root = _setup_project(tmp_path)
        try:
            # Initial index
            _run(call_tool("set_project_root", {"path": root}))

            # Modify the file: add a new function
            main_py = tmp_path / "main.py"
            original = main_py.read_text(encoding="utf-8")
            main_py.write_text(
                original + "\ndef new_func():\n    return 42\n",
                encoding="utf-8",
            )
            # Ensure mtime is different (some filesystems have 1s granularity)
            future_time = time.time() + 2
            os.utime(str(main_py), (future_time, future_time))

            # find_symbol triggers _prep -> maybe_update -> mtime check
            result = _run(call_tool("find_symbol", {"name": "new_func"}))
            text = _text(result)
            # Should find the new function
            assert "new_func" in text
        finally:
            _cleanup_slot(root)


# ---------------------------------------------------------------------------
# Test 3: Cache roundtrip -- build, save, load in new CacheManager
# ---------------------------------------------------------------------------


class TestCacheRoundtrip:
    def test_save_and_reload_cache(self, tmp_path):
        root = _setup_project(tmp_path)
        try:
            # Build index via call_tool
            _run(call_tool("set_project_root", {"path": root}))

            slot = _slot_mgr.projects[root]
            assert slot.indexer is not None
            idx = slot.indexer._project_index
            assert idx is not None

            # Save cache
            cache_mgr = CacheManager(root, cache_version=2)
            cache_mgr.save(idx)

            # Verify cache file exists
            cache_path = cache_mgr.path()
            assert os.path.exists(cache_path)

            # Load in a fresh CacheManager
            cache_mgr2 = CacheManager(root, cache_version=2)
            loaded_idx = cache_mgr2.load()
            assert loaded_idx is not None
            assert loaded_idx.total_files == idx.total_files
            assert loaded_idx.total_functions == idx.total_functions
            assert loaded_idx.root_path == idx.root_path
        finally:
            # Clean up cache file
            cache_path = os.path.join(root, CacheManager.FILENAME)
            if os.path.exists(cache_path):
                os.remove(cache_path)
            _cleanup_slot(root)


# ---------------------------------------------------------------------------
# Test 4: Unknown tool returns error
# ---------------------------------------------------------------------------


class TestUnknownTool:
    def test_unknown_tool_returns_error(self):
        result = _run(call_tool("nonexistent_tool", {}))
        text = _text(result)
        assert "unknown tool" in text.lower() or "Error" in text


# ---------------------------------------------------------------------------
# Test 5: Path traversal in create_checkpoint is rejected
# ---------------------------------------------------------------------------


class TestPathTraversal:
    def test_create_checkpoint_rejects_traversal(self, tmp_path):
        root = _setup_project(tmp_path)
        try:
            _run(call_tool("set_project_root", {"path": root}))

            result = _run(
                call_tool(
                    "create_checkpoint",
                    {"file_paths": ["../../../etc/passwd"], "label": "evil"},
                )
            )
            text = _text(result)
            # Should contain an error about unsafe path
            assert "unsafe" in text.lower() or "error" in text.lower()
        finally:
            _cleanup_slot(root)


class TestSessionResultCache:
    def test_find_symbol_second_call_is_cached(self, tmp_path):
        from token_savior import server_state as s

        root = _setup_project(tmp_path)
        try:
            _run(call_tool("set_project_root", {"path": root}))
            hits_before = s._src_hits
            _run(call_tool("find_symbol", {"name": "hello"}))
            _run(call_tool("find_symbol", {"name": "hello"}))
            assert s._src_hits == hits_before + 1
        finally:
            _cleanup_slot(root)

    def test_cache_invalidated_when_cache_gen_bumps(self, tmp_path):
        from token_savior import server_state as s

        root = _setup_project(tmp_path)
        try:
            _run(call_tool("set_project_root", {"path": root}))
            _run(call_tool("find_symbol", {"name": "hello"}))
            slot = s._slot_mgr.projects[root]
            slot.cache_gen += 1
            hits_before = s._src_hits
            _run(call_tool("find_symbol", {"name": "hello"}))
            # After cache_gen bump, key differs → miss, not hit
            assert s._src_hits == hits_before
        finally:
            _cleanup_slot(root)


class TestRequestTracing:
    """Coverage for issue #27: TOKEN_SAVIOR_TRACE=1 emits stderr lifecycle
    events around every MCP tool call so users on platforms with timing-
    sensitive transports (Windows stdio, slow bridges) can correlate hangs
    with concrete request boundaries."""

    def test_trace_disabled_by_default_emits_nothing(self, tmp_path, capsys, monkeypatch):
        # Force a clean reload of the module-level _TRACE_REQUESTS flag.
        monkeypatch.delenv("TOKEN_SAVIOR_TRACE", raising=False)
        import importlib
        from token_savior import server as srv

        importlib.reload(srv)

        root = _setup_project(tmp_path)
        try:
            _run(srv.call_tool("set_project_root", {"path": root}))
            _run(srv.call_tool("find_symbol", {"name": "hello"}))
        finally:
            _cleanup_slot(root)

        err = capsys.readouterr().err
        assert "[token-savior] -> call" not in err
        assert "[token-savior] <- ok" not in err

    def test_trace_enabled_logs_start_and_complete(self, tmp_path, capsys, monkeypatch):
        monkeypatch.setenv("TOKEN_SAVIOR_TRACE", "1")
        import importlib
        from token_savior import server as srv

        importlib.reload(srv)

        root = _setup_project(tmp_path)
        try:
            _run(srv.call_tool("set_project_root", {"path": root}))
            _run(srv.call_tool("find_symbol", {"name": "hello"}))
        finally:
            _cleanup_slot(root)
            # Reset module so other tests don't see TRACE behaviour.
            monkeypatch.delenv("TOKEN_SAVIOR_TRACE", raising=False)
            importlib.reload(srv)

        err = capsys.readouterr().err
        assert "[token-savior] -> call set_project_root" in err
        assert "[token-savior] <- ok   set_project_root" in err
        assert "[token-savior] -> call find_symbol" in err
        assert "[token-savior] <- ok   find_symbol" in err
