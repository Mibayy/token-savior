"""Vitest output compactor — failures only + summary, ANSI stripped."""
from __future__ import annotations

import re

from .base import Compactor


_ANSI_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
# Vitest default reporter glyphs:
#   ✓ test passed
#   ✗ / × test failed
#   ❯ section header
#   ↳ context line
_PASS_GLYPHS = ("✓",)
# `×` and `✗` are noisy preview lines that vitest re-prints right after `FAIL`
# blocks. We drop them entirely (treated as a third class, not pass not fail).
_DROP_GLYPHS = ("×", "✗")
_FAIL_GLYPHS = ("❯ FAIL", "FAIL")
_SUMMARY_KEYS = (
    "Test Files",
    "Tests ",
    "Tests:",
    "Start at",
    "Duration",
    "Snapshots",
)


def _strip_ansi(text: str) -> str:
    return _ANSI_RE.sub("", text)


class VitestCompactor(Compactor):
    """Drop pass markers, keep fail blocks + summary."""

    _CMD_RE = re.compile(
        r"^\s*(npx\s+|yarn\s+|pnpm\s+(run\s+|exec\s+|dlx\s+)?)?vitest(?![\w\-])"
    )

    def matches(self, command: str) -> bool:
        return bool(self._CMD_RE.search(command))

    def compact(self, stdout: str, stderr: str = "") -> str:
        haystack = _strip_ansi((stdout or "") + ("\n" + stderr if stderr else ""))
        lines = haystack.splitlines()

        out: list[str] = []
        in_fail_block = False
        fail_block: list[str] = []
        fail_files: list[str] = []

        def is_pass(s: str) -> bool:
            s = s.lstrip()
            return any(s.startswith(g) for g in _PASS_GLYPHS) or any(
                s.startswith(g) for g in _DROP_GLYPHS
            )

        def is_fail(s: str) -> bool:
            s = s.lstrip()
            return any(s.startswith(g) for g in _FAIL_GLYPHS) or " FAIL " in s

        def is_summary(s: str) -> bool:
            s = s.lstrip()
            return any(s.startswith(k) for k in _SUMMARY_KEYS)

        def flush() -> None:
            if not fail_block:
                return
            kept: list[str] = []
            for ln in fail_block:
                s = ln.strip()
                if not s:
                    continue
                if re.match(r"^[─━=_\-⎯\s]+$", s):
                    continue
                # Drop source-context lines: "  14|  code" / "  > 16| code"
                if re.match(r"^>?\s*\d+\|", s):
                    continue
                # Drop caret indicator lines
                if re.match(r"^\|?\s*\^+\s*$", s):
                    continue
                kept.append(s)
            # Cap each fail block to first 4 informative lines (header, message, path:line, first frame)
            out.extend(kept[:4])
            fail_block.clear()

        for raw in lines:
            line = raw.rstrip()
            stripped = line.strip()

            # Skip pure pass lines
            if is_pass(stripped):
                if in_fail_block:
                    flush()
                    in_fail_block = False
                continue

            if is_fail(stripped):
                flush()
                # Track failing file path (best-effort): tokens ending with .test.ts(x) / .spec.ts etc
                m = re.search(r"(\S+\.(?:test|spec)\.(?:ts|tsx|js|jsx|mjs|cjs))", stripped)
                if m:
                    fail_files.append(m.group(1))
                out.append(stripped)
                in_fail_block = True
                continue

            if is_summary(stripped):
                if in_fail_block:
                    flush()
                    in_fail_block = False
                out.append(stripped)
                continue

            if in_fail_block:
                fail_block.append(line)

        flush()

        if not out:
            for raw in lines:
                if "passed" in raw and "failed" not in raw:
                    return _strip_ansi(raw).strip()
            return "ok"

        # Drop pure visual separator rows from final output too
        out = [
            ln for ln in out
            if not re.match(r"^[─━=_\-⎯\s]+$", ln)
            and not re.match(r"^⎯+\s*Failed Tests?.*⎯+$", ln)
        ]
        # Strip wide separators embedded in fail headers
        out = [re.sub(r"\s*⎯+\s*Failed Tests?\s*\d*\s*⎯+\s*", "", ln) for ln in out]
        out = [ln for ln in out if ln.strip()]

        unique_files: list[str] = []
        for f in fail_files:
            if f not in unique_files:
                unique_files.append(f)
        if len(unique_files) > 1:
            return f"failed files ({len(unique_files)}): " + ", ".join(unique_files) + "\n" + "\n".join(out)
        return "\n".join(out)
