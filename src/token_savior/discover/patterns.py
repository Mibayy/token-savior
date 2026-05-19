"""Pattern detectors for missed Token Savior opportunities.

Each detector is a pure function over a *single session's* event list,
already sorted by timestamp. Detectors yield :class:`Finding` instances
that the top-level :func:`discover` aggregates across sessions.

A pattern carries:

* ``pattern``     -- short stable identifier, used as the aggregation key.
* ``replacement`` -- the canonical TS call that would have replaced the chain.
* ``count``       -- how many times the chain was observed.
* ``last_seen``   -- timestamp of the most recent occurrence.
* ``example``     -- a short, PII-safe sample (tool names + extensions only).
"""

from __future__ import annotations

import os
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Iterable, Iterator

from token_savior.discover.transcript_scanner import Event


_CODE_EXTS = (".py", ".ts", ".tsx", ".js", ".jsx", ".rs", ".go")
_NATIVE_SHELL_CODE_VERBS = ("grep", "cat", "head", "sed", "awk", "find", "rg", "tail")


@dataclass
class Finding:
    pattern: str
    replacement: str
    count: int = 1
    last_seen: datetime | None = None
    example: str = ""
    sessions: set[str] = field(default_factory=set)

    def to_dict(self) -> dict:
        return {
            "pattern": self.pattern,
            "replacement": self.replacement,
            "count": self.count,
            "last_seen": self.last_seen.isoformat() if self.last_seen else None,
            "example": self.example,
            "sessions": len(self.sessions),
        }


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _file_ext(path: str) -> str:
    if not path:
        return ""
    _, ext = os.path.splitext(path)
    return ext.lower()


def _is_code_file(path: str) -> bool:
    return _file_ext(path) in _CODE_EXTS


def _within(a: datetime | None, b: datetime | None, seconds: float) -> bool:
    if a is None or b is None:
        return False
    return abs((b - a).total_seconds()) <= seconds


def _project_of(ev: Event) -> str:
    # Best-effort project key: prefer Read/Edit's file_path top-level dir.
    fp = ev.args.get("file_path")
    if isinstance(fp, str) and fp.startswith("/"):
        parts = fp.split("/")
        if len(parts) >= 3:
            return "/".join(parts[:3])  # e.g. /root/ts-f4
    return ev.project


# ---------------------------------------------------------------------------
# Pattern base class
# ---------------------------------------------------------------------------


class Pattern(ABC):
    name: str
    replacement: str

    @abstractmethod
    def detect(self, events: list[Event]) -> Iterator[Finding]:
        ...


# ---------------------------------------------------------------------------
# 1. Read -> Grep -> Read chain  ->  get_full_context(name, depth=2)
# ---------------------------------------------------------------------------


class ReadGrepReadPattern(Pattern):
    name = "read_grep_read_chain"
    replacement = "get_full_context(name, depth=2)"
    window_seconds = 120.0

    def detect(self, events: list[Event]) -> Iterator[Finding]:
        # Sliding window: any (Read, Grep, Read) triple within window_seconds
        # on the same project root.
        n = len(events)
        for i in range(n - 2):
            a, b, c = events[i], events[i + 1], events[i + 2]
            if a.tool_name != "Read" or b.tool_name != "Grep" or c.tool_name != "Read":
                continue
            if not _within(a.ts, c.ts, self.window_seconds):
                continue
            proj = _project_of(a)
            if proj != _project_of(c):
                continue
            ext_a = _file_ext(a.args.get("file_path", "") or "")
            ext_c = _file_ext(c.args.get("file_path", "") or "")
            example = f"Read{ext_a} -> Grep -> Read{ext_c}"
            yield Finding(
                pattern=self.name,
                replacement=self.replacement,
                count=1,
                last_seen=c.ts,
                example=example,
                sessions={a.session_id},
            )


# ---------------------------------------------------------------------------
# 2. >=3 find_symbol calls in a row within 60s  ->  batch find_symbol(names=[...])
# ---------------------------------------------------------------------------


class BatchFindSymbolPattern(Pattern):
    name = "sequential_find_symbol"
    replacement = "find_symbol(names=[...])  # batch up to 10"
    window_seconds = 60.0
    threshold = 3

    def _is_find_symbol(self, ev: Event) -> bool:
        tn = ev.tool_name
        return tn == "find_symbol" or tn.endswith("__find_symbol")

    def detect(self, events: list[Event]) -> Iterator[Finding]:
        i = 0
        n = len(events)
        while i < n:
            if not self._is_find_symbol(events[i]):
                i += 1
                continue
            j = i + 1
            while (
                j < n
                and self._is_find_symbol(events[j])
                and _within(events[i].ts, events[j].ts, self.window_seconds)
                # also bail if name= was a list (already batched)
                and not isinstance(events[j].args.get("names"), list)
                and not isinstance(events[i].args.get("names"), list)
            ):
                j += 1
            run = j - i
            if run >= self.threshold:
                yield Finding(
                    pattern=self.name,
                    replacement=self.replacement,
                    count=1,
                    last_seen=events[j - 1].ts,
                    example=f"{run} sequential find_symbol calls within {self.window_seconds:.0f}s",
                    sessions={events[i].session_id},
                )
            i = j if j > i else i + 1


