"""Tests for the SlotManager class."""

import os
import time

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


def _repo_with_nested_worktree(tmp_path):
    """A main checkout plus a linked worktree nested inside it, the way
    Claude Code lays them out (`repo/.claude/worktrees/<branch>`). The
    worktree carries a `.git` FILE (gitfile pointer), the main checkout a
    `.git` directory — that difference is what the routing relies on."""
    repo = tmp_path / "repo"
    (repo / "src").mkdir(parents=True)
    (repo / ".git").mkdir()
    wt = repo / ".claude" / "worktrees" / "fix-98"
    (wt / "src").mkdir(parents=True)
    (wt / ".git").write_text("gitdir: ../../../.git/worktrees/fix-98\n")
    return str(repo), str(wt)


class TestResolvePath:
    """Per-call routing of absolute paths — the signal parallel agents in
    sibling worktrees rely on, since they share one server and one
    active_root."""

    def test_nested_worktree_wins_over_parent(self, tmp_path):
        repo, wt = _repo_with_nested_worktree(tmp_path)
        mgr = SlotManager(cache_version=2)
        mgr.register_roots([repo])
        slot = mgr.resolve_path(os.path.join(wt, "src", "mod.py"))
        assert slot is not None
        assert slot.root == wt, "path inside the worktree must reach the worktree slot"
        assert wt in mgr.projects, "unregistered worktree root registers on the fly"

    def test_path_inside_parent_resolves_parent(self, tmp_path):
        repo, _wt = _repo_with_nested_worktree(tmp_path)
        mgr = SlotManager(cache_version=2)
        mgr.register_roots([repo])
        slot = mgr.resolve_path(os.path.join(repo, "src", "mod.py"))
        assert slot is not None and slot.root == repo
        assert list(mgr.projects) == [repo], "no spurious registration for in-tree paths"

    def test_does_not_touch_active_root(self, tmp_path):
        repo, wt = _repo_with_nested_worktree(tmp_path)
        mgr = SlotManager(cache_version=2)
        mgr.register_roots([repo])
        mgr.active_root = repo
        mgr.resolve_path(os.path.join(wt, "src", "mod.py"))
        assert mgr.active_root == repo, (
            "implicit path routing must never mutate the shared default — "
            "that is the race between parallel agents"
        )

    def test_relative_path_is_no_signal(self, tmp_path):
        mgr = SlotManager(cache_version=2)
        mgr.register_roots([str(tmp_path)])
        assert mgr.resolve_path("src/mod.py") is None

    def test_path_outside_any_project(self, tmp_path):
        mgr = SlotManager(cache_version=2)
        bare = tmp_path / "no-marker" / "deep"
        bare.mkdir(parents=True)
        assert mgr.resolve_path(str(bare / "f.py")) is None


class TestResolvePathHints:
    """resolve() with a path-like hint routes to the OWNING project."""

    def test_worktree_path_hint_resolves_worktree(self, tmp_path):
        repo, wt = _repo_with_nested_worktree(tmp_path)
        mgr = SlotManager(cache_version=2)
        mgr.register_roots([repo])
        slot, err = mgr.resolve(os.path.join(wt, "src"))
        assert err == ""
        assert slot.root == wt

    def test_subdir_hint_resolves_owner_not_itself(self, tmp_path):
        """Before: a real-but-unregistered subdirectory got registered as a
        project of its own. Now it routes to the project that owns it."""
        repo, _wt = _repo_with_nested_worktree(tmp_path)
        mgr = SlotManager(cache_version=2)
        mgr.register_roots([repo])
        sub = os.path.join(repo, "src")
        slot, err = mgr.resolve(sub)
        assert err == ""
        assert slot.root == repo
        assert sub not in mgr.projects

    def test_markerless_dir_hint_still_registers_as_before(self, tmp_path):
        """A directory with no marker anywhere up the tree keeps the old
        contract: register it as given."""
        mgr = SlotManager(cache_version=2)
        other = str(tmp_path / "proj-registered")
        os.makedirs(other)
        mgr.projects[other] = _ProjectSlot(root=other)
        bare = tmp_path / "pile-of-files"
        bare.mkdir()
        slot, err = mgr.resolve(str(bare))
        assert err == ""
        assert slot.root == str(bare)


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


class TestCacheConfigKey:
    """A cache built under one configuration must not be served under another (#61)."""

    def _git_repo(self, tmp_path):
        import subprocess

        root = str(tmp_path)
        (tmp_path / "a.py").write_text("def fa():\n    pass\n")
        (tmp_path / "b.php").write_text("<?php\nfunction fb() { return 1; }\n")
        for cmd in (
            ["git", "init", "-q"],
            ["git", "config", "user.email", "t@t"],
            ["git", "config", "user.name", "t"],
            ["git", "add", "."],
            ["git", "commit", "-qm", "init"],
        ):
            subprocess.run(cmd, cwd=root, check=True, capture_output=True)
        return root

    def _ensure(self, root):
        mgr = SlotManager(cache_version=2)
        mgr.projects[root] = _ProjectSlot(root=root)
        slot = mgr.projects[root]
        mgr.ensure(slot)
        return slot

    def test_include_pattern_change_invalidates_cache(self, tmp_path, monkeypatch):
        root = self._git_repo(tmp_path)
        monkeypatch.setenv("INCLUDE_PATTERNS", "**/*.py")
        slot = self._ensure(root)
        files = slot.indexer._project_index.files
        assert "a.py" in files
        assert "b.php" not in files

        # Same HEAD, wider patterns: the cached narrow index must NOT be served.
        monkeypatch.setenv("INCLUDE_PATTERNS", "**/*.py:**/*.php")
        slot2 = self._ensure(root)
        files2 = slot2.indexer._project_index.files
        assert "b.php" in files2, "stale cache served after INCLUDE_PATTERNS change"

    def test_cache_hit_indexer_uses_env_patterns(self, tmp_path, monkeypatch):
        root = self._git_repo(tmp_path)
        monkeypatch.setenv("INCLUDE_PATTERNS", "**/*.py")
        self._ensure(root)  # builds + saves the cache
        slot = self._ensure(root)  # same config: served from cache
        # The cache-hit path must configure its indexer like build() would,
        # or incremental updates re-index under the wrong patterns.
        assert slot.indexer.include_patterns == ["**/*.py"]

    def test_same_config_reload_serves_cache(self, tmp_path, monkeypatch):
        root = self._git_repo(tmp_path)
        monkeypatch.setenv("INCLUDE_PATTERNS", "**/*.py")
        self._ensure(root)
        cache_path = os.path.join(root, ".token-savior-cache.json")
        before = os.path.getmtime(cache_path)
        slot = self._ensure(root)
        # Served from cache: index present, cache file not rewritten.
        assert "a.py" in slot.indexer._project_index.files
        assert os.path.getmtime(cache_path) == before
