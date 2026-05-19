"""GitHub CLI (gh) output compactors."""
from __future__ import annotations

import re

from .base import Compactor


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
