"""TypeScript compiler (tsc) output compactor — group errors by file."""
from __future__ import annotations

import re
from collections import defaultdict

from .base import Compactor


_TSC_LINE = re.compile(r"^(?P<file>[^()]+)\((?P<line>\d+),(?P<col>\d+)\):\s+error\s+(?P<code>TS\d+):\s+(?P<msg>.+)$")


class TscCompactor(Compactor):
    _CMD_RE = re.compile(r"^\s*(npx\s+)?tsc\b|^\s*pnpm\s+(run\s+)?(tsc|typecheck)\b|^\s*yarn\s+(tsc|typecheck)\b")

    def matches(self, command: str) -> bool:
        return bool(self._CMD_RE.search(command))

    def compact(self, stdout: str, stderr: str = "") -> str:
        by_file: dict[str, list[str]] = defaultdict(list)
        total = 0
        for raw in stdout.splitlines():
            line = raw.rstrip()
            m = _TSC_LINE.match(line)
            if m:
                by_file[m.group("file")].append(
                    f"  {m.group('line')}:{m.group('col')} {m.group('code')} {m.group('msg')}"
                )
                total += 1
        out: list[str] = []
        for file, errs in by_file.items():
            out.append(f"{file} ({len(errs)}):")
            out.extend(errs)
        if total:
            out.append(f"{total} errors in {len(by_file)} files")
        return "\n".join(out)
