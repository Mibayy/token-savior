"""ts_search cold-start bridge: when TS_SEARCH_COLD_DELEGATE is on and the
in-process Nomic model is still cold, the first ts_search delegates to a warm
daemon; otherwise (flag off, model warm, or daemon unreachable) it runs the
in-process path unchanged.
"""

from __future__ import annotations

import json

from token_savior import server, server_state


def _call(monkeypatch, *, flag, cold, daemon_returns):
    monkeypatch.setattr(server_state, "_TS_SEARCH_COLD_DELEGATE", flag)
    monkeypatch.setattr(server, "_local_embed_model_cold", lambda: cold)
    from token_savior import daemon_client
    calls = []

    def fake_call(tool, args, **kw):
        calls.append((tool, args))
        return daemon_returns

    monkeypatch.setattr(daemon_client, "call_daemon", fake_call)
    result = server._handle_ts_search({"query": "find dependents", "top_k": 3})
    return result, calls


def test_delegates_when_on_and_cold(monkeypatch):
    result, calls = _call(monkeypatch, flag=True, cold=True, daemon_returns="DAEMON_JSON")
    assert result[0].text == "DAEMON_JSON"
    assert calls and calls[0][0] == "ts_search"


def test_no_delegate_when_flag_off(monkeypatch):
    result, calls = _call(monkeypatch, flag=False, cold=True, daemon_returns="DAEMON_JSON")
    assert calls == []
    # In-process path returns a JSON payload with matched_tools.
    assert "matched_tools" in json.loads(result[0].text)


def test_no_delegate_when_model_warm(monkeypatch):
    result, calls = _call(monkeypatch, flag=True, cold=False, daemon_returns="DAEMON_JSON")
    assert calls == []
    assert "matched_tools" in json.loads(result[0].text)


def test_falls_back_when_daemon_unreachable(monkeypatch):
    result, calls = _call(monkeypatch, flag=True, cold=True, daemon_returns=None)
    assert calls and calls[0][0] == "ts_search"  # attempted
    assert "matched_tools" in json.loads(result[0].text)  # fell back in-process
