"""Pure rendering helpers for ``get_usage_stats`` v2 output.

Stateless utilities that take aggregated history rows and produce ASCII
artifacts (sparkline, daily table, top-tools, session delta). No imports
from runtime state: callers pass in the data they want rendered.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Iterable


SPARK = "▁▂▃▄▅▆▇█"


def sparkline(vals: list[int]) -> str:
    """Render an ASCII sparkline. Empty / all-zero input renders as flat baseline."""
    if not vals:
        return ""
    m = max(vals)
    if m <= 0:
        return SPARK[0] * len(vals)
    return "".join(SPARK[min(7, int(v / m * 7))] for v in vals)


def _parse_ts(ts: str) -> datetime | None:
    """Parse an ISO-Z timestamp (``2026-05-01T12:00:00Z``). Returns None on bad input."""
    if not ts:
        return None
    try:
        if ts.endswith("Z"):
            ts = ts[:-1] + "+00:00"
        return datetime.fromisoformat(ts).astimezone(timezone.utc)
    except Exception:
        return None


def daily_token_savings(history: Iterable[dict[str, Any]], days: int) -> list[int]:
    """Return token-savings per day for the last ``days`` days (oldest first)."""
    if days <= 0:
        return []
    today = datetime.now(timezone.utc).date()
    buckets: dict[Any, int] = {today - timedelta(days=i): 0 for i in range(days)}
    for entry in history:
        ts = _parse_ts(entry.get("timestamp", ""))
        if ts is None:
            continue
        day = ts.date()
        if day not in buckets:
            continue
        saved = int(entry.get("tokens_naive", 0)) - int(entry.get("tokens_used", 0))
        if saved > 0:
            buckets[day] += saved
    return [buckets[today - timedelta(days=i)] for i in range(days - 1, -1, -1)]


def daily_breakdown(history: Iterable[dict[str, Any]], days: int = 7) -> list[dict[str, Any]]:
    """Aggregate history into per-day rows (oldest first).

    Each row: ``{date, calls, tokens_saved, top_tool}``.
    """
    today = datetime.now(timezone.utc).date()
    days_window = [today - timedelta(days=i) for i in range(days - 1, -1, -1)]
    by_day: dict[Any, dict[str, Any]] = {
        d: {"date": d.isoformat(), "calls": 0, "tokens_saved": 0, "tools": {}}
        for d in days_window
    }
    for entry in history:
        ts = _parse_ts(entry.get("timestamp", ""))
        if ts is None:
            continue
        day = ts.date()
        if day not in by_day:
            continue
        row = by_day[day]
        row["calls"] += int(entry.get("query_calls", 0))
        row["tokens_saved"] += max(
            0, int(entry.get("tokens_naive", 0)) - int(entry.get("tokens_used", 0))
        )
        for tool, count in (entry.get("tool_counts") or {}).items():
            row["tools"][tool] = row["tools"].get(tool, 0) + int(count)
    out: list[dict[str, Any]] = []
    for d in days_window:
        row = by_day[d]
        tools = row.pop("tools")
        top_tool = max(tools.items(), key=lambda kv: kv[1])[0] if tools else ""
        row["top_tool"] = top_tool
        out.append(row)
    return out


def render_daily_table(rows: list[dict[str, Any]]) -> list[str]:
    """Render daily breakdown rows as a fixed-width ASCII table."""
    if not rows:
        return []
    lines = [
        "Daily breakdown (last {n} days):".format(n=len(rows)),
        f"  {'date':<10} {'calls':>6} {'tokens saved':>14}  top tool",
    ]
    for row in rows:
        tt = row.get("top_tool") or "-"
        lines.append(
            f"  {row['date']:<10} {row['calls']:>6} {row['tokens_saved']:>14,}  {tt}"
        )
    return lines


def top_tools_by_savings(
    history: Iterable[dict[str, Any]], top_n: int = 5
) -> list[dict[str, Any]]:
    """Estimate per-tool tokens economized across all history.

    Per session entry, total savings = ``tokens_naive - tokens_used``,
    distributed proportionally to per-tool call counts in that session.
    """
    per_tool_calls: dict[str, int] = {}
    per_tool_saved: dict[str, float] = {}
    for entry in history:
        tools = entry.get("tool_counts") or {}
        total_calls = sum(int(c) for c in tools.values())
        if total_calls <= 0:
            continue
        saved = max(
            0,
            int(entry.get("tokens_naive", 0)) - int(entry.get("tokens_used", 0)),
        )
        for tool, count in tools.items():
            c = int(count)
            per_tool_calls[tool] = per_tool_calls.get(tool, 0) + c
            if saved > 0:
                per_tool_saved[tool] = per_tool_saved.get(tool, 0.0) + saved * c / total_calls
    ranked = sorted(
        per_tool_calls.items(),
        key=lambda kv: (-per_tool_saved.get(kv[0], 0.0), -kv[1]),
    )[:top_n]
    return [
        {
            "tool": tool,
            "calls": calls,
            "tokens_saved": int(round(per_tool_saved.get(tool, 0.0))),
        }
        for tool, calls in ranked
    ]


def render_top_tools(rows: list[dict[str, Any]]) -> list[str]:
    if not rows:
        return []
    lines = [
        f"Top {len(rows)} tools (cumulative tokens economized):",
        f"  {'tool':<32} {'calls':>6} {'tokens saved':>14}",
    ]
    for r in rows:
        lines.append(
            f"  {r['tool']:<32} {r['calls']:>6} {r['tokens_saved']:>14,}"
        )
    return lines


def session_delta(history: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Return delta between the two most recent sessions, or None."""
    if len(history) < 2:
        return None
    prev, curr = history[-2], history[-1]
    cur_calls = int(curr.get("query_calls", 0))
    prev_calls = int(prev.get("query_calls", 0))
    cur_saved = max(
        0, int(curr.get("tokens_naive", 0)) - int(curr.get("tokens_used", 0))
    )
    prev_saved = max(
        0, int(prev.get("tokens_naive", 0)) - int(prev.get("tokens_used", 0))
    )
    return {
        "current_session": curr.get("session_id", ""),
        "previous_session": prev.get("session_id", ""),
        "delta_calls": cur_calls - prev_calls,
        "delta_tokens_saved": cur_saved - prev_saved,
        "current_calls": cur_calls,
        "previous_calls": prev_calls,
        "current_tokens_saved": cur_saved,
        "previous_tokens_saved": prev_saved,
    }


def render_session_delta(delta: dict[str, Any] | None) -> list[str]:
    if not delta:
        return []
    sign_c = "+" if delta["delta_calls"] >= 0 else ""
    sign_s = "+" if delta["delta_tokens_saved"] >= 0 else ""
    return [
        "Session vs previous:",
        f"  calls         : {delta['previous_calls']} -> {delta['current_calls']} ({sign_c}{delta['delta_calls']})",
        f"  tokens saved  : {delta['previous_tokens_saved']:,} -> {delta['current_tokens_saved']:,} ({sign_s}{delta['delta_tokens_saved']:,})",
    ]


def render_sparkline_section(vals: list[int], days: int) -> list[str]:
    if not vals:
        return []
    total = sum(vals)
    return [
        f"Token savings sparkline (last {days} days, total {total:,}):",
        f"  {sparkline(vals)}",
    ]
