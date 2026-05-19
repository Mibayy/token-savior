"""Tests for v4.3.0 F3a — pytest regex fix + git/gh extra compactors."""
from __future__ import annotations

from token_savior.compactors import compact, registry
from token_savior.compactors.gh import (
    GhIssueViewCompactor,
    GhPrDiffCompactor,
    GhPrViewCompactor,
    GhRepoViewCompactor,
)
from token_savior.compactors.git import (
    GitFetchCompactor,
)
from token_savior.compactors.pytest_ import PytestCompactor


# ---------------------------------------------------------------------------
# PytestCompactor.matches() — extended forms
# ---------------------------------------------------------------------------


def test_pytest_matches_bare_pytest():
    assert PytestCompactor().matches("pytest")


def test_pytest_matches_pytest_with_args():
    assert PytestCompactor().matches("pytest tests/foo.py -v")


def test_pytest_matches_python3_module_form():
    assert PytestCompactor().matches("python3 -m pytest tests/")


def test_pytest_matches_python_module_form():
    assert PytestCompactor().matches("python -m pytest")


def test_pytest_matches_absolute_python_path():
    assert PytestCompactor().matches("/root/.venv/bin/python3 -m pytest tests/")


def test_pytest_matches_uv_run_form():
    assert PytestCompactor().matches("uv run pytest tests/")


def test_pytest_matches_poetry_run_form():
    assert PytestCompactor().matches("poetry run pytest")


def test_pytest_does_not_match_unrelated():
    c = PytestCompactor()
    assert not c.matches("pytestify --foo")
    assert not c.matches("mypytest")
    assert not c.matches("echo pytest")
    assert not c.matches("npm test")


# Sanity: the existing failures-only path still works through the dispatcher
# when the command uses the new wrapped form.
def test_pytest_dispatch_through_python3_module_form():
    PYTEST_OUTPUT = """============================= test session starts ==============================
platform linux -- Python 3.12.3, pytest-8.3.2, pluggy-1.5.0
rootdir: /root/ts-f3a
collected 10 items

tests/test_x.py .........F                                              [100%]

=================================== FAILURES ===================================
______________________________ test_thing ________________________________

    def test_thing():
>       assert 1 == 2
E       assert 1 == 2

tests/test_x.py:5: AssertionError
=========================== short test summary info ============================
FAILED tests/test_x.py::test_thing - assert 1 == 2
================== 1 failed, 9 passed in 0.42s ===============================
"""
    r = compact("python3 -m pytest tests/test_x.py", PYTEST_OUTPUT)
    assert r is not None
    assert "test_thing" in r.text
    assert "assert 1 == 2" in r.text
    assert "1 failed" in r.text


# ---------------------------------------------------------------------------
# GitFetchCompactor
# ---------------------------------------------------------------------------


GIT_FETCH_OUTPUT = """remote: Enumerating objects: 145, done.
remote: Counting objects: 100% (145/145), done.
remote: Compressing objects: 100% (50/50), done.
remote: Total 95 (delta 70), reused 80 (delta 60), pack-reused 0
Receiving objects: 100% (95/95), 12.34 KiB | 6.17 MiB/s, done.
Resolving deltas: 100% (70/70), completed with 25 local objects.
From github.com:Mibayy/token-savior
   95213c6..a1b2c3d  main       -> origin/main
 * [new branch]      feature/x  -> origin/feature/x
   abc1234..def5678  bugfix/y   -> origin/bugfix/y
"""


def test_git_fetch_keeps_from_and_refs_drops_progress():
    r = compact("git fetch", GIT_FETCH_OUTPUT)
    assert r is not None
    assert "From github.com:Mibayy/token-savior" in r.text
    assert "main       -> origin/main" in r.text
    assert "[new branch]" in r.text
    assert "Counting objects" not in r.text
    assert "Receiving objects" not in r.text
    assert "Resolving deltas" not in r.text
    assert r.savings_pct >= 50.0


def test_git_fetch_origin_matches():
    assert GitFetchCompactor().matches("git fetch origin")


def test_git_fetch_all_matches():
    assert GitFetchCompactor().matches("git fetch --all")


