"""Chain-nudge: prepend a top-of-payload notice when a wasteful tool chain is
detected (e.g. find_symbol(X) -> get_function_source(X) within 60s).

Data from 9 days of usage (2026-05-17..26) showed 42 find_symbol ->
get_function_source same-symbol chains and 26 find_symbol -> get_full_context
chains. Trailing _hints were ignored; the nudge is prepended to land above
the payload (compressors keep the head, drop the tail).
"""

from __future__ import annotations

import asyncio

import pytest

from token_savior import server, server_state


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


@pytest.fixture(autouse=True)
def _reset_chain_state(tmp_path):
    server_state._chain_calls.clear()
    server_state._chain_nudges_emitted = 0
    prev_active = server_state._slot_mgr.active_root
    prev_keys = set(server_state._slot_mgr.projects)
    # Snapshot _tool_call_counts so chain-nudge calls don't push the global
    # over the navigation-overuse threshold (15) and flip code_nav._stop_hint
    # behavior in unrelated tests (test_query_api::test_navigation_hints_*).
    prev_counts = dict(server_state._tool_call_counts)
    yield
    server_state._chain_calls.clear()
    server_state._tool_call_counts.clear()
    server_state._tool_call_counts.update(prev_counts)
    # Drop any tmp_path slots this test created so the next test sees a clean
    # SlotManager (memory_viewer tests resolve the active project at search
    # time -- a stale active_root pointing into a deleted tmp_path silently
    # returns 0 results).
    for key in list(server_state._slot_mgr.projects):
        if key not in prev_keys:
            server_state._slot_mgr.projects.pop(key, None)
    server_state._slot_mgr.active_root = prev_active


def _setup_project(tmp_path):
    (tmp_path / "main.py").write_text(
        "def hello():\n    return 'world'\n"
        "def greet(name):\n    return f'Hello {name}'\n",
        encoding="utf-8",
    )
    return str(tmp_path)


class TestChainNudge:
    def test_find_then_get_source_emits_nudge(self, tmp_path):
        root = _setup_project(tmp_path)
        _run(server.call_tool("set_project_root", {"path": root}))
        _run(server.call_tool("find_symbol", {"name": "hello"}))
        result = _run(server.call_tool("get_function_source", {"name": "hello"}))

        head = result[0].text
        assert head.startswith("[NUDGE]"), f"first item must be the nudge: {head!r}"
        assert "get_full_context('hello')" in head
        assert server_state._chain_nudges_emitted == 1
        # Payload must still be present after the nudge.
        assert len(result) >= 2

    def test_no_nudge_when_different_symbol(self, tmp_path):
        root = _setup_project(tmp_path)
        _run(server.call_tool("set_project_root", {"path": root}))
        _run(server.call_tool("find_symbol", {"name": "hello"}))
        result = _run(server.call_tool("get_function_source", {"name": "greet"}))

        assert not result[0].text.startswith("[NUDGE]")
        assert server_state._chain_nudges_emitted == 0

    def test_no_nudge_for_first_call(self, tmp_path):
        root = _setup_project(tmp_path)
        _run(server.call_tool("set_project_root", {"path": root}))
        result = _run(server.call_tool("get_function_source", {"name": "hello"}))
        assert not result[0].text.startswith("[NUDGE]")
        assert server_state._chain_nudges_emitted == 0

    def test_no_nudge_outside_window(self, tmp_path, monkeypatch):
        root = _setup_project(tmp_path)
        _run(server.call_tool("set_project_root", {"path": root}))
        _run(server.call_tool("find_symbol", {"name": "hello"}))

        # Backdate the find_symbol entry past the 60s window.
        ts_old = server_state._chain_calls[-1][0] - 120.0
        tool, sym = server_state._chain_calls[-1][1], server_state._chain_calls[-1][2]
        server_state._chain_calls[-1] = (ts_old, tool, sym)

        result = _run(server.call_tool("get_function_source", {"name": "hello"}))
        assert not result[0].text.startswith("[NUDGE]")
        assert server_state._chain_nudges_emitted == 0

    def test_read_then_full_context_emits_nudge(self, tmp_path):
        # Pattern 2: get_function_source(X) -> get_full_context(X).
        # 9-day data: 187 occurrences -- dominant remaining wasteful chain.
        root = _setup_project(tmp_path)
        _run(server.call_tool("set_project_root", {"path": root}))
        _run(server.call_tool("get_function_source", {"name": "hello"}))
        result = _run(server.call_tool("get_full_context", {"name": "hello"}))

        head = result[0].text
        assert head.startswith("[NUDGE]"), f"first item must be nudge: {head!r}"
        assert "get_function_source('hello')" in head
        assert "get_full_context('hello')" in head
        assert "Start with get_full_context" in head
        assert server_state._chain_nudges_emitted == 1

    def test_get_class_source_then_full_context_emits_nudge(self, tmp_path):
        # get_class_source -> get_full_context is the same wasteful pattern.
        (tmp_path / "models.py").write_text(
            "class User:\n    def __init__(self):\n        self.name = ''\n",
            encoding="utf-8",
        )
        _run(server.call_tool("set_project_root", {"path": str(tmp_path)}))
        _run(server.call_tool("get_class_source", {"name": "User"}))
        result = _run(server.call_tool("get_full_context", {"name": "User"}))
        head = result[0].text
        assert head.startswith("[NUDGE]")
        assert "get_class_source('User')" in head
        assert server_state._chain_nudges_emitted == 1

    def test_disable_via_env(self, tmp_path, monkeypatch):
        monkeypatch.setattr(server_state, "_CHAIN_NUDGE_DISABLED", True)
        root = _setup_project(tmp_path)
        _run(server.call_tool("set_project_root", {"path": root}))
        _run(server.call_tool("find_symbol", {"name": "hello"}))
        result = _run(server.call_tool("get_function_source", {"name": "hello"}))
        assert not result[0].text.startswith("[NUDGE]")
        # Buffer should also stay empty since the disable flag short-circuits push.
        # (find_symbol call still triggered push? Check: yes the push is gated.)
        # We just assert nudge wasn't emitted.
        assert server_state._chain_nudges_emitted == 0


