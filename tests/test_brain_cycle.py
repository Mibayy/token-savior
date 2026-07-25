from scripts import brain_cycle as bc


def test_skeptic_refutes_thin_promote():
    props = [{"kind": "promote_to_rule", "subject": "obs:1", "confidence": 0.5,
              "risk": "high", "auto_applicable": False}]
    rev = bc.skeptical_review(props)
    assert rev[0]["skeptic_verdict"] == "insufficient_evidence"


def test_skeptic_flags_high_risk_for_human():
    props = [{"kind": "promote_to_rule", "subject": "obs:1", "confidence": 0.9,
              "risk": "high", "auto_applicable": False}]
    rev = bc.skeptical_review(props)
    assert rev[0]["skeptic_verdict"] == "needs_human"


def test_skeptic_passes_solid_low_risk():
    props = [{"kind": "tighten_retrieval", "subject": "retrieval", "confidence": 0.9,
              "risk": "low", "auto_applicable": True}]
    rev = bc.skeptical_review(props)
    assert rev[0]["skeptic_verdict"] == "worth_review"


def test_build_cycle_record_shape():
    health = {"total_events": 42, "miss_classes": {"ignored": 4}}
    reviewed = [{"kind": "promote_to_rule", "skeptic_verdict": "needs_human"},
                {"kind": "tighten_retrieval", "skeptic_verdict": "worth_review"}]
    rec = bc.build_cycle_record(1_700_000_000, health, reviewed)
    assert rec["ts"] == 1_700_000_000
    assert rec["total_events"] == 42
    assert rec["actionable"] == 1  # only the worth_review one
    assert rec["needs_human"] == 1


def test_skeptic_survives_empty():
    assert bc.skeptical_review([]) == []


def test_build_record_empty_is_safe():
    rec = bc.build_cycle_record(1, {"total_events": 0, "miss_classes": {}}, [])
    assert rec["actionable"] == 0 and rec["needs_human"] == 0 and rec["refuted"] == 0


def test_never_auto_applies_enforcement():
    # invariant: nothing that touches rules is ever marked auto-applied
    reviewed = bc.skeptical_review([
        {"kind": "promote_to_rule", "subject": "x", "confidence": 1.0,
         "risk": "high", "auto_applicable": False}])
    assert all(r["skeptic_verdict"] != "auto_applied" for r in reviewed)
