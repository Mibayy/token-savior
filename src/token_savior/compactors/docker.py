"""Docker output compactors."""
from __future__ import annotations

import re

from .base import Compactor


class DockerPsCompactor(Compactor):
    _CMD_RE = re.compile(r"^\s*docker\s+(ps|container\s+ls)\b")

    def matches(self, command: str) -> bool:
        return bool(self._CMD_RE.search(command))

    def compact(self, stdout: str, stderr: str = "") -> str:
        lines = [line for line in stdout.splitlines() if line.strip()]
        if not lines:
            return ""
        # Header row tells us column starts; we keep NAMES, IMAGE, STATUS only.
        header = lines[0]
        try:
            i_image = header.index("IMAGE")
            i_command = header.index("COMMAND")
            i_status = header.index("STATUS")
            i_ports = header.index("PORTS") if "PORTS" in header else -1
            i_names = header.index("NAMES")
        except ValueError:
            # Unknown header layout — return as-is
            return stdout

        out = ["NAME  IMAGE  STATUS"]
        for row in lines[1:]:
            if len(row) < i_names:
                continue
            image = row[i_image:i_command].strip()
            status = row[i_status:(i_ports if i_ports > 0 else i_names)].strip()
            name = row[i_names:].strip()
            out.append(f"{name}  {image}  {status}")
        return "\n".join(out)


class DockerLogsCompactor(Compactor):
    _CMD_RE = re.compile(r"^\s*docker\s+(logs|service\s+logs|container\s+logs)\b")

    def matches(self, command: str) -> bool:
        return bool(self._CMD_RE.search(command))

    @staticmethod
    def _normalize(line: str) -> str:
        # Strip leading timestamp so identical messages dedupe regardless of clock
        return re.sub(r"^\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(\.\d+)?(Z| [+-]\d{4})?\s*", "", line)

    def compact(self, stdout: str, stderr: str = "") -> str:
        haystack = stdout + ("\n" + stderr if stderr else "")
        out: list[str] = []
        prev_norm: str | None = None
        prev_raw: str | None = None
        count = 0
        for raw in haystack.splitlines():
            norm = self._normalize(raw)
            if norm == prev_norm:
                count += 1
                continue
            if prev_raw is not None:
                if count > 1:
                    out.append(f"{prev_raw} (x{count})")
                else:
                    out.append(prev_raw)
            prev_raw = raw
            prev_norm = norm
            count = 1
        if prev_raw is not None:
            if count > 1:
                out.append(f"{prev_raw} (x{count})")
            else:
                out.append(prev_raw)
        return "\n".join(out)