def test_git_push_still_works_after_fetch_carveout():
    # Sanity: `git push` should still go to GitPushPullCompactor, not the
    # new GitFetchCompactor or anything else.
    GIT_PUSH = """Enumerating objects: 21, done.
Total 12 (delta 8), reused 0 (delta 0), pack-reused 0
To github.com:Mibayy/token-savior.git
   95213c6..a1b2c3d  main -> main
"""
    r = compact("git push origin main", GIT_PUSH)
    assert r is not None
    assert r.text.lower().startswith("ok")


# ---------------------------------------------------------------------------
# GitCheckoutCompactor
# ---------------------------------------------------------------------------


def test_git_checkout_success_oneline():
    # Real-world checkout to a tracking branch produces a hint banner that
    # can run several lines — this is the verbose case the compactor targets.
    out = """Branch 'feature/big-thing' set up to track remote branch 'feature/big-thing' from 'origin'.
Switched to a new branch 'feature/big-thing'
"""
    r = compact("git checkout -b feature/big-thing origin/feature/big-thing", out)
    assert r is not None
    assert "ok -> branch feature/big-thing" in r.text
    assert r.savings_pct >= 50.0


def test_git_checkout_bare_success_minimal_savings():
    # Bare "Switched to branch 'x'" lines have less headroom — we just
    # require the one-line output, not large savings.
    out = "Switched to branch 'feature/big-thing'\n"
    r = compact("git checkout feature/big-thing", out)
    assert r is not None
    assert "ok -> branch feature/big-thing" in r.text


def test_git_checkout_new_branch_oneline():
    out = "Switched to a new branch 'topic/abc'\n"
    r = compact("git checkout -b topic/abc", out)
    assert r is not None
    assert "topic/abc" in r.text


def test_git_checkout_conflict_kept_verbatim():
    out = """error: Your local changes to the following files would be overwritten by checkout:
\tsrc/foo.py
Please commit your changes or stash them before you switch branches.
Aborting
"""
    r = compact("git checkout main", out)
    assert r is not None
    # The conflict guidance MUST survive — we don't strip it.
    assert "would be overwritten" in r.text
    assert "Aborting" in r.text


def test_git_checkout_matches_through_dispatcher():
    # GitCheckoutCompactor must claim `git checkout`, NOT GitAddCompactor.
    assert any(
        c.__class__.__name__ == "GitCheckoutCompactor" and c.matches("git checkout main")
        for c in registry
    )
    # And GitAddCompactor must no longer claim `git checkout`.
    add = next(c for c in registry if c.__class__.__name__ == "GitAddCompactor")
    assert not add.matches("git checkout main")


# ---------------------------------------------------------------------------
# GitBranchCompactor
# ---------------------------------------------------------------------------


GIT_BRANCH_OUTPUT = """  bugfix/old-thing
  feature/a
  feature/b
* main
  release/v3.0
  release/v3.1
"""


def test_git_branch_keeps_current_marker():
    r = compact("git branch", GIT_BRANCH_OUTPUT)
    assert r is not None
    assert "* main" in r.text
    assert "feature/a" in r.text


def test_git_branch_truncates_huge_list():
    lines = "\n".join(f"  branch-{i}" for i in range(40)) + "\n* main\n"
    r = compact("git branch -a", lines)
    assert r is not None
    # Must show only first 15 + a "(N more)" summary.
    assert "(26 more)" in r.text
    kept_lines = [line for line in r.text.splitlines() if line.strip()]
    assert len(kept_lines) == 16  # 15 + summary
    assert r.savings_pct >= 50.0


def test_git_branch_strips_ansi():
    ansi = "\x1b[32m* main\x1b[0m\n  feature/x\n"
    r = compact("git branch", ansi)
    assert r is not None
    assert "\x1b[" not in r.text
    assert "* main" in r.text


# ---------------------------------------------------------------------------
# GitWorktreeListCompactor
# ---------------------------------------------------------------------------


GIT_WORKTREE_OUTPUT = """/root/token-savior         95213c6  [main]
/root/ts-f3a               a1b2c3d  [f3a-pytest-git-gh-extras]
/root/ts-f3b               b2c3d4e  [f3b-grep-find-cat]
/root/ts-f3c               c3d4e5f  [f3c-something-else]
/tmp/throwaway             d4e5f6a  (detached)
"""


def test_git_worktree_list_keeps_path_and_sha():
    r = compact("git worktree list", GIT_WORKTREE_OUTPUT)
    assert r is not None
    assert "/root/ts-f3a" in r.text
    assert "a1b2c3d" in r.text
    # Detached marker is load-bearing — must survive.
    assert "(detached)" in r.text


