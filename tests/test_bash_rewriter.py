"""Unit tests for token_savior.bash_rewriter."""
from __future__ import annotations

import pytest

from token_savior.bash_rewriter import is_unsafe_to_rewrite, rewrite


# ---------------------------------------------------------------------------
# Safety / pass-through
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "cmd",
    [
        "git status | grep foo",
        "git diff > /tmp/x",
        "git log; echo done",
        "pytest && echo ok",
        "echo $(git rev-parse HEAD)",
        "git status `wc -l`",
        "git diff -- README.md",
    ],
)
def test_unsafe_commands_are_passed_through(cmd: str) -> None:
    new, reason = rewrite(cmd)
    assert new == cmd
    assert reason is None
    assert is_unsafe_to_rewrite(cmd) is True


@pytest.mark.parametrize(
    "cmd",
    [
        "git status -v",
        "git diff --verbose",
        "pytest -vv",
        "cargo test -v",
    ],
)
def test_verbose_intent_is_respected(cmd: str) -> None:
    new, reason = rewrite(cmd)
    assert new == cmd
    assert reason is None


def test_unknown_command_passes_through() -> None:
    new, reason = rewrite("rsync -a src dst")
    assert new == "rsync -a src dst"
    assert reason is None


def test_empty_command() -> None:
    new, reason = rewrite("")
    assert new == ""
    assert reason is None


def test_non_string() -> None:
    new, reason = rewrite(None)  # type: ignore[arg-type]
    assert new is None
    assert reason is None


# ---------------------------------------------------------------------------
# git status
# ---------------------------------------------------------------------------


def test_git_status_bare() -> None:
    new, reason = rewrite("git status")
    assert new == "git status --porcelain=v2 --branch"
    assert reason and "porcelain" in reason


def test_git_status_with_flag_skipped() -> None:
    # ``git status --short`` is the user's call — leave alone.
    new, _ = rewrite("git status --short")
    assert new == "git status --short"


# ---------------------------------------------------------------------------
# git diff
# ---------------------------------------------------------------------------


def test_git_diff_bare() -> None:
    new, reason = rewrite("git diff")
    assert new == "git diff --no-color --stat=200,5"
    assert reason


def test_git_diff_with_positional_refs() -> None:
    new, _ = rewrite("git diff main feature")
    assert new == "git diff --no-color --stat=200,5 main feature"


def test_git_diff_with_user_flag_skipped() -> None:
    new, _ = rewrite("git diff --staged")
    assert new == "git diff --staged"


# ---------------------------------------------------------------------------
# git log
# ---------------------------------------------------------------------------


def test_git_log_bare() -> None:
    new, _ = rewrite("git log")
    assert new == "git log --oneline --decorate -n 20"


def test_git_log_with_n_flag() -> None:
    new, _ = rewrite("git log -n 5")
    assert new == "git log --oneline --decorate -n 5"


def test_git_log_with_max_count_eq() -> None:
    new, _ = rewrite("git log --max-count=12")
    assert new == "git log --oneline --decorate -n 12"


def test_git_log_with_other_flag_skipped() -> None:
    new, _ = rewrite("git log --graph")
    assert new == "git log --graph"


# ---------------------------------------------------------------------------
# tsc
# ---------------------------------------------------------------------------


def test_tsc_bare() -> None:
    new, _ = rewrite("tsc")
    assert new == "tsc --pretty false"


def test_npx_tsc_bare() -> None:
    new, _ = rewrite("npx tsc")
    assert new == "npx tsc --pretty false"


def test_tsc_with_arg_skipped() -> None:
    new, _ = rewrite("tsc --noEmit")
    assert new == "tsc --noEmit"


# ---------------------------------------------------------------------------
# pytest
# ---------------------------------------------------------------------------


def test_pytest_bare() -> None:
    new, _ = rewrite("pytest")
    assert new == "pytest -q --tb=line"


def test_pytest_with_path() -> None:
    new, _ = rewrite("pytest tests/test_foo.py")
    assert new == "pytest tests/test_foo.py -q --tb=line"


def test_pytest_with_q_skipped() -> None:
    new, _ = rewrite("pytest -q")
    assert new == "pytest -q"


def test_pytest_with_tb_skipped() -> None:
    new, _ = rewrite("pytest --tb=short")
    assert new == "pytest --tb=short"


def test_python_m_pytest() -> None:
    new, _ = rewrite("python -m pytest tests/")
    assert new == "python -m pytest tests/ -q --tb=line"


# ---------------------------------------------------------------------------
# npm/yarn/pnpm test
# ---------------------------------------------------------------------------


def test_npm_test_bare() -> None:
    new, _ = rewrite("npm test")
    assert new == "npm test --silent"


def test_yarn_test_bare() -> None:
    new, _ = rewrite("yarn test")
    assert new == "yarn test --silent"


def test_pnpm_test_bare() -> None:
    new, _ = rewrite("pnpm test")
    assert new == "pnpm test --silent"


def test_npm_run_build_unchanged() -> None:
    new, _ = rewrite("npm run build")
    assert new == "npm run build"


# ---------------------------------------------------------------------------
# cargo test
# ---------------------------------------------------------------------------


def test_cargo_test_bare() -> None:
    new, _ = rewrite("cargo test")
    assert new == "cargo test --quiet"


def test_cargo_test_with_quiet_skipped() -> None:
    new, _ = rewrite("cargo test --quiet")
    assert new == "cargo test --quiet"


# ---------------------------------------------------------------------------
# gh
# ---------------------------------------------------------------------------


def test_gh_run_watch_bare() -> None:
    new, _ = rewrite("gh run watch")
    assert new == "gh run watch --exit-status"


def test_gh_run_watch_with_id() -> None:
    new, _ = rewrite("gh run watch 12345")
    assert new == "gh run watch 12345 --exit-status"


def test_gh_run_watch_already_exit_status_skipped() -> None:
    new, _ = rewrite("gh run watch --exit-status")
    assert new == "gh run watch --exit-status"


def test_gh_pr_list_bare() -> None:
    new, _ = rewrite("gh pr list")
    assert new == "gh pr list --limit 30"


def test_gh_pr_list_with_limit_skipped() -> None:
    new, _ = rewrite("gh pr list --limit 5")
    assert new == "gh pr list --limit 5"


# ---------------------------------------------------------------------------
# docker
# ---------------------------------------------------------------------------


def test_docker_ps_bare() -> None:
    new, _ = rewrite("docker ps")
    assert "docker ps --format" in new
    assert "{{.Names}}" in new


def test_docker_ps_with_format_skipped() -> None:
    cmd = 'docker ps --format json'
    new, _ = rewrite(cmd)
    assert new == cmd


# ---------------------------------------------------------------------------
# Whitespace normalization
# ---------------------------------------------------------------------------


def test_leading_trailing_whitespace_stripped_in_rewrite_pathway() -> None:
    # The rewriter only acts on the stripped form. When a rule matches,
    # we want the rewritten command to be the canonical form (no extra
    # spaces) — easier for the hook to ship downstream.
    new, _ = rewrite("   git status   ")
    assert new == "git status --porcelain=v2 --branch"
