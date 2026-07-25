from scripts import rules_backtest

CATALOG = [
    {"id": "no-force-push-protected",
     "trigger": {"tool": "Bash", "command_regex":
                 r"(?=.*(?:^|[;&|]\s*)git\s+push\b)(?=.*(?:--force\b))(?=.*\b(?:main|master)\b)"},
     "action": {"type": "deny", "message": "x"}, "severity": "hard"},
    {"id": "preflight-before-push",
     "trigger": {"tool": "Bash", "command_regex": r"(?:^|[;&|]\s*)git\s+push\b"},
     "action": {"type": "require_precondition", "precondition": "preflight", "message": "y"},
     "severity": "hard"},
]


def test_backtest_counts_deny_hits_and_pushes():
    cmds = [
        "git push --force origin main",   # deny (force-push)
        "git push origin feat",           # push, no force → counts as push only
        "ls -la",                         # nothing
        "echo git push --force main",     # mention only → nothing
    ]
    res = rules_backtest.backtest(cmds, catalog=CATALOG)
    assert res["analyzed"] == 4
    assert [h["rule_id"] for h in res["deny_hits"]] == ["no-force-push-protected"]
    assert res["deny_hits"][0]["command"] == "git push --force origin main"
    # both real git pushes counted; the echo mention is not
    assert res["push_count"] == 2


def test_clean_backtest_has_no_deny_hits():
    res = rules_backtest.backtest(["ls", "pytest -q", "git status"], catalog=CATALOG)
    assert res["deny_hits"] == []
    assert res["push_count"] == 0
