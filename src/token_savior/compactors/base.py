"""Compactor ABC + result dataclass."""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass


@dataclass(frozen=True)
class CompactResult:
    text: str
    original_bytes: int
    compact_bytes: int
    savings_pct: float
    # Full original output (stdout + stderr) preserved so callers can route it
    # to the sandbox in hybrid mode without re-fetching. Defaults to "" for
    # backward-compat with pre-v4.2 call sites.
    original_text: str = ""


class Compactor(ABC):
    @abstractmethod
    def matches(self, command: str) -> bool: ...

    @abstractmethod
    def compact(self, stdout: str, stderr: str = "") -> str: ...
