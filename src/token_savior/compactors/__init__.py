"""Bash output compactors — opt-in compression layer for the tool-capture hook.

Each compactor is a pure function (no I/O, no globals) that recognizes a
known command family and returns a token-efficient rendering of the output.
The dispatcher returns ``None`` when no compactor matches, leaving the
existing sandbox path untouched.
"""
from __future__ import annotations

from .base import CompactResult, Compactor
from .cargo_ import CargoBuildCompactor, CargoTestCompactor
from .docker import DockerLogsCompactor, DockerPsCompactor
from .gh import GhRunListCompactor, GhRunViewCompactor
from .git import (
    GitAddCompactor,
    GitCommitCompactor,
    GitDiffCompactor,
    GitLogCompactor,
    GitPushPullCompactor,
    GitStatusCompactor,
)
from .pytest_ import PytestCompactor
from .tsc import TscCompactor

# Order matters: more-specific patterns first so `gh run view` does not
# fall through to a hypothetical generic `gh` matcher.
registry: list[Compactor] = [
    GhRunViewCompactor(),
    GhRunListCompactor(),
    GitStatusCompactor(),
    GitDiffCompactor(),
    GitLogCompactor(),
    GitPushPullCompactor(),
    GitCommitCompactor(),
    GitAddCompactor(),
    PytestCompactor(),
    CargoTestCompactor(),
    CargoBuildCompactor(),
    TscCompactor(),
    DockerPsCompactor(),
    DockerLogsCompactor(),
]


def compact(command: str, stdout: str, stderr: str = "") -> CompactResult | None:
    if not command or not (stdout or stderr):
        return None
    original = (stdout or "") + (stderr or "")
    original_bytes = len(original.encode("utf-8"))
    if original_bytes == 0:
        return None
    for c in registry:
        if c.matches(command):
            text = c.compact(stdout, stderr)
            compact_bytes = len(text.encode("utf-8"))
            savings = 100.0 * (1.0 - compact_bytes / max(1, original_bytes))
            return CompactResult(
                text=text,
                original_bytes=original_bytes,
                compact_bytes=compact_bytes,
                savings_pct=savings,
                original_text=original,
            )
    return None


__all__ = ["compact", "registry", "CompactResult", "Compactor"]
