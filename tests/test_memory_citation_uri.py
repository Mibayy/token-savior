"""P3: ts://obs/{id} citation URIs — parser + memory_get acceptance + index rendering."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from token_savior import memory_db
from token_savior.server_handlers.memory import (
    _mh_memory_get,
    _parse_obs_id,
)

PROJECT = "/tmp/test-project-p3"


@pytest.fixture(autouse=True)
def _memory_tmpdb(tmp_path: Path):
    db_path = tmp_path / "memory.db"
    with patch.object(memory_db, "MEMORY_DB_PATH", db_path):
        yield db_path


class TestParseObsId:
    def test_int(self):
        assert _parse_obs_id(42) == 42

    def test_digit_string(self):
        assert _parse_obs_id("42") == 42
        assert _parse_obs_id("  7 ") == 7

    def test_uri(self):
        assert _parse_obs_id("ts://obs/42") == 42
        assert _parse_obs_id("ts://obs/1") == 1
        assert _parse_obs_id("  ts://obs/9  ") == 9

    def test_uri_case_insensitive(self):
        assert _parse_obs_id("TS://OBS/42") == 42

    def test_invalid(self):
        assert _parse_obs_id(None) is None
        assert _parse_obs_id("") is None
        assert _parse_obs_id("garbage") is None
        assert _parse_obs_id("ts://obs/") is None
        assert _parse_obs_id("ts://obs/abc") is None
        assert _parse_obs_id("ts://other/42") is None
        # bool is not a valid id even though isinstance(True, int)
        assert _parse_obs_id(True) is None


class TestMemoryGetUriDispatch:
    def _save(self, title: str, content: str) -> int:
        sid = memory_db.session_start(PROJECT)
        oid = memory_db.observation_save(
            sid, PROJECT, "convention", title, content,
        )
        assert oid is not None
        return oid

    def test_memory_get_accepts_uri(self):
        oid = self._save("rule", "do the thing")
        out = _mh_memory_get({"ids": [f"ts://obs/{oid}"]})
        assert f"## #{oid}" in out
        assert "rule" in out

    def test_memory_get_accepts_mix(self):
        oid_a = self._save("alpha", "A content")
        oid_b = self._save("beta", "B content")
        out = _mh_memory_get({"ids": [oid_a, f"ts://obs/{oid_b}", str(oid_b)]})
        assert f"## #{oid_a}" in out
        assert f"## #{oid_b}" in out

    def test_memory_get_flags_invalid_tokens(self):
        oid = self._save("gamma", "G content")
        out = _mh_memory_get({"ids": [f"ts://obs/{oid}", "not-a-uri"]})
        assert f"## #{oid}" in out
        assert "Invalid id" in out
        assert "not-a-uri" in out

    def test_memory_get_missing_id_uri(self):
        """Unknown id via URI → "Not found", not a parse error."""
        out = _mh_memory_get({"ids": ["ts://obs/999999"]})
        assert "## #999999" in out
        assert "Not found" in out
