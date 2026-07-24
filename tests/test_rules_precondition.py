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
