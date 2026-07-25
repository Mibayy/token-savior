import io
import json

import pytest

from token_savior import db_core
from token_savior.memory import ledger, preflight_hook


@pytest.fixture
def isolated_db(monkeypatch, tmp_path):
    db_path = tmp_path / "m.sqlite"
    monkeypatch.setattr(db_core, "MEMORY_DB_PATH", db_path)
    db_core.run_migrations(db_path)
    yield db_path


def _stdin(monkeypatch, payload):
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(payload)))


def test_reflex_injects_checklist_and_logs(isolated_db, monkeypatch, capsys):
    _stdin(monkeypatch, {"tool_name": "Bash",
                         "tool_input": {"command": "rm -rf /root/data"},
                         "session_id": "s1"})
    assert preflight_hook.main() == 0
    out = capsys.readouterr().out
    assert "PRÉ-VOL" in out and "destructive-fs" in out
    events = ledger.ledger_query(event_type="preflight", session_id="s1")
    assert len(events) == 1 and events[0]["subject"] == "destructive-fs"


def test_trivial_emits_nothing(isolated_db, monkeypatch, capsys):
    _stdin(monkeypatch, {"tool_name": "Bash",
                         "tool_input": {"command": "git status"}, "session_id": "s2"})
    assert preflight_hook.main() == 0
    assert capsys.readouterr().out == ""
    assert ledger.ledger_query(event_type="preflight", session_id="s2") == []


def test_killswitch(isolated_db, monkeypatch, capsys):
    monkeypatch.setenv("TS_RULES_DISABLE", "1")
    _stdin(monkeypatch, {"tool_name": "Bash",
                         "tool_input": {"command": "rm -rf /x"}})
    assert preflight_hook.main() == 0
    assert capsys.readouterr().out == ""


def test_garbage_stdin_fails_open(isolated_db, monkeypatch, capsys):
    monkeypatch.setattr("sys.stdin", io.StringIO("not json"))
    assert preflight_hook.main() == 0
    assert capsys.readouterr().out == ""


def test_checklist_still_shown_if_logging_fails(isolated_db, monkeypatch, capsys):
    # a ledger failure must not swallow the checklist nor crash the hook
    from token_savior.memory import preflight
    monkeypatch.setattr(preflight, "record_preflight",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("db down")))
    _stdin(monkeypatch, {"tool_name": "Bash",
                         "tool_input": {"command": "rm -rf /root/x"}})
    assert preflight_hook.main() == 0
    assert "PRÉ-VOL" in capsys.readouterr().out
