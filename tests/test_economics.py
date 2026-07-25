from scripts import economics as ec


def test_routing_batch_to_haiku():
    r = ec.recommend_model("cron")
    assert r["model"] == "haiku"
    assert ec.recommend_model("batch")["model"] == "haiku"


def test_routing_debug_archi_to_opus():
    assert ec.recommend_model("debug")["model"] == "opus"
    assert ec.recommend_model("architecture")["model"] == "opus"


def test_routing_ts_fed_dev_to_sonnet():
    assert ec.recommend_model("dev", ts_fed=True)["model"] == "sonnet"
    # dev WITHOUT token-savior context → not auto-downgraded
    assert ec.recommend_model("dev", ts_fed=False)["model"] == "opus"


def test_routing_review_to_sonnet():
    assert ec.recommend_model("review")["model"] == "sonnet"


def test_unknown_defaults_to_opus():
    assert ec.recommend_model("random-thing")["model"] == "opus"


def test_token_spend_sums_injection_cost():
    events = [
        {"event_type": "injection", "cost_tokens": 100},
        {"event_type": "injection", "cost_tokens": 50},
        {"event_type": "miss", "cost_tokens": 0},
    ]
    assert ec.token_spend(events)["injection_tokens"] == 150


def test_flag_waste_from_counterproductive():
    nv = {"counterproductive": ["retrieval"], "totals": {"token_cost": 5000}}
    flags = ec.flag_waste(nv)
    assert "retrieval" in flags
