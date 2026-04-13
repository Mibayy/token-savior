"""Tests for self-consistency Beta-Binomial validity scoring."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from token_savior import memory_db

PROJECT = "/tmp/test-consistency"


@pytest.fixture(autouse=True)
def _memory_tmpdb(tmp_path: Path):
    db_path = tmp_path / "memory.db"
    with patch.object(memory_db, "MEMORY_DB_PATH", db_path):
        yield db_path


def _make_obs() -> int:
    sid = memory_db.session_start(PROJECT)
    obs_id = memory_db.observation_save(
        sid, PROJECT, "convention", "t", "content example"
    )
    assert obs_id is not None
    return obs_id


class TestValidity:
    def test_default_prior(self):
        obs_id = _make_obs()
        v = memory_db.get_validity_score(obs_id)
        # Default Beta(2,1) → mean = 2/3 ≈ 0.666.
        assert v["validity"] == pytest.approx(2 / 3, abs=0.01)

    def test_validated_increases_score(self):
        obs_id = _make_obs()
        before = memory_db.get_validity_score(obs_id)["validity"]
        for _ in range(5):
            memory_db.update_consistency_score(obs_id, success=True)
        after = memory_db.get_validity_score(obs_id)["validity"]
        assert after > before

    def test_contradiction_decreases_score(self):
        obs_id = _make_obs()
        before = memory_db.get_validity_score(obs_id)["validity"]
        for _ in range(5):
            memory_db.update_consistency_score(obs_id, success=False)
        after = memory_db.get_validity_score(obs_id)["validity"]
        assert after < before

    def test_quarantine_at_threshold(self):
        obs_id = _make_obs()
        for _ in range(10):
            memory_db.update_consistency_score(obs_id, success=False)
        v = memory_db.get_validity_score(obs_id)
        assert v["validity"] < 0.4
