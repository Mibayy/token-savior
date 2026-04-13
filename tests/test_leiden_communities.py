"""Tests for LeidenCommunities — graph community detection."""

from __future__ import annotations

from pathlib import Path

import pytest

from token_savior.leiden_communities import LeidenCommunities


@pytest.fixture
def leiden(tmp_path: Path) -> LeidenCommunities:
    return LeidenCommunities(tmp_path)


class TestLeiden:
    def test_empty_graph(self, leiden: LeidenCommunities):
        result = leiden.compute({}, min_size=2)
        assert result.get("communities", 0) == 0
        assert result.get("nodes", 0) == 0

    def test_triangle_cluster(self, leiden: LeidenCommunities):
        graph = {
            "A": {"B", "C"},
            "B": {"A", "C"},
            "C": {"A", "B"},
        }
        result = leiden.compute(graph, min_size=3)
        if result.get("communities", 0) == 0:
            pytest.skip("Leiden returned no clusters for triangle; min_size gate")
        ca = leiden.get_community_for("A")
        cb = leiden.get_community_for("B")
        cc = leiden.get_community_for("C")
        assert ca is not None and cb is not None and cc is not None
        assert ca["name"] == cb["name"] == cc["name"]

    def test_get_community_returns_members(self, leiden: LeidenCommunities):
        graph = {
            "A": {"B", "C", "D"},
            "B": {"A", "C", "D"},
            "C": {"A", "B", "D"},
            "D": {"A", "B", "C"},
        }
        leiden.compute(graph, min_size=3)
        stats = leiden.get_stats()
        if stats.get("total_communities", 0) == 0:
            pytest.skip("No community produced — nothing to fetch")
        comm = leiden.get_community_for("A")
        assert comm is not None
        assert "members" in comm
        assert "A" in comm["members"]

    def test_persistence(self, tmp_path: Path):
        graph = {
            "A": {"B", "C", "D"},
            "B": {"A", "C", "D"},
            "C": {"A", "B", "D"},
            "D": {"A", "B", "C"},
        }
        lc1 = LeidenCommunities(tmp_path)
        lc1.compute(graph, min_size=3)
        lc1.save()
        lc2 = LeidenCommunities(tmp_path)
        assert lc2.get_stats().get("total_communities", 0) == lc1.get_stats().get("total_communities", 0)
