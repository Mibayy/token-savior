from scripts import world_model as wm


NOW = 1_000_000_000


def test_classify_activity_bands():
    assert wm.classify_activity(NOW - 2 * 86400, now_epoch=NOW) == "active"
    assert wm.classify_activity(NOW - 20 * 86400, now_epoch=NOW) == "recent"
    assert wm.classify_activity(NOW - 60 * 86400, now_epoch=NOW) == "stale"
    assert wm.classify_activity(NOW - 200 * 86400, now_epoch=NOW) == "dormant"
    assert wm.classify_activity(None, now_epoch=NOW) == "dormant"


def test_merge_overlay_attaches_business_context():
    projects = [{"name": "improvence", "path": "/root/improvence"},
                {"name": "gw2cc", "path": "/x"}]
    overlay = {"improvence": {"client": "Improvence", "deadline": "2026-08-01",
                              "priority": "high"}}
    merged = wm.merge_overlay(projects, overlay)
    imp = next(p for p in merged if p["name"] == "improvence")
    assert imp["client"] == "Improvence" and imp["priority"] == "high"
    gw = next(p for p in merged if p["name"] == "gw2cc")
    assert gw.get("client") is None  # no overlay → untouched


def test_map_services_to_projects():
    projects = [{"name": "intel"}, {"name": "gw2cc"}]
    services = {"intel-api.service": "active", "gw2cc.service": "failed",
                "unrelated.service": "active"}
    mapped = wm.map_services(projects, services)
    intel = next(p for p in mapped if p["name"] == "intel")
    assert intel["service"] == "intel-api.service" and intel["service_status"] == "active"
    gw = next(p for p in mapped if p["name"] == "gw2cc")
    assert gw["service_status"] == "failed"


def test_map_services_no_false_substring_match():
    # short/unrelated names must NOT map to an incidentally-containing service
    projects = [{"name": "a"}, {"name": "ec"}]
    services = {"intel-api.service": "active", "eclatauto-api.service": "active"}
    mapped = wm.map_services(projects, services)
    assert all("service" not in p for p in mapped)


def test_map_services_picks_most_specific():
    projects = [{"name": "intel"}]
    services = {"intel.service": "active", "intel-api.service": "failed"}
    mapped = wm.map_services(projects, services)
    # 'intel-api' is the more specific (longer) anchored match
    assert mapped[0]["service"] == "intel-api.service"


def test_needs_attention_flags_failed_service():
    projects = [{"name": "a", "service_status": "failed", "activity": "active"},
                {"name": "b", "service_status": "active", "activity": "active"}]
    s = wm.world_summary(projects)
    assert "a" in s["needs_attention"]
    assert "b" not in s["needs_attention"]


def test_world_summary_counts_by_activity():
    projects = [{"name": "a", "activity": "active"}, {"name": "b", "activity": "active"},
                {"name": "c", "activity": "dormant"}]
    s = wm.world_summary(projects)
    assert s["by_activity"]["active"] == 2
    assert s["by_activity"]["dormant"] == 1
    assert s["total"] == 3
