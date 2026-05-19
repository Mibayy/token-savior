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
from datetime import datetime
from typing import Iterable, Iterator

from token_savior.discover.transcript_scanner import Event


_CODE_EXTS = (".py", ".ts", ".tsx", ".js", ".jsx", ".rs", ".go")
_NATIVE_SHELL_CODE_VERBS = ("grep", "cat", "head", "sed", "awk", "find", "rg", "tail")

# Tools considered "Token Savior" calls for adoption accounting.
TS_TOOL_PREFIX = "mcp__token-savior__"

# Native tools that compete with TS equivalents. Used by adoption-mode only;
# pattern detectors keep their own tighter heuristics.
_NATIVE_CODE_TOOLS = frozenset(
    {
        "Read",
        "Grep",
        "Glob",
        "Edit",
        "Write",
        "MultiEdit",
        "Bash",
        "NotebookEdit",
        "NotebookRead",
    }
)


def is_ts_tool(tool_name: str) -> bool:
    """Return True if ``tool_name`` is a Token Savior MCP call."""
    return bool(tool_name) and tool_name.startswith(TS_TOOL_PREFIX)


def is_native_tool(tool_name: str) -> bool:
    """Return True if ``tool_name`` is a native Claude Code tool that
    competes with a TS equivalent (Read/Grep/Edit/Bash/...).
    """
    return tool_name in _NATIVE_CODE_TOOLS


@dataclass
class Finding:
    pattern: str
    replacement: str
    count: int = 1
    last_seen: datetime | None = None
    example: str = ""
    sessions: set[str] = field(default_factory=set)
    # Per-project hit counts (sanitized dir name -> count). Populated by the
    # cross-project aggregator in ``discover()``; empty for single-project
    # scans or when a Finding is constructed directly by a pattern.
    top_projects: dict[str, int] = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "pattern": self.pattern,
            "replacement": self.replacement,
            "count": self.count,
            "last_seen": self.last_seen.isoformat() if self.last_seen else None,
            "example": self.example,
            "sessions": len(self.sessions),
            "top_projects": dict(
                sorted(self.top_projects.items(), key=lambda kv: -kv[1])[:5]
            ),
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


# ---------------------------------------------------------------------------
# Adoption mode — overall TS vs native ratio across recent sessions
# ---------------------------------------------------------------------------


@dataclass
class SessionAdoption:
    """Per-session TS / native call counts.

    ``project`` is the sanitized transcript dir name; ``last_ts`` is the most
    recent event timestamp observed in that session (used to split the window
    into halves for trend computation).
    """

    session_id: str
    project: str
    ts_calls: int = 0
    native_calls: int = 0
    other_calls: int = 0
    last_ts: datetime | None = None

    @property
    def total_relevant(self) -> int:
        return self.ts_calls + self.native_calls

    @property
    def ts_ratio(self) -> float:
        denom = self.total_relevant
        return (self.ts_calls / denom) if denom else 0.0

    def to_dict(self) -> dict:
        return {
            "session_id": self.session_id,
            "project": self.project,
            "ts_calls": self.ts_calls,
            "native_calls": self.native_calls,
            "other_calls": self.other_calls,
            "ts_ratio": round(self.ts_ratio, 4),
            "last_ts": self.last_ts.isoformat() if self.last_ts else None,
        }


