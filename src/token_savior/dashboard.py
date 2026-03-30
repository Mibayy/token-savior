from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import urlparse


DEFAULT_STATS_DIR = Path(os.environ.get("TOKEN_SAVIOR_STATS_DIR", "~/.local/share/token-savior")).expanduser()
HOST = os.environ.get("TOKEN_SAVIOR_DASHBOARD_HOST", "127.0.0.1")
PORT = int(os.environ.get("TOKEN_SAVIOR_DASHBOARD_PORT", "8921"))
INCLUDE_TMP_PROJECTS = os.environ.get("TOKEN_SAVIOR_INCLUDE_TMP_PROJECTS", "").lower() in {"1", "true", "yes"}
STARTED_AT = datetime.now(timezone.utc)


def load_payload(path: Path) -> dict | None:
    try:
        with path.open(encoding="utf-8") as fh:
            payload = json.load(fh)
            if isinstance(payload, dict):
                return payload
    except Exception:
        return None
    return None


def _project_name(payload: dict, path: Path) -> str:
    project_root = str(payload.get("project") or "").rstrip("/")
    if project_root:
        base = os.path.basename(project_root) or project_root
        return "token-savior" if base == "mcp-codebase-index" else base
    derived = path.stem.rsplit("-", 1)[0]
    return "token-savior" if derived == "mcp-codebase-index" else derived


def _display_project_root(value: object) -> str:
    project_root = str(value or "").strip()
    if not project_root:
        return ""
    return project_root.replace("/root/mcp-codebase-index", "/root/token-savior")


def _safe_int(payload: dict, key: str) -> int:
    try:
        return int(payload.get(key, 0) or 0)
    except Exception:
        return 0


def _recent_sessions(payload: dict, project_name: str) -> list[dict]:
    sessions = []
    for entry in payload.get("history", []):
        session = dict(entry)
        session["project"] = project_name
        session["client_name"] = _client_name(entry.get("client_name"))
        sessions.append(session)
    return sessions


def _client_name(value: object) -> str:
    name = str(value or "").strip()
    return name or "unknown"


def _project_client_counts(payload: dict) -> dict[str, int]:
    client_counts: dict[str, int] = {}
    for client_name, count in payload.get("client_counts", {}).items():
        try:
            normalized = _client_name(client_name)
            client_counts[normalized] = client_counts.get(normalized, 0) + int(count)
        except Exception:
            continue
    if client_counts:
        return client_counts
    history = payload.get("history", [])
    if history:
        for entry in history:
            normalized = _client_name(entry.get("client_name"))
            client_counts[normalized] = client_counts.get(normalized, 0) + 1
        return client_counts
    sessions = _safe_int(payload, "sessions")
    if sessions > 0:
        client_counts["unknown"] = sessions
    return client_counts


def _should_include_project(payload: dict, path: Path) -> bool:
    if INCLUDE_TMP_PROJECTS:
        return True
    project_root = str(payload.get("project") or "")
    if project_root.startswith("/tmp/") or "/pytest-of-root/" in project_root:
        return False
    return True


