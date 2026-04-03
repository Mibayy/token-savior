from __future__ import annotations

import json
from pathlib import Path

from token_savior.dashboard import collect_dashboard_data


def test_collect_dashboard_data_aggregates_projects(tmp_path: Path):
    (tmp_path / "alpha-11111111.json").write_text(
        json.dumps(
            {
                "project": "/root/alpha",
                "total_calls": 10,
                "total_chars_returned": 400,
                "total_naive_chars": 4000,
                "sessions": 2,
                "tool_counts": {"find_symbol": 7, "run_impacted_tests": 3},
                "client_counts": {"codex": 1, "hermes": 1},
                "last_client": "codex",
                "history": [
                    {
                        "timestamp": "2026-03-30T10:00:00Z",
                        "client_name": "codex",
                        "tokens_used": 50,
                        "tokens_naive": 500,
                        "savings_pct": 90.0,
                    }
                ],
                "last_session": "2026-03-30T10:00:00Z",
            }
        ),
        encoding="utf-8",
    )
    (tmp_path / "beta-22222222.json").write_text(
        json.dumps(
            {
                "project": "/root/beta",
                "total_calls": 4,
                "total_chars_returned": 800,
                "total_naive_chars": 1600,
                "sessions": 1,
                "tool_counts": {"find_symbol": 2},
                "client_counts": {"hermes": 1},
                "last_client": "hermes",
                "history": [
                    {
                        "timestamp": "2026-03-30T11:00:00Z",
                        "client_name": "hermes",
                        "tokens_used": 200,
                        "tokens_naive": 400,
                        "savings_pct": 50.0,
                    }
                ],
                "last_session": "2026-03-30T11:00:00Z",
            }
        ),
        encoding="utf-8",
    )

    data = collect_dashboard_data(tmp_path)

    assert data["project_count"] == 2
    assert data["totals"]["queries"] == 14
    assert data["totals"]["tokens_used"] == (400 + 800) // 4
    assert data["totals"]["tokens_naive"] == (4000 + 1600) // 4
    assert data["projects"][0]["project"] == "alpha"
    assert data["projects"][0]["last_client"] == "codex"
    assert data["recent_sessions"][0]["project"] == "beta"
    assert data["recent_sessions"][0]["client_name"] == "hermes"
    assert data["top_tools"][0]["tool"] == "find_symbol"
    assert data["codex"]["active"] is True
    assert data["codex"]["sessions"] == 1
    assert data["client_count"] == 2
    assert data["started_at"]


def test_collect_dashboard_data_handles_missing_dir(tmp_path: Path):
    missing = tmp_path / "does-not-exist"
    data = collect_dashboard_data(missing)
    assert data["project_count"] == 0
    assert data["projects"] == []
    assert data["recent_sessions"] == []


def test_collect_dashboard_data_hides_tmp_projects_by_default(tmp_path: Path):
    (tmp_path / "real-11111111.json").write_text(
        json.dumps(
            {
                "project": "/root/real-project",
                "total_calls": 2,
                "total_chars_returned": 100,
                "total_naive_chars": 1000,
                "sessions": 1,
                "tool_counts": {},
                "history": [],
            }
        ),
        encoding="utf-8",
    )
    (tmp_path / "tmp-22222222.json").write_text(
        json.dumps(
            {
                "project": "/tmp/token-savior-bench-abc",
                "total_calls": 2,
                "total_chars_returned": 100,
                "total_naive_chars": 1000,
                "sessions": 1,
                "tool_counts": {},
                "history": [],
            }
        ),
        encoding="utf-8",
    )

    data = collect_dashboard_data(tmp_path)

    assert data["project_count"] == 1
    assert data["projects"][0]["project"] == "real-project"


def test_collect_dashboard_data_falls_back_to_unknown_client_for_legacy_stats(tmp_path: Path):
    (tmp_path / "legacy-11111111.json").write_text(
        json.dumps(
            {
                "project": "/root/legacy",
                "total_calls": 3,
                "total_chars_returned": 120,
                "total_naive_chars": 1200,
                "sessions": 2,
                "tool_counts": {"find_symbol": 3},
                "history": [
                    {"timestamp": "2026-03-30T10:00:00Z"},
                    {"timestamp": "2026-03-30T11:00:00Z"},
                ],
            }
        ),
        encoding="utf-8",
    )

    data = collect_dashboard_data(tmp_path)

    assert data["client_count"] == 1
    assert data["clients"][0]["client"] == "unknown"
    assert data["clients"][0]["sessions"] == 2
    assert data["projects"][0]["last_client"] == "unknown"


def test_collect_dashboard_data_rebrands_legacy_self_project(tmp_path: Path):
    (tmp_path / "token-savior-11111111.json").write_text(
        json.dumps(
            {
                "project": "/root/token-savior",
                "total_calls": 1,
                "total_chars_returned": 20,
                "total_naive_chars": 200,
                "sessions": 1,
                "tool_counts": {},
                "history": [],
            }
        ),
        encoding="utf-8",
    )

    data = collect_dashboard_data(tmp_path)

    assert data["projects"][0]["project"] == "token-savior"
    assert data["projects"][0]["project_root"] == "/root/token-savior"
