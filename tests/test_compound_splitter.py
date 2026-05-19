"""Tests for the compound-command splitter (F3c)."""
from __future__ import annotations

import pytest

from token_savior.compactors import compact, pick_meaningful_segment


# ---------------------------------------------------------------------------
# pick_meaningful_segment — pure splitter
# ---------------------------------------------------------------------------


class TestPickMeaningfulSegment:
    def test_cd_then_command(self):
        assert pick_meaningful_segment("cd /root/foo && git status") == "git status"

    def test_cd_then_command_semicolon(self):
        assert pick_meaningful_segment("cd /root/foo ; git status") == "git status"

    def test_cd_echo_then_command(self):
        assert (
            pick_meaningful_segment("cd /tmp && echo === && git log --oneline")
            == "git log --oneline"
        )

    def test_echo_banner_then_command(self):
        assert (
            pick_meaningful_segment('echo "===== build =====" ; cargo build')
            == "cargo build"
        )

    def test_pipe_returns_none(self):
        assert pick_meaningful_segment("git status | grep modified") is None

    def test_last_meaningful_wins(self):
        # Two non-trivial segments: should return the LAST.
        assert (
            pick_meaningful_segment("cargo test && git push origin main")
            == "git push origin main"
        )

    def test_multi_cd_chain(self):
        assert (
            pick_meaningful_segment("cd /tmp && cd /root && pytest -q")
            == "pytest -q"
        )

    def test_subshell_returns_none(self):
        assert (
            pick_meaningful_segment("echo $(git rev-parse HEAD) && git status")
            is None
        )

    def test_backtick_returns_none(self):
        assert pick_meaningful_segment("git checkout `git rev-parse HEAD`") is None

    def test_just_cd_returns_none(self):
        # Single segment, no separators at all.
        assert pick_meaningful_segment("cd /root/foo") is None

    def test_comment_only_returns_none(self):
        assert pick_meaningful_segment("# just a comment") is None

    def test_comment_in_chain_skipped(self):
        # `# nope` is dropped, last meaningful is git status.
        result = pick_meaningful_segment("cd /tmp && git status ; # nope")
        assert result == "git status"

    def test_heredoc_returns_none(self):
        cmd = "cat <<EOF\nhello\nEOF"
        assert pick_meaningful_segment(cmd) is None

    def test_env_var_plus_command(self):
        # `PATH=foo` env-prefix is stripped from the segment, command kept.
        result = pick_meaningful_segment("cd /tmp && PATH=/usr/local/bin git status")
        assert result == "git status"

    def test_env_only_segment_is_trivial(self):
        # ``FOO=bar`` alone is just an assignment, not a real command.
        assert pick_meaningful_segment("FOO=bar && cd /tmp") is None

    def test_for_loop_returns_none(self):
        assert (
            pick_meaningful_segment("for f in *.py ; do echo $f ; done")
            is None
        )

    def test_if_then_returns_none(self):
        assert (
            pick_meaningful_segment("if [ -f x ]; then cat x; fi")
            is None
        )

    def test_logical_or(self):
        assert (
            pick_meaningful_segment("cd /tmp || pytest -q")
            == "pytest -q"
        )

    def test_only_trivial_segments_returns_none(self):
        assert pick_meaningful_segment("cd /tmp && echo done") is None

    def test_empty_returns_none(self):
        assert pick_meaningful_segment("") is None
        assert pick_meaningful_segment("   ") is None

    def test_quoted_separator_not_split(self):
        # The `;` inside quotes must not split.
        result = pick_meaningful_segment('cd /tmp && echo "a ; b" && git status')
        assert result == "git status"

    def test_pipe_in_quotes_not_a_pipe(self):
        # Pipe inside quotes shouldn't trigger the pipe bailout.
        result = pick_meaningful_segment("cd /tmp && git log --format='%h | %s'")
        assert result == "git log --format='%h | %s'"

    def test_single_command_with_no_separator(self):
        # Not compound; nothing to split.
        assert pick_meaningful_segment("git status") is None


# ---------------------------------------------------------------------------
# End-to-end: compact() now compacts compound commands.
# ---------------------------------------------------------------------------


GIT_STATUS_OUTPUT = """On branch f3c-compound-splitter
Your branch is up to date with 'origin/f3c-compound-splitter'.

Changes not staged for commit:
  (use "git add <file>..." to update what will be committed)
  (use "git restore <file>..." to discard changes in working directory)
	modified:   src/token_savior/compactors/__init__.py
	modified:   src/token_savior/compactors/compound.py

Untracked files:
  (use "git add <file>..." to include in what will be committed)
	tests/test_compound_splitter.py

no changes added to commit (use "git add -a" and/or "git commit -a")
"""


class TestCompactCompoundDispatch:
    def test_cd_then_git_status_compacts(self):
        result = compact("cd /root/foo && git status", GIT_STATUS_OUTPUT)
        assert result is not None
        # Savings should be positive; status compactor squashes the
        # verbose hints into a structured one-liner table.
        assert result.savings_pct > 0
        # Original text preserved in full (stdout + stderr).
        assert result.original_text == GIT_STATUS_OUTPUT
        assert result.original_bytes == len(GIT_STATUS_OUTPUT.encode("utf-8"))

    def test_cd_echo_chain_still_compacts(self):
        cmd = 'cd /root && echo "===" && git status'
        result = compact(cmd, GIT_STATUS_OUTPUT)
        assert result is not None
        assert result.original_text == GIT_STATUS_OUTPUT

    def test_pipe_blocks_compound_splitting(self):
        # A compound chain that ends in a pipe must NOT route through
        # the compound splitter. (If the very first token matches a
        # compactor directly, that's a separate path and out of scope.)
        # Here `cd` is trivial and `git log` is post-pipe — splitter bails.
        assert (
            pick_meaningful_segment("cd /tmp && git log | head -20") is None
        )

    def test_subshell_does_not_compact(self):
        assert compact("echo $(git status) && git push", "anything\n") is None

    def test_no_compactor_match_returns_none(self):
        # Compound command but the meaningful segment still doesn't match.
        result = compact("cd /tmp && some_unknown_tool --flag", "output\n")
        assert result is None

    def test_plain_command_unchanged(self):
        # The refactor must not regress the simple path.
        result = compact("git status", GIT_STATUS_OUTPUT)
        assert result is not None
        assert result.original_text == GIT_STATUS_OUTPUT
