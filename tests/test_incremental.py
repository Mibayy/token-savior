"""Integration tests for incremental re-indexing with a real temp git repo."""

import os
import subprocess
import tempfile

import pytest

from token_savior.project_indexer import ProjectIndexer


@pytest.fixture
def git_repo():
    """Create a temporary git repo with some Python files."""
    with tempfile.TemporaryDirectory() as tmpdir:
        subprocess.run(["git", "init"], cwd=tmpdir, capture_output=True, check=True)
        subprocess.run(
            ["git", "config", "user.email", "test@test.com"],
            cwd=tmpdir,
            capture_output=True,
            check=True,
        )
        subprocess.run(
            ["git", "config", "user.name", "Test"],
            cwd=tmpdir,
            capture_output=True,
            check=True,
        )

        # Create initial files
        with open(os.path.join(tmpdir, "main.py"), "w") as f:
            f.write("def hello():\n    return 'hello'\n")

        with open(os.path.join(tmpdir, "utils.py"), "w") as f:
            f.write("def add(a, b):\n    return a + b\n")

        subprocess.run(["git", "add", "."], cwd=tmpdir, capture_output=True, check=True)
        subprocess.run(
            ["git", "commit", "-m", "initial"],
            cwd=tmpdir,
            capture_output=True,
            check=True,
        )

        yield tmpdir


class TestIncrementalReindex:
    def test_modify_file_detected(self, git_repo):
        """Modifying a file and reindexing should update the index."""
        indexer = ProjectIndexer(git_repo)
        index = indexer.index()

        # Verify initial state
        assert "main.py" in index.files
        funcs = [f.name for f in index.files["main.py"].functions]
        assert "hello" in funcs

        # Modify the file - add a new function
        with open(os.path.join(git_repo, "main.py"), "w") as f:
            f.write("def hello():\n    return 'hello'\n\ndef goodbye():\n    return 'bye'\n")

        # Reindex just this file
        indexer.reindex_file("main.py")

        funcs = [f.name for f in index.files["main.py"].functions]
        assert "hello" in funcs
        assert "goodbye" in funcs

    def test_add_new_file(self, git_repo):
        """Adding a new file and reindexing should include it."""
        indexer = ProjectIndexer(git_repo)
        index = indexer.index()

        initial_file_count = index.total_files
        assert "extra.py" not in index.files

        # Add a new file
        with open(os.path.join(git_repo, "extra.py"), "w") as f:
            f.write("class Foo:\n    pass\n")

        # Reindex the new file
        indexer.reindex_file("extra.py", skip_graph_rebuild=True)
        indexer.rebuild_graphs()

        assert "extra.py" in index.files
        assert index.total_files == initial_file_count + 1
        assert "Foo" in index.symbol_table

    def test_delete_file(self, git_repo):
        """Removing a file via remove_file should remove it from the index."""
        indexer = ProjectIndexer(git_repo)
        index = indexer.index()

        assert "utils.py" in index.files
        assert "add" in index.symbol_table
        initial_file_count = index.total_files

        # Remove the file from the index
        indexer.remove_file("utils.py")
        indexer.rebuild_graphs()

        assert "utils.py" not in index.files
        assert index.total_files == initial_file_count - 1
        assert "add" not in index.symbol_table

    def test_stats_correct_after_incremental(self, git_repo):
        """Stats should be consistent after incremental updates."""
        indexer = ProjectIndexer(git_repo)
        index = indexer.index()

        # Add a file with known content
        with open(os.path.join(git_repo, "new.py"), "w") as f:
            f.write("def func_a():\n    pass\n\ndef func_b():\n    pass\n")

        indexer.reindex_file("new.py", skip_graph_rebuild=True)

        # Remove a file
        indexer.remove_file("utils.py")

        indexer.rebuild_graphs()

        # Recount to verify
        expected_files = len(index.files)
        expected_functions = sum(len(m.functions) for m in index.files.values())
        expected_classes = sum(len(m.classes) for m in index.files.values())
        expected_lines = sum(m.total_lines for m in index.files.values())

        assert index.total_files == expected_files
        assert index.total_functions == expected_functions
        assert index.total_classes == expected_classes
        assert index.total_lines == expected_lines

    def test_batch_reindex_with_skip_graph_rebuild(self, git_repo):
        """Batch reindexing with skip_graph_rebuild should work correctly."""
        indexer = ProjectIndexer(git_repo)
        index = indexer.index()

        # Modify both files
        with open(os.path.join(git_repo, "main.py"), "w") as f:
            f.write("def hello():\n    return 'hi'\n\ndef world():\n    return 'world'\n")

        with open(os.path.join(git_repo, "utils.py"), "w") as f:
            f.write("def multiply(a, b):\n    return a * b\n")

        # Batch reindex
        indexer.reindex_file("main.py", skip_graph_rebuild=True)
        indexer.reindex_file("utils.py", skip_graph_rebuild=True)
        indexer.rebuild_graphs()

        funcs = {f.name for f in index.files["main.py"].functions}
        assert funcs == {"hello", "world"}

        funcs = {f.name for f in index.files["utils.py"].functions}
        assert funcs == {"multiply"}
        assert "add" not in index.symbol_table
