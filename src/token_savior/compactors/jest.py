"""Jest output compactor — failures only + summary, ANSI stripped."""
from __future__ import annotations

import re

from .base import Compactor


_ANSI_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
_PASS_LINE_RE = re.compile(r"^\s*PASS\s+")
_FAIL_LINE_RE = re.compile(r"^\s*FAIL\s+")
_SUMMARY_KEYS = ("Tests:", "Test Suites:", "Snapshots:", "Time:", "Ran all test suites")


def _strip_ansi(text: str) -> str:
    return _ANSI_RE.sub("", text)


class JestCompactor(Compactor):
    """Drop PASS lines, keep FAIL blocks + final summary."""

    # First non-flag token: jest | npx jest | yarn jest | pnpm jest | pnpm run jest
    _CMD_RE = re.compile(
        r"^\s*(npx\s+|yarn\s+|pnpm\s+(run\s+|exec\s+|dlx\s+)?)?jest(?![\w\-])"
    )

    def matches(self, command: str) -> bool:
        return bool(self._CMD_RE.search(command))

    def compact(self, stdout: str, stderr: str = "") -> str:
        # Jest writes most progress to stderr — merge both streams.
        haystack = _strip_ansi((stdout or "") + ("\n" + stderr if stderr else ""))
        lines = haystack.splitlines()

        out: list[str] = []
        fail_files: list[str] = []
        # Capture FAIL detail blocks (between a FAIL line and the next PASS/FAIL/blank-summary)
        in_fail_block = False
        fail_block: list[str] = []

        def flush_fail_block() -> None:
            if not fail_block:
                return
            kept: list[str] = []
            for ln in fail_block:
                s = ln.strip()
                if not s:
                    continue
                if re.match(r"^[─━=_\-\s]+$", s):
                    continue
                # Drop jest source-context (e.g. "  16 |   expect..." and "    > 16 |")
                if re.match(r"^>?\s*\d+\s*\|", s):
                    continue
                # Drop pure caret indicator rows ("|       ^")
                if re.match(r"^\|?\s*\^+\s*$", s):
                    continue
                kept.append(s)
            # Cap each fail block to first 5 informative lines
            out.extend(kept[:5])
            fail_block.clear()

        for raw in lines:
            line = raw.rstrip()
            stripped = line.strip()

            if _PASS_LINE_RE.match(line):
                # Closing any open fail block before pivoting away
                if in_fail_block:
                    flush_fail_block()
                    in_fail_block = False
                continue

            if _FAIL_LINE_RE.match(line):
                flush_fail_block()
                # `FAIL src/foo.test.ts (1.234 s)` -> keep the path token
                m = re.match(r"^\s*FAIL\s+(\S+)", line)
                if m:
                    fail_files.append(m.group(1))
                out.append(stripped)
                in_fail_block = True
                continue

            # Final summary block: any line starting with one of the summary keys
            if any(stripped.startswith(k) for k in _SUMMARY_KEYS):
                if in_fail_block:
                    flush_fail_block()
                    in_fail_block = False
                out.append(stripped)
                continue

            if in_fail_block:
                fail_block.append(line)

        flush_fail_block()

        # If absolutely nothing kept (all green, no summary captured) emit a one-liner.
        if not out:
            # Look for any " passed" mention as a last resort
            for raw in lines:
                if "passed" in raw and "failed" not in raw:
                    return _strip_ansi(raw).strip()
            return "ok"

        # Prepend a compact failed-files header if we have multiple
        if len(fail_files) > 1:
            return f"failed files ({len(fail_files)}): " + ", ".join(fail_files) + "\n" + "\n".join(out)
        return "\n".join(out)
