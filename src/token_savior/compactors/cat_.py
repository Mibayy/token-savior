"""cat output compactor — head/tail truncation for accidental huge cats."""
from __future__ import annotations

import re

from .base import Compactor


_SHELL_COMPOSITION_RE = re.compile(r"[|;&]|&&|\|\|")

_PASSTHROUGH_LIMIT = 50
_MEDIUM_LIMIT = 200
_MEDIUM_HEAD = 25
_MEDIUM_TAIL = 10
_LARGE_HEAD = 30
_LARGE_TAIL = 10


class CatCompactor:
    """Truncate ``cat`` output when the file is large.

    Strategy:
      - Match ``cat <file>`` and ``cat -n <file>``, including ``cat -A``,
        ``cat -v``, ``-b``, etc., as long as there's no shell composition.
      - Pass through ≤ 50 lines.
      - 51..200 lines → head 25 + ``(N lines elided)`` + tail 10.
      - > 200 lines  → head 30 + ``(N lines elided)`` + tail 10.
    """

    _CMD_RE = re.compile(r"^\s*cat(?![\w\-])")

    def matches(self, command: str) -> bool:
        if not command:
            return False
        if _SHELL_COMPOSITION_RE.search(command):
            return False
        return bool(self._CMD_RE.search(command))

    def compact(self, stdout: str, stderr: str = "") -> str:
        # cat output is mostly stdout; preserve stderr separately if any.
        text = stdout or ""
        lines = text.splitlines()
        n = len(lines)
        if n <= _PASSTHROUGH_LIMIT:
            out = text
            if stderr:
                out = (out + "\n" + stderr) if out else stderr
            return out

        if n <= _MEDIUM_LIMIT:
            head = lines[:_MEDIUM_HEAD]
            tail = lines[-_MEDIUM_TAIL:]
            elided = n - _MEDIUM_HEAD - _MEDIUM_TAIL
            body = "\n".join(head + [f"... ({elided} lines elided)"] + tail)
        else:
            head = lines[:_LARGE_HEAD]
            tail = lines[-_LARGE_TAIL:]
            elided = n - _LARGE_HEAD - _LARGE_TAIL
            body = "\n".join(head + [f"... ({elided} lines elided)"] + tail)

        if stderr:
            body = body + "\n" + stderr
        return body
