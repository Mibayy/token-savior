from scripts import initiative as ini


NOW = 1_753_000_000  # ~2025-07; fixed for determinism


def test_days_until_parses_deadline():
    # 10 days ahead
    epoch = NOW
    assert ini.days_until("1970-01-01", now_epoch=0) == 0
    d = ini.days_until("2025-08-01", now_epoch=int(__import__("datetime").datetime(
        2025, 7, 22, tzinfo=__import__("datetime").timezone.utc).timestamp()))
    assert d == 10


def test_deadline_soon_ranks_high():
    now = int(__import__("datetime").datetime(
        2025, 7, 28, tzinfo=__import__("datetime").timezone.utc).timestamp())
    projects = [{"name": "qbr", "deadline": "2025-07-30", "priority": "high"}]
    acts = ini.rank_actions(projects, now_epoch=now)
    dl = [a for a in acts if a["kind"] == "deadline_soon"]
    assert len(dl) == 1 and dl[0]["urgency"] >= 80


def test_failed_service_action():
    acts = ini.rank_actions([{"name": "improvence", "service_status": "failed",
                              "service": "improvence.service"}], now_epoch=NOW)
    fs = [a for a in acts if a["kind"] == "failed_service"]
    assert len(fs) == 1 and "journalctl" in fs[0]["suggested"]


def test_uncommitted_work_flagged_when_stale():
    acts = ini.rank_actions([{"name": "x", "dirty_files": 4, "activity": "stale"}],
                            now_epoch=NOW)
    assert any(a["kind"] == "uncommitted_work" for a in acts)


def test_no_noise_on_clean_active_project():
    acts = ini.rank_actions([{"name": "clean", "activity": "active", "dirty_files": 0}],
                            now_epoch=NOW)
    assert acts == []


def test_far_deadline_still_outranks_uncommitted_work():
    # a deadline 12 days out must NOT be buried under a single dirty file
    now = int(__import__("datetime").datetime(
        2025, 7, 16, tzinfo=__import__("datetime").timezone.utc).timestamp())
    projects = [{"name": "dirty", "dirty_files": 1, "activity": "stale"},
                {"name": "q", "deadline": "2025-07-28", "priority": "high"}]  # 12 days
    acts = ini.rank_actions(projects, now_epoch=now)
    assert acts[0]["project"] == "q"


def test_actions_sorted_by_urgency():
    now = int(__import__("datetime").datetime(
        2025, 7, 28, tzinfo=__import__("datetime").timezone.utc).timestamp())
    projects = [{"name": "x", "dirty_files": 2, "activity": "stale"},
                {"name": "q", "deadline": "2025-07-29", "priority": "high"}]
    acts = ini.rank_actions(projects, now_epoch=now)
    assert acts[0]["project"] == "q"  # deadline outranks uncommitted work