def collect_dashboard_data(stats_dir: Path = DEFAULT_STATS_DIR) -> dict:
    files = sorted(stats_dir.glob("*.json")) if stats_dir.exists() else []
    projects = []
    recent_sessions = []
    tool_totals: dict[str, int] = {}
    client_totals: dict[str, int] = {}
    total_calls = 0
    total_chars_used = 0
    total_chars_naive = 0

    for path in files:
        payload = load_payload(path)
        if not payload:
            continue
        if not _should_include_project(payload, path):
            continue
        project_name = _project_name(payload, path)
        chars_used = _safe_int(payload, "total_chars_returned")
        chars_naive = _safe_int(payload, "total_naive_chars")
        calls = _safe_int(payload, "total_calls")
        sessions = _safe_int(payload, "sessions")
        project_client_counts = _project_client_counts(payload)
        savings_pct = round((1 - chars_used / chars_naive) * 100, 2) if chars_naive > 0 else 0.0

        project_row = {
            "project": project_name,
            "project_root": _display_project_root(payload.get("project", "")),
            "raw_project_root": str(payload.get("project") or ""),
            "stats_file": str(path),
            "sessions": sessions,
            "queries": calls,
            "chars_used": chars_used,
            "chars_naive": chars_naive,
            "tokens_used": chars_used // 4,
            "tokens_naive": chars_naive // 4,
            "chars_saved": max(chars_naive - chars_used, 0),
            "tokens_saved": max(chars_naive - chars_used, 0) // 4,
            "savings_pct": savings_pct,
            "last_session": payload.get("last_session"),
            "last_client": _client_name(payload.get("last_client") or next(iter(project_client_counts), "")),
            "tool_counts": payload.get("tool_counts", {}),
            "client_counts": project_client_counts,
        }
        projects.append(project_row)
        recent_sessions.extend(_recent_sessions(payload, project_name))
        total_calls += calls
        total_chars_used += chars_used
        total_chars_naive += chars_naive
        for tool, count in payload.get("tool_counts", {}).items():
            try:
                tool_totals[tool] = tool_totals.get(tool, 0) + int(count)
            except Exception:
                continue
        for client_name, count in project_client_counts.items():
            try:
                client_totals[client_name] = client_totals.get(client_name, 0) + int(count)
            except Exception:
                continue

    projects.sort(
        key=lambda item: (
            -item["tokens_saved"],
            -item["queries"],
            -item["sessions"],
            item["project"].lower(),
        )
    )
    recent_sessions.sort(key=lambda item: item.get("timestamp", ""), reverse=True)
    recent_sessions = recent_sessions[:25]
    top_tools = sorted(tool_totals.items(), key=lambda item: (-item[1], item[0]))[:12]
    top_clients = sorted(client_totals.items(), key=lambda item: (-item[1], item[0]))
    generated_at = datetime.now(timezone.utc).isoformat()
    codex_sessions = client_totals.get("codex", 0)
    total_sessions = sum(client_totals.values())

    return {
        "generated_at": generated_at,
        "started_at": STARTED_AT.isoformat(),
        "stats_dir": str(stats_dir),
        "project_count": len(projects),
        "active_project_count": len(projects),
        "configured_project_count": len(projects),
        "idle_project_count": 0,
        "client_count": len(client_totals),
        "clients": [{"client": client_name, "sessions": count} for client_name, count in top_clients],
        "codex": {
            "sessions": codex_sessions,
            "active": codex_sessions > 0,
            "coverage_pct": round((codex_sessions / total_sessions) * 100, 2) if total_sessions > 0 else 0.0,
        },
        "projects": projects,
        "recent_sessions": recent_sessions,
        "top_tools": [{"tool": tool, "count": count} for tool, count in top_tools],
        "totals": {
            "queries": total_calls,
            "chars_used": total_chars_used,
            "chars_naive": total_chars_naive,
            "tokens_used": total_chars_used // 4,
            "tokens_naive": total_chars_naive // 4,
            "chars_saved": max(total_chars_naive - total_chars_used, 0),
            "tokens_saved": max(total_chars_naive - total_chars_used, 0) // 4,
            "savings_pct": round((1 - total_chars_used / total_chars_naive) * 100, 2) if total_chars_naive > 0 else 0.0,
        },
    }


