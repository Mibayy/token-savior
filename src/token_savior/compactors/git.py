"""Git command compactors."""
from __future__ import annotations

import re

from .base import Compactor


_GIT_HINT_PATTERNS = (
    re.compile(r'^\s*\(use "git '),
    re.compile(r'^\s*\(commit or discard the untracked or modified content'),
    re.compile(r"^no changes added to commit"),
    re.compile(r"^nothing to commit"),
    re.compile(r"^Your branch is "),
    re.compile(r"^\s*$"),
)


def _is_git_hint(line: str) -> bool:
    return any(p.search(line) for p in _GIT_HINT_PATTERNS)


class GitStatusCompactor(Compactor):
    """Group `git status` by section, drop instructional hints."""

    _CMD_RE = re.compile(r"^\s*git\s+status\b")

    def matches(self, command: str) -> bool:
        return bool(self._CMD_RE.search(command))

    def compact(self, stdout: str, stderr: str = "") -> str:
        lines = stdout.splitlines()
        out: list[str] = []
        # Section state machine — sections are introduced by lines ending ":"
        staged: list[str] = []
        unstaged: list[str] = []
        untracked: list[str] = []
        branch = ""
        section: str | None = None
        for raw in lines:
            line = raw.rstrip()
            if line.startswith("On branch "):
                branch = line[len("On branch "):]
                continue
            if line.startswith("HEAD detached"):
                branch = line
                continue
            if _is_git_hint(line):
                continue
            stripped = line.strip()
            if stripped == "Changes to be committed:":
                section = "staged"
                continue
            if stripped == "Changes not staged for commit:":
                section = "unstaged"
                continue
            if stripped == "Untracked files:":
                section = "untracked"
                continue
            if not stripped or section is None:
                continue
            # File entries: either "\tmodified:   path" or "\tpath"
            entry = stripped
            if section == "staged":
                staged.append(entry)
            elif section == "unstaged":
                unstaged.append(entry)
            elif section == "untracked":
                untracked.append(entry)

        if branch:
            out.append(f"branch: {branch}")
        if staged:
            out.append(f"staged ({len(staged)}):")
            out.extend(f"  {e}" for e in staged)
        if unstaged:
            out.append(f"unstaged ({len(unstaged)}):")
            out.extend(f"  {e}" for e in unstaged)
        if untracked:
            out.append(f"untracked ({len(untracked)}):")
            out.extend(f"  {e}" for e in untracked)
        if not (staged or unstaged or untracked):
            out.append("clean")
        return "\n".join(out)


class GitDiffCompactor(Compactor):
    """Keep file headers + hunk markers + +/- lines. Drop unchanged context."""

    _CMD_RE = re.compile(r"^\s*git\s+(diff|show)\b")

    def matches(self, command: str) -> bool:
        return bool(self._CMD_RE.search(command))

    def compact(self, stdout: str, stderr: str = "") -> str:
        kept: list[str] = []
        for line in stdout.splitlines():
            if line.startswith("diff --git "):
                # Shorten `diff --git a/foo b/foo` to `--- foo`
                m = re.match(r"diff --git a/(\S+) b/\S+", line)
                if m:
                    kept.append(f"# {m.group(1)}")
                else:
                    kept.append(line)
            elif line.startswith("@@"):
                # Hunk header — keep but drop the trailing context after second @@
                m = re.match(r"(@@ [^@]+ @@)", line)
                kept.append(m.group(1) if m else line)
            elif line.startswith("+++") or line.startswith("---"):
                # File markers redundant with our `# path` header
                continue
            elif line.startswith("index "):
                continue
            elif line.startswith("+") or line.startswith("-"):
                kept.append(line)
            # Everything else (unchanged context, mode lines, similarity, etc.) is dropped
        return "\n".join(kept)


class GitLogCompactor(Compactor):
    """Reduce verbose `git log` to oneline: `<short-sha> <subject>`."""

    _CMD_RE = re.compile(r"^\s*git\s+log\b")

    def matches(self, command: str) -> bool:
        return bool(self._CMD_RE.search(command))

    def compact(self, stdout: str, stderr: str = "") -> str:
        lines = stdout.splitlines()
        out: list[str] = []
        sha = ""
        for line in lines:
            if line.startswith("commit "):
                sha = line.split()[1][:8]
                continue
            if line.startswith("Author:") or line.startswith("Date:") or line.startswith("Merge:"):
                continue
            stripped = line.strip()
            if not stripped:
                continue
            if sha:
                out.append(f"{sha} {stripped}")
                sha = ""
        return "\n".join(out)


