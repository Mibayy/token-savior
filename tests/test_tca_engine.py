"""Tests for TCAEngine — tensor of symbol co-activation with NPMI scoring."""

from __future__ import annotations

from pathlib import Path

import pytest

from token_savior.tca_engine import TCAEngine


@pytest.fixture
def tca(tmp_path: Path) -> TCAEngine:
    return TCAEngine(tmp_path)


class TestTCA:
    def test_coactivation_recorded(self, tca: TCAEngine):
        for _ in range(3):
            tca.record_activation("foo")
            tca.record_activation("bar")
            tca.flush_session()
        coactive = tca.get_coactive_symbols("foo", top_k=5, min_coactivation=1)
        names = [n for n, _ in coactive]
        assert "bar" in names

    def test_pmi_positive_for_coactive(self, tca: TCAEngine):
        for _ in range(3):
            tca.record_activation("foo")
            tca.record_activation("bar")
            tca.flush_session()
        coactive = tca.get_coactive_symbols("foo", top_k=5, min_coactivation=1)
        assert coactive
        top_score = coactive[0][1]
        assert top_score > 0

    def test_no_self_coactivation(self, tca: TCAEngine):
        for _ in range(3):
            tca.record_activation("foo")
            tca.record_activation("foo")
            tca.flush_session()
        coactive = tca.get_coactive_symbols("foo", top_k=5, min_coactivation=1)
        names = [n for n, _ in coactive]
        assert "foo" not in names

    def test_min_coactivation_filter(self, tca: TCAEngine):
        tca.record_activation("foo")
        tca.record_activation("bar")
        tca.flush_session()
        filtered = tca.get_coactive_symbols("foo", top_k=5, min_coactivation=5)
        assert filtered == []
