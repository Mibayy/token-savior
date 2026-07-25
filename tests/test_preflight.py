from token_savior.memory import preflight as pf


def _c(cmd):
    return pf.classify_action("Bash", {"command": cmd})


def test_irreversible_actions_trigger_reflex():
    for cmd in ["rm -rf /root/data", "systemctl stop intel-api",
                "systemctl disable gw2cc", "psql -c 'DROP TABLE users'",
                "git push origin main", "chmod -R 777 /etc",
                "vercel deploy --prod"]:
        r = _c(cmd)
        assert r["level"] == "reflex", cmd
        assert len(r["checklist"]) >= 2


def test_trivial_actions_no_reflex():
    for cmd in ["ls -la", "git status", "cat file.py", "pytest -q",
                "grep foo bar", "echo hello", "git diff"]:
        r = _c(cmd)
        assert r["level"] == "none", cmd
        assert r["checklist"] == []


def test_checklist_mentions_reversibility_and_intent():
    r = _c("rm -rf /root/x")
    text = " ".join(r["checklist"]).lower()
    assert "réversib" in text or "reversib" in text
    assert "intention" in text or "intent" in text


def test_non_bash_tools_are_not_reflex():
    assert pf.classify_action("Read", {"file_path": "/x"})["level"] == "none"
    assert pf.classify_action("Grep", {"pattern": "rm -rf"})["level"] == "none"


def test_reflex_category_is_reported():
    assert _c("rm -rf /x")["category"] == "destructive-fs"
    assert _c("systemctl restart foo")["category"] == "service"
    assert _c("git push origin main")["category"] == "publish"


def test_echo_of_dangerous_pattern_not_reflex():
    # a dangerous pattern only inside a string/echo is not an actual action
    assert _c('echo "rm -rf /root/data"')["level"] == "none"
