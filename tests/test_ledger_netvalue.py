import pytest

from token_savior import db_core
from token_savior.memory import ledger


@pytest.fixture
def isolated_db(monkeypatch, tmp_path):
    db_path = tmp_path / "m.sqlite"
    monkeypatch.setattr(db_core, "MEMORY_DB_PATH", db_path)
    db_core.run_migrations(db_path)
    yield db_path


def test_friction_axis_is_event_counts(isolated_db):
    # "good": a justified hard_block that prevented a real error.
    ledger.ledger_put("hard_block", subject="good",
                      outcome={"block_justified": True, "prevented_error": True})
    # "noise": two false positives, never helped.
    ledger.ledger_put("false_positive", subject="noise")
    ledger.ledger_put("false_positive", subject="noise")

    nv = ledger.ledger_net_value()
    assert nv["by_subject"]["good"]["friction_net"] == 1
    assert nv["by_subject"]["good"]["token_cost"] == 0
    assert nv["by_subject"]["noise"]["friction_net"] == -2
    assert "noise" in nv["counterproductive"]
    assert "good" not in nv["counterproductive"]
    assert nv["totals"]["friction_net"] == -1


def test_tokens_never_subtracted_from_benefit(isolated_db):
    # An expensive injection that STILL prevented an error must NOT be flagged:
    # token cost lives on a separate axis and never drags friction_net down.
    ledger.ledger_put("injection", subject="useful_expensive",
                      cost_tokens=5000, outcome={"prevented_error": True})
    nv = ledger.ledger_net_value()
    b = nv["by_subject"]["useful_expensive"]
    assert b["benefit_events"] == 1
    assert b["token_cost"] == 5000
    assert b["friction_net"] == 1
    assert "useful_expensive" not in nv["counterproductive"]
    # token_cost also sums into totals.
    assert nv["totals"]["token_cost"] == 5000


def test_acted_on_soft_remind_counts_as_benefit(isolated_db):
    # The second benefit contributor: a soft reminder that was acted on.
    ledger.ledger_put("soft_remind", subject="reminder",
                      outcome={"acted_on": True})
    nv = ledger.ledger_net_value()
    assert nv["by_subject"]["reminder"]["benefit_events"] == 1
    assert nv["by_subject"]["reminder"]["friction_net"] == 1
    assert "reminder" not in nv["counterproductive"]


def test_pure_token_waste_is_flagged(isolated_db):
    # Tokens burned above threshold, never helped → pure waste.
    ledger.ledger_put("injection", subject="waste",
                      cost_tokens=ledger.TOKEN_WASTE_THRESHOLD + 1)
    nv = ledger.ledger_net_value()
    b = nv["by_subject"]["waste"]
    assert b["benefit_events"] == 0
    assert b["friction_net"] == 0            # no friction events...
    assert "waste" in nv["counterproductive"]  # ...but pure token waste


def test_cheap_zero_benefit_not_flagged(isolated_db):
    # A miss (cost_tokens=0, no benefit) must NOT be flagged as waste.
    ledger.ledger_put("miss", subject="a_miss")
    nv = ledger.ledger_net_value()
    assert "a_miss" not in nv["counterproductive"]


def test_none_and_empty_subject_do_not_merge(isolated_db):
    # subject=None buckets under "(none)"; an empty-string subject keeps its
    # own "" bucket. The old `subj or "(none)"` truthiness collapsed both —
    # this locks the `is not None` fix.
    ledger.ledger_put("miss", subject=None)
    ledger.ledger_put("miss", subject="")
    nv = ledger.ledger_net_value()
    assert "(none)" in nv["by_subject"]
    assert "" in nv["by_subject"]
    assert nv["by_subject"]["(none)"] is not nv["by_subject"][""]
