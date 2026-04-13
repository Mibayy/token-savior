"""Tests for SessionWarmStart — cross-session similarity via 32-d signature."""

from __future__ import annotations

import math
from pathlib import Path

import pytest

from token_savior.session_warmstart import SessionWarmStart, compute_signature


def _cos(a: list[float], b: list[float]) -> float:
    num = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    if na == 0 or nb == 0:
        return 0.0
    return num / (na * nb)


class TestSignature:
    def test_signature_length(self):
        sig = compute_signature({
            "tool_counts": {"get_function_source": 5, "search_codebase": 3},
            "duration_min": 12.0,
            "turns": 15,
            "obs_accessed": 2,
            "symbols": ["foo", "bar"],
            "mode": "code",
        })
        assert len(sig) == SessionWarmStart.SIGNATURE_DIM == 32
        for v in sig:
            assert 0.0 <= v <= 1.0

    def test_cosine_identity(self):
        sig = compute_signature({
            "tool_counts": {"a": 2},
            "duration_min": 5.0,
            "turns": 3,
            "obs_accessed": 1,
            "symbols": ["x"],
            "mode": "code",
        })
        assert _cos(sig, sig) == pytest.approx(1.0)

    def test_cosine_orthogonal(self):
        a = [1.0] + [0.0] * 31
        b = [0.0] + [1.0] + [0.0] * 30
        assert _cos(a, b) == pytest.approx(0.0)


class TestWarmStartStore:
    def test_save_and_find(self, tmp_path: Path):
        ws = SessionWarmStart(tmp_path)
        data = {
            "tool_counts": {"get_function_source": 5, "search_codebase": 3},
            "duration_min": 12.0,
            "turns": 15,
            "obs_accessed": 2,
            "symbols": ["foo"],
            "mode": "code",
        }
        sig = ws.save_session_signature(1, "/proj/a", data)
        sim = ws.find_similar_sessions(sig, project_root="/proj/a", top_k=3, min_sim=0.5)
        assert sim
        entry, score = sim[0]
        assert score > 0.9

    def test_no_cross_project(self, tmp_path: Path):
        ws = SessionWarmStart(tmp_path)
        data = {
            "tool_counts": {"x": 1},
            "duration_min": 5.0,
            "turns": 3,
            "obs_accessed": 0,
            "symbols": [],
            "mode": "code",
        }
        sig_a = ws.save_session_signature(1, "/proj/a", data)
        found_from_b = ws.find_similar_sessions(sig_a, project_root="/proj/b", top_k=3, min_sim=0.1)
        assert found_from_b == []
