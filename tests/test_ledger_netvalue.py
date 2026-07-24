import pytest
from token_savior import db_core
from token_savior.memory import ledger


@pytest.fixture
def isolated_db(monkeypatch, tmp_path):
    db_path = tmp_path / "m.sqlite"
    monkeypatch.setattr(db_core, "MEMORY_DB_PATH", db_path)
    db_core.run_migrations(db_path)
    yield db_path


def test_net_value_flags_counterproductive(isolated_db):
    # rule "good": prevented a real error once, no cost
    ledger.ledger_put("hard_block", subject="good",
                      outcome={"block_justified": True, "prevented_error": True})
    # rule "noise": 2 false positives, never prevented anything
    ledger.ledger_put("false_positive", subject="noise")
    ledger.ledger_put("false_positive", subject="noise")

    nv = ledger.ledger_net_value()
    assert nv["by_subject"]["good"]["net"] == 1
    assert nv["by_subject"]["noise"]["net"] == -2
    assert "noise" in nv["counterproductive"]
    assert "good" not in nv["counterproductive"]
    assert nv["totals"]["net"] == -1
