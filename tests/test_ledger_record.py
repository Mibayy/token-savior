import pytest

from token_savior import db_core
from token_savior.memory import ledger


@pytest.fixture
def isolated_db(monkeypatch, tmp_path):
    db_path = tmp_path / "m.sqlite"
    monkeypatch.setattr(db_core, "MEMORY_DB_PATH", db_path)
    db_core.run_migrations(db_path)
    yield db_path


def test_record_writes_miss_on_correction(isolated_db):
    res = ledger.record_from_userprompt(
        {"prompt": "je t'ai déjà dit de regarder les logs"},
        session_id="s1")
    assert res is not None and res["id"] > 0

    misses = ledger.ledger_query(event_type="miss")
    assert len(misses) == 1
    assert misses[0]["meta"]["phrase"] == "je t'ai déjà dit"
    assert misses[0]["session_id"] == "s1"


def test_record_returns_none_without_correction(isolated_db):
    res = ledger.record_from_userprompt({"prompt": "ajoute une fonction"})
    assert res is None
    assert ledger.ledger_query(event_type="miss") == []