@dataclass
class AdoptionReport:
    """Aggregate TS / native adoption report across a window.

    ``trend`` compares the first half of the window (by event timestamp) to
    the second half. A positive ``trend_delta`` means TS adoption is growing.
    """

    total_ts: int = 0
    total_native: int = 0
    total_other: int = 0
    sessions: list[SessionAdoption] = field(default_factory=list)
    first_half_ts: int = 0
    first_half_native: int = 0
    second_half_ts: int = 0
    second_half_native: int = 0

    @property
    def total_relevant(self) -> int:
        return self.total_ts + self.total_native

    @property
    def ts_ratio(self) -> float:
        denom = self.total_relevant
        return (self.total_ts / denom) if denom else 0.0

    @property
    def native_ratio(self) -> float:
        denom = self.total_relevant
        return (self.total_native / denom) if denom else 0.0

    @property
    def first_half_ratio(self) -> float:
        denom = self.first_half_ts + self.first_half_native
        return (self.first_half_ts / denom) if denom else 0.0

    @property
    def second_half_ratio(self) -> float:
        denom = self.second_half_ts + self.second_half_native
        return (self.second_half_ts / denom) if denom else 0.0

    @property
    def trend_delta(self) -> float:
        """Second-half ratio minus first-half ratio (positive = growing TS use)."""
        return self.second_half_ratio - self.first_half_ratio

    def worst_sessions(self, k: int = 5) -> list[SessionAdoption]:
        """Top-k sessions with the worst (most native-heavy) ratios.

        Sessions with no relevant traffic are excluded. Ties break on absolute
        native call count (more native wins) so high-volume offenders rank above
        tiny ones.
        """
        eligible = [s for s in self.sessions if s.total_relevant > 0]
        eligible.sort(key=lambda s: (s.ts_ratio, -s.native_calls))
        return eligible[:k]

    def to_dict(self) -> dict:
        return {
            "total_ts": self.total_ts,
            "total_native": self.total_native,
            "total_other": self.total_other,
            "ts_ratio": round(self.ts_ratio, 4),
            "native_ratio": round(self.native_ratio, 4),
            "first_half_ratio": round(self.first_half_ratio, 4),
            "second_half_ratio": round(self.second_half_ratio, 4),
            "trend_delta": round(self.trend_delta, 4),
            "session_count": len(self.sessions),
            "worst_sessions": [s.to_dict() for s in self.worst_sessions(5)],
        }


def compute_adoption(
    events_by_session: dict[str, list[Event]] | Iterable[tuple[str, Iterable[Event]]],
) -> AdoptionReport:
    """Compute TS vs native adoption ratios over a set of sessions.

    Accepts either ``{session_id: [Event, ...]}`` or an iterable of
    ``(session_id, events)`` pairs. Events are streamed once per session;
    no global accumulation of events is performed.

    Trend split: gather every relevant event's timestamp, take the median, then
    classify each event into first / second half by that midpoint. Events
    without timestamps are excluded from the trend (but still counted in
    totals).
    """
    report = AdoptionReport()
    # Two-pass over each session to avoid storing all events globally.
    # Pass 1: per-session counts + collect timestamps for the trend split.
    # Pass 2: re-tally relevant events into first/second half using the
    # median computed from pass 1.

    items: list[tuple[str, list[Event]]]
    if isinstance(events_by_session, dict):
        items = [(sid, evs) for sid, evs in events_by_session.items()]
    else:
        items = [(sid, list(evs)) for sid, evs in events_by_session]

    timestamps: list[datetime] = []
    for session_id, events in items:
        sa = SessionAdoption(session_id=session_id, project="")
        for ev in events:
            if not sa.project and ev.project:
                sa.project = ev.project
            if ev.ts and (sa.last_ts is None or ev.ts > sa.last_ts):
                sa.last_ts = ev.ts
            tn = ev.tool_name
            if is_ts_tool(tn):
                sa.ts_calls += 1
                if ev.ts:
                    timestamps.append(ev.ts)
            elif is_native_tool(tn):
                sa.native_calls += 1
                if ev.ts:
                    timestamps.append(ev.ts)
            else:
                sa.other_calls += 1
        report.total_ts += sa.ts_calls
        report.total_native += sa.native_calls
        report.total_other += sa.other_calls
        report.sessions.append(sa)

    # Trend split — median of relevant timestamps. Empty / single-event
    # windows leave both halves at zero (trend_delta == 0.0).
    if len(timestamps) >= 2:
        timestamps.sort()
        midpoint = timestamps[len(timestamps) // 2]
        for _, events in items:
            for ev in events:
                if not ev.ts:
                    continue
                tn = ev.tool_name
                is_ts = is_ts_tool(tn)
                is_nat = (not is_ts) and is_native_tool(tn)
                if not (is_ts or is_nat):
                    continue
                if ev.ts < midpoint:
                    if is_ts:
                        report.first_half_ts += 1
                    else:
                        report.first_half_native += 1
                else:
                    if is_ts:
                        report.second_half_ts += 1
                    else:
                        report.second_half_native += 1

    return report
