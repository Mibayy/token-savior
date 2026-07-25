from scripts import reflection as rf


def _miss(obs, cls="ignored"):
    return {"event_type": "miss", "meta": {"miss_class": cls, "expected_obs": obs}}


def test_recurring_ignored_miss_proposes_rule():
    events = [_miss(64), _miss(64), _miss(64)]  # obs 64 ignored 3×
    props = rf.analyze(events, {"counterproductive": []}, min_count=3)
    promote = [p for p in props if p["kind"] == "promote_to_rule"]
    assert len(promote) == 1
    assert promote[0]["subject"] == "obs:64"
    assert promote[0]["count"] == 3
    assert promote[0]["auto_applicable"] is False  # high risk → propose only


def test_thin_evidence_is_not_proposed():
    # adversarial gate: 2 < min_count → no proposal (don't act on noise)
    events = [_miss(64), _miss(64)]
    props = rf.analyze(events, {"counterproductive": []}, min_count=3)
    assert [p for p in props if p["kind"] == "promote_to_rule"] == []


def test_counterproductive_subject_proposed_for_review():
    props = rf.analyze([], {"counterproductive": ["noisy-rule"]}, min_count=3)
    rev = [p for p in props if p["kind"] == "review_counterproductive"]
    assert len(rev) == 1 and rev[0]["subject"] == "noisy-rule"


def test_uncertain_and_invisible_misses_dont_propose_rules():
    # only 'ignored' (activation) misses argue for a rule; others don't
    events = [_miss(64, "invisible"), _miss(64, "uncertain"), _miss(64, "unrecorded")]
    props = rf.analyze(events, {"counterproductive": []}, min_count=1)
    assert [p for p in props if p["kind"] == "promote_to_rule"] == []


def test_empty_ledger_no_proposals():
    assert rf.analyze([], {"counterproductive": []}) == []
