"""P4: observation_get_by_file — file_path matching + importance sort + access bump."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from token_savior import memory_db

PROJECT = "/tmp/test-project-p4"


@pytest.fixture(autouse=True)
def _memory_tmpdb(tmp_path: Path):
    db_path = tmp_path / "memory.db"
    with patch.object(memory_db, "MEMORY_DB_PATH", db_path):
        yield db_path


def _save(title: str, content: str, *, file_path: str | None = None, importance: int = 5) -> int:
    sid = memory_db.session_start(PROJECT)
    oid = memory_db.observation_save(
        sid, PROJECT, "convention", title, content,
        file_path=file_path, importance=importance,
    )
    assert oid is not None, f"save failed for {title!r}"
    return oid


class TestExactMatch:
    def test_returns_obs_for_file(self):
        oid = _save("file-a rule", "content a", file_path="src/foo.py")
        rows = memory_db.observation_get_by_file(PROJECT, "src/foo.py")
        ids = [r["id"] for r in rows]
        assert oid in ids

    def test_filters_other_files(self):
        _save("file-a rule", "content a", file_path="src/foo.py")
        oid_b = _save("file-b rule", "content b", file_path="src/bar.py")
        rows = memory_db.observation_get_by_file(PROJECT, "src/bar.py")
        ids = [r["id"] for r in rows]
        assert ids == [oid_b]

    def test_empty_when_no_obs(self):
        rows = memory_db.observation_get_by_file(PROJECT, "src/never.py")
        assert rows == []

    def test_empty_when_file_path_blank(self):
        _save("rule", "content", file_path="src/foo.py")
        assert memory_db.observation_get_by_file(PROJECT, "") == []


class TestBasenameFallback:
    def test_matches_basename(self):
        oid = _save("auth rule", "content", file_path="src/auth/middleware.ts")
        rows = memory_db.observation_get_by_file(PROJECT, "middleware.ts")
        assert oid in [r["id"] for r in rows]


class TestOrdering:
    def test_importance_desc_primary(self):
        oid_low = _save("low", "c1", file_path="src/foo.py", importance=3)
        oid_high = _save("high", "c2", file_path="src/foo.py", importance=9)
        rows = memory_db.observation_get_by_file(PROJECT, "src/foo.py")
        assert [r["id"] for r in rows] == [oid_high, oid_low]

    def test_limit_clamps_results(self):
        for i in range(8):
            _save(f"obs {i}", f"content {i}", file_path="src/foo.py", importance=5)
        rows = memory_db.observation_get_by_file(PROJECT, "src/foo.py", limit=3)
        assert len(rows) == 3


class TestAccessBump:
    def test_bumps_access_count(self):
        oid = _save("rule", "content", file_path="src/foo.py")
        conn = memory_db.get_db()
        before = conn.execute(
            "SELECT access_count, last_accessed_at FROM observations WHERE id=?",
            (oid,),
        ).fetchone()
        conn.close()

        memory_db.observation_get_by_file(PROJECT, "src/foo.py")

        conn = memory_db.get_db()
        after = conn.execute(
            "SELECT access_count, last_accessed_at FROM observations WHERE id=?",
            (oid,),
        ).fetchone()
        conn.close()
        assert after["access_count"] == before["access_count"] + 1
        assert after["last_accessed_at"] is not None

    def test_bump_access_false_skips_bump(self):
        oid = _save("rule", "content", file_path="src/foo.py")
        memory_db.observation_get_by_file(PROJECT, "src/foo.py", bump_access=False)
        conn = memory_db.get_db()
        row = conn.execute(
            "SELECT access_count FROM observations WHERE id=?", (oid,),
        ).fetchone()
        conn.close()
        assert row["access_count"] == 0


class TestArchivedExcluded:
    def test_archived_rows_not_returned(self):
        oid = _save("rule", "content", file_path="src/foo.py")
        conn = memory_db.get_db()
        conn.execute("UPDATE observations SET archived=1 WHERE id=?", (oid,))
        conn.commit()
        conn.close()
        rows = memory_db.observation_get_by_file(PROJECT, "src/foo.py")
        assert rows == []
