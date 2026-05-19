"""ts_discover — scan Claude Code transcripts for missed Token Savior opportunities.

Walks per-project transcripts under ``~/.claude/projects/<sanitized>/*.jsonl``,
extracts tool-call events, and runs a small set of pattern detectors that
flag chains of native calls that should have been a single Token Savior
call (inspired by ``rtk discover``).

Public API:

* :func:`discover` returns a ranked list of :class:`Finding`s, each with a
  count, a last-seen timestamp, a representative example, and the canonical
  TS call that would have replaced the chain. As of v4.2.0 ``project=None``
  means "all transcript projects"; pass a substring to filter.
* :func:`discover_adoption` returns an :class:`AdoptionReport` of TS vs
  native tool-call ratios over the same window.

Read-only — never mutates transcript files. PII-safe — Findings carry tool
names, counts, file extensions, and timestamps only, never user prompts
or tool inputs verbatim.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterator

from token_savior.discover.transcript_scanner import (
    Event,
    iter_events,
    transcript_root,
)
from token_savior.discover.patterns import (
    ALL_PATTERNS,
    AdoptionReport,
    Finding,
    SessionAdoption,
    compute_adoption,
)


__all__ = [
    "AdoptionReport",
    "Event",
    "Finding",
    "SessionAdoption",
    "discover",
    "discover_adoption",
    "transcript_root",
]


def _iter_project_dirs(base: Path) -> Iterator[str]:
    """Yield sanitized project-dir names under ``base`` (one per subdir)."""
    if not base.exists() or not base.is_dir():
        return
    for entry in sorted(base.iterdir()):
        if entry.is_dir():
            yield entry.name


def _stream_sessions(
    base: Path,
    cutoff: datetime,
    project: str | None,
) -> Iterator[tuple[str, str, list[Event]]]:
    """Yield ``(session_id, project_dir, events_sorted_by_ts)`` per session.

    Streams session by session: at most one session's events live in memory at
    a time. ``project=None`` walks every project dir; otherwise the substring
    filter from :func:`iter_events` applies.
    """
    current_sid: str | None = None
    current_proj: str = ""
    current_events: list[Event] = []
    for ev in iter_events(base, since=cutoff, project=project):
        sid = ev.session_id or "_"
        if current_sid is None:
            current_sid = sid
            current_proj = ev.project
        elif sid != current_sid:
            current_events.sort(
                key=lambda e: e.ts or datetime.min.replace(tzinfo=timezone.utc)
            )
            yield current_sid, current_proj, current_events
            current_sid = sid
            current_proj = ev.project
            current_events = []
        current_events.append(ev)
    if current_sid is not None:
        current_events.sort(
            key=lambda e: e.ts or datetime.min.replace(tzinfo=timezone.utc)
        )
        yield current_sid, current_proj, current_events


def discover(
    since_days: int = 7,
    project: str | None = None,
    root: Path | None = None,
) -> list[Finding]:
    """Scan recent transcripts and return missed-TS-opportunity Findings.

    Args:
        since_days: Only consider events with ``ts`` newer than ``now - since_days``.
        project: ``None`` (default) scans **all** transcript project dirs under
            the transcript root and aggregates findings across them. Pass a
            substring to restrict to dirs whose sanitized name contains it.
        root: Override the transcript root (defaults to ``~/.claude/projects``).
            Mainly for tests.

    Returns:
        A list of Findings ranked by count (descending), then by pattern name.
        Each Finding carries a ``top_projects: dict[str, int]`` mapping the
        sanitized project dir to the number of chain hits observed there (only
        populated when scanning across multiple projects).
    """
    base = root if root is not None else transcript_root()
    if not base.exists():
        return []

    cutoff = datetime.now(timezone.utc) - timedelta(days=max(0, since_days))

    # Aggregator: bucket by (pattern, replacement) so we can sum across
    # sessions and projects without retaining individual events.
    bucket: dict[tuple[str, str], Finding] = {}
    for _sid, proj, session_events in _stream_sessions(base, cutoff, project):
        for pattern in ALL_PATTERNS:
            for f in pattern.detect(session_events):
                key = (f.pattern, f.replacement)
                existing = bucket.get(key)
                if existing is None:
                    # Stamp the project hit count for this Finding.
                    if proj:
                        f.top_projects[proj] = f.top_projects.get(proj, 0) + f.count
                    bucket[key] = f
                else:
                    existing.count += f.count
                    if f.last_seen and (
                        existing.last_seen is None or f.last_seen > existing.last_seen
                    ):
                        existing.last_seen = f.last_seen
                    if not existing.example and f.example:
                        existing.example = f.example
                    existing.sessions.update(f.sessions)
                    if proj:
                        existing.top_projects[proj] = (
                            existing.top_projects.get(proj, 0) + f.count
                        )

    out = list(bucket.values())
    out.sort(key=lambda f: (-f.count, f.pattern))
    return out


def discover_adoption(
    since_days: int = 7,
    project: str | None = None,
    root: Path | None = None,
) -> AdoptionReport:
    """Compute TS vs native adoption ratios over recent transcripts.

    Same window semantics as :func:`discover`: ``project=None`` scans all
    transcript project dirs; pass a substring to restrict.
    """
    base = root if root is not None else transcript_root()
    if not base.exists():
        return AdoptionReport()

    cutoff = datetime.now(timezone.utc) - timedelta(days=max(0, since_days))

    # Stream session-by-session into compute_adoption to keep memory bounded.
    def _gen() -> Iterator[tuple[str, list[Event]]]:
        for sid, _proj, evs in _stream_sessions(base, cutoff, project):
            yield sid, evs

    return compute_adoption(_gen())
