import pytest

from token_savior import db_core
from token_savior.memory import ledger


@pytest.fixture
def isolated_db(monkeypatch, tmp_path):
    db_path = tmp_path / "m.sqlite"
    monkeypatch.setattr(db_core, "MEMORY_DB_PATH", db_path)
    db_core.run_migrations(db_path)
    yield db_path


def _search(results):
    """Build a fake observation_search returning fixed results."""
    def _f(project_root, query, *, limit=5, **kw):
        return list(results)
    return _f


def test_thin_query_is_uncertain():
    # "je t'ai déjà dit" stripped leaves nothing meaningful → uncertain.
    out = ledger.classify_miss("je t'ai déjà dit", [], "/p", search_fn=_search([]))
    assert out["miss_class"] == "uncertain"
    assert out["expected_obs"] is None


def test_no_results_is_unrecorded():
    out = ledger.classify_miss(
        "je t'ai déjà dit de regarder les logs applicatifs",
        [], "/p", search_fn=_search([]))
    assert out["miss_class"] == "unrecorded"
    assert out["expected_obs"] is None


def test_high_overlap_injected_is_ignored():
    obs = {"id": 42, "title": "regarder logs applicatifs",
           "excerpt": "toujours regarder les logs applicatifs quand erreur"}
    out = ledger.classify_miss(
        "je t'ai déjà dit de regarder les logs applicatifs",
        [42], "/p", search_fn=_search([obs]))
    assert out["miss_class"] == "ignored"
    assert out["expected_obs"] == 42
    assert out["overlap"] >= ledger.OVERLAP_HIGH


def test_high_overlap_not_injected_is_invisible():
    obs = {"id": 7, "title": "regarder logs applicatifs",
           "excerpt": "toujours regarder les logs applicatifs quand erreur"}
    out = ledger.classify_miss(
        "je t'ai déjà dit de regarder les logs applicatifs",
        [99], "/p", search_fn=_search([obs]))
    assert out["miss_class"] == "invisible"
    assert out["expected_obs"] == 7


def test_weak_overlap_is_uncertain():
    # Top result shares almost nothing with the query tokens → don't guess.
    obs = {"id": 5, "title": "config nginx reverse proxy",
           "excerpt": "bloc server listen 443 ssl"}
    out = ledger.classify_miss(
        "je t'ai déjà dit de regarder les logs applicatifs",
        [5], "/p", search_fn=_search([obs]))
    assert out["miss_class"] == "uncertain"
