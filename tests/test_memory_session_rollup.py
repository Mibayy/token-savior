"""P5: structured session-end rollups + FTS search + history handler."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from token_savior import memory_db
from token_savior.server_handlers.memory import (
    _mh_memory_search,
    _mh_memory_session_history,
)

PROJECT = "/tmp/test-project-p5"


@pytest.fixture(autouse=True)
def _memory_tmpdb(tmp_path: Path):
    db_path = tmp_path / "memory.db"
    with patch.object(memory_db, "MEMORY_DB_PATH", db_path):
        yield db_path


def _end_with_rollup(**fields) -> tuple[int, int]:
    """Open a session, end it with the given rollup fields, return (sid, summary_id)."""
    sid = memory_db.session_start(PROJECT)
    memory_db.session_end(sid, summary="done", **fields)
    conn = memory_db.get_db()
    row = conn.execute(
        "SELECT id FROM session_summaries WHERE session_id=?", (sid,)
    ).fetchone()
    conn.close()
    return sid, (row["id"] if row else 0)


class TestSessionSummariesSchema:
    def test_table_and_fts_exist(self):
        memory_db.session_start(PROJECT)  # ensures migrations ran
        conn = memory_db.get_db()
        names = {
            r["name"]
            for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type IN ('table','view')"
            ).fetchall()
        }
        conn.close()
        assert "session_summaries" in names
        assert "session_summaries_fts" in names


class TestSessionEndRollup:
    def test_session_end_without_rollup_fields_writes_nothing(self):
        sid = memory_db.session_start(PROJECT)
        memory_db.session_end(sid, summary="plain done")
        conn = memory_db.get_db()
        row = conn.execute(
            "SELECT COUNT(*) AS n FROM session_summaries WHERE session_id=?", (sid,)
        ).fetchone()
        conn.close()
        assert row["n"] == 0

    def test_session_end_with_one_field_writes_row(self):
        sid, sum_id = _end_with_rollup(request="add a hook")
        assert sum_id > 0
        conn = memory_db.get_db()
        row = conn.execute(
            "SELECT request, investigated, learned, completed, next_steps, notes, "
            "       project_root FROM session_summaries WHERE id=?",
            (sum_id,),
        ).fetchone()
        conn.close()
        assert row["request"] == "add a hook"
        assert row["investigated"] is None
        assert row["project_root"] == PROJECT

    def test_session_end_persists_all_rollup_fields(self):
        _, sum_id = _end_with_rollup(
            request="P5 rollup", investigated="sessions.py",
            learned="FTS5 triggers", completed="schema + fn",
            next_steps="write tests", notes="careful with migrations",
        )
        conn = memory_db.get_db()
        row = conn.execute(
            "SELECT * FROM session_summaries WHERE id=?", (sum_id,),
        ).fetchone()
        conn.close()
        assert row["request"] == "P5 rollup"
        assert row["investigated"] == "sessions.py"
        assert row["learned"] == "FTS5 triggers"
        assert row["completed"] == "schema + fn"
        assert row["next_steps"] == "write tests"
        assert row["notes"] == "careful with migrations"

    def test_blank_fields_treated_as_empty(self):
        sid = memory_db.session_start(PROJECT)
        memory_db.session_end(sid, request="   ", notes="")
        conn = memory_db.get_db()
        row = conn.execute(
            "SELECT COUNT(*) AS n FROM session_summaries WHERE session_id=?", (sid,)
        ).fetchone()
        conn.close()
        assert row["n"] == 0


class TestSessionSummarySearch:
    def test_fts_finds_rollup(self):
        _end_with_rollup(
            request="wire hook", learned="PreToolUse fires before call",
        )
        hits = memory_db.session_summary_search(PROJECT, "PreToolUse")
        assert len(hits) >= 1
        assert "PreToolUse" in (hits[0].get("excerpt") or "") or hits[0]["learned"]

    def test_fts_scoped_to_project(self):
        _end_with_rollup(learned="project A secret")
        hits = memory_db.session_summary_search("/tmp/other-project", "secret")
        assert hits == []


class TestMemorySearchHandlerIncludesSummaries:
    def test_search_handler_surfaces_rollup_section(self):
        # observation (would match query)
        sid = memory_db.session_start(PROJECT)
        memory_db.observation_save(
            sid, PROJECT, "convention", "rollup note",
            "content mentioning specialkeywordxyz",
        )
        # rollup (also matches query via FTS)
        _end_with_rollup(learned="specialkeywordxyz in the body")

        out = _mh_memory_search({"query": "specialkeywordxyz", "project": PROJECT})
        assert "Session rollups" in out

    def test_search_handler_no_rollup_section_when_no_match(self):
        sid = memory_db.session_start(PROJECT)
        memory_db.observation_save(
            sid, PROJECT, "convention", "plain note", "plain content with uniqueobstoken",
        )
        out = _mh_memory_search({"query": "uniqueobstoken", "project": PROJECT})
        assert "Session rollups" not in out


class TestMemorySessionHistoryHandler:
    def test_empty(self):
        # need at least one obs so _resolve_memory_project returns PROJECT
        sid = memory_db.session_start(PROJECT)
        memory_db.observation_save(sid, PROJECT, "convention", "seed", "seed content")
        out = _mh_memory_session_history({"project": PROJECT})
        assert "No session rollups" in out

    def test_formats_recent_rollups(self):
        _end_with_rollup(
            request="do it", learned="did it", next_steps="next",
        )
        out = _mh_memory_session_history({"project": PROJECT, "limit": 5})
        assert "Session #" in out
        assert "Request" in out
        assert "do it" in out
        assert "Learned" in out
        assert "Next steps" in out

    def test_limit_clamps(self):
        for i in range(4):
            _end_with_rollup(request=f"req {i}")
        out = _mh_memory_session_history({"project": PROJECT, "limit": 2})
        # Two blocks separated by ---
        assert out.count("Session #") == 2
