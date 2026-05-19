"""ts_discover — scan Claude Code transcripts for missed Token Savior opportunities.

Walks per-project transcripts under ``~/.claude/projects/<sanitized>/*.jsonl``,
extracts tool-call events, and runs a small set of pattern detectors that
flag chains of native calls that should have been a single Token Savior
call (inspired by ``rtk discover``).

Public API: :func:`discover` returns a ranked list of :class:`Finding`s,
each with a count, a last-seen timestamp, a representative example, and
the canonical TS call that would have replaced the chain.

Read-only — never mutates transcript files. PII-safe — Findings carry tool
names, counts, file extensions, and timestamps only, never user prompts
or tool inputs verbatim.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

from token_savior.discover.transcript_scanner import (
    Event,
    iter_events,
    transcript_root,
)
from token_savior.discover.patterns import ALL_PATTERNS, Finding


__all__ = [
    "Event",
    "Finding",
    "discover",
    "transcript_root",
]


def discover(
    since_days: int = 7,
    project: str | None = None,
    root: Path | None = None,
) -> list[Finding]:
    """Scan recent transcripts and return missed-TS-opportunity Findings.

    Args:
        since_days: Only consider events with ``ts`` newer than ``now - since_days``.
        project: If given, restrict to transcripts whose ``project`` field
            (sanitized cwd) matches this string (substring match).
        root: Override the transcript root (defaults to ``~/.claude/projects``).
            Mainly for tests.

    Returns:
        A list of Findings ranked by count (descending), then by tool name.
    """
    base = root if root is not None else transcript_root()
    if not base.exists():
        return []

    cutoff = datetime.now(timezone.utc) - timedelta(days=max(0, since_days))

    events_by_session: dict[str, list[Event]] = {}
    for ev in iter_events(base, since=cutoff, project=project):
        events_by_session.setdefault(ev.session_id or "_", []).append(ev)

    findings: list[Finding] = []
    for pattern in ALL_PATTERNS:
        for session_events in events_by_session.values():
            session_events.sort(key=lambda e: e.ts or datetime.min.replace(tzinfo=timezone.utc))
            findings.extend(pattern.detect(session_events))

    # Aggregate by (pattern_name, replacement) — count chains across sessions.
    bucket: dict[tuple[str, str], Finding] = {}
    for f in findings:
        key = (f.pattern, f.replacement)
        existing = bucket.get(key)
        if existing is None:
            bucket[key] = f
        else:
            existing.count += f.count
            if f.last_seen and (existing.last_seen is None or f.last_seen > existing.last_seen):
                existing.last_seen = f.last_seen
            # Keep one example only; don't accumulate to avoid memory blow-up.
            if not existing.example and f.example:
                existing.example = f.example

    out = list(bucket.values())
    out.sort(key=lambda f: (-f.count, f.pattern))
    return out
