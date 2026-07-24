from scripts import neuron_miner as nm


def test_looks_like_error_detects_common_failures():
    for out in ["Traceback (most recent call last):", "bash: foo: command not found",
                "fatal: not a git repository", "No such file or directory",
                "ERROR: could not connect", "npm ERR! code E404"]:
        assert nm.looks_like_error(out), out


def test_looks_like_error_ignores_clean_output():
    for out in ["Hello world", "3 passed in 1.2s", "", "commit a1b2c3 done"]:
        assert not nm.looks_like_error(out), out


def test_cluster_corrections_groups_and_counts():
    prompts = [
        "je t'ai déjà dit de regarder les logs",
        "je t'ai déjà dit de checker les logs applicatifs",
        "tu devais me prévenir sur telegram",
        "ajoute une fonction ici",  # not a correction
    ]
    clusters = nm.cluster_corrections(prompts)
    phrases = {c["phrase"] for c in clusters}
    assert "je t'ai déjà dit" in phrases
    jt = next(c for c in clusters if c["phrase"] == "je t'ai déjà dit")
    assert jt["count"] == 2
    assert len(jt["examples"]) == 2


def test_top_error_commands_groups_failures():
    caps = [
        {"command": "systemctl status foo", "output": "Unit foo not found"},
        {"command": "systemctl status foo", "output": "fatal: whatever"},
        {"command": "ls", "output": "a  b  c"},  # clean → excluded
    ]
    hits = nm.top_error_commands(caps)
    assert len(hits) == 1
    assert hits[0]["count"] == 2
    assert "systemctl status foo" in hits[0]["command"]
