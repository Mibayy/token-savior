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
    """Single-line summary for push/pull/fetch."""

    _CMD_RE = re.compile(r"^\s*git\s+(push|pull|fetch|clone)\b")

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
    _CMD_RE = re.compile(r"^\s*git\s+(add|rm|mv|restore|checkout)\b")

    def matches(self, command: str) -> bool:
        return bool(self._CMD_RE.search(command))

    def compact(self, stdout: str, stderr: str = "") -> str:
        haystack = (stdout or "") + (stderr or "")
        # These are usually silent on success; if there's output it's a warning/error worth keeping verbatim
        body = haystack.strip()
        return body if body else "ok"
