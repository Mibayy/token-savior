import io
import json

import pytest

from token_savior import db_core
from token_savior.memory import ledger, ledger_hook


@pytest.fixture
def isolated_db(monkeypatch, tmp_path):
    db_path = tmp_path / "m.sqlite"
    monkeypatch.setattr(db_core, "MEMORY_DB_PATH", db_path)
    db_core.run_migrations(db_path)
    yield db_path


def test_entrypoint_records_miss(isolated_db, monkeypatch):
    payload = {"prompt": "je t'ai déjà dit de checker les logs",
               "session_id": "sX"}
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(payload)))

    rc = ledger_hook.main()
    assert rc == 0  # never breaks the session

    misses = ledger.ledger_query(event_type="miss")
    assert len(misses) == 1 and misses[0]["session_id"] == "sX"


def test_entrypoint_survives_garbage_stdin(isolated_db, monkeypatch):
    monkeypatch.setattr("sys.stdin", io.StringIO("not json at all"))
    assert ledger_hook.main() == 0  # returns 0, writes nothing
    assert ledger.ledger_query(event_type="miss") == []