def test_git_worktree_list_drops_redundant_branch():
    # `ts-f3a` dir already contains `f3a` — branch tag `[f3a-pytest-...]`
    # carries information so it should still be kept (we only drop tags
    # that are a substring of the basename or vice versa).
    out = "/root/main-tree  abc1234  [main]\n"
    r = compact("git worktree list", out)
    assert r is not None
    # `main-tree` contains `main` → branch tag is redundant.
    assert "[main]" not in r.text
    assert "abc1234" in r.text


def test_git_worktree_list_savings_on_long_list():
    # When every dir basename mirrors its branch name, every `[branch]` tag
    # is redundant and gets dropped. With short paths and long branch names
    # the savings comfortably clear 50%.
    body = "\n".join(
        f"/wt/x-{i:03d}  abc1234  [some-very-long-branch-name-x-{i:03d}-with-padding]"
        for i in range(20)
    )
    r = compact("git worktree list", body)
    assert r is not None
    # Branch tags dropped because basename `x-{i}` is a substring of branch.
    assert "[some-very-long-branch-name" not in r.text
    assert r.savings_pct >= 50.0


# ---------------------------------------------------------------------------
# GitStashListCompactor
# ---------------------------------------------------------------------------


GIT_STASH_OUTPUT = """stash@{0}: WIP on main: 95213c6 fix: a really long stash message that goes on and on and on and exceeds sixty chars
stash@{1}: On feature/x: a1b2c3d short msg
stash@{2}: WIP on bugfix: b2c3d4e another shortish description here
"""


def test_git_stash_list_truncates_long_message():
    r = compact("git stash list", GIT_STASH_OUTPUT)
    assert r is not None
    assert "stash@{0}" in r.text
    assert "stash@{1}" in r.text
    assert "..." in r.text  # first entry should have been truncated


def test_git_stash_list_savings_on_long_messages():
    # Realistic case: agents often stash with paste-bombed messages that
    # carry several hundred characters of debug context. With 60-char cap,
    # savings comfortably clear 50% on these.
    huge_msg = "x" * 500
    body = "\n".join(
        f"stash@{{{i}}}: WIP on main: 95213c6 {huge_msg}" for i in range(15)
    )
    r = compact("git stash list", body)
    assert r is not None
    assert r.savings_pct >= 50.0


def test_git_stash_list_keeps_short_messages_intact():
    r = compact("git stash list", GIT_STASH_OUTPUT)
    assert r is not None
    assert "short msg" in r.text


# ---------------------------------------------------------------------------
# GhRepoViewCompactor
# ---------------------------------------------------------------------------


def _gh_repo_view_fixture() -> str:
    header = """name:        Mibayy/token-savior
description: MCP server for structural code navigation
homepage:    https://github.com/Mibayy/token-savior
license:     MIT
"""
    long_readme = "\n".join(f"line {i} of the readme body" for i in range(80))
    return header + "\n" + long_readme


def test_gh_repo_view_truncates_readme_keeps_header():
    out = _gh_repo_view_fixture()
    r = compact("gh repo view Mibayy/token-savior", out)
    assert r is not None
    assert "Mibayy/token-savior" in r.text
    assert "MCP server for structural code navigation" in r.text
    # README body capped — line 79 should NOT survive.
    assert "line 79 of the readme body" not in r.text
    assert "(50 more lines)" in r.text or "more lines" in r.text
    assert r.savings_pct >= 50.0


def test_gh_repo_view_matches_no_slug():
    assert GhRepoViewCompactor().matches("gh repo view")


# ---------------------------------------------------------------------------
# GhPrViewCompactor
# ---------------------------------------------------------------------------


def _gh_pr_view_fixture() -> str:
    header = """title:       F3a: fix pytest regex + git/gh extra subcommands
state:       OPEN
author:      Mibayy
labels:      enhancement
assignees:   Mibayy
projects:
milestone:
number:      42
url:         https://github.com/Mibayy/token-savior/pull/42
additions:   210
deletions:   12
"""
    body = "\n".join(f"body line {i} with more verbose content to inflate fixture" for i in range(120))
    return header + "\n--\n" + body