class GitPushPullCompactor(Compactor):
    """Single-line summary for push/pull/clone.

    Note: `git fetch` has its own dedicated compactor (``GitFetchCompactor``)
    that preserves ref-update detail, so it is intentionally excluded here.
    """

    _CMD_RE = re.compile(r"^\s*git\s+(push|pull|clone)\b")

    def matches(self, command: str) -> bool:
        return bool(self._CMD_RE.search(command))

    def compact(self, stdout: str, stderr: str = "") -> str:
        # Most useful info from `git push` lives in stderr (the "To <remote>" + ref update),
        # but in our hook we receive stdout+stderr merged via the calling convention.
        # Keep the ref-update line if present, fall back to last non-empty.
        haystack = (stdout or "") + "\n" + (stderr or "")
        lines = [line.rstrip() for line in haystack.splitlines() if line.strip()]
        ref_line = next((line for line in lines if "->" in line), None)
        to_line = next((line for line in lines if line.startswith("To ")), None)
        if ref_line:
            base = ref_line.strip()
            if to_line:
                return f"ok {to_line.strip()} {base}"
            return f"ok {base}"
        if lines:
            return f"ok {lines[-1]}"
        return "ok"


class GitCommitCompactor(Compactor):
    _CMD_RE = re.compile(r"^\s*git\s+commit\b")

    def matches(self, command: str) -> bool:
        return bool(self._CMD_RE.search(command))

    def compact(self, stdout: str, stderr: str = "") -> str:
        lines = [line for line in stdout.splitlines() if line.strip()]
        if not lines:
            return "ok"
        head = lines[0]  # e.g. "[main a1b2c3d] subject"
        stats = next((line for line in lines if "changed" in line and ("insertion" in line or "deletion" in line)), "")
        if stats:
            return f"{head} | {stats.strip()}"
        return head


class GitAddCompactor(Compactor):
    # `git checkout` has its own dedicated compactor (``GitCheckoutCompactor``)
    # so it is intentionally excluded here.
    _CMD_RE = re.compile(r"^\s*git\s+(add|rm|mv|restore)\b")

    def matches(self, command: str) -> bool:
        return bool(self._CMD_RE.search(command))

    def compact(self, stdout: str, stderr: str = "") -> str:
        haystack = (stdout or "") + (stderr or "")
        # These are usually silent on success; if there's output it's a warning/error worth keeping verbatim
        body = haystack.strip()
        return body if body else "ok"


# ---------------------------------------------------------------------------
# v4.3.0 F3a additions — fetch / checkout / branch / worktree / stash
# ---------------------------------------------------------------------------


_FETCH_PROGRESS_RE = re.compile(
    r"^\s*("
    r"remote: "
    r"|Enumerating objects"
    r"|Counting objects"
    r"|Compressing objects"
    r"|Receiving objects"
    r"|Resolving deltas"
    r"|Total \d+ \(delta"
    r"|Unpacking objects"
    r")",
)


class GitFetchCompactor(Compactor):
    """Drop transfer progress lines, keep the `From <url>` block + ref updates."""

    _CMD_RE = re.compile(r"^\s*git\s+fetch\b")

    def matches(self, command: str) -> bool:
        return bool(self._CMD_RE.search(command))

    def compact(self, stdout: str, stderr: str = "") -> str:
        haystack = (stdout or "") + "\n" + (stderr or "")
        kept: list[str] = []
        for raw in haystack.splitlines():
            line = raw.rstrip()
            if not line.strip():
                continue
            if _FETCH_PROGRESS_RE.search(line):
                continue
            kept.append(line.strip())
        return "\n".join(kept) if kept else "ok"


