"""Biome (check / lint) output compactor — group diagnostics by rule."""
from __future__ import annotations

import re
from collections import defaultdict

from .base import Compactor


_ANSI_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")

# Biome diagnostic header lines look like one of:
#   ./src/foo.ts:14:5 lint/suspicious/noExplicitAny ━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#   ./src/foo.ts:14:5 lint/suspicious/noExplicitAny FIXABLE ━━━━━━━━━━━━
#   src/foo.ts:14:5 parse ━━━━━
# We match the path + (line:col) + category/name token.
_HEADER_RE = re.compile(
    r"^(?P<path>\.{0,2}/?[^\s:]+\.[A-Za-z0-9]+):(?P<line>\d+):(?P<col>\d+)\s+"
    r"(?P<rule>[a-z][\w]*(?:/[\w]+)+)(?:\s+\S+)?\s*━+\s*$"
)

# Final summary like: "Checked 42 files in 120ms. Found 3 errors."
_SUMMARY_RE = re.compile(
    r"^(Checked\s+\d+\s+files?.*|Found\s+\d+\s+(errors?|warnings?).*|\d+\s+errors?\s+found.*)$",
    re.IGNORECASE,
)


def _strip_ansi(text: str) -> str:
    return _ANSI_RE.sub("", text)


class BiomeCompactor(Compactor):
    """Group biome diagnostics by rule, list file:line refs."""

    _CMD_RE = re.compile(
        r"^\s*(npx\s+|yarn\s+|pnpm\s+(run\s+|exec\s+|dlx\s+)?)?biome(?![\w\-])"
    )

    def matches(self, command: str) -> bool:
        return bool(self._CMD_RE.search(command))

    def compact(self, stdout: str, stderr: str = "") -> str:
        haystack = _strip_ansi((stdout or "") + ("\n" + stderr if stderr else ""))
        lines = haystack.splitlines()

        by_rule: dict[str, list[str]] = defaultdict(list)
        summary_lines: list[str] = []

        for raw in lines:
            line = raw.rstrip()
            stripped = line.strip()
            if not stripped:
                continue
            m = _HEADER_RE.match(stripped)
            if m:
                rule = m.group("rule")
                by_rule[rule].append(f"{m.group('path')}:{m.group('line')}")
                continue
            if _SUMMARY_RE.match(stripped):
                summary_lines.append(stripped)
                continue

        if not by_rule:
            return "\n".join(summary_lines) if summary_lines else "ok"

        ordered = sorted(by_rule.items(), key=lambda kv: (-len(kv[1]), kv[0]))
        out: list[str] = []
        for rule, locs in ordered:
            out.append(f"{rule} ({len(locs)}x) — " + ", ".join(locs))
        if summary_lines:
            # Keep just the last summary line — that's the verdict
            out.append(summary_lines[-1])
        else:
            total = sum(len(v) for v in by_rule.values())
            out.append(f"{total} diagnostics in {len(by_rule)} rules")
        return "\n".join(out)
