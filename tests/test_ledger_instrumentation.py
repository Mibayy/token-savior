import pytest

from token_savior import db_core, memory_db
from token_savior.memory import ledger


@pytest.fixture
def isolated_db(monkeypatch, tmp_path):
    db_path = tmp_path / "m.sqlite"
    # db_core and memory_db carry SEPARATE MEMORY_DB_PATH globals; patch both,
    # or observation_save/search would hit the real production memory.db.
    monkeypatch.setattr(db_core, "MEMORY_DB_PATH", db_path)
    monkeypatch.setattr(memory_db, "MEMORY_DB_PATH", db_path)
    db_core.run_migrations(db_path)
    yield db_path


def test_record_injection_writes_event(isolated_db):
    res = ledger.record_injection("s1", "/proj", [32, 126, 7],
                                  injected_text="x" * 40)
    assert res["id"] > 0
    rows = ledger.ledger_query(event_type="injection", session_id="s1")
    assert len(rows) == 1
    assert rows[0]["meta"]["obs_ids"] == [32, 126, 7]
    assert rows[0]["cost_tokens"] == 10  # 40 chars // 4


def test_recent_injected_obs_is_session_union(isolated_db):
    ledger.record_injection("s1", "/proj", [1, 2])
    ledger.record_injection("s1", "/proj", [2, 3])
    ledger.record_injection("other", "/proj", [99])
    ids = ledger._recent_injected_obs("s1")
    assert sorted(set(ids)) == [1, 2, 3]
    assert 99 not in ids


def test_record_from_userprompt_stores_miss_class(isolated_db):
    # Real observation_search runs against an empty FTS → no results →
    # the miss is classified 'unrecorded' and the class is persisted.
    res = ledger.record_from_userprompt(
        {"prompt": "je t'ai déjà dit de regarder les logs applicatifs"},
        session_id="s1", project_root="/proj")
    assert res is not None
    miss = ledger.ledger_query(event_type="miss", session_id="s1")[0]
    assert miss["meta"]["miss_class"] == "unrecorded"
    assert miss["outcome"]["was_visible"] is None  # unrecorded → not a visibility verdict


def test_no_project_root_yields_uncertain(isolated_db):
    ledger.record_from_userprompt(
        {"prompt": "je t'ai déjà dit de regarder les logs applicatifs"},
        session_id="s2", project_root=None)
    miss = ledger.ledger_query(event_type="miss", session_id="s2")[0]
    assert miss["meta"]["miss_class"] == "uncertain"


def test_ignored_via_real_search(isolated_db, monkeypatch):
    # Force FTS-only (a real production mode) so the search is deterministic:
    # vector fusion carries global embedding state across tests and would make
    # this flaky. We're testing the wiring (real search + injected-set join),
    # not the vector ranking.
    monkeypatch.setattr(db_core, "VECTOR_SEARCH_AVAILABLE", False)
    # Seed the obs, inject it this session, then correct → the obs was surfaced
    # but not acted on → 'ignored', was_visible=1. Exercises the REAL search +
    # injected-set join (not a fake search_fn).
    from token_savior import memory_db
    oid = memory_db.observation_save(
        None, "/proj", "guardrail",
        "regarder les logs applicatifs quand erreur",
        "toujours regarder les logs applicatifs")
    ledger.record_injection("s1", "/proj", [oid])
    ledger.record_from_userprompt(
        {"prompt": "je t'ai déjà dit de regarder les logs applicatifs"},
        session_id="s1")
    m = ledger.ledger_query(event_type="miss", session_id="s1")[0]
    assert m["meta"]["miss_class"] == "ignored"
    assert m["meta"]["expected_obs"] == oid
    assert m["outcome"]["was_visible"] == 1


def test_invisible_via_real_search(isolated_db, monkeypatch):
    monkeypatch.setattr(db_core, "VECTOR_SEARCH_AVAILABLE", False)
    # Same obs exists and is retrieved, but it was NOT injected this session →
    # 'invisible', was_visible=0.
    from token_savior import memory_db
    memory_db.observation_save(
        None, "/proj", "guardrail",
        "regarder les logs applicatifs quand erreur",
        "toujours regarder les logs applicatifs")
    ledger.record_injection("s2", "/proj", [99999])  # something else surfaced
    ledger.record_from_userprompt(
        {"prompt": "je t'ai déjà dit de regarder les logs applicatifs"},
        session_id="s2")
    m = ledger.ledger_query(event_type="miss", session_id="s2")[0]
    assert m["meta"]["miss_class"] == "invisible"
    assert m["outcome"]["was_visible"] == 0
