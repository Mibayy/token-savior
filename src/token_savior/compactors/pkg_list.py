"""Package manager listing compactors: npm/yarn/pnpm list, pip list/show."""
from __future__ import annotations

import re

from .base import Compactor


_VER_RE = re.compile(r"(\d+)\.(\d+)(?:\.\d+)?(?:[-+][\w.]+)?")


def _trim_version(name_at_ver: str) -> str:
    """``react@18.2.4-canary.1``  ->  ``react@18.2``. Pass-through if no match."""
    if "@" not in name_at_ver:
        return name_at_ver
    head, _, ver = name_at_ver.rpartition("@")
    # scoped package: ``@scope/pkg@1.2.3``
    if not head:
        return name_at_ver
    m = _VER_RE.match(ver)
    if not m:
        return name_at_ver
    return f"{head}@{m.group(1)}.{m.group(2)}"


class NpmListCompactor(Compactor):
    """Collapse npm/yarn/pnpm dependency tree to top-level packages only.

    Recognizes ASCII tree characters (``├── └── │``) and keeps only the
    first indentation level. Trims long semver strings to ``major.minor``.
    """

    _CMD_RE = re.compile(r"^\s*(npm|yarn|pnpm)\s+(list|ls)\b")
    # Match a tree line: optional spaces, ├── or └── or ` `, then the entry.
    _TOPLEVEL_RE = re.compile(r"^[├└][─-]{2}\s+(\S.*)$")

    def matches(self, command: str) -> bool:
        return bool(self._CMD_RE.search(command))

    def compact(self, stdout: str, stderr: str = "") -> str:
        out: list[str] = []
        first_line = True
        for raw in stdout.splitlines():
            line = raw.rstrip()
            if not line.strip():
                continue
            # Keep the project header line (first non-empty, e.g. ``myapp@1.0.0 /path``)
            if first_line and not line.startswith(("├", "└", "│", " ")):
                out.append(line.split(" ")[0])  # drop path
                first_line = False
                continue
            first_line = False
            m = self._TOPLEVEL_RE.match(line)
            if not m:
                continue  # nested dep, drop
            entry = m.group(1).split()[0]  # strip "deduped", "invalid", etc.
            out.append(_trim_version(entry))
        return "\n".join(out)


class PipListCompactor(Compactor):
    """Drop deprecation header from ``pip list`` / keep table from ``pip show``."""

    _CMD_RE = re.compile(r"^\s*pip3?\s+(list|show)\b")

    def matches(self, command: str) -> bool:
        return bool(self._CMD_RE.search(command))

    def compact(self, stdout: str, stderr: str = "") -> str:
        out: list[str] = []
        for raw in stdout.splitlines():
            line = raw.rstrip()
            stripped = line.strip()
            if not stripped:
                continue
            # Drop pip's verbose deprecation warnings + "DEPRECATION:" preambles
            if stripped.startswith("DEPRECATION:") or stripped.startswith("WARNING:"):
                continue
            if "pip install --upgrade pip" in stripped:
                continue
            # Drop the "Package    Version" rule line (---- ----)
            if re.match(r"^-{2,}(\s+-{2,})+$", stripped):
                continue
            out.append(line)
        return "\n".join(out)
