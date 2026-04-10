"""Unit tests for git_tracker module."""

from unittest.mock import patch, MagicMock
import subprocess

from token_savior.git_tracker import (
    is_git_repo,
    get_head_commit,
    get_changed_files,
    get_git_status,
    GitChangeSet,
)


class TestIsGitRepo:
    def test_returns_true_for_git_repo(self):
        with patch("token_savior.git_tracker.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout="true\n")
            assert is_git_repo("/some/path") is True

    def test_returns_false_for_non_git(self):
        with patch("token_savior.git_tracker.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=128, stdout="")
            assert is_git_repo("/some/path") is False

    def test_returns_false_when_git_not_installed(self):
        with patch("token_savior.git_tracker.subprocess.run") as mock_run:
            mock_run.side_effect = FileNotFoundError
            assert is_git_repo("/some/path") is False

    def test_returns_false_on_timeout(self):
        with patch("token_savior.git_tracker.subprocess.run") as mock_run:
            mock_run.side_effect = subprocess.TimeoutExpired(cmd="git", timeout=10)
            assert is_git_repo("/some/path") is False


class TestGetHeadCommit:
    def test_returns_commit_hash(self):
        with patch("token_savior.git_tracker.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout="abc123\n")
            assert get_head_commit("/some/path") == "abc123"

    def test_returns_none_on_failure(self):
        with patch("token_savior.git_tracker.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=128, stdout="")
            assert get_head_commit("/some/path") is None

    def test_returns_none_when_git_not_installed(self):
        with patch("token_savior.git_tracker.subprocess.run") as mock_run:
            mock_run.side_effect = FileNotFoundError
            assert get_head_commit("/some/path") is None


class TestGetChangedFiles:
    def test_returns_empty_when_since_ref_is_none(self):
        changeset = get_changed_files("/some/path", None)
        assert changeset.is_empty

    def test_parses_modified_files(self):
        def mock_run(cmd, **kwargs):
            if cmd[:3] == ["git", "diff", "--name-status"] and len(cmd) == 5:
                # committed changes: git diff --name-status <ref> HEAD
                return MagicMock(returncode=0, stdout="M\tfoo.py\n")
            return MagicMock(returncode=0, stdout="")

        with patch("token_savior.git_tracker.subprocess.run", side_effect=mock_run):
            changeset = get_changed_files("/some/path", "abc123")
            assert "foo.py" in changeset.modified

    def test_parses_added_files(self):
        def mock_run(cmd, **kwargs):
            if cmd[:3] == ["git", "diff", "--name-status"] and len(cmd) == 5:
                return MagicMock(returncode=0, stdout="A\tnew_file.py\n")
            return MagicMock(returncode=0, stdout="")

        with patch("token_savior.git_tracker.subprocess.run", side_effect=mock_run):
            changeset = get_changed_files("/some/path", "abc123")
            assert "new_file.py" in changeset.added

    def test_parses_deleted_files(self):
        def mock_run(cmd, **kwargs):
            if cmd[:3] == ["git", "diff", "--name-status"] and len(cmd) == 5:
                return MagicMock(returncode=0, stdout="D\told_file.py\n")
            return MagicMock(returncode=0, stdout="")

        with patch("token_savior.git_tracker.subprocess.run", side_effect=mock_run):
            changeset = get_changed_files("/some/path", "abc123")
            assert "old_file.py" in changeset.deleted

    def test_rename_handling(self):
        def mock_run(cmd, **kwargs):
            if cmd[:3] == ["git", "diff", "--name-status"] and len(cmd) == 5:
                return MagicMock(returncode=0, stdout="R100\told.py\tnew.py\n")
            return MagicMock(returncode=0, stdout="")

        with patch("token_savior.git_tracker.subprocess.run", side_effect=mock_run):
            changeset = get_changed_files("/some/path", "abc123")
            assert "old.py" in changeset.deleted
            assert "new.py" in changeset.added

    def test_overlap_resolution_added_and_deleted_becomes_modified(self):
        """If a file appears in both added and deleted, treat as modified."""

        def mock_run(cmd, **kwargs):
            if cmd[:3] == ["git", "diff", "--name-status"] and len(cmd) == 5:
                # Committed: file was deleted
                return MagicMock(returncode=0, stdout="D\toverlap.py\n")
            if cmd == ["git", "diff", "--name-status"]:
                # Unstaged: file was added
                return MagicMock(returncode=0, stdout="A\toverlap.py\n")
            return MagicMock(returncode=0, stdout="")

        with patch("token_savior.git_tracker.subprocess.run", side_effect=mock_run):
            changeset = get_changed_files("/some/path", "abc123")
            assert "overlap.py" in changeset.modified
            assert "overlap.py" not in changeset.added
            assert "overlap.py" not in changeset.deleted

    def test_untracked_files_added(self):
        def mock_run(cmd, **kwargs):
            if cmd[:2] == ["git", "ls-files"]:
                return MagicMock(returncode=0, stdout="untracked.py\n")
            return MagicMock(returncode=0, stdout="")

        with patch("token_savior.git_tracker.subprocess.run", side_effect=mock_run):
            changeset = get_changed_files("/some/path", "abc123")
            assert "untracked.py" in changeset.added

    def test_graceful_failure_git_not_found(self):
        with patch("token_savior.git_tracker.subprocess.run") as mock_run:
            mock_run.side_effect = FileNotFoundError
            changeset = get_changed_files("/some/path", "abc123")
            assert changeset.is_empty


class TestGitChangeSet:
    def test_is_empty_true(self):
        cs = GitChangeSet()
        assert cs.is_empty

    def test_is_empty_false_with_modified(self):
        cs = GitChangeSet(modified=["foo.py"])
        assert not cs.is_empty

    def test_is_empty_false_with_added(self):
        cs = GitChangeSet(added=["foo.py"])
        assert not cs.is_empty

    def test_is_empty_false_with_deleted(self):
        cs = GitChangeSet(deleted=["foo.py"])
        assert not cs.is_empty


class TestGetGitStatus:
    def test_parses_branch_tracking_and_file_buckets(self):
        output = (
            "## main...origin/main [ahead 2, behind 1]\n"
            "M  staged.py\n"
            " M unstaged.py\n"
            "MM both.py\n"
            "?? new.py\n"
            "UU conflict.py\n"
        )

        with patch("token_savior.git_tracker.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout=output, stderr="")
            result = get_git_status("/some/path")

        assert result["ok"] is True
        assert result["branch"] == "main"
        assert result["upstream"] == "origin/main"
        assert result["ahead"] == 2
        assert result["behind"] == 1
        assert result["staged"] == ["staged.py", "both.py"]
        assert result["unstaged"] == ["unstaged.py", "both.py"]
        assert result["untracked"] == ["new.py"]
        assert result["conflicted"] == ["conflict.py"]
        assert result["clean"] is False

    def test_returns_error_when_git_fails(self):
        with patch("token_savior.git_tracker.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(
                returncode=128, stdout="", stderr="fatal: not a git repo"
            )
            result = get_git_status("/some/path")

        assert result == {"ok": False, "error": "fatal: not a git repo"}
