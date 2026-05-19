"""find output compactor — head/tail truncation with a single-root prefix strip."""
from __future__ import annotations

import os
import re

from .base import Compactor


_SHELL_COMPOSITION_RE = re.compile(r"[|;&]|&&|\|\|")

# Limits chosen to match the spec.
_PASSTHROUGH_LIMIT = 30
_MEDIUM_LIMIT = 200
_MEDIUM_HEAD = 15
_MEDIUM_TAIL = 5
_LARGE_HEAD = 10
_LARGE_TAIL = 5


class FindCompactor:
    """Truncate ``find`` output to head + tail, optionally stripping the root.

    Strategy:
      - Match ``find`` as a standalone verb.
      - Pass through if output has ≤ 30 lines.
      - 30..200 lines → strip a common path prefix (the user-supplied root,
        if all lines share it), then show first 15 + last 5.
      - > 200 lines → first 10 + last 5.
    """

    _CMD_RE = re.compile(r"^\s*find(?![\w\-])")

    def matches(self, command: str) -> bool:
        if not command:
            return False
        if _SHELL_COMPOSITION_RE.search(command):
            return False
        return bool(self._CMD_RE.search(command))

    def compact(self, stdout: str, stderr: str = "") -> str:
        text = stdout or ""
        if stderr:
            text = (text + "\n" + stderr) if text else stderr

        lines = [ln for ln in text.splitlines() if ln.strip()]
        n = len(lines)
        if n <= _PASSTHROUGH_LIMIT:
            return "\n".join(lines)

        # Try to find a common path prefix that we can strip. We only strip
        # when *every* line shares it, to avoid mangling output.
        stripped = self._strip_common_prefix(lines)

        if n <= _MEDIUM_LIMIT:
            head = stripped[:_MEDIUM_HEAD]
            tail = stripped[-_MEDIUM_TAIL:]
            more = n - _MEDIUM_HEAD - _MEDIUM_TAIL
            return "\n".join(head + [f"... ({more} more)"] + tail)

        head = stripped[:_LARGE_HEAD]
        tail = stripped[-_LARGE_TAIL:]
        more = n - _LARGE_HEAD - _LARGE_TAIL
        return "\n".join(
            head + [f"... ({more} more — {n} items total)"] + tail
        )

    @staticmethod
    def _strip_common_prefix(lines: list[str]) -> list[str]:
        prefix = os.path.commonpath(lines) if lines else ""
        # commonpath returns the longest *path* prefix. Only strip if the
        # prefix is a non-trivial absolute or relative root that ends a
        # directory (so we don't chop mid-filename).
        if not prefix or prefix in (".", "/"):
            return lines
        strip = prefix.rstrip("/") + "/"
        out: list[str] = []
        for ln in lines:
            if ln.startswith(strip):
                out.append(ln[len(strip):])
            else:
                # commonpath says they share `prefix`, but a sibling at the
                # same depth could differ in trailing slash semantics. Bail
                # back to the raw list if any line doesn't conform.
                return lines
        return out
