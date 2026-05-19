"""curl output compactor — truncates large bodies, keeps status/content-type."""
from __future__ import annotations

import re

from .base import Compactor


_HEAD_KEEP_BYTES = 2048
_TAIL_KEEP_BYTES = 200
_THRESHOLD_BYTES = 4096


class CurlCompactor(Compactor):
    """Compact ``curl`` body output.

    Strategy:
      - Detect HTTP response-line + headers (from ``-i`` / ``-v``) and keep the
        status line + ``Content-Type`` header.
      - Strip ``-w`` write-out boilerplate (anything past a recognizable
        ``write-out:`` / ``http_code:`` / ``time_total:`` block).
      - If the remaining body exceeds ``_THRESHOLD_BYTES``, keep the first
        ``_HEAD_KEEP_BYTES`` + last ``_TAIL_KEEP_BYTES`` bytes joined by a
        ``... N bytes truncated ...`` marker.
    """

    _CMD_RE = re.compile(r"^\s*curl(\s|$)")
    _STATUS_RE = re.compile(r"^HTTP/[\d.]+\s+\d{3}")
    _HEADER_RE = re.compile(r"^[A-Za-z][A-Za-z0-9\-]*:\s")
    _WRITE_OUT_RE = re.compile(
        r"^(write-out:|---WRITE-OUT---|time_total:|http_code:|size_download:)",
        re.IGNORECASE,
    )

    def matches(self, command: str) -> bool:
        return bool(self._CMD_RE.search(command))

    def compact(self, stdout: str, stderr: str = "") -> str:
        haystack = stdout
        status_line = ""
        content_type = ""
        lines = haystack.splitlines()
        body_start = 0
        for i, line in enumerate(lines):
            stripped = line.strip()
            if self._STATUS_RE.match(stripped):
                status_line = stripped
                continue
            if status_line and self._HEADER_RE.match(stripped):
                if stripped.lower().startswith("content-type:"):
                    content_type = stripped
                continue
            if status_line and stripped == "":
                body_start = i + 1
                break

        body = "\n".join(lines[body_start:]) if status_line else haystack

        # Strip --write-out trailing boilerplate (everything from first match onward)
        body_lines = body.splitlines()
        for idx, line in enumerate(body_lines):
            if self._WRITE_OUT_RE.match(line.strip()):
                body_lines = body_lines[:idx]
                break
        body = "\n".join(body_lines).rstrip()

        body_bytes = body.encode("utf-8")
        if len(body_bytes) > _THRESHOLD_BYTES:
            head = body_bytes[:_HEAD_KEEP_BYTES].decode("utf-8", errors="replace")
            tail = body_bytes[-_TAIL_KEEP_BYTES:].decode("utf-8", errors="replace")
            skipped = len(body_bytes) - _HEAD_KEEP_BYTES - _TAIL_KEEP_BYTES
            body = f"{head}\n... {skipped} bytes truncated ...\n{tail}"

        header_block: list[str] = []
        if status_line:
            header_block.append(status_line)
        if content_type:
            header_block.append(content_type)
        if header_block:
            return "\n".join(header_block) + (("\n" + body) if body else "")
        return body
