from scripts import fleet as fl


def test_solo_for_small_low_risk_single():
    plan = fl.plan_fleet(subtasks=1, size="small", risk="low", needs_review=False)
    assert plan["mode"] == "solo"
    assert plan["agents"] == 0


def test_parallel_fanout_for_independent_subtasks():
    plan = fl.plan_fleet(subtasks=5, size="small", risk="low")
    assert plan["mode"] == "parallel_fanout"
    assert plan["agents"] == 5


def test_subagent_driven_for_large_or_review():
    plan = fl.plan_fleet(subtasks=3, size="large", risk="low", needs_review=True)
    assert plan["mode"] == "subagent_driven"
    # implementer + reviewer per task
    assert any(r["role"] == "reviewer" for r in plan["roles"])


def test_high_risk_adds_adversarial_verify():
    plan = fl.plan_fleet(subtasks=2, size="large", risk="high", needs_review=True)
    assert plan["verify"]["adversarial"] is True
    assert plan["verify"]["skeptics"] >= 2


def test_final_review_uses_opus():
    plan = fl.plan_fleet(subtasks=4, size="large", risk="high", needs_review=True)
    final = [r for r in plan["roles"] if r["role"] == "final_review"]
    assert final and final[0]["model"] == "opus"


def test_implementers_use_sonnet():
    plan = fl.plan_fleet(subtasks=3, size="large", risk="low", needs_review=True)
    impl = [r for r in plan["roles"] if r["role"] == "implementer"]
    assert impl and impl[0]["model"] == "sonnet"
