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
    yield
    server_state._chain_calls.clear()
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