# ---------------------------------------------------------------------------
# 3. get_function_source -> Edit on same symbol w/o prior get_full_context
#    -> get_edit_context(name)
# ---------------------------------------------------------------------------


class EditWithoutContextPattern(Pattern):
    name = "edit_without_context"
    replacement = "get_edit_context(name)"
    window_seconds = 300.0

    @staticmethod
    def _is_get_function_source(ev: Event) -> bool:
        return ev.tool_name == "get_function_source" or ev.tool_name.endswith(
            "__get_function_source"
        )

    @staticmethod
    def _is_get_full_context(ev: Event) -> bool:
        return ev.tool_name == "get_full_context" or ev.tool_name.endswith(
            "__get_full_context"
        )

    @staticmethod
    def _is_get_edit_context(ev: Event) -> bool:
        return ev.tool_name == "get_edit_context" or ev.tool_name.endswith(
            "__get_edit_context"
        )

    @staticmethod
    def _is_edit(ev: Event) -> bool:
        tn = ev.tool_name
        return tn in {"Edit", "Write"} or tn.endswith("__replace_symbol_source") or tn.endswith("__insert_near_symbol")

    def detect(self, events: list[Event]) -> Iterator[Finding]:
        n = len(events)
        # Track the most recent get_full_context / get_edit_context call so
        # we can short-circuit if the model already did the right thing.
        last_context_ts: datetime | None = None
        last_gfs: Event | None = None
        for ev in events:
            if self._is_get_full_context(ev) or self._is_get_edit_context(ev):
                last_context_ts = ev.ts
                last_gfs = None
                continue
            if self._is_get_function_source(ev):
                last_gfs = ev
                continue
            if self._is_edit(ev) and last_gfs is not None:
                if not _within(last_gfs.ts, ev.ts, self.window_seconds):
                    last_gfs = None
                    continue
                if last_context_ts is not None and _within(last_context_ts, ev.ts, self.window_seconds):
                    last_gfs = None
                    continue
                yield Finding(
                    pattern=self.name,
                    replacement=self.replacement,
                    count=1,
                    last_seen=ev.ts,
                    example="get_function_source -> Edit (no prior get_full/edit_context)",
                    sessions={ev.session_id},
                )
                last_gfs = None


# ---------------------------------------------------------------------------
# 4. memory_search without prior memory_index  ->  always memory_index first
# ---------------------------------------------------------------------------


class MemorySearchWithoutIndexPattern(Pattern):
    name = "memory_search_without_index"
    replacement = "memory_index(query=...)  # Layer 1 first"

    @staticmethod
    def _is_memory_search(ev: Event) -> bool:
        return ev.tool_name == "memory_search" or ev.tool_name.endswith("__memory_search")

    @staticmethod
    def _is_memory_index(ev: Event) -> bool:
        return ev.tool_name == "memory_index" or ev.tool_name.endswith("__memory_index")

    def detect(self, events: list[Event]) -> Iterator[Finding]:
        # Per session: if memory_search runs and its most recent preceding
        # memory_* call (in same session) wasn't memory_index -> flag.
        last_memory_call: str | None = None
        for ev in events:
            if self._is_memory_search(ev):
                if last_memory_call != "memory_index":
                    yield Finding(
                        pattern=self.name,
                        replacement=self.replacement,
                        count=1,
                        last_seen=ev.ts,
                        example="memory_search without memory_index in session",
                        sessions={ev.session_id},
                    )
                last_memory_call = "memory_search"
            elif self._is_memory_index(ev):
                last_memory_call = "memory_index"


# ---------------------------------------------------------------------------
# 5. Native Bash grep/cat/head/sed/awk/find on code files -> TS tool
# ---------------------------------------------------------------------------


class NativeShellOnCodePattern(Pattern):
    name = "native_shell_on_code"
    replacement = "search_codebase / get_function_source / find_symbol"

    def detect(self, events: list[Event]) -> Iterator[Finding]:
        for ev in events:
            if ev.tool_name != "Bash":
                continue
            cmd = ev.args.get("command")
            if not isinstance(cmd, str) or not cmd:
                continue
            # Strip leading shell prefixes (cd dir && ..., env=val ...).
            head = cmd.strip().lstrip("(").lstrip()
            # Drop a leading "cd path && " segment if present.
            if head.startswith("cd "):
                idx = head.find("&&")
                if idx != -1:
                    head = head[idx + 2 :].strip()
            verb = head.split(None, 1)[0] if head else ""
            if verb not in _NATIVE_SHELL_CODE_VERBS:
                continue
            # Does the command target a code file? Look for any token ending
            # in one of the code extensions.
            ext_hit = ""
            for tok in cmd.split():
                e = _file_ext(tok)
                if e in _CODE_EXTS:
                    ext_hit = e
                    break
            if not ext_hit:
                continue
            yield Finding(
                pattern=self.name,
                replacement=self.replacement,
                count=1,
                last_seen=ev.ts,
                example=f"Bash {verb} on *{ext_hit}",
                sessions={ev.session_id},
            )


ALL_PATTERNS: list[Pattern] = [
    ReadGrepReadPattern(),
    BatchFindSymbolPattern(),
    EditWithoutContextPattern(),
    MemorySearchWithoutIndexPattern(),
    NativeShellOnCodePattern(),
]
