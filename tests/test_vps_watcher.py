from scripts import vps_watcher as w


def test_parse_failed_services():
    out = ("  intel-api.service loaded failed failed Intel API\n"
           "  gw2cc.service     loaded failed failed GW2 CC\n")
    assert w.parse_failed_services(out) == ["intel-api.service", "gw2cc.service"]


def test_parse_failed_services_empty():
    assert w.parse_failed_services("") == []
    assert w.parse_failed_services("0 loaded units listed.") == []


def test_expiring_certs_filters_by_threshold():
    certs = [{"name": "a.duckdns.org", "days_left": 5},
             {"name": "b.duckdns.org", "days_left": 40},
             {"name": "c.duckdns.org", "days_left": 13}]
    exp = w.expiring_certs(certs, threshold_days=14)
    assert {c["name"] for c in exp} == {"a.duckdns.org", "c.duckdns.org"}


def test_days_left_from_notafter():
    # notAfter is 100000 epoch; now is 10000 → ~1 day left (86400s).
    assert w.days_left_from_epoch(10000 + 86400 * 3, now_epoch=10000) == 3


def test_dead_timers_from_listing():
    out = ("NEXT                        LEFT      LAST  UNIT              ACTIVATES\n"
           "Mon 2026-07-28 06:00:00 UTC 2 days    -     intel-weekly.timer intel-weekly\n"
           "-                           -         -     broken.timer       broken\n")
    dead = w.parse_dead_timers(out)
    assert dead == ["broken.timer"]


def test_build_report_flags_only_problems():
    rep = w.build_report(failed=["x.service"], certs_exp=[{"name": "a", "days_left": 3}],
                         dead_timers=[], svc_errors=[])
    assert rep["needs_attention"] is True
    assert "x.service" in rep["failed_services"]
    clean = w.build_report(failed=[], certs_exp=[], dead_timers=[], svc_errors=[])
    assert clean["needs_attention"] is False
