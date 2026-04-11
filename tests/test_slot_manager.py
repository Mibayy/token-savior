"""Tests for the SlotManager class."""

import os
import time

import pytest

from token_savior.slot_manager import SlotManager, _ProjectSlot


class TestResolve:
    """Tests for SlotManager.resolve()."""

    def test_no_projects_returns_error(self):
        mgr = SlotManager(cache_version=2)
        slot, err = mgr.resolve()
        assert slot is None
        assert "No projects registered" in err

    def test_single_project_auto_selects(self, tmp_path):
        mgr = SlotManager(cache_version=2)
        root = str(tmp_path)
        mgr.projects[root] = _ProjectSlot(root=root)
        slot, err = mgr.resolve()
        assert err == ""
        assert slot is not None
        assert slot.root == root
        assert mgr.active_root == root

    def test_explicit_hint_finds_by_basename(self, tmp_path):
        mgr = SlotManager(cache_version=2)
        root = str(tmp_path)
        mgr.projects[root] = _ProjectSlot(root=root)
        slot, err = mgr.resolve(os.path.basename(root))
        assert err == ""
        assert slot is not None
        assert slot.root == root

    def test_explicit_hint_finds_by_abspath(self, tmp_path):
        mgr = SlotManager(cache_version=2)
        root = str(tmp_path)
        mgr.projects[root] = _ProjectSlot(root=root)
        slot, err = mgr.resolve(root)
        assert err == ""
        assert slot.root == root

    def test_unknown_hint_returns_error(self, tmp_path):
        mgr = SlotManager(cache_version=2)
        root = str(tmp_path)
        mgr.projects[root] = _ProjectSlot(root=root)
        slot, err = mgr.resolve("nonexistent-project")
        assert slot is None
        assert "not found" in err

    def test_active_root_used_when_no_hint(self, tmp_path):
        mgr = SlotManager(cache_version=2)
        root1 = str(tmp_path / "proj1")
        root2 = str(tmp_path / "proj2")
        os.makedirs(root1, exist_ok=True)
        os.makedirs(root2, exist_ok=True)
        mgr.projects[root1] = _ProjectSlot(root=root1)
        mgr.projects[root2] = _ProjectSlot(root=root2)
        mgr.active_root = root2
        slot, err = mgr.resolve()
        assert err == ""
        assert slot.root == root2

    def test_multiple_projects_no_active_returns_error(self, tmp_path):
        mgr = SlotManager(cache_version=2)
        root1 = str(tmp_path / "proj1")
        root2 = str(tmp_path / "proj2")
        os.makedirs(root1, exist_ok=True)
        os.makedirs(root2, exist_ok=True)
        mgr.projects[root1] = _ProjectSlot(root=root1)
        mgr.projects[root2] = _ProjectSlot(root=root2)
        slot, err = mgr.resolve()
        assert slot is None
        assert "Multiple projects" in err


class TestEnsure:
    """Tests for SlotManager.ensure()."""

    def test_new_project_builds_index(self, tmp_path):
        (tmp_path / "main.py").write_text("def hello():\n    return 'world'\n")
        mgr = SlotManager(cache_version=2)
        slot = _ProjectSlot(root=str(tmp_path))
        mgr.ensure(slot)
        assert slot.indexer is not None
        assert slot.query_fns is not None

    def test_already_indexed_is_noop(self, tmp_path):
        (tmp_path / "main.py").write_text("x = 1\n")
        mgr = SlotManager(cache_version=2)
        slot = _ProjectSlot(root=str(tmp_path))
        mgr.ensure(slot)
        indexer_ref = slot.indexer
        mgr.ensure(slot)
        assert slot.indexer is indexer_ref  # same object, not rebuilt


class TestCheckMtimeChanges:
    """Tests for SlotManager.check_mtime_changes()."""

    def test_no_changes_returns_empty(self, tmp_path):
        (tmp_path / "main.py").write_text("x = 1\n")
        mgr = SlotManager(cache_version=2)
        slot = _ProjectSlot(root=str(tmp_path))
        mgr.build(slot)
        # Immediately after build, mtimes should match
        changed = mgr.check_mtime_changes(slot)
        assert changed == []

    def test_modified_file_returned(self, tmp_path):
        (tmp_path / "main.py").write_text("x = 1\n")
        mgr = SlotManager(cache_version=2)
        slot = _ProjectSlot(root=str(tmp_path))
        mgr.build(slot)
        # Modify the file with a different mtime
        time.sleep(0.05)
        (tmp_path / "main.py").write_text("x = 2\n")
        changed = mgr.check_mtime_changes(slot)
        assert "main.py" in changed


class TestRegisterRoots:
    """Tests for SlotManager.register_roots()."""

    def test_register_creates_slots(self, tmp_path):
        mgr = SlotManager(cache_version=2)
        root = str(tmp_path)
        mgr.register_roots([root])
        assert root in mgr.projects
        assert mgr.active_root == root

    def test_register_does_not_overwrite(self, tmp_path):
        mgr = SlotManager(cache_version=2)
        root = str(tmp_path)
        mgr.register_roots([root])
        slot_ref = mgr.projects[root]
        mgr.register_roots([root])
        assert mgr.projects[root] is slot_ref


class TestBuild:
    """Tests for SlotManager.build()."""

    def test_build_indexes_files(self, tmp_path):
        (tmp_path / "app.py").write_text("def run():\n    pass\n")
        mgr = SlotManager(cache_version=2)
        slot = _ProjectSlot(root=str(tmp_path))
        mgr.build(slot)
        assert slot.indexer is not None
        idx = slot.indexer._project_index
        assert idx.total_files >= 1
        assert idx.total_functions >= 1
