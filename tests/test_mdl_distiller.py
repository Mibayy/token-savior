"""Tests for MDL-based observation distillation."""

from __future__ import annotations

from token_savior import mdl_distiller


class TestMDLPrimitives:
    def test_description_length(self):
        short = mdl_distiller.description_length("short")
        longer = mdl_distiller.description_length("a much longer piece of content here")
        assert longer > short > 0

    def test_shared_tokens_detects_common_words(self):
        texts = [
            "never commit secrets to the repository",
            "do not commit secrets directly",
            "commit secrets into the vault not the repo",
        ]
        shared = mdl_distiller.compute_shared_tokens(texts, min_freq=0.6)
        assert "commit" in shared
        assert "secrets" in shared

    def test_delta_shorter_than_original(self):
        abstraction = "Never commit secrets to the repository"
        content = "Never commit secrets to the repository in any case"
        delta = mdl_distiller.delta_encode(content, abstraction)
        assert len(delta) < len(content)


class TestFindCandidates:
    def test_no_cluster_below_min_size(self):
        obs = [
            {"id": 1, "content": "never commit secrets to the repo", "type": "guardrail"},
            {"id": 2, "content": "never commit secrets to the repo", "type": "guardrail"},
        ]
        clusters = mdl_distiller.find_distillation_candidates(
            obs, jaccard_threshold=0.3, min_cluster_size=3
        )
        assert clusters == []

    def test_cluster_detected(self):
        base = "never commit secrets to the repository avoid credentials"
        obs = [
            {"id": i, "content": base + f" case {i}", "type": "guardrail"}
            for i in range(4)
        ]
        clusters = mdl_distiller.find_distillation_candidates(
            obs, jaccard_threshold=0.3, min_cluster_size=3
        )
        assert len(clusters) >= 1

    def test_mdl_criterion(self):
        base = "never commit secrets to the repository avoid credentials"
        obs = [
            {"id": i, "content": base + f" variant {i}", "type": "guardrail"}
            for i in range(5)
        ]
        clusters = mdl_distiller.find_distillation_candidates(
            obs, jaccard_threshold=0.3, min_cluster_size=3,
            compression_required=0.1,
        )
        assert clusters, "expected at least one compression-qualifying cluster"
        for c in clusters:
            assert c.mdl_after < c.mdl_before
