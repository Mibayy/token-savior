"""Persisted tool-description embeddings: skip re-embedding all ~66 tool
descriptions on every stdio cold start (one half of the ~5.7s ts_search cold
start measured 2026-07-04). Keyed by a content+model signature so edited
descriptions or a model swap invalidate the cache automatically.
"""

from __future__ import annotations

from token_savior.server_handlers import tool_search as tsx


def test_signature_stable_and_content_sensitive():
    a = tsx._cache_signature({"x": "x: foo", "y": "y: bar"})
    b = tsx._cache_signature({"y": "y: bar", "x": "x: foo"})  # order-independent
    c = tsx._cache_signature({"x": "x: FOO", "y": "y: bar"})  # changed desc
    assert a == b
    assert a != c


def test_save_load_roundtrip(tmp_path, monkeypatch):
    monkeypatch.setattr(tsx, "_EMBED_CACHE_FILE", str(tmp_path / "e.json"))
    monkeypatch.setattr(tsx, "_STATS_DIR", str(tmp_path))
    descs = {"a": "a: foo", "b": "b: bar"}
    sig = tsx._cache_signature(descs)
    embeds = {"a": [0.1, 0.2, 0.3], "b": [0.4, 0.5, 0.6]}

    tsx._save_disk_embeds(sig, embeds)
    assert tsx._load_disk_embeds(sig) == embeds


def test_signature_mismatch_invalidates(tmp_path, monkeypatch):
    monkeypatch.setattr(tsx, "_EMBED_CACHE_FILE", str(tmp_path / "e.json"))
    monkeypatch.setattr(tsx, "_STATS_DIR", str(tmp_path))
    tsx._save_disk_embeds("sig-old", {"a": [0.1]})
    assert tsx._load_disk_embeds("sig-new") is None


def test_load_missing_file_returns_none(tmp_path, monkeypatch):
    monkeypatch.setattr(tsx, "_EMBED_CACHE_FILE", str(tmp_path / "absent.json"))
    assert tsx._load_disk_embeds("anything") is None
