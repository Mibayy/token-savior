# tests/test_ledger_schema.py
from token_savior import db_core


def test_ledger_events_table_created(tmp_path):
    db = tmp_path / "m.sqlite"
    db_core.run_migrations(db)
    conn = db_core.get_db(db)
    cols = {r[1] for r in conn.execute("PRAGMA table_info(ledger_events)")}
    conn.close()
    assert {"id", "ts_epoch", "event_type", "subject", "cost_tokens",
            "acted_on", "was_visible", "meta_json"} <= cols
