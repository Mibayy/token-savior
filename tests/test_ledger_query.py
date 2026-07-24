import pytest
from token_savior import db_core
from token_savior.memory import ledger


@pytest.fixture
def isolated_db(monkeypatch, tmp_path):
    db_path = tmp_path / "m.sqlite"
    monkeypatch.setattr(db_core, "MEMORY_DB_PATH", db_path)
    db_core.run_migrations(db_path)
    yield db_path


def test_query_filters_by_type(isolated_db):
    ledger.ledger_put("miss", subject="a")
    ledger.ledger_put("injection", subject="b", cost_tokens=120)
    ledger.ledger_put("miss", subject="c")

    misses = ledger.ledger_query(event_type="miss")
    assert len(misses) == 2
    assert {m["subject"] for m in misses} == {"a", "c"}
    assert all(m["event_type"] == "miss" for m in misses)

    inj = ledger.ledger_query(event_type="injection")
    assert inj[0]["cost_tokens"] == 120
