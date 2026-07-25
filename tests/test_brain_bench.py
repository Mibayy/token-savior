from scripts import brain_bench as bb


EVENTS = [
    {"event_type": "miss", "ts_epoch": 1_000_000, "meta": {"miss_class": "invisible"}},
    {"event_type": "miss", "ts_epoch": 1_000_100, "meta": {"miss_class": "invisible"}},
    {"event_type": "miss", "ts_epoch": 1_100_000, "meta": {"miss_class": "uncertain"}},
    {"event_type": "hard_block", "ts_epoch": 1_000_050, "subject": "no-force-push-protected"},
    {"event_type": "hard_block", "ts_epoch": 1_000_060, "subject": "no-force-push-protected"},
    {"event_type": "injection", "ts_epoch": 1_000_070, "cost_tokens": 40},
    {"event_type": "injection", "ts_epoch": 1_000_080, "cost_tokens": 60},
]


def test_miss_class_breakdown():
    b = bb.miss_class_breakdown(EVENTS)
    assert b == {"invisible": 2, "uncertain": 1}


def test_rule_firings():
    assert bb.rule_firings(EVENTS) == {"no-force-push-protected": 2}


def test_injection_stats():
    s = bb.injection_stats(EVENTS)
    assert s["count"] == 2 and s["total_token_cost"] == 100


def test_misses_per_day_uses_injected_dayfn():
    per = bb.misses_per_day(EVENTS, day_of=lambda e: "D1" if e < 1_050_000 else "D2")
    assert per == {"D1": 2, "D2": 1}


def test_health_summary_combines():
    h = bb.health_summary(EVENTS)
    assert h["total_events"] == 7
    assert h["miss_classes"] == {"invisible": 2, "uncertain": 1}
    assert h["injections"]["count"] == 2


def test_preflight_stats_counts_by_category():
    evs = [{"event_type": "preflight", "subject": "destructive-fs"},
           {"event_type": "preflight", "subject": "service"},
           {"event_type": "preflight", "subject": "destructive-fs"},
           {"event_type": "miss", "meta": {}}]
    s = bb.preflight_stats(evs)
    assert s["count"] == 3
    assert s["by_category"] == {"destructive-fs": 2, "service": 1}


def test_empty_is_safe():
    h = bb.health_summary([])
    assert h["total_events"] == 0
    assert h["miss_classes"] == {}
    assert h["injections"] == {"count": 0, "total_token_cost": 0}
