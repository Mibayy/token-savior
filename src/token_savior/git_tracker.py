"""Git change detection for incremental re-indexing."""

from __future__ import annotations

import subprocess
from dataclasses import dataclass, field


@dataclass
class GitChangeSet:
    """Set of files changed since a given git ref."""

    modified: list[str] = field(default_factory=list)
    added: list[str] = field(default_factory=list)
    deleted: list[str] = field(default_factory=list)

    @property
    def is_empty(self) -> bool:
        return not self.modified and not self.added and not self.deleted


@dataclass
class GitStatus:
    """Structured git status summary for the current work tree."""

    branch: str | None = None
    upstream: str | None = None
    ahead: int = 0
    behind: int = 0
    staged: list[str] = field(default_factory=list)
    unstaged: list[str] = field(default_factory=list)
    untracked: list[str] = field(default_factory=list)
    conflicted: list[str] = field(default_factory=list)
    clean: bool = True


def is_git_repo(root_path: str) -> bool:
    """Check if the given path is inside a git work tree."""
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--is-inside-work-tree"],
            cwd=root_path,
            capture_output=True,
            text=True,
            timeout=10,
        )
        return result.returncode == 0 and result.stdout.strip() == "true"
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False


def get_head_commit(root_path: str) -> str | None:
    """Get the current HEAD commit hash."""
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=root_path,
            capture_output=True,
            text=True,
            timeout=10,
        )
        if result.returncode == 0:
            return result.stdout.strip()
        return None
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return None


def get_git_status(root_path: str) -> dict:
    """Return a structured status summary for the git work tree."""
    try:
        result = subprocess.run(
            ["git", "status", "--porcelain=1", "--branch"],
            cwd=root_path,
            capture_output=True,
            text=True,
            timeout=10,
        )
        if result.returncode != 0:
            return {"ok": False, "error": result.stderr.strip() or "git status failed"}
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return {"ok": False, "error": "git status unavailable"}

    status = _parse_status_porcelain(result.stdout)
    return {
        "ok": True,
        "branch": status.branch,
        "upstream": status.upstream,
        "ahead": status.ahead,
        "behind": status.behind,
        "staged": status.staged,
        "unstaged": status.unstaged,
        "untracked": status.untracked,
        "conflicted": status.conflicted,
        "clean": status.clean,
    }


def get_changed_files(root_path: str, since_ref: str | None) -> GitChangeSet:
    """Get files changed since a given git ref.

    Combines committed changes (since_ref..HEAD), staged changes,
    unstaged changes, and untracked files into a single GitChangeSet.
    """
    if since_ref is None:
        return GitChangeSet()

    modified: set[str] = set()
    added: set[str] = set()
    deleted: set[str] = set()

    # 1. Committed changes since the ref
    _parse_diff_output(
        root_path, ["git", "diff", "--name-status", since_ref, "HEAD"], modified, added, deleted
    )

    # 2. Unstaged changes
    _parse_diff_output(root_path, ["git", "diff", "--name-status"], modified, added, deleted)

    # 3. Staged changes
    _parse_diff_output(
        root_path, ["git", "diff", "--name-status", "--cached"], modified, added, deleted
    )

    # 4. Untracked files
    try:
        result = subprocess.run(
            ["git", "ls-files", "--others", "--exclude-standard"],
            cwd=root_path,
            capture_output=True,
            text=True,
            timeout=10,
        )
        if result.returncode == 0:
            for line in result.stdout.strip().splitlines():
                path = line.strip()
                if path:
                    added.add(path)
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass

    # Resolve overlaps: file in both added and deleted → modified
    overlap = added & deleted
    modified |= overlap
    added -= overlap
    deleted -= overlap

    return GitChangeSet(
        modified=sorted(modified),
        added=sorted(added),
        deleted=sorted(deleted),
    )


def _parse_diff_output(
    root_path: str,
    cmd: list[str],
    modified: set[str],
    added: set[str],
    deleted: set[str],
) -> None:
    """Parse git diff --name-status output into modified/added/deleted sets."""
    try:
        result = subprocess.run(
            cmd,
            cwd=root_path,
            capture_output=True,
            text=True,
            timeout=10,
        )
        if result.returncode != 0:
            return
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return

    for line in result.stdout.strip().splitlines():
        parts = line.split("\t")
        if len(parts) < 2:
            continue
        status = parts[0]
        path = parts[1]

        if status == "M":
            modified.add(path)
        elif status == "A":
            added.add(path)
        elif status == "D":
            deleted.add(path)
        elif status.startswith("R"):
            # Rename: delete old path, add new path
            deleted.add(path)
            if len(parts) >= 3:
                added.add(parts[2])


def _parse_status_porcelain(output: str) -> GitStatus:
    """Parse `git status --porcelain=1 --branch` output."""
    status = GitStatus()
    lines = output.splitlines()
    if not lines:
        return status

    if lines[0].startswith("## "):
        _parse_branch_header(lines[0][3:], status)
        lines = lines[1:]

    for line in lines:
        if len(line) < 3:
            continue
        index_status = line[0]
        worktree_status = line[1]
        path = line[3:]
        if index_status == "?" and worktree_status == "?":
            status.untracked.append(path)
            continue

        if (
            index_status == "U"
            or worktree_status == "U"
            or (index_status == "A" and worktree_status == "A")
        ):
            status.conflicted.append(path)
            continue

        if index_status not in (" ", "?"):
            status.staged.append(path)
        if worktree_status not in (" ", "?"):
            status.unstaged.append(path)

    status.clean = not (status.staged or status.unstaged or status.untracked or status.conflicted)
    return status


def _parse_branch_header(header: str, status: GitStatus) -> None:
    """Parse the branch/upstream metadata from the porcelain header line."""
    branch_part, _, tracking_part = header.partition("...")
    status.branch = branch_part.strip() or None
    if tracking_part:
        upstream_part, _, counts_part = tracking_part.partition(" ")
        status.upstream = upstream_part.strip() or None
        counts = counts_part.strip().strip("[]")
        if counts:
            for item in counts.split(","):
                piece = item.strip()
                if piece.startswith("ahead "):
                    status.ahead = int(piece.split(" ", 1)[1])
                elif piece.startswith("behind "):
                    status.behind = int(piece.split(" ", 1)[1])
