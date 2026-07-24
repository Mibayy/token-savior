import pytest

from token_savior import db_core, memory_db
from scripts import neuron_miner as nm


@pytest.fixture
def isolated_db(monkeypatch, tmp_path):
    db_path = tmp_path / "m.sqlite"
    monkeypatch.setattr(db_core, "MEMORY_DB_PATH", db_path)
    monkeypatch.setattr(memory_db, "MEMORY_DB_PATH", db_path)
    db_core.run_migrations(db_path)
    yield db_path


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
    jt = next(c for c in clusters if c["phrase"] == "je t'ai déjà dit")
    assert jt["count"] == 2


def test_detect_correction_loose_catches_implicit():
    for txt in ["en fait il manque plein de choses", "t'as oublié de me prévenir",
                "check tes logs d'hier soir", "je t'avais demandé autre chose",
                "combien de fois faut-il te le dire"]:
        assert nm.detect_correction_loose(txt) is not None, txt
    for txt in ["ajoute une fonction", "merci c'est parfait", ""]:
        assert nm.detect_correction_loose(txt) is None, txt


def test_top_error_commands_excludes_viewers():
    caps = [
        {"command": "systemctl status foo", "output": "Unit foo not found"},
        {"command": "systemctl status foo", "output": "fatal: whatever"},
        {"command": "gh run view 123 --log-failed | tail", "output": "FAILED step"},
        {"command": "journalctl -u bar", "output": "error error error"},
        {"command": "ls", "output": "a  b  c"},  # clean
    ]
    hits = nm.top_error_commands(caps)
    # only the real systemctl failure survives; viewers excluded
    assert len(hits) == 1
    assert hits[0]["count"] == 2
    assert "systemctl status foo" in hits[0]["command"]


def test_backfill_dry_run_writes_nothing(isolated_db):
    memory_db.prompt_save(None, "/proj", "je t'ai déjà dit de regarder les logs applicatifs")
    memory_db.prompt_save(None, "/proj", "ajoute une fonction ici")  # not a correction
    res = nm.backfill_misses(write=False)
    assert res["total_detected"] == 1
    assert res["written"] == 0
    from token_savior.memory import ledger
    assert ledger.ledger_query(event_type="miss") == []  # nothing persisted


def test_backfill_write_is_additive_and_idempotent(isolated_db):
    memory_db.prompt_save(None, "/proj", "je t'ai déjà dit de regarder les logs applicatifs")
    from token_savior.memory import ledger
    r1 = nm.backfill_misses(write=True)
    assert r1["written"] == 1
    assert len(ledger.ledger_query(event_type="miss")) == 1
    # re-run: idempotent, no duplicate
    r2 = nm.backfill_misses(write=True)
    assert r2["written"] == 0
    assert len(ledger.ledger_query(event_type="miss")) == 1
