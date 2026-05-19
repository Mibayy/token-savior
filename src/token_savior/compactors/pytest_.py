"""Pytest output compactor — failures-only with bounded traceback."""
from __future__ import annotations

import re

from .base import Compactor


_PROGRESS_RE = re.compile(r"^.+\.py\s+[.FsExX]+\s*(\[\s*\d+%\])?\s*$")
_SUMMARY_RE = re.compile(r"^=+\s+(\d+ passed|\d+ failed|\d+ error|\d+ skipped|short test summary)")


class PytestCompactor(Compactor):
    # Match the bare `pytest` invocation as well as common wrappers:
    #   - `pytest ...`
    #   - `python -m pytest ...` / `python3 -m pytest ...` / `python3.12 -m pytest`
    #   - `/path/to/venv/bin/python3 -m pytest ...`
    #   - `uv run pytest ...` / `poetry run pytest ...` / `hatch run pytest ...`
    _CMD_RE = re.compile(
        r"^\s*"
        r"("
        r"(?:\S*/)?python[0-9.]*\s+-m\s+"           # optional path + python(3) -m
        r"|"
        r"(?:uv|poetry|hatch|pdm|rye)\s+run\s+"     # task runners
        r")?"
        r"pytest\b"
    )

    def matches(self, command: str) -> bool:
        return bool(self._CMD_RE.search(command))

    def compact(self, stdout: str, stderr: str = "") -> str:
        lines = stdout.splitlines()
        out: list[str] = []

        in_failures = False
        in_failure_block = False
        failure_block: list[str] = []

        def flush_failure() -> None:
            if not failure_block:
                return
            # Compact header: `____ test_name ____` -> `# test_name`
            header_raw = failure_block[0]
            m = re.match(r"^_+\s+(.*?)\s+_+$", header_raw.strip())
            header = f"# {m.group(1)}" if m else header_raw
            # Keep last 3 non-cosmetic lines (source, exception, file:line) — drop visual separators
            tail = [
                line for line in failure_block[1:]
                if line.strip() and not re.match(r"^[_\-= ]+$", line.strip())
            ][-3:]
            out.append(header)
            out.extend(tail)
            failure_block.clear()

        for raw in lines:
            line = raw.rstrip()
            stripped = line.strip()

            if stripped.startswith("=") and "FAILURES" in stripped:
                in_failures = True
                out.append("FAILURES:")
                continue
            if stripped.startswith("=") and "short test summary" in stripped.lower():
                in_failures = False
                flush_failure()
                continue
            # Final summary banner (e.g. `=== 2 failed, 118 passed in 4.27s ===`) -> strip the `=`
            if stripped.startswith("=") and ("passed" in stripped or "failed" in stripped or " error" in stripped):
                in_failures = False
                flush_failure()
                m = re.match(r"^=+\s*(.*?)\s*=+$", stripped)
                out.append(m.group(1) if m else stripped)
                continue

            if in_failures:
                # A failure header looks like `____ test_name ____`
                if re.match(r"^_+\s+\S.*\s+_+$", stripped):
                    flush_failure()
                    failure_block.append(stripped)
                    in_failure_block = True
                    continue
                if in_failure_block:
                    failure_block.append(line)
                continue

            # Outside failures block: keep FAILED/ERROR summary lines, drop everything else noisy
            if stripped.startswith("FAILED ") or stripped.startswith("ERROR "):
                out.append(stripped)
                continue
            if _PROGRESS_RE.match(stripped):
                # Skip dot-progress lines
                continue
            if stripped.startswith("platform ") or stripped.startswith("rootdir:") or stripped.startswith("plugins:") or stripped.startswith("collected "):
                continue
            if stripped.startswith("===") and "test session" in stripped:
                continue
            # Skip purely cosmetic separator lines (e.g. `_ _ _ _ _`)
            if re.match(r"^[_\-= ]+$", stripped):
                continue

        flush_failure()
        return "\n".join(out)