HTML = """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Token Savior Dashboard</title>
  <style>
    :root {
      color-scheme: dark;
      --bg: #07111b;
      --bg2: #0d1724;
      --panel: rgba(10, 22, 35, 0.86);
      --panel-2: rgba(12, 28, 45, 0.9);
      --line: rgba(148, 181, 220, 0.16);
      --line-strong: rgba(148, 181, 220, 0.28);
      --text: #ebf2ff;
      --muted: #93a8c3;
      --soft: #c3d5ea;
      --good: #67e8b0;
      --warn: #f7c96a;
      --bad: #ff8f8f;
      --accent: #7cc7ff;
      --accent-2: #9a7cff;
      --shadow: 0 22px 60px rgba(0, 0, 0, 0.34);
      --radius: 24px;
    }
    * { box-sizing: border-box; }
    html, body { min-height: 100%; }
    body {
      margin: 0;
      font-family: Inter, "IBM Plex Sans", "Segoe UI", sans-serif;
      color: var(--text);
      background:
        radial-gradient(circle at top left, rgba(124, 199, 255, 0.12), transparent 32%),
        radial-gradient(circle at top right, rgba(154, 124, 255, 0.16), transparent 28%),
        linear-gradient(180deg, var(--bg) 0%, var(--bg2) 100%);
    }
    .shell {
      max-width: 1560px;
      margin: 0 auto;
      padding: 24px;
    }
    .hero {
      position: relative;
      overflow: hidden;
      padding: 26px;
      border-radius: 30px;
      border: 1px solid var(--line);
      background:
        linear-gradient(135deg, rgba(124,199,255,0.16), rgba(154,124,255,0.1)),
        rgba(8, 20, 32, 0.88);
      box-shadow: var(--shadow);
      margin-bottom: 18px;
    }
    .hero::after {
      content: "";
      position: absolute;
      inset: auto -8% -30% auto;
      width: 340px;
      height: 340px;
      border-radius: 50%;
      background: radial-gradient(circle, rgba(103,232,176,0.18), transparent 68%);
      pointer-events: none;
    }
    .hero-top {
      position: relative;
      z-index: 1;
      display: flex;
      align-items: flex-start;
      justify-content: space-between;
      gap: 16px;
      margin-bottom: 20px;
    }
    .eyebrow {
      display: inline-flex;
      align-items: center;
      gap: 8px;
      margin-bottom: 14px;
      padding: 7px 12px;
      border-radius: 999px;
      border: 1px solid rgba(124, 199, 255, 0.22);
      background: rgba(8, 20, 32, 0.4);
      color: var(--soft);
      font-size: 12px;
      letter-spacing: 0.08em;
      text-transform: uppercase;
    }
    .dot {
      width: 8px;
      height: 8px;
      border-radius: 999px;
      background: var(--good);
      box-shadow: 0 0 18px rgba(103, 232, 176, 0.75);
    }
    h1, h2, h3, p { margin: 0; }
    h1 {
      font-size: clamp(32px, 4vw, 48px);
      line-height: 1.02;
      letter-spacing: -0.045em;
      max-width: 11ch;
    }
    h2 {
      font-size: 18px;
      letter-spacing: -0.02em;
    }
    .lead {
      margin-top: 12px;
      max-width: 780px;
      color: var(--soft);
      font-size: 15px;
      line-height: 1.6;
    }
    .subtle, .meta, .muted {
      color: var(--muted);
      font-size: 13px;
      line-height: 1.5;
    }
    .hero-meta {
      min-width: 300px;
      padding: 18px;
      border-radius: 22px;
      border: 1px solid rgba(148, 181, 220, 0.18);
      background: rgba(7, 17, 27, 0.54);
      backdrop-filter: blur(12px);
    }
    .hero-meta strong {
      display: block;
      margin-top: 8px;
      font-size: 28px;
      letter-spacing: -0.04em;
    }
    .stats-grid {
      position: relative;
      z-index: 1;
      display: grid;
      grid-template-columns: repeat(6, minmax(0, 1fr));
      gap: 12px;
    }
    .stat {
      padding: 16px;
      border-radius: 20px;
      border: 1px solid var(--line);
      background: rgba(7, 17, 27, 0.62);
      min-height: 116px;
    }
    .stat-label {
      color: var(--muted);
      font-size: 12px;
      text-transform: uppercase;
      letter-spacing: 0.08em;
    }
    .stat-value {
      margin-top: 10px;
      font-size: clamp(24px, 2.8vw, 36px);
      font-weight: 700;
      letter-spacing: -0.04em;
    }
    .stat-hint {
      margin-top: 8px;
      color: var(--soft);
      font-size: 13px;
      line-height: 1.45;
    }
    .accent { color: var(--accent); }
    .good { color: var(--good); }
    .warn { color: var(--warn); }
    .bad { color: var(--bad); }
    .mono { font-family: "IBM Plex Mono", Consolas, monospace; }
    .layout {
      display: grid;
      grid-template-columns: minmax(0, 1.6fr) minmax(360px, 0.95fr);
      gap: 18px;
      align-items: start;
    }
    .stack {
      display: grid;
      gap: 18px;
    }
    .panel {
      border-radius: var(--radius);
      border: 1px solid var(--line);
      background: var(--panel);
      box-shadow: var(--shadow);
      overflow: hidden;
    }
    .panel-head {
      display: flex;
      align-items: flex-start;
      justify-content: space-between;
      gap: 16px;
      padding: 22px 22px 14px;
      border-bottom: 1px solid rgba(148, 181, 220, 0.1);
    }
    .panel-body {
      padding: 18px 22px 22px;
    }
    .toolbar {
      display: flex;
      flex-wrap: wrap;
      gap: 10px;
      margin-bottom: 16px;
    }
    .search {
      flex: 1 1 240px;
      min-width: 0;
      padding: 12px 14px;
      border-radius: 14px;
      border: 1px solid var(--line-strong);
      background: rgba(6, 16, 25, 0.88);
      color: var(--text);
      outline: none;
    }
    .search:focus {
      border-color: rgba(124, 199, 255, 0.5);
      box-shadow: 0 0 0 3px rgba(124, 199, 255, 0.12);
    }
    .chip-row, .pill-row {
      display: flex;
      flex-wrap: wrap;
      gap: 8px;
    }
    .chip, .pill {
      display: inline-flex;
      align-items: center;
      gap: 6px;
      padding: 7px 10px;
      border-radius: 999px;
      border: 1px solid var(--line);
      background: rgba(10, 24, 38, 0.82);
      color: var(--soft);
      font-size: 12px;
      line-height: 1;
    }
    .chip strong, .pill strong {
      color: var(--text);
      font-weight: 600;
    }
    .projects-grid {
      display: grid;
      gap: 12px;
    }
    .project-card {
      padding: 16px;
      border-radius: 20px;
      border: 1px solid var(--line);
      background: linear-gradient(180deg, rgba(10, 24, 38, 0.94), rgba(8, 18, 29, 0.9));
    }
    .project-top {
      display: flex;
      align-items: flex-start;
      justify-content: space-between;
      gap: 12px;
    }
    .project-title {
      font-size: 18px;
      letter-spacing: -0.03em;
    }
    .project-path {
      margin-top: 6px;
      color: var(--muted);
      font-size: 12px;
      line-height: 1.5;
      word-break: break-all;
    }
    .project-metrics {
      display: grid;
      grid-template-columns: repeat(4, minmax(0, 1fr));
      gap: 10px;
      margin-top: 14px;
    }
    .mini {
      padding: 10px 12px;
      border-radius: 14px;
      background: rgba(255,255,255,0.02);
      border: 1px solid rgba(148, 181, 220, 0.08);
    }
    .mini-label {
      color: var(--muted);
      font-size: 11px;
      text-transform: uppercase;
      letter-spacing: 0.08em;
    }
    .mini-value {
      margin-top: 6px;
      font-size: 20px;
      font-weight: 700;
      letter-spacing: -0.03em;
    }
    .bar {
      margin-top: 14px;
      height: 10px;
      border-radius: 999px;
      background: rgba(255,255,255,0.06);
      overflow: hidden;
      border: 1px solid rgba(148, 181, 220, 0.08);
    }
    .bar > span {
      display: block;
      height: 100%;
      border-radius: inherit;
      background: linear-gradient(90deg, var(--accent), var(--good));
    }
    .project-foot {
      display: flex;
      flex-wrap: wrap;
      gap: 8px;
      justify-content: space-between;
      align-items: center;
      margin-top: 14px;
    }
    .list {
      display: grid;
      gap: 10px;
    }
    .session-row {
      padding: 10px 12px;
      border-radius: 14px;
      border: 1px solid var(--line);
      background: linear-gradient(180deg, rgba(10, 24, 38, 0.94), rgba(8, 18, 29, 0.9));
    }
    .session-top {
      display: flex;
      justify-content: space-between;
      align-items: center;
      gap: 12px;
      margin-bottom: 6px;
    }
    .session-grid {
      display: grid;
      grid-template-columns: 1.2fr 0.9fr 0.9fr 0.8fr;
      gap: 8px;
    }
    .session-cell {
      display: flex;
      flex-direction: column;
      gap: 2px;
      min-width: 0;
    }
    .session-label {
      color: var(--muted);
      font-size: 10px;
      text-transform: uppercase;
      letter-spacing: 0.08em;
    }
    .session-value {
      font-size: 13px;
      color: var(--soft);
      line-height: 1.25;
      white-space: nowrap;
      overflow: hidden;
      text-overflow: ellipsis;
    }
    .tag-cloud {
      display: flex;
      flex-wrap: wrap;
      gap: 8px;
    }
    .tag {
      display: inline-flex;
      align-items: center;
      padding: 8px 10px;
      border-radius: 14px;
      border: 1px solid var(--line);
      background: rgba(10, 24, 38, 0.82);
      color: var(--soft);
      font-size: 12px;
    }
    .empty {
      padding: 18px;
      border-radius: 18px;
      border: 1px dashed var(--line-strong);
      color: var(--muted);
      background: rgba(10, 24, 38, 0.52);
      font-size: 13px;
    }
    .footnote {
      margin-top: 14px;
      color: var(--muted);
      font-size: 12px;
      line-height: 1.55;
    }
    @media (max-width: 1280px) {
      .stats-grid { grid-template-columns: repeat(3, minmax(0, 1fr)); }
      .layout { grid-template-columns: 1fr; }
    }
    @media (max-width: 860px) {
      .shell { padding: 14px; }
      .hero, .panel-head, .panel-body { padding-left: 16px; padding-right: 16px; }
      .hero-top { flex-direction: column; }
      .hero-meta { min-width: 0; width: 100%; }
      .stats-grid { grid-template-columns: repeat(2, minmax(0, 1fr)); }
      .project-metrics, .session-grid { grid-template-columns: repeat(2, minmax(0, 1fr)); }
    }
    @media (max-width: 560px) {
      .stats-grid, .project-metrics, .session-grid { grid-template-columns: 1fr; }
    }
  </style>
</head>
<body>
  <div class="shell">
    <section class="hero">
      <div class="hero-top">
        <div>
          <div class="eyebrow"><span class="dot"></span> live token efficiency telemetry</div>
          <h1>Token Savior workspace dashboard</h1>
          <p class="lead">A cleaner view of what is actually being used, what is merely configured, and where the savings are coming from.</p>
        </div>
        <div class="hero-meta">
          <div class="meta">Workspace health</div>
          <strong id="headlineProject">0 active projects</strong>
          <p class="subtle" id="subline">Waiting for stats...</p>
          <p class="footnote mono" id="statsDir"></p>
        </div>
      </div>
      <div class="stats-grid">
        <div class="stat">
          <div class="stat-label">Savings rate</div>
          <div class="stat-value good" id="savingsPct">0%</div>
          <div class="stat-hint" id="savedTokens">0 tokens saved</div>
        </div>
        <div class="stat">
          <div class="stat-label">Tokens used</div>
          <div class="stat-value" id="tokensUsed">0</div>
          <div class="stat-hint" id="tokensNaive">Naive 0</div>
        </div>
        <div class="stat">
          <div class="stat-label">Queries</div>
          <div class="stat-value accent" id="queries">0</div>
          <div class="stat-hint" id="sessionCount">0 observed sessions</div>
        </div>
        <div class="stat">
          <div class="stat-label">Projects in view</div>
          <div class="stat-value" id="projects">0</div>
          <div class="stat-hint" id="projectCount">0 active • 0 idle</div>
        </div>
        <div class="stat">
          <div class="stat-label">Clients</div>
          <div class="stat-value" id="clientsCount">0</div>
          <div class="stat-hint" id="clientSummary">Watching persisted Token Savior sessions.</div>
        </div>
        <div class="stat">
          <div class="stat-label">Codex coverage</div>
          <div class="stat-value" id="codexCoverage">0%</div>
          <div class="stat-hint" id="codexSummary">No Codex-attributed sessions yet.</div>
        </div>
      </div>
    </section>

    <section class="layout">
      <div class="stack">
        <section class="panel">
          <div class="panel-head">
            <div>
              <h2>Projects</h2>
              <p class="subtle">A project appears here only after Token Savior has actually been used on it.</p>
            </div>
            <div class="chip-row" id="projectSummaryChips"></div>
          </div>
          <div class="panel-body">
            <div class="toolbar">
              <input class="search" id="projectSearch" type="search" placeholder="Filter by project, client, or path">
            </div>
            <div class="projects-grid" id="projectsGrid"></div>
          </div>
        </section>
      </div>

      <div class="stack">
        <section class="panel">
          <div class="panel-head">
            <div>
              <h2>Clients and tools</h2>
              <p class="subtle">Who is calling the server and which tools are doing the work.</p>
            </div>
          </div>
          <div class="panel-body">
            <div class="tag-cloud" id="topClients"></div>
            <div style="height:12px"></div>
            <div class="tag-cloud" id="topTools"></div>
          </div>
        </section>

        <section class="panel">
          <div class="panel-head">
            <div>
              <h2>Recent sessions</h2>
              <p class="subtle">Latest persisted snapshots across the workspace.</p>
            </div>
          </div>
          <div class="panel-body">
            <div class="list" id="sessionsList"></div>
          </div>
        </section>
      </div>
    </section>
  </div>

  <script>
    const state = { data: null };

    function fmtInt(value) {
      return new Intl.NumberFormat('en-US').format(Number(value || 0));
    }

    function fmtPct(value) {
      return `${Number(value || 0).toFixed(1)}%`;
    }

    function fmtDate(value) {
      if (!value) return 'No sessions yet';
      return String(value).replace('T', ' ').replace('Z', ' UTC').slice(0, 23);
    }

    function esc(value) {
      return String(value || '')
        .replaceAll('&', '&amp;')
        .replaceAll('<', '&lt;')
        .replaceAll('>', '&gt;')
        .replaceAll('"', '&quot;')
        .replaceAll("'", '&#39;');
    }

    function pillClassFromPct(value) {
      const pct = Number(value || 0);
      if (pct >= 85) return 'good';
      if (pct >= 60) return 'warn';
      return 'bad';
    }

    function renderProjects(data) {
      const query = String(document.getElementById('projectSearch').value || '').trim().toLowerCase();
      const rows = (data.projects || []).filter((row) => {
        if (!query) return true;
        const haystack = [
          row.project,
          row.project_root,
          row.last_client,
          Object.keys(row.client_counts || {}).join(' '),
        ].join(' ').toLowerCase();
        return haystack.includes(query);
      });

      document.getElementById('projectsGrid').innerHTML = rows.map((row) => {
        const clientPills = Object.entries(row.client_counts || {}).map(([client, count]) =>
          `<span class="pill mono"><strong>${esc(client)}</strong> ${fmtInt(count)}</span>`
        ).join('') || '<span class="pill">No client history</span>';
        const lastSeen = row.last_session ? fmtDate(row.last_session) : 'No persisted session yet';
        const path = row.project_root || row.stats_file || 'No root recorded';
        return `
          <article class="project-card">
            <div class="project-top">
              <div>
                <div class="project-title">${esc(row.project)}</div>
                <div class="project-path mono">${esc(path)}</div>
              </div>
              <span class="pill good">Observed</span>
            </div>
            <div class="project-metrics">
              <div class="mini">
                <div class="mini-label">Savings</div>
                <div class="mini-value ${pillClassFromPct(row.savings_pct)}">${fmtPct(row.savings_pct)}</div>
              </div>
              <div class="mini">
                <div class="mini-label">Sessions</div>
                <div class="mini-value">${fmtInt(row.sessions)}</div>
              </div>
              <div class="mini">
                <div class="mini-label">Queries</div>
                <div class="mini-value">${fmtInt(row.queries)}</div>
              </div>
              <div class="mini">
                <div class="mini-label">Saved tokens</div>
                <div class="mini-value good">${fmtInt(row.tokens_saved)}</div>
              </div>
            </div>
            <div class="bar"><span style="width:${Math.max(4, Math.min(100, Number(row.savings_pct || 0)))}%"></span></div>
            <div class="project-foot">
              <div class="pill-row">${clientPills}</div>
              <div class="muted">Last client: <span class="mono">${esc(row.last_client || 'unknown')}</span> • Last seen: <span class="mono">${esc(lastSeen)}</span></div>
            </div>
          </article>
        `;
      }).join('') || '<div class="empty">No project matches this filter.</div>';
    }

    function renderSessions(data) {
      document.getElementById('sessionsList').innerHTML = (data.recent_sessions || []).map((row) => `
        <article class="session-row">
          <div class="session-top">
            <div>
              <div><strong>${esc(row.project)}</strong></div>
              <div class="muted mono">${esc(fmtDate(row.timestamp))}</div>
            </div>
            <span class="pill ${pillClassFromPct(row.savings_pct)}">${fmtPct(row.savings_pct)}</span>
          </div>
          <div class="session-grid">
            <div class="session-cell">
              <div class="session-label">Client</div>
              <div class="session-value mono">${esc(row.client_name || 'unknown')}</div>
            </div>
            <div class="session-cell">
              <div class="session-label">Used</div>
              <div class="session-value mono">${fmtInt(row.tokens_used)}</div>
            </div>
            <div class="session-cell">
              <div class="session-label">Naive</div>
              <div class="session-value mono">${fmtInt(row.tokens_naive)}</div>
            </div>
            <div class="session-cell">
              <div class="session-label">Calls</div>
              <div class="session-value mono">${fmtInt(row.query_calls)}</div>
            </div>
          </div>
        </article>
      `).join('') || '<div class="empty">No sessions yet.</div>';
    }

    function renderTags(targetId, rows, labelFormatter) {
      document.getElementById(targetId).innerHTML = rows.map(labelFormatter).join('') || '<div class="empty">Nothing recorded yet.</div>';
    }

    function render(data) {
      state.data = data;
      const observedSessions = (data.recent_sessions || []).length;
      document.getElementById('headlineProject').textContent = `${fmtInt(data.project_count || 0)} active projects`;
      document.getElementById('subline').textContent = `Started ${fmtDate(data.started_at)} • Updated ${fmtDate(data.generated_at)}`;
      document.getElementById('statsDir').textContent = data.stats_dir || '';

      document.getElementById('savingsPct').textContent = fmtPct(data.totals.savings_pct);
      document.getElementById('savedTokens').textContent = `${fmtInt(data.totals.tokens_saved)} tokens saved across the workspace`;
      document.getElementById('tokensUsed').textContent = fmtInt(data.totals.tokens_used);
      document.getElementById('tokensNaive').textContent = `Naive baseline ${fmtInt(data.totals.tokens_naive)}`;
      document.getElementById('queries').textContent = fmtInt(data.totals.queries);
      document.getElementById('sessionCount').textContent = `${fmtInt(observedSessions)} recent snapshots shown`;
      document.getElementById('projects').textContent = fmtInt(data.project_count);
      document.getElementById('projectCount').textContent = `${fmtInt(data.project_count || 0)} active projects`;
      document.getElementById('clientsCount').textContent = fmtInt(data.client_count);
      document.getElementById('clientSummary').textContent = data.client_count
        ? `${fmtInt(data.client_count)} distinct clients seen in persisted stats`
        : 'Watching persisted Token Savior sessions.';
      document.getElementById('codexCoverage').textContent = fmtPct(data.codex.coverage_pct || 0);
      document.getElementById('codexSummary').textContent = data.codex.active
        ? `Codex sessions: ${fmtInt(data.codex.sessions)} of observed persisted sessions`
        : 'No Codex-attributed sessions yet. Set TOKEN_SAVIOR_CLIENT=codex in the MCP config if you want attribution.';

      document.getElementById('projectSummaryChips').innerHTML = [
        `<span class="chip"><strong>${fmtInt(data.project_count)}</strong> shown</span>`,
        `<span class="chip"><strong>${fmtInt(data.project_count || 0)}</strong> used with Token Savior</span>`,
        `<span class="chip"><strong>${fmtInt(data.client_count || 0)}</strong> clients seen</span>`,
        `<span class="chip"><strong>${fmtInt(data.totals.queries || 0)}</strong> total queries</span>`,
      ].join('');

      renderTags('topClients', data.clients || [], (row) => `<span class="tag mono">${esc(row.client)} · ${fmtInt(row.sessions)} sessions</span>`);
      renderTags('topTools', data.top_tools || [], (row) => `<span class="tag mono">${esc(row.tool)} · ${fmtInt(row.count)}</span>`);
      renderProjects(data);
      renderSessions(data);
    }

    async function refresh() {
      const res = await fetch('./api/status', { cache: 'no-store' });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const data = await res.json();
      render(data);
    }

    async function safeRefresh() {
      try {
        await refresh();
      } catch (error) {
        const message = error && error.message ? error.message : String(error || 'unknown error');
        document.getElementById('subline').textContent = `Dashboard fetch failed: ${message}`;
      }
    }

    document.getElementById('projectSearch').addEventListener('input', () => {
      if (state.data) renderProjects(state.data);
    });

    safeRefresh();
    setInterval(safeRefresh, 5000);
  </script>
</body>
</html>
"""


class Handler(BaseHTTPRequestHandler):
    def _send(self, code: int, body: bytes, content_type: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        path = urlparse(self.path).path
        if path == "/api/status":
            body = json.dumps(collect_dashboard_data(), indent=2).encode("utf-8")
            self._send(200, body, "application/json")
            return
        if path == "/":
            self._send(200, HTML.encode("utf-8"), "text/html; charset=utf-8")
            return
        self._send(404, b"not found", "text/plain; charset=utf-8")

    def log_message(self, format: str, *args) -> None:
        return


def main() -> None:
    server = HTTPServer((HOST, PORT), Handler)
    print(f"Token Savior dashboard listening on http://{HOST}:{PORT}")
    server.serve_forever()


if __name__ == "__main__":
    main()
