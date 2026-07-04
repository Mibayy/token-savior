"""Persisted nudge-fire telemetry: lets a later audit compare how often each
nudge fired against whether the targeted tool's adoption rose.
"""

from __future__ import annotations

from token_savior import telemetry


def _fresh(monkeypatch, tmp_path):
    monkeypatch.setenv("TOKEN_SAVIOR_STATS_DIR", str(tmp_path))
    # Drop the in-process caches so the isolated dir is picked up.
    telemetry._nudge_state = None
    return telemetry


def test_record_and_read_roundtrip(monkeypatch, tmp_path):
    t = _fresh(monkeypatch, tmp_path)
    t.record_nudge("edit_context")
    t.record_nudge("edit_context")
    t.record_nudge("ts_execute")
    assert t.nudge_counts() == {"edit_context": 2, "ts_execute": 1}


def test_empty_kind_ignored(monkeypatch, tmp_path):
    t = _fresh(monkeypatch, tmp_path)
    t.record_nudge("")
    assert t.nudge_counts() == {}


def test_persists_across_reload(monkeypatch, tmp_path):
    t = _fresh(monkeypatch, tmp_path)
    t.record_nudge("find_then_read")
    # Simulate a fresh process: clear cache, re-read from disk.
    t._nudge_state = None
    assert t.nudge_counts() == {"find_then_read": 1}


def test_fire_nudge_records(monkeypatch, tmp_path):
    from token_savior import server
    monkeypatch.setenv("TOKEN_SAVIOR_STATS_DIR", str(tmp_path))
    telemetry._nudge_state = None
    text = server._fire_nudge("edit_context", "[NUDGE] hi")
    assert text == "[NUDGE] hi"
    assert telemetry.nudge_counts().get("edit_context") == 1
