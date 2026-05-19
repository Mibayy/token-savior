"""ESLint output compactor — group by rule, count occurrences."""
from __future__ import annotations

import re
from collections import defaultdict

from .base import Compactor


_ANSI_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")

# Stylish reporter shape:
#   /abs/path/src/foo.ts
#     14:5  error    'x' is defined but never used  no-unused-vars
#     33:3  warning  Missing semicolon              semi
#
#   ✖ 12 problems (10 errors, 2 warnings)
_FILE_HEADER_RE = re.compile(r"^(?P<path>[./][^\s:]+|[A-Za-z]:[^\s:]+)\s*$")
_PROBLEM_RE = re.compile(
    r"^\s+(?P<line>\d+):(?P<col>\d+)\s+(?P<sev>error|warning)\s+(?P<msg>.+?)\s{2,}(?P<rule>[A-Za-z@][\w@\-/]*)\s*$"
)
_SUMMARY_RE = re.compile(r"^\s*✖?\s*(\d+)\s+problems?\s*\(.*\)\s*$")


def _strip_ansi(text: str) -> str:
    return _ANSI_RE.sub("", text)


class EslintCompactor(Compactor):
    """Group ESLint stylish output by rule name."""

    _CMD_RE = re.compile(
        r"^\s*(npx\s+|yarn\s+|pnpm\s+(run\s+|exec\s+|dlx\s+)?)?eslint(?![\w\-])"
    )

    def matches(self, command: str) -> bool:
        return bool(self._CMD_RE.search(command))

    def compact(self, stdout: str, stderr: str = "") -> str:
        haystack = _strip_ansi((stdout or "") + ("\n" + stderr if stderr else ""))
        lines = haystack.splitlines()

        # rule -> list of "file:line" strings
        by_rule: dict[str, list[str]] = defaultdict(list)
        current_file: str | None = None
        total_errors = 0
        total_warnings = 0
        summary: str | None = None

        for raw in lines:
            line = raw.rstrip()
            if not line.strip():
                continue
            m = _PROBLEM_RE.match(line)
            if m and current_file is not None:
                rule = m.group("rule")
                if m.group("sev") == "error":
                    total_errors += 1
                else:
                    total_warnings += 1
                by_rule[rule].append(f"{current_file}:{m.group('line')}")
                continue
            m_sum = _SUMMARY_RE.match(line)
            if m_sum:
                summary = line.strip().lstrip("✖").strip()
                continue
            # File header — a path-looking line that didn't match a problem line
            if _FILE_HEADER_RE.match(line.strip()) and ":" not in line.split()[0]:
                current_file = line.strip()
                continue
            # Absolute path file header (no leading . or /)
            if re.match(r"^/[^\s]+$", line.strip()):
                current_file = line.strip()
                continue

        if not by_rule:
            # Nothing matched — likely an "all clean" output, return a stub.
            if summary:
                return summary
            return "ok"

        # Sort by count desc, then rule name for determinism
        ordered = sorted(by_rule.items(), key=lambda kv: (-len(kv[1]), kv[0]))
        out: list[str] = []
        for rule, locs in ordered:
            head = f"{rule} ({len(locs)}x) — " + ", ".join(locs)
            out.append(head)
        out.append(
            f"{total_errors + total_warnings} problems "
            f"({total_errors} errors, {total_warnings} warnings)"
        )
        return "\n".join(out)
