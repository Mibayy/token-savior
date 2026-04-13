"""Tests for PPMPrefetcher — variable-order Markov tool-call predictor."""

from __future__ import annotations

from pathlib import Path

import pytest

from token_savior.markov_prefetcher import PPMPrefetcher


@pytest.fixture
def ppm(tmp_path: Path) -> PPMPrefetcher:
    return PPMPrefetcher(tmp_path)


class TestPPM:
    def test_order1_fallback(self, ppm: PPMPrefetcher):
        preds = ppm.predict_next("nothing_seen_before", "", top_k=3)
        assert isinstance(preds, list)

    def test_order2_after_training(self, ppm: PPMPrefetcher):
        for _ in range(5):
            ppm.record_call("A")
            ppm.record_call("B")
            ppm.record_call("C")
        preds = ppm.predict_next("B", "", top_k=3)
        assert preds
        tools = [t for t, _ in preds]
        assert "C" in tools

    def test_probabilities_sum_leq_1(self, ppm: PPMPrefetcher):
        for _ in range(3):
            ppm.record_call("A")
            ppm.record_call("B")
        preds = ppm.predict_next("A", "", top_k=5)
        total = sum(p for _, p in preds)
        assert total <= 1.0 + 1e-6

    def test_persistence(self, tmp_path: Path):
        p1 = PPMPrefetcher(tmp_path)
        for _ in range(4):
            p1.record_call("X")
            p1.record_call("Y")
        p1.save_model()
        p2 = PPMPrefetcher(tmp_path)
        stats = p2.get_stats()
        assert stats["transitions"] > 0
