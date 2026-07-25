import pytest

from token_savior import db_core
from token_savior.memory import ledger


@pytest.fixture
def isolated_db(monkeypatch, tmp_path):
    db_path = tmp_path / "m.sqlite"
    monkeypatch.setattr(db_core, "MEMORY_DB_PATH", db_path)
    db_core.run_migrations(db_path)
    yield db_path


def test_ledger_put_inserts_row(isolated_db):
    res = ledger.ledger_put("miss", subject="ts://obs/42",
                            meta={"phrase": "je t'ai déjà dit"},
                            outcome={"was_visible": True})
    assert res["id"] > 0
    assert res["uri"] == f"ts://ledger/{res['id']}"

    conn = db_core.get_db()  # picks up patched MEMORY_DB_PATH
    row = conn.execute(
        "SELECT event_type, subject, was_visible FROM ledger_events WHERE id=?",
        (res["id"],)).fetchone()
    conn.close()
    assert row[0] == "miss"
    assert row[1] == "ts://obs/42"
    assert row[2] == 1  # bool True -> int 1
