"""Tests for LinUCBInjector — contextual bandit ranker for memory observations."""

from __future__ import annotations

from pathlib import Path

import pytest

from token_savior.linucb_injector import LinUCBInjector


@pytest.fixture
def linucb(tmp_path: Path) -> LinUCBInjector:
    return LinUCBInjector(tmp_path)


class TestLinUCBInjector:
    def test_feature_dim(self, linucb: LinUCBInjector):
        obs = {"type": "convention", "title": "x", "content": "y", "importance": 5}
        feats = linucb.extract_features(obs, {})
        assert len(feats) == LinUCBInjector.FEATURE_DIM == 10
        for f in feats:
            assert 0.0 <= f <= 1.0

    def test_guardrail_type_score(self, linucb: LinUCBInjector):
        obs = {"type": "guardrail", "title": "x", "content": "y", "importance": 5}
        feats = linucb.extract_features(obs, {})
        assert feats[0] == 1.0

    def test_score_positive(self, linucb: LinUCBInjector):
        obs = {"type": "convention", "title": "x", "content": "y", "importance": 5}
        score = linucb.score_observation(obs, {})
        assert score > 0

    def test_update_changes_model(self, linucb: LinUCBInjector):
        obs = {"type": "convention", "title": "x", "content": "y", "importance": 5}
        before = linucb.score_observation(obs, {})
        for _ in range(5):
            linucb.update(obs, {}, reward=1.0)
        after = linucb.score_observation(obs, {})
        assert after != before

    def test_rank_top_k(self, linucb: LinUCBInjector):
        obs_list = [
            {"type": "guardrail", "title": f"t{i}", "content": "c", "importance": 5}
            for i in range(10)
        ]
        ranked = linucb.rank_observations(obs_list, {}, top_k=3)
        assert len(ranked) == 3
        for o, score in ranked:
            assert isinstance(score, float)
            assert isinstance(o, dict)
            assert "type" in o

    def test_persistence(self, tmp_path: Path):
        lu = LinUCBInjector(tmp_path)
        obs = {"type": "convention", "title": "x", "content": "y", "importance": 5}
        for _ in range(3):
            lu.update(obs, {}, reward=1.0)
        lu.save()
        lu2 = LinUCBInjector(tmp_path)
        s = lu2.get_stats()
        assert s["updates"] == 3
