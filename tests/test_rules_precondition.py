import pytest

from token_savior import db_core
from token_savior.memory import rules


@pytest.fixture
def isolated_db(monkeypatch, tmp_path):
    db_path = tmp_path / "m.sqlite"
    monkeypatch.setattr(db_core, "MEMORY_DB_PATH", db_path)
    db_core.run_migrations(db_path)
    yield db_path


def test_successful_preflight_records_precondition(isolated_db):
    payload = {"tool_name": "Bash",
               "tool_input": {"command": "bash scripts/preflight.sh"},
               "tool_response": {"exit_code": 0}}
    res = rules.record_precondition(payload, session_id="s1")
    assert res is not None
    assert rules.precondition_met("s1", "preflight") is True


def test_failed_preflight_does_not_record(isolated_db):
    payload = {"tool_name": "Bash",
               "tool_input": {"command": "bash scripts/preflight.sh"},
               "tool_response": {"exit_code": 1}}
    assert rules.record_precondition(payload, session_id="s2") is None
    assert rules.precondition_met("s2", "preflight") is False


def test_naming_preflight_does_not_satisfy(isolated_db):
    # Merely reading/naming preflight must NOT satisfy the precondition.
    for cmd in ("cat scripts/preflight.sh", "grep -n foo preflight.sh",
                "echo run preflight now", "ls preflight.sh"):
        payload = {"tool_input": {"command": cmd}, "tool_response": {"exit_code": 0}}
        assert rules.record_precondition(payload, session_id="sN") is None, cmd
    assert rules.precondition_met("sN", "preflight") is False


def test_invoking_preflight_satisfies(isolated_db):
    for cmd in ("bash scripts/preflight.sh", "./preflight.sh", "sh preflight.sh",
                "preflight.sh"):
        payload = {"tool_input": {"command": cmd}, "tool_response": {"exit_code": 0}}
        assert rules.record_precondition(payload, session_id="sY") is not None, cmd


def test_unrelated_command_records_nothing(isolated_db):
    payload = {"tool_name": "Bash",
               "tool_input": {"command": "ls -la"},
               "tool_response": {"exit_code": 0}}
    assert rules.record_precondition(payload, session_id="s3") is None


def test_precondition_is_session_scoped(isolated_db):
    rules.record_precondition(
        {"tool_input": {"command": "preflight"}, "tool_response": {"exit_code": 0}},
        session_id="sA")
    assert rules.precondition_met("sA", "preflight") is True
    assert rules.precondition_met("sB", "preflight") is False


def test_db_backup_precondition_recognized(isolated_db):
    for cmd in ("cp ~/.local/share/token-savior/memory.db memory.db.bak-20260725",
                "sqlite3 memory.db \".backup /tmp/b.db\"",
                "pg_dump mydb > dump.sql"):
        payload = {"tool_input": {"command": cmd}, "tool_response": {"exit_code": 0}}
        assert rules.record_precondition(payload, session_id="sB") is not None, cmd
    assert rules.precondition_met("sB", "db-backup") is True


def test_python_delete_with_where_requires_backup(isolated_db):
    # THE actual 25/07 failure pattern: a python .execute DELETE *with* a WHERE.
    # Rules can't judge the WHERE, but they can guarantee a backup exists first.
    cmd = 'python3 -c "conn.execute(\'DELETE FROM user_prompts WHERE prompt_text IN (x)\')"'
    d = rules.evaluate("Bash", {"command": cmd}, "sX",
                       precondition_check=lambda s, n: False)  # no backup
    assert d["decision"] == "deny"
    assert d["rule_id"] == "backup-before-destructive-db"
    # with a backup this session → allowed
    d2 = rules.evaluate("Bash", {"command": cmd}, "sX",
                        precondition_check=lambda s, n: True)
    assert d2["decision"] == "allow"


def test_echo_of_delete_not_gated(isolated_db):
    # No client / no .execute → not a real DB op → not gated.
    d = rules.evaluate("Bash", {"command": 'echo "delete from users where id=1"'},
                       "sX", precondition_check=lambda s, n: False)
    assert d["decision"] == "allow"


def test_evaluate_end_to_end_with_real_precondition(isolated_db):
    # No precondition yet → push denied.
    push = ("Bash", {"command": "git push origin main"}, "sX")
    catalog = [{
        "id": "preflight-before-push",
        "trigger": {"tool": "Bash", "command_regex": r"git\s+push\b"},
        "action": {"type": "require_precondition", "precondition": "preflight",
                   "message": "preflight d'abord"},
        "severity": "hard",
    }]
    assert rules.evaluate(*push, rules=catalog)["decision"] == "deny"
    # Run preflight successfully → push now allowed.
    rules.record_precondition(
        {"tool_input": {"command": "preflight"}, "tool_response": {"exit_code": 0}},
        session_id="sX")
    assert rules.evaluate(*push, rules=catalog)["decision"] == "allow"
