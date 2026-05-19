"""MCP handler for ``ts_discover`` — surface missed Token Savior opportunities.

Wraps :func:`token_savior.discover.discover`, formats the resulting Findings
either as a readable Markdown table (default) or JSON. Read-only on
transcripts, PII-safe by construction (Findings carry tool names + counts
only — see ``discover/patterns.py``).
"""

from __future__ import annotations

import json
from typing import Any

from token_savior._compat import TextContent
from token_savior._compat import types

from token_savior.discover import discover as _discover


def _fmt_table(findings: list) -> str:
    if not findings:
        return "No missed Token Savior opportunities found in window."
    rows = ["# ts_discover — missed Token Savior opportunities", ""]
    rows.append("| count | pattern | replacement | last seen | example |")
    rows.append("| ----: | ------- | ----------- | --------- | ------- |")
    for f in findings:
        last = f.last_seen.strftime("%Y-%m-%d %H:%M") if f.last_seen else "-"
        rows.append(
            f"| {f.count} | {f.pattern} | `{f.replacement}` | {last} | {f.example} |"
        )
    total = sum(f.count for f in findings)
    rows.append("")
    rows.append(f"_Total: {total} occurrences across {len(findings)} pattern(s)._")
    return "\n".join(rows)


def _hm_ts_discover(arguments: dict[str, Any]) -> list[types.TextContent]:
    since_days = int(arguments.get("since_days", 7) or 7)
    project = arguments.get("project") or None
    fmt = (arguments.get("format") or "table").lower()
    limit = arguments.get("limit")

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
