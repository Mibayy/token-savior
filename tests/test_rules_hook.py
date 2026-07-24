import io
import json

import pytest

from token_savior import db_core
from token_savior.memory import ledger, precondition_hook, rules_hook


@pytest.fixture
def isolated_db(monkeypatch, tmp_path):
    db_path = tmp_path / "m.sqlite"
    monkeypatch.setattr(db_core, "MEMORY_DB_PATH", db_path)
    db_core.run_migrations(db_path)
    yield db_path


def _stdin(monkeypatch, payload):
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(payload)))


def test_deny_emits_deny_json_and_logs(isolated_db, monkeypatch, capsys):
    _stdin(monkeypatch, {"tool_name": "Bash",
                         "tool_input": {"command": "git push --force origin main"},
                         "session_id": "s1"})
    rc = rules_hook.main()
    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    assert out["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert "force-push" in out["hookSpecificOutput"]["permissionDecisionReason"].lower()
    blocks = ledger.ledger_query(event_type="hard_block", session_id="s1")
    assert len(blocks) == 1 and blocks[0]["subject"] == "no-force-push-protected"


def test_allow_emits_nothing(isolated_db, monkeypatch, capsys):
    _stdin(monkeypatch, {"tool_name": "Bash",
                         "tool_input": {"command": "ls -la"}, "session_id": "s2"})
    assert rules_hook.main() == 0
    assert capsys.readouterr().out == ""


def test_killswitch_allows(isolated_db, monkeypatch, capsys):
    monkeypatch.setenv("TS_RULES_DISABLE", "1")
    _stdin(monkeypatch, {"tool_name": "Bash",
                         "tool_input": {"command": "git push --force origin main"}})
    assert rules_hook.main() == 0
    assert capsys.readouterr().out == ""


def test_garbage_stdin_fails_open(isolated_db, monkeypatch, capsys):
    monkeypatch.setattr("sys.stdin", io.StringIO("not json"))
    assert rules_hook.main() == 0
    assert capsys.readouterr().out == ""


def test_precondition_hook_records(isolated_db, monkeypatch):
    _stdin(monkeypatch, {"tool_name": "Bash",
                         "tool_input": {"command": "bash preflight.sh"},
                         "tool_response": {"exit_code": 0}, "session_id": "s3"})
    assert precondition_hook.main() == 0
    from token_savior.memory import rules
    assert rules.precondition_met("s3", "preflight") is True
