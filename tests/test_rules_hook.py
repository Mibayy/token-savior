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


def test_deny_force_push_trailing_flag(isolated_db, monkeypatch, capsys):
    # Real catalog: the flag AFTER the branch must still be blocked.
    _stdin(monkeypatch, {"tool_name": "Bash",
                         "tool_input": {"command": "git push origin main --force"},
                         "session_id": "sT"})
    assert rules_hook.main() == 0
    out = json.loads(capsys.readouterr().out)
    assert out["hookSpecificOutput"]["permissionDecision"] == "deny"


def test_deny_sudo_prefixed_force_push(isolated_db, monkeypatch, capsys):
    # sudo prefix must NOT bypass the hard-deny anchor.
    _stdin(monkeypatch, {"tool_name": "Bash",
                         "tool_input": {"command": "sudo git push --force origin main"},
                         "session_id": "sSudo"})
    assert rules_hook.main() == 0
    out = json.loads(capsys.readouterr().out)
    assert out["hookSpecificOutput"]["permissionDecision"] == "deny"


def test_allow_emits_nothing(isolated_db, monkeypatch, capsys):
    _stdin(monkeypatch, {"tool_name": "Bash",
                         "tool_input": {"command": "ls -la"}, "session_id": "s2"})
    assert rules_hook.main() == 0
    assert capsys.readouterr().out == ""


def test_non_executing_mention_not_blocked(isolated_db, monkeypatch, capsys):
    # Real catalog: a force-push pattern that only APPEARS in a string (echo,
    # test, script) must NOT be blocked — the rule anchors to actual execution.
    for cmd in ('echo git push origin main --force',
                'python3 -c "x git push origin main --force y"'):
        _stdin(monkeypatch, {"tool_name": "Bash", "tool_input": {"command": cmd},
                             "session_id": "sM"})
        assert rules_hook.main() == 0
        assert capsys.readouterr().out == "", cmd


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
