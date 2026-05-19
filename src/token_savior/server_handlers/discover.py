"""MCP handler for ``ts_discover`` — surface missed Token Savior opportunities.

Wraps :func:`token_savior.discover.discover` and
:func:`token_savior.discover.discover_adoption`. Formats output as Markdown by
default or JSON for machine consumption. Read-only on transcripts; PII-safe
by construction (Findings carry tool names + counts only — see
``discover/patterns.py``).

Supported ``format`` values:

* ``table`` (default) -- ranked missed-opportunity table.
* ``json``            -- machine-readable Findings payload.
* ``adoption``        -- TS vs native adoption summary as a Markdown table.
* ``adoption_json``   -- adoption summary as JSON.
"""

from __future__ import annotations

import json
from typing import Any

from token_savior._compat import TextContent
from token_savior._compat import types

from token_savior.discover import (
    discover as _discover,
    discover_adoption as _discover_adoption,
)
from token_savior.discover.patterns import AdoptionReport


def _fmt_table(findings: list) -> str:
    if not findings:
        return "No missed Token Savior opportunities found in window."
    rows = ["# ts_discover — missed Token Savior opportunities", ""]
    rows.append("| count | pattern | replacement | last seen | top projects | example |")
    rows.append("| ----: | ------- | ----------- | --------- | ------------ | ------- |")
    for f in findings:
        last = f.last_seen.strftime("%Y-%m-%d %H:%M") if f.last_seen else "-"
        if f.top_projects:
            top = sorted(f.top_projects.items(), key=lambda kv: -kv[1])[:3]
            top_str = ", ".join(f"{p}:{c}" for p, c in top)
        else:
            top_str = "-"
        rows.append(
            f"| {f.count} | {f.pattern} | `{f.replacement}` | {last} | {top_str} | {f.example} |"
        )
    total = sum(f.count for f in findings)
    rows.append("")
    rows.append(f"_Total: {total} occurrences across {len(findings)} pattern(s)._")
    return "\n".join(rows)


def _fmt_adoption(report: AdoptionReport) -> str:
    if not report.sessions:
        return "No sessions in window."
    ts_pct = report.ts_ratio * 100.0
    native_pct = report.native_ratio * 100.0
    trend = report.trend_delta * 100.0
    arrow = "↑" if trend > 0.5 else ("↓" if trend < -0.5 else "→")

    lines = [
        "# ts_discover — Token Savior adoption",
        "",
        f"**Overall:** {report.total_ts} TS / {report.total_native} native "
        f"({ts_pct:.1f}% TS, {native_pct:.1f}% native) across {len(report.sessions)} session(s)",
        "",
        f"**Trend:** first half {report.first_half_ratio * 100.0:.1f}% TS  "
        f"→  second half {report.second_half_ratio * 100.0:.1f}% TS  ({arrow} {trend:+.1f}pp)",
        "",
        "## Worst 5 sessions (most native-heavy)",
        "",
        "| session | project | ts | native | TS % | last seen |",
        "| ------- | ------- | -: | -----: | ---: | --------- |",
    ]
    for s in report.worst_sessions(5):
        last = s.last_ts.strftime("%Y-%m-%d %H:%M") if s.last_ts else "-"
        sid_short = (s.session_id[:8] + "…") if len(s.session_id) > 8 else s.session_id
        proj = s.project or "-"
        lines.append(
            f"| {sid_short} | {proj} | {s.ts_calls} | {s.native_calls} | "
            f"{s.ts_ratio * 100.0:.1f}% | {last} |"
        )
    return "\n".join(lines)


def _hm_ts_discover(arguments: dict[str, Any]) -> list[types.TextContent]:
    since_days = int(arguments.get("since_days", 7) or 7)
    project = arguments.get("project") or None
    fmt = (arguments.get("format") or "table").lower()
    limit = arguments.get("limit")

    if fmt in ("adoption", "adoption_json"):
        try:
            report = _discover_adoption(since_days=since_days, project=project)
        except Exception as exc:  # defensive — never break dispatch
            return [TextContent(type="text", text=f"ts_discover error: {exc}")]

        if fmt == "adoption_json":
            payload = {
                "since_days": since_days,
                "project": project,
                "adoption": report.to_dict(),
            }
            return [TextContent(type="text", text=json.dumps(payload, indent=2))]
        return [TextContent(type="text", text=_fmt_adoption(report))]

    try:
        findings = _discover(since_days=since_days, project=project)
    except Exception as exc:  # defensive — never break dispatch
        return [TextContent(type="text", text=f"ts_discover error: {exc}")]

    if isinstance(limit, int) and limit > 0:
        findings = findings[:limit]

    if fmt == "json":
        payload = {
            "since_days": since_days,
            "project": project,
            "findings": [f.to_dict() for f in findings],
        }
        return [TextContent(type="text", text=json.dumps(payload, indent=2))]

    return [TextContent(type="text", text=_fmt_table(findings))]


HANDLERS: dict[str, Any] = {
    "ts_discover": _hm_ts_discover,
}
