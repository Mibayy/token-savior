"""Tests for token_savior.cache_ops.CacheManager."""

from __future__ import annotations

import json
import os

import pytest

from token_savior.cache_ops import CacheManager
from token_savior.models import ProjectIndex


def _make_index(root: str) -> ProjectIndex:
    """Create a minimal but valid ProjectIndex for testing."""
    return ProjectIndex(
        root_path=root,
        files={},
        global_dependency_graph={},
        reverse_dependency_graph={},
        import_graph={},
        reverse_import_graph={},
        symbol_table={},
        total_files=3,
        total_lines=42,
        total_functions=5,
        total_classes=1,
        index_build_time_seconds=0.1,
        index_memory_bytes=1024,
        last_indexed_git_ref="abc123",
        file_mtimes={"foo.py": 1234567890.0},
    )


class TestCacheManagerSaveLoad:
    """Roundtrip: save then load."""

    def test_roundtrip(self, tmp_path):
        root = str(tmp_path)
        mgr = CacheManager(root, cache_version=2)
        idx = _make_index(root)

        mgr.save(idx)
        loaded = mgr.load()

        assert loaded is not None
        assert loaded.root_path == idx.root_path
        assert loaded.total_files == idx.total_files
        assert loaded.total_lines == idx.total_lines
        assert loaded.total_functions == idx.total_functions
        assert loaded.total_classes == idx.total_classes
        assert loaded.last_indexed_git_ref == idx.last_indexed_git_ref
        assert loaded.file_mtimes == idx.file_mtimes
        assert loaded.index_build_time_seconds == idx.index_build_time_seconds
        assert loaded.index_memory_bytes == idx.index_memory_bytes

    def test_cache_file_exists_after_save(self, tmp_path):
        root = str(tmp_path)
        mgr = CacheManager(root, cache_version=2)
        mgr.save(_make_index(root))

        assert os.path.isfile(os.path.join(root, CacheManager.FILENAME))


class TestCacheManagerLoadFailures:
    """load() returns None for missing, corrupt, or incompatible caches."""

    def test_missing_file(self, tmp_path):
        mgr = CacheManager(str(tmp_path), cache_version=2)
        assert mgr.load() is None

    def test_corrupt_json(self, tmp_path):
        root = str(tmp_path)
        path = os.path.join(root, CacheManager.FILENAME)
        with open(path, "w") as f:
            f.write("{not valid json!!")

        mgr = CacheManager(root, cache_version=2)
        assert mgr.load() is None

    def test_version_mismatch(self, tmp_path):
        root = str(tmp_path)
        # Save with version 2
        mgr_v2 = CacheManager(root, cache_version=2)
        mgr_v2.save(_make_index(root))

        # Load with version 99
        mgr_v99 = CacheManager(root, cache_version=99)
        assert mgr_v99.load() is None

    def test_missing_index_key(self, tmp_path):
        root = str(tmp_path)
        path = os.path.join(root, CacheManager.FILENAME)
        with open(path, "w") as f:
            json.dump({"version": 2}, f)

        mgr = CacheManager(root, cache_version=2)
        # Missing "index" key should cause load to fail gracefully
        assert mgr.load() is None


class TestCacheManagerLegacyMigration:
    """Legacy filename is auto-migrated to the new name."""

    def test_path_migrates_legacy(self, tmp_path):
        root = str(tmp_path)
        legacy_path = os.path.join(root, CacheManager.LEGACY_FILENAME)
        new_path = os.path.join(root, CacheManager.FILENAME)

        # Create a file with the legacy name
        with open(legacy_path, "w") as f:
            f.write("{}")

        mgr = CacheManager(root, cache_version=2)
        result = mgr.path()

        # Should have migrated
        assert result == new_path
        assert os.path.exists(new_path)
        assert not os.path.exists(legacy_path)

    def test_path_prefers_new_name(self, tmp_path):
        root = str(tmp_path)
        new_path = os.path.join(root, CacheManager.FILENAME)

        # Create a file with the new name already
        with open(new_path, "w") as f:
            f.write("{}")

        mgr = CacheManager(root, cache_version=2)
        result = mgr.path()

        assert result == new_path

    def test_legacy_roundtrip(self, tmp_path):
        """Save with legacy name present, ensure load works after migration."""
        root = str(tmp_path)
        legacy_path = os.path.join(root, CacheManager.LEGACY_FILENAME)
        idx = _make_index(root)

        # First, save normally to get a valid cache
        mgr = CacheManager(root, cache_version=2)
        mgr.save(idx)

        # Rename to legacy name
        new_path = os.path.join(root, CacheManager.FILENAME)
        os.rename(new_path, legacy_path)
        assert not os.path.exists(new_path)

        # Load should migrate and succeed
        mgr2 = CacheManager(root, cache_version=2)
        loaded = mgr2.load()
        assert loaded is not None
        assert loaded.total_files == idx.total_files
        assert os.path.exists(new_path)


class TestIndexSerialization:
    """Static methods index_to_dict / index_from_dict."""

    def test_to_dict_returns_dict(self):
        idx = _make_index("/tmp/test")
        result = CacheManager.index_to_dict(idx)
        assert isinstance(result, dict)
        assert result["root_path"] == "/tmp/test"
        assert result["total_files"] == 3

    def test_from_dict_restores_index(self):
        idx = _make_index("/tmp/test")
        d = CacheManager.index_to_dict(idx)
        restored = CacheManager.index_from_dict(d)
        assert isinstance(restored, ProjectIndex)
        assert restored.root_path == "/tmp/test"
        assert restored.total_files == 3

    def test_sets_roundtrip(self):
        """Sets in dependency graphs survive serialization (sorted lists -> sets)."""
        idx = _make_index("/tmp/test")
        idx.global_dependency_graph = {"foo": {"bar", "baz"}}
        idx.reverse_dependency_graph = {"bar": {"foo"}}

        d = CacheManager.index_to_dict(idx)
        # In dict form, sets become sorted lists
        assert d["global_dependency_graph"]["foo"] == ["bar", "baz"]

        restored = CacheManager.index_from_dict(d)
        assert restored.global_dependency_graph["foo"] == {"bar", "baz"}
        assert restored.reverse_dependency_graph["bar"] == {"foo"}
