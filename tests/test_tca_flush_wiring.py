"""TCA co-activation was DEAD (session_count stuck at 0) because flush_session
was never called. It is now flushed at each switch_project boundary and at
server exit. These tests lock the wiring in.
"""

from __future__ import annotations

from token_savior import server, server_state


class _Spy:
    def __init__(self):
        self.flushed = 0

    def flush_session(self):
        self.flushed += 1
        return 0

    def record_activation(self, _sym):
        pass


def test_switch_project_flushes_tca(monkeypatch):
    spy = _Spy()
    monkeypatch.setattr(server_state, "_tca_engine", spy)
    monkeypatch.setattr(server_state, "_auto_save_enabled", False)
    server._track_call("switch_project", {})
    assert spy.flushed == 1


def test_non_switch_call_does_not_flush(monkeypatch):
    spy = _Spy()
    monkeypatch.setattr(server_state, "_tca_engine", spy)
    server._track_call("find_symbol", {"name": "x"})
    assert spy.flushed == 0


def test_safe_flush_swallows_errors(monkeypatch):
    class Boom:
        def flush_session(self):
            raise RuntimeError("disk full")

    monkeypatch.setattr(server_state, "_tca_engine", Boom())
    server._safe_flush_tca()  # must not raise
