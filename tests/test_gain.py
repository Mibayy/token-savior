"""`ts gain`: savings numbers outside the dashboard's HTTP endpoint (#63, #39).

Scripts (statusline badges, shell prompts, CI summaries) had to either run the
dashboard or parse `$TOKEN_SAVIOR_STATS_DIR/*.json` by hand, which is an
internal format that drifts. These pin the public shape.
"""
from __future__ import annotations

import json

from token_savior.dashboard import gain_report, format_gain


def _write_stats(tmp_path, name, project, used, naive, calls=10, sessions=2):
    (tmp_path / f"{name}.json").write_text(json.dumps({
        "project": project,
        "total_chars_returned": used,
        "total_naive_chars": naive,
        "total_calls": calls,
        "sessions": sessions,
    }))


def test_aggregates_every_project_by_default(tmp_path):
    _write_stats(tmp_path, "a", "/srv/alpha", used=1000, naive=5000)
    _write_stats(tmp_path, "b", "/srv/beta", used=1000, naive=15000)

    r = gain_report(stats_dir=tmp_path)

    assert r["project"] is None
    assert r["chars_used"] == 2000
    assert r["chars_naive"] == 20000
    assert r["tokens_saved"] == (20000 - 2000) // 4
    assert r["savings_pct"] == 90.0


def test_filters_to_one_project_by_root(tmp_path):
    _write_stats(tmp_path, "a", "/srv/alpha", used=1000, naive=5000)
    _write_stats(tmp_path, "b", "/srv/beta", used=1000, naive=15000)

    r = gain_report(stats_dir=tmp_path, project="/srv/beta")

    assert r["project"] == "/srv/beta"
    assert r["chars_naive"] == 15000
    assert r["tokens_saved"] == (15000 - 1000) // 4


def test_unknown_project_reports_zero_not_the_global_total(tmp_path):
    _write_stats(tmp_path, "a", "/srv/alpha", used=1000, naive=5000)

    r = gain_report(stats_dir=tmp_path, project="/srv/nope")

    assert r["found"] is False
    assert r["tokens_saved"] == 0


def test_empty_stats_dir_is_not_a_crash(tmp_path):
    r = gain_report(stats_dir=tmp_path)
    assert r["queries"] == 0
    assert r["savings_pct"] == 0.0


def test_text_format_is_short_enough_for_a_statusline(tmp_path):
    _write_stats(tmp_path, "a", "/srv/alpha", used=1000, naive=5000)
    line = format_gain(gain_report(stats_dir=tmp_path), compact=True)
    assert line.startswith("[TS ")
    assert "↓" in line
    assert len(line) < 32


def test_long_format_carries_the_numbers(tmp_path):
    _write_stats(tmp_path, "a", "/srv/alpha", used=1000, naive=5000, calls=7)
    out = format_gain(gain_report(stats_dir=tmp_path))
    assert "7" in out           # queries
    assert "80" in out          # savings %
