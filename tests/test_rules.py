import json

import pytest

from token_savior.memory import rules


DENY = {
    "id": "no-force-push",
    "trigger": {"tool": "Bash", "command_regex": r"git\s+push\b.*(--force|-f)\b"},
    "action": {"type": "deny", "message": "force-push bloqué"},
    "severity": "hard",
}
WARN = {
    "id": "warn-edit-config",
    "trigger": {"tool": "Edit", "file_glob": "*.env"},
    "action": {"type": "warn", "message": "édition d'un .env"},
    "severity": "soft",
}
PREFLIGHT = {
    "id": "preflight-before-push",
    "trigger": {"tool": "Bash", "command_regex": r"git\s+push\b"},
    "action": {"type": "require_precondition", "precondition": "preflight",
               "message": "preflight d'abord"},
    "severity": "hard",
}


def test_load_rules_reads_json(tmp_path):
    p = tmp_path / "r.json"
    p.write_text(json.dumps([DENY]))
    loaded = rules.load_rules(p)
    assert len(loaded) == 1 and loaded[0]["id"] == "no-force-push"


def test_load_rules_missing_file_is_empty(tmp_path):
    assert rules.load_rules(tmp_path / "nope.json") == []


def test_match_command_regex():
    m = rules.match("Bash", {"command": "git push --force origin main"}, [DENY])
    assert [r["id"] for r in m] == ["no-force-push"]
    assert rules.match("Bash", {"command": "git push origin feat"}, [DENY]) == []


def test_match_respects_tool():
    assert rules.match("Read", {"command": "git push -f"}, [DENY]) == []


def test_match_file_glob():
    assert rules.match("Edit", {"file_path": "/a/.env"}, [WARN])
    assert rules.match("Edit", {"file_path": "/a/main.py"}, [WARN]) == []


def test_evaluate_deny():
    d = rules.evaluate("Bash", {"command": "git push -f origin main"}, "s", rules=[DENY])
    assert d["decision"] == "deny"
    assert d["rule_id"] == "no-force-push"
    assert "force-push" in d["reason"]


def test_evaluate_allow_when_no_match():
    d = rules.evaluate("Bash", {"command": "ls -la"}, "s", rules=[DENY])
    assert d["decision"] == "allow"
    assert d["rule_id"] is None


def test_evaluate_warn_allows_with_reason():
    d = rules.evaluate("Edit", {"file_path": "/a/.env"}, "s", rules=[WARN])
    assert d["decision"] == "allow"
    assert d["rule_id"] == "warn-edit-config"
    assert d["reason"]


def test_evaluate_require_precondition_denies_when_unmet():
    d = rules.evaluate("Bash", {"command": "git push origin main"}, "s",
                       rules=[PREFLIGHT], precondition_check=lambda sid, n: False)
    assert d["decision"] == "deny"
    assert d["rule_id"] == "preflight-before-push"


def test_evaluate_require_precondition_allows_when_met():
    d = rules.evaluate("Bash", {"command": "git push origin main"}, "s",
                       rules=[PREFLIGHT], precondition_check=lambda sid, n: True)
    assert d["decision"] == "allow"


def test_evaluate_require_precondition_allows_without_session():
    # No session_id → cannot verify → fail OPEN (allow), never block.
    d = rules.evaluate("Bash", {"command": "git push origin main"}, None,
                       rules=[PREFLIGHT], precondition_check=lambda sid, n: False)
    assert d["decision"] == "allow"


def test_evaluate_deny_wins_over_precondition_met():
    # A hard deny and a satisfied precondition both match; deny still wins.
    d = rules.evaluate("Bash", {"command": "git push --force origin main"}, "s",
                       rules=[PREFLIGHT, DENY], precondition_check=lambda sid, n: True)
    assert d["decision"] == "deny"
    assert d["rule_id"] == "no-force-push"