def test_gh_pr_view_keeps_status_truncates_body():
    out = _gh_pr_view_fixture()
    r = compact("gh pr view 42", out)
    assert r is not None
    assert "F3a: fix pytest regex" in r.text
    assert "state:       OPEN" in r.text
    assert "body line 0" in r.text
    # Body truncated.
    assert "body line 119" not in r.text
    assert r.savings_pct >= 50.0


def test_gh_pr_view_matches_bare():
    assert GhPrViewCompactor().matches("gh pr view")


# ---------------------------------------------------------------------------
# GhPrDiffCompactor — registry-order check
# ---------------------------------------------------------------------------


GH_PR_DIFF_OUTPUT = """diff --git a/foo.py b/foo.py
index 1234..5678 100644
--- a/foo.py
+++ b/foo.py
@@ -1,10 +1,12 @@
 import os
 import sys

-def hello():
-    return "hi"
+def hello(name):
+    return f"hi {name}"

 def unused():
     pass
"""


def test_gh_pr_diff_uses_git_diff_logic():
    # Use a larger diff with many unchanged context lines so the savings
    # from dropping that context are >= 50%, matching the F3a constraint.
    big_diff = "diff --git a/foo.py b/foo.py\nindex 1..2 100644\n--- a/foo.py\n+++ b/foo.py\n@@ -1,30 +1,32 @@\n"
    big_diff += "\n".join(f" unchanged context line {i} that takes up space" for i in range(28))
    big_diff += "\n-def hello():\n-    return \"hi\"\n+def hello(name):\n+    return f\"hi {name}\"\n"
    big_diff += "\n".join(f" more unchanged context line {i}" for i in range(10))
    r = compact("gh pr diff 42", big_diff)
    assert r is not None
    assert "+def hello(name):" in r.text
    assert "-def hello():" in r.text
    assert "unchanged context" not in r.text  # dropped
    assert r.savings_pct >= 50.0


def test_gh_pr_diff_matches_before_pr_view_in_registry():
    # The dispatcher iterates registry in order. Find positions.
    names = [c.__class__.__name__ for c in registry]
    assert names.index("GhPrDiffCompactor") < names.index("GhPrViewCompactor")


def test_gh_pr_diff_does_not_collide_with_pr_view():
    diff_c = GhPrDiffCompactor()
    view_c = GhPrViewCompactor()
    # `gh pr diff` must claim only diff.
    assert diff_c.matches("gh pr diff 42")
    # `gh pr view` must claim only view (not diff).
    assert view_c.matches("gh pr view 42")
    assert not diff_c.matches("gh pr view 42")


# ---------------------------------------------------------------------------
# GhIssueViewCompactor
# ---------------------------------------------------------------------------


def _gh_issue_view_fixture() -> str:
    header = """title:    Bug: pytest regex misses `python3 -m pytest`
state:    OPEN
author:   louis
labels:   bug
"""
    body = "\n".join(f"comment line {i} with extra verbose content here to inflate" for i in range(120))
    return header + "\n--\n" + body


def test_gh_issue_view_keeps_status_truncates_body():
    out = _gh_issue_view_fixture()
    r = compact("gh issue view 17", out)
    assert r is not None
    assert "Bug: pytest regex misses" in r.text
    assert "state:    OPEN" in r.text
    assert "comment line 0" in r.text
    assert "comment line 119" not in r.text
    assert r.savings_pct >= 50.0


def test_gh_issue_view_matches():
    assert GhIssueViewCompactor().matches("gh issue view 17")
    assert GhIssueViewCompactor().matches("gh issue view")


# ---------------------------------------------------------------------------
# Registry sanity — all new compactors are registered, no duplicates
# ---------------------------------------------------------------------------


def test_all_v43_compactors_in_registry():
    names = {c.__class__.__name__ for c in registry}
    expected = {
        "GitFetchCompactor",
        "GitCheckoutCompactor",
        "GitBranchCompactor",
        "GitWorktreeListCompactor",
        "GitStashListCompactor",
        "GhRepoViewCompactor",
        "GhPrViewCompactor",
        "GhPrDiffCompactor",
        "GhIssueViewCompactor",
    }
    missing = expected - names
    assert not missing, f"missing from registry: {missing}"


def test_registry_still_unique():
    names = [c.__class__.__name__ for c in registry]
    assert len(names) == len(set(names))