class GitCheckoutCompactor(Compactor):
    """One-line summary on success; full output if there's a conflict or dirty tree."""

    _CMD_RE = re.compile(r"^\s*git\s+checkout\b")
    # Phrases that indicate the checkout was NOT a clean switch.
    _PROBLEM_RE = re.compile(
        r"(error:|fatal:|conflict|would be overwritten|aborting|"
        r"please commit your changes or stash|untracked working tree files)",
        re.IGNORECASE,
    )

    def matches(self, command: str) -> bool:
        return bool(self._CMD_RE.search(command))

    def compact(self, stdout: str, stderr: str = "") -> str:
        haystack = (stdout or "") + "\n" + (stderr or "")
        if self._PROBLEM_RE.search(haystack):
            # Keep the full output verbatim so the agent can act on it.
            return haystack.strip()
        lines = [line.strip() for line in haystack.splitlines() if line.strip()]
        # Find a `Switched to ...` / `Already on ...` / `HEAD is now at ...` line.
        for line in lines:
            if (
                line.startswith("Switched to ")
                or line.startswith("Already on ")
                or line.startswith("HEAD is now at ")
            ):
                # Compact: `Switched to branch 'foo'` -> `ok -> branch foo`
                m = re.match(r"^Switched to (a new )?branch '([^']+)'", line)
                if m:
                    return f"ok -> branch {m.group(2)}"
                m = re.match(r"^Already on '([^']+)'", line)
                if m:
                    return f"ok -> branch {m.group(1)} (already)"
                return f"ok {line}"
        return "ok"


class GitBranchCompactor(Compactor):
    """List branches: drop ANSI, keep `*` current marker, truncate at 20 lines."""

    # Match `git branch`, `git branch -a`, `git branch --merged`, etc.
    # Crucially, do NOT match `git branch -d <name>` style deletes? Those still
    # produce list-free output, so it's harmless to share a compactor.
    _CMD_RE = re.compile(r"^\s*git\s+branch\b")
    _ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")

    def matches(self, command: str) -> bool:
        return bool(self._CMD_RE.search(command))

    def compact(self, stdout: str, stderr: str = "") -> str:
        haystack = (stdout or "") + (stderr or "")
        kept: list[str] = []
        for raw in haystack.splitlines():
            line = self._ANSI_RE.sub("", raw).rstrip()
            if not line.strip():
                continue
            kept.append(line)
        if len(kept) > 20:
            extra = len(kept) - 15
            kept = kept[:15] + [f"({extra} more)"]
        return "\n".join(kept)


class GitWorktreeListCompactor(Compactor):
    """`git worktree list` — keep dir + sha7, drop redundant `[branch]` markers."""

    _CMD_RE = re.compile(r"^\s*git\s+worktree\s+list\b")

    def matches(self, command: str) -> bool:
        return bool(self._CMD_RE.search(command))

    def compact(self, stdout: str, stderr: str = "") -> str:
        out: list[str] = []
        for raw in stdout.splitlines():
            line = raw.rstrip()
            if not line.strip():
                continue
            # Format: `<path>  <sha7>  [branch]` or `<path>  <sha7>  (detached)`
            parts = re.split(r"\s{2,}", line.strip(), maxsplit=2)
            if len(parts) < 2:
                out.append(line.strip())
                continue
            path, sha = parts[0], parts[1]
            tail = parts[2] if len(parts) > 2 else ""
            # Drop `[branch]` if the directory basename already implies it.
            keep_tail = ""
            if tail:
                m = re.match(r"^\[(.+)\]$", tail)
                if m:
                    branch = m.group(1)
                    base = path.rstrip("/").rsplit("/", 1)[-1]
                    if branch.lower() not in base.lower() and base.lower() not in branch.lower():
                        keep_tail = f" [{branch}]"
                elif tail == "(detached)":
                    # Keep detached marker — it's load-bearing.
                    keep_tail = " (detached)"
                else:
                    keep_tail = f" {tail}"
            out.append(f"{path}  {sha}{keep_tail}")
        return "\n".join(out)


class GitStashListCompactor(Compactor):
    """`git stash list` — one line per stash, truncate message at 60 chars."""

    _CMD_RE = re.compile(r"^\s*git\s+stash\s+list\b")

    def matches(self, command: str) -> bool:
        return bool(self._CMD_RE.search(command))

    def compact(self, stdout: str, stderr: str = "") -> str:
        out: list[str] = []
        for raw in stdout.splitlines():
            line = raw.rstrip()
            if not line.strip():
                continue
            # `stash@{0}: WIP on main: a1b2c3d <subject>`
            # Truncate any quoted message after 60 chars, but keep the prefix intact.
            prefix_m = re.match(r"^(stash@\{\d+\}:\s*[^:]+:\s*\S+\s+)(.*)$", line)
            if prefix_m:
                prefix, msg = prefix_m.group(1), prefix_m.group(2)
                if len(msg) > 60:
                    msg = msg[:57] + "..."
                out.append(prefix + msg)
            else:
                # Fallback: blanket truncate the whole line at 100 chars.
                out.append(line if len(line) <= 100 else line[:97] + "...")
        return "\n".join(out)
