"""GitHub CLI (gh) output compactors."""
from __future__ import annotations

import re

from .base import Compactor
from .git import GitDiffCompactor


class GhRunListCompactor(Compactor):
    _CMD_RE = re.compile(r"^\s*gh\s+run\s+(list|watch)\b")

    def matches(self, command: str) -> bool:
        return bool(self._CMD_RE.search(command))

    def compact(self, stdout: str, stderr: str = "") -> str:
        lines = [line.rstrip() for line in stdout.splitlines() if line.strip()]
        if not lines:
            return ""
        # gh run list output is whitespace-aligned columns: STATUS TITLE WORKFLOW BRANCH EVENT ID ELAPSED AGE
        # We keep STATUS + TITLE + ID for compactness; the ID gives the agent a handle.
        out = ["STATUS  TITLE  ID"]
        for row in lines[1:]:
            # split on 2+ whitespace runs to preserve titles
            cols = re.split(r"\s{2,}", row.strip())
            if len(cols) < 6:
                continue
            status = cols[0]
            title = cols[1]
            run_id = cols[5] if len(cols) > 5 else ""
            out.append(f"{status}  {title}  {run_id}")
        return "\n".join(out)


class GhRunViewCompactor(Compactor):
    _CMD_RE = re.compile(r"^\s*gh\s+run\s+view\b")

    def matches(self, command: str) -> bool:
        return bool(self._CMD_RE.search(command))

    def compact(self, stdout: str, stderr: str = "") -> str:
        lines = stdout.splitlines()
        out: list[str] = []
        keeping_job_body = False
        for raw in lines:
            line = raw.rstrip()
            stripped = line.strip()
            if not stripped:
                keeping_job_body = False
                continue
            # Top-level run header (e.g. "X main CI · 18923459001")
            if re.match(r"^[X✓*]\s+\S", stripped) and "CI" in stripped:
                out.append(stripped)
                continue
            # Top-level jobs (start of line, e.g. "X test in 1m24s (ID 12346)")
            if re.match(r"^[X✓*]\s+\w+\s+in\s+", stripped):
                if stripped.startswith("X") or stripped.startswith("*"):
                    out.append(stripped)
                    keeping_job_body = True
                else:
                    # Passing job: summarize, drop body
                    out.append(stripped)
                    keeping_job_body = False
                continue
            # Sub-step inside a job: "X Run pytest", "✓ Checkout", "- Upload coverage"
            if re.match(r"^[X✓*\-]\s+\S", stripped):
                if stripped.startswith("X") or stripped.startswith("*"):
                    out.append(f"  {stripped}")
                    keeping_job_body = True
                else:
                    # Passing sub-step: drop entirely
                    continue
            elif keeping_job_body:
                out.append(f"    {stripped}")
            elif stripped.startswith("Triggered ") or stripped.startswith("JOBS"):
                out.append(stripped)
        return "\n".join(out)


# ---------------------------------------------------------------------------
# v4.3.0 F3a additions — gh repo / pr / issue view + gh pr diff
# ---------------------------------------------------------------------------


def _truncate_lines(text: str, max_lines: int) -> str:
    lines = text.splitlines()
    if len(lines) <= max_lines:
        return text
    extra = len(lines) - max_lines
    return "\n".join(lines[:max_lines] + [f"({extra} more lines)"])


class GhRepoViewCompactor(Compactor):
    """`gh repo view [slug]` — keep top metadata block + first 30 README lines."""

    _CMD_RE = re.compile(r"^\s*gh\s+repo\s+view\b")
    # gh repo view emits a metadata block (name, description, fork count, etc.)
    # then a markdown rendering of the README. Cut the README to 30 lines.
    _README_MARKER_RE = re.compile(r"^\s*(--|━━|##\s|#\s)")

    def matches(self, command: str) -> bool:
        return bool(self._CMD_RE.search(command))

    def compact(self, stdout: str, stderr: str = "") -> str:
        lines = stdout.splitlines()
        if not lines:
            return ""
        # Find where the README body starts: gh repo view inserts a blank line
        # between the metadata block and the rendered README.
        header: list[str] = []
        body: list[str] = []
        in_body = False
        blank_run = 0
        for raw in lines:
            line = raw.rstrip()
            if not in_body:
                if not line.strip():
                    blank_run += 1
                    if blank_run >= 1 and header:
                        in_body = True
                    continue
                blank_run = 0
                header.append(line)
            else:
                body.append(line)
        # Trim header to non-trivial lines (drop pure separator runs).
        header = [line for line in header if line.strip()]
        body_text = "\n".join(body).strip("\n")
        if body_text:
            body_text = _truncate_lines(body_text, 30)
        if header and body_text:
            return "\n".join(header) + "\n\n" + body_text
        return "\n".join(header) if header else body_text


class GhPrDiffCompactor(Compactor):
    """`gh pr diff [num]` — reuse GitDiffCompactor's logic on the patch."""

    # Note: this MUST match before GhPrViewCompactor because `gh pr view --diff`
    # exists too, but the explicit `gh pr diff` form is unambiguous.
    _CMD_RE = re.compile(r"^\s*gh\s+pr\s+diff\b")
    _GIT_DIFF = GitDiffCompactor()

    def matches(self, command: str) -> bool:
        return bool(self._CMD_RE.search(command))

    def compact(self, stdout: str, stderr: str = "") -> str:
        return self._GIT_DIFF.compact(stdout, stderr)


class GhPrViewCompactor(Compactor):
    """`gh pr view [num]` — keep status block + checks summary + body head."""

    # NB: registry order ensures `gh pr diff` is checked first; we still guard
    # here so that `gh pr view --diff` (which we do NOT want to swallow as a view)
    # falls through to the more appropriate handler if it ever exists.
    _CMD_RE = re.compile(r"^\s*gh\s+pr\s+view\b(?!\s+.*--diff\b)")

    def matches(self, command: str) -> bool:
        return bool(self._CMD_RE.search(command))

    def compact(self, stdout: str, stderr: str = "") -> str:
        lines = stdout.splitlines()
        if not lines:
            return ""
        # gh pr view: top metadata (title, state, author, labels...) then a
        # separator (`--`), then the body. Optionally followed by checks/comments.
        header: list[str] = []
        body: list[str] = []
        section = "header"
        for raw in lines:
            line = raw.rstrip()
            stripped = line.strip()
            if section == "header":
                if stripped == "--" or stripped.startswith("──"):
                    section = "body"
                    continue
                if stripped:
                    header.append(line)
            else:
                body.append(line)
        body_text = "\n".join(body).strip("\n")
        if body_text:
            body_text = _truncate_lines(body_text, 50)
        parts = []
        if header:
            parts.append("\n".join(header))
        if body_text:
            parts.append(body_text)
        return "\n\n".join(parts)


class GhIssueViewCompactor(Compactor):
    """`gh issue view [num]` — same pattern as PR view."""

    _CMD_RE = re.compile(r"^\s*gh\s+issue\s+view\b")
    _PR_VIEW = GhPrViewCompactor()

    def matches(self, command: str) -> bool:
        return bool(self._CMD_RE.search(command))

    def compact(self, stdout: str, stderr: str = "") -> str:
        return self._PR_VIEW.compact(stdout, stderr)