class TestEditContextNudge:
    """Pattern 3: edit tool without a preceding get_edit_context on the symbol.

    Audit 2026-07-04: 0 get_edit_context calls across ~199 edits. Editing blind
    risks breaking callers the agent never looked at.
    """

    def _seed(self, entries):
        import time
        server_state._chain_calls.clear()
        now = time.monotonic()
        for dt, tool, sym in entries:
            server_state._chain_calls.append((now + dt, tool, sym))

    def test_edit_without_context_emits_nudge(self):
        self._seed([(0.0, "replace_symbol_source", "foo")])
        nudge = server._detect_chain_nudge("replace_symbol_source", "foo")
        assert nudge and "get_edit_context('foo')" in nudge

    def test_edit_with_context_no_nudge(self):
        self._seed([(-1.0, "get_edit_context", "foo"), (0.0, "replace_symbol_source", "foo")])
        assert server._detect_chain_nudge("replace_symbol_source", "foo") is None

    def test_edit_context_different_symbol_still_nudges(self):
        self._seed([(-1.0, "get_edit_context", "bar"), (0.0, "replace_symbol_source", "foo")])
        nudge = server._detect_chain_nudge("replace_symbol_source", "foo")
        assert nudge and "get_edit_context('foo')" in nudge

    def test_context_outside_window_still_nudges(self):
        self._seed([(-120.0, "get_edit_context", "foo"), (0.0, "replace_symbol_source", "foo")])
        assert server._detect_chain_nudge("replace_symbol_source", "foo") is not None


class TestTsExecuteNudge:
    """Pattern 4: many individual nav calls in a window -> suggest Code Mode."""

    def _seed_nav(self, n):
        import time
        server_state._chain_calls.clear()
        now = time.monotonic()
        for _ in range(n):
            server_state._chain_calls.append((now, "search_codebase", ""))

    def test_fifth_nav_call_nudges(self):
        self._seed_nav(5)
        nudge = server._detect_chain_nudge("search_codebase", "")
        assert nudge and "ts_execute" in nudge

    def test_four_nav_calls_no_nudge(self):
        self._seed_nav(4)
        assert server._detect_chain_nudge("search_codebase", "") is None

    def test_sixth_nav_call_no_repeat(self):
        # Fires once at the threshold, not on every subsequent call.
        self._seed_nav(6)
        assert server._detect_chain_nudge("search_codebase", "") is None
