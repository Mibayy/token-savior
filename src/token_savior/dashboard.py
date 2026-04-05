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
        return "token-savior" if base == "token-savior" else base
    derived = path.stem.rsplit("-", 1)[0]
    return "token-savior" if derived == "token-savior" else derived


def _display_project_root(value: object) -> str:
    project_root = str(value or "").strip()
    if not project_root:
        return ""
    return project_root.replace("/root/token-savior", "/root/token-savior")


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
    total_sessions = sum(client_totals.values())

    result = {
        "generated_at": generated_at,
        "started_at": STARTED_AT.isoformat(),
        "stats_dir": str(stats_dir),
        "project_count": len(projects),
        "client_count": len(client_totals),
        "total_sessions": total_sessions,
        "clients": [{"client": c, "sessions": n} for c, n in top_clients],
        "projects": projects,
        "recent_sessions": recent_sessions,
        "top_tools": [{"tool": t, "count": n} for t, n in top_tools],
        "totals": {
            "queries": total_calls,
            "chars_used": total_chars_used,
            "chars_naive": total_chars_naive,
            "tokens_used": total_chars_used // 4,
            "tokens_naive": total_chars_naive // 4,
            "chars_saved": max(total_chars_naive - total_chars_used, 0),
            "tokens_saved": max(total_chars_naive - total_chars_used, 0) // 4,
            "savings_pct": round((1 - total_chars_used / total_chars_naive) * 100, 2) if total_chars_naive > 0 else 0.0,
            "estimated_savings_usd": round(max(total_chars_naive - total_chars_used, 0) / 4 * 3.0 / 1_000_000, 2),
        },
    }
    for client_name, session_count in client_totals.items():
        result[client_name] = {"active": True, "sessions": session_count}
    return result


HTML = """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Token Savior</title>
  <style>
    @import url('https://fonts.googleapis.com/css2?family=Inter:wght@300;400;500;600;700&family=JetBrains+Mono:wght@400;500;600&display=swap');

    :root {
      --bg:        #060912;
      --bg2:       #0b0f1a;
      --card:      #0e1420;
      --card2:     #111827;
      --border:    rgba(255,255,255,0.06);
      --border2:   rgba(255,255,255,0.1);
      --text:      #f0f4ff;
      --muted:     #5a6a82;
      --soft:      #8fa3be;
      --emerald:   #10d98e;
      --emerald2:  #05a36a;
      --cyan:      #38bdf8;
      --violet:    #818cf8;
      --amber:     #f59e0b;
      --rose:      #fb7185;
      --r:         14px;
      --r2:        10px;
    }

    *, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }

    html { font-size: 14px; -webkit-font-smoothing: antialiased; }

    body {
      font-family: 'Inter', system-ui, sans-serif;
      background: var(--bg);
      color: var(--text);
      min-height: 100vh;
    }

    /* ─── Dot grid background ─── */
    body::before {
      content: '';
      position: fixed;
      inset: 0;
      background-image: radial-gradient(circle, rgba(255,255,255,0.04) 1px, transparent 1px);
      background-size: 28px 28px;
      pointer-events: none;
      z-index: 0;
    }

    .wrap {
      position: relative;
      z-index: 1;
      max-width: 1360px;
      margin: 0 auto;
      padding: 32px 24px 56px;
    }

    /* ─── Topbar ─── */
    .topbar {
      display: flex;
      align-items: center;
      justify-content: space-between;
      margin-bottom: 40px;
    }
    .brand {
      display: flex;
      align-items: center;
      gap: 10px;
    }
    .brand-icon {
      width: 34px; height: 34px;
      border-radius: 9px;
      background: linear-gradient(135deg, #1a3554, #0d2240);
      border: 1px solid rgba(56,189,248,0.25);
      display: grid; place-items: center;
      font-size: 16px;
      flex-shrink: 0;
    }
    .brand-name {
      font-size: 15px;
      font-weight: 600;
      letter-spacing: -0.02em;
      color: var(--text);
    }
    .brand-tagline {
      font-size: 11px;
      color: var(--muted);
      margin-top: 1px;
    }
    .status-pill {
      display: flex;
      align-items: center;
      gap: 8px;
      padding: 7px 14px;
      border-radius: 999px;
      border: 1px solid rgba(16,217,142,0.18);
      background: rgba(16,217,142,0.05);
      font-size: 12px;
      color: var(--emerald);
      font-weight: 500;
      letter-spacing: 0.01em;
    }
    .dot {
      width: 7px; height: 7px;
      border-radius: 50%;
      background: var(--emerald);
      flex-shrink: 0;
      animation: blink 2.4s ease-in-out infinite;
    }
    @keyframes blink {
      0%, 100% { opacity: 1; }
      50% { opacity: 0.3; }
    }

    /* ─── Hero ─── */
    .hero {
      text-align: center;
      margin-bottom: 48px;
      padding: 0 16px;
    }
    .hero-eyebrow {
      display: inline-flex;
      align-items: center;
      gap: 7px;
      padding: 5px 12px;
      border-radius: 999px;
      border: 1px solid var(--border2);
      background: rgba(255,255,255,0.03);
      font-size: 11px;
      font-weight: 500;
      color: var(--soft);
      letter-spacing: 0.06em;
      text-transform: uppercase;
      margin-bottom: 20px;
    }
    .hero-number {
      font-family: 'JetBrains Mono', monospace;
      font-size: clamp(72px, 12vw, 120px);
      font-weight: 600;
      letter-spacing: -0.05em;
      line-height: 1;
      background: linear-gradient(135deg, #10d98e 0%, #38bdf8 60%, #818cf8 100%);
      -webkit-background-clip: text;
      -webkit-text-fill-color: transparent;
      background-clip: text;
      margin-bottom: 12px;
    }
    .hero-label {
      font-size: 16px;
      color: var(--soft);
      font-weight: 400;
      letter-spacing: -0.01em;
    }
    .hero-sub {
      margin-top: 6px;
      font-size: 13px;
      color: var(--muted);
    }

    /* ─── Stats row ─── */
    .stats-row {
      display: grid;
      grid-template-columns: repeat(4, 1fr);
      gap: 10px;
      margin-bottom: 32px;
    }
    .stat-card {
      padding: 18px 20px;
      border-radius: var(--r);
      border: 1px solid var(--border);
      background: var(--card);
      position: relative;
      overflow: hidden;
    }
    .stat-card::after {
      content: '';
      position: absolute;
      top: 0; left: 0; right: 0;
      height: 1px;
      background: var(--stat-line, transparent);
    }
    .stat-card.s-emerald { --stat-line: linear-gradient(90deg, transparent, var(--emerald), transparent); }
    .stat-card.s-cyan    { --stat-line: linear-gradient(90deg, transparent, var(--cyan), transparent); }
    .stat-card.s-violet  { --stat-line: linear-gradient(90deg, transparent, var(--violet), transparent); }
    .stat-card.s-amber   { --stat-line: linear-gradient(90deg, transparent, var(--amber), transparent); }
    .stat-label {
      font-size: 11px;
      font-weight: 500;
      text-transform: uppercase;
      letter-spacing: 0.08em;
      color: var(--muted);
      margin-bottom: 10px;
    }
    .stat-value {
      font-family: 'JetBrains Mono', monospace;
      font-size: 26px;
      font-weight: 600;
      letter-spacing: -0.04em;
      line-height: 1;
    }
    .stat-hint {
      margin-top: 8px;
      font-size: 12px;
      color: var(--muted);
    }
    .c-emerald { color: var(--emerald); }
    .c-cyan    { color: var(--cyan); }
    .c-violet  { color: var(--violet); }
    .c-amber   { color: var(--amber); }
    .c-rose    { color: var(--rose); }
    .c-soft    { color: var(--soft); }
    .c-muted   { color: var(--muted); }

    /* ─── Main grid ─── */
    .main-grid {
      display: grid;
      grid-template-columns: 1fr 380px;
      gap: 16px;
      align-items: start;
    }
    .left-col  { display: flex; flex-direction: column; gap: 16px; }
    .right-col { display: flex; flex-direction: column; gap: 16px; }

    /* ─── Section card ─── */
    .section {
      border-radius: var(--r);
      border: 1px solid var(--border);
      background: var(--card);
      overflow: hidden;
    }
    .section-head {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 12px;
      padding: 16px 20px;
      border-bottom: 1px solid var(--border);
    }
    .section-title {
      font-size: 13px;
      font-weight: 600;
      letter-spacing: -0.01em;
    }
    .section-sub {
      font-size: 11px;
      color: var(--muted);
      margin-top: 2px;
    }
    .section-body { padding: 16px 20px; }
    .count-badge {
      font-family: 'JetBrains Mono', monospace;
      font-size: 12px;
      color: var(--soft);
      padding: 3px 8px;
      border-radius: 6px;
      border: 1px solid var(--border2);
      background: rgba(255,255,255,0.03);
    }

    /* ─── Search ─── */
    .search-bar {
      width: 100%;
      padding: 9px 13px;
      border-radius: var(--r2);
      border: 1px solid var(--border2);
      background: rgba(255,255,255,0.03);
      color: var(--text);
      font-size: 13px;
      font-family: inherit;
      outline: none;
      margin-bottom: 14px;
      transition: border-color .15s, box-shadow .15s;
    }
    .search-bar::placeholder { color: var(--muted); }
    .search-bar:focus {
      border-color: rgba(56,189,248,0.35);
      box-shadow: 0 0 0 3px rgba(56,189,248,0.08);
    }

    /* ─── Project rows ─── */
    .proj-list { display: flex; flex-direction: column; gap: 8px; }
    .proj-row {
      padding: 14px 16px;
      border-radius: var(--r2);
      border: 1px solid var(--border);
      background: var(--card2);
      display: grid;
      grid-template-columns: 1fr auto;
      gap: 12px;
      align-items: start;
      transition: border-color .15s;
    }
    .proj-row:hover { border-color: var(--border2); }
    .proj-name {
      font-size: 14px;
      font-weight: 600;
      letter-spacing: -0.02em;
      margin-bottom: 3px;
    }
    .proj-path {
      font-family: 'JetBrains Mono', monospace;
      font-size: 11px;
      color: var(--muted);
    }
    .proj-nums {
      display: flex;
      gap: 16px;
      margin-top: 12px;
      align-items: center;
    }
    .pn-item { display: flex; flex-direction: column; gap: 2px; }
    .pn-label { font-size: 10px; text-transform: uppercase; letter-spacing: 0.07em; color: var(--muted); }
    .pn-value { font-family: 'JetBrains Mono', monospace; font-size: 14px; font-weight: 600; }
    .proj-bar-wrap {
      margin-top: 10px;
      height: 3px;
      border-radius: 999px;
      background: rgba(255,255,255,0.05);
      overflow: hidden;
    }
    .proj-bar {
      height: 100%;
      border-radius: inherit;
      background: linear-gradient(90deg, var(--cyan), var(--emerald));
      transition: width .5s ease;
    }
    .proj-right {
      display: flex;
      flex-direction: column;
      align-items: flex-end;
      gap: 8px;
    }
    .pct-badge {
      font-family: 'JetBrains Mono', monospace;
      font-size: 18px;
      font-weight: 600;
      letter-spacing: -0.03em;
    }
    .proj-chips { display: flex; gap: 5px; flex-wrap: wrap; justify-content: flex-end; }
    .chip {
      font-size: 10px;
      font-family: 'JetBrains Mono', monospace;
      padding: 2px 7px;
      border-radius: 5px;
      border: 1px solid var(--border2);
      color: var(--soft);
    }

    /* ─── Tool bars ─── */
    .tool-list { display: flex; flex-direction: column; gap: 7px; }
    .tool-row {
      display: grid;
      grid-template-columns: 1fr auto;
      align-items: center;
      gap: 10px;
    }
    .tool-name {
      font-family: 'JetBrains Mono', monospace;
      font-size: 12px;
      color: var(--soft);
      white-space: nowrap;
      overflow: hidden;
      text-overflow: ellipsis;
    }
    .tool-bar-wrap {
      grid-column: 1 / -1;
      height: 3px;
      border-radius: 999px;
      background: rgba(255,255,255,0.05);
      overflow: hidden;
      margin-top: -4px;
    }
    .tool-bar {
      height: 100%;
      border-radius: inherit;
      background: linear-gradient(90deg, var(--violet), var(--cyan));
    }
    .tool-count {
      font-family: 'JetBrains Mono', monospace;
      font-size: 12px;
      color: var(--muted);
      flex-shrink: 0;
    }

    /* ─── Sessions ─── */
    .sess-list { display: flex; flex-direction: column; gap: 6px; }
    .sess-row {
      padding: 10px 12px;
      border-radius: var(--r2);
      border: 1px solid var(--border);
      background: var(--card2);
    }
    .sess-head {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 8px;
      margin-bottom: 7px;
    }
    .sess-project { font-size: 13px; font-weight: 600; }
    .sess-badge {
      font-family: 'JetBrains Mono', monospace;
      font-size: 11px;
      font-weight: 600;
      padding: 2px 7px;
      border-radius: 5px;
      border: 1px solid currentColor;
      opacity: 0.85;
    }
    .sess-meta {
      display: grid;
      grid-template-columns: repeat(4, 1fr);
      gap: 6px;
    }
    .sm-label { font-size: 10px; text-transform: uppercase; letter-spacing: 0.06em; color: var(--muted); }
    .sm-value { font-family: 'JetBrains Mono', monospace; font-size: 12px; color: var(--soft); margin-top: 1px; }

    /* ─── Clients ─── */
    .client-list { display: flex; flex-direction: column; gap: 8px; }
    .client-row {
      display: flex;
      align-items: center;
      gap: 10px;
    }
    .client-name { font-family: 'JetBrains Mono', monospace; font-size: 13px; min-width: 90px; }
    .client-track { flex: 1; height: 4px; border-radius: 999px; background: rgba(255,255,255,0.05); overflow: hidden; }
    .client-fill  { height: 100%; border-radius: inherit; background: var(--cyan); }
    .client-n { font-family: 'JetBrains Mono', monospace; font-size: 12px; color: var(--muted); min-width: 28px; text-align: right; }

    /* ─── Empty ─── */
    .empty {
      padding: 24px;
      text-align: center;
      color: var(--muted);
      font-size: 12px;
      border: 1px dashed var(--border2);
      border-radius: var(--r2);
    }

    /* ─── Footer ─── */
    .footer {
      margin-top: 40px;
      text-align: center;
      font-size: 11px;
      color: var(--muted);
      letter-spacing: 0.02em;
    }

    /* ─── Responsive ─── */
    @media (max-width: 1100px) {
      .main-grid { grid-template-columns: 1fr; }
    }
    @media (max-width: 700px) {
      .wrap { padding: 20px 14px 40px; }
      .stats-row { grid-template-columns: repeat(2, 1fr); }
      .hero-number { font-size: 72px; }
      .proj-nums { flex-wrap: wrap; gap: 10px; }
    }
    @media (max-width: 420px) {
      .stats-row { grid-template-columns: 1fr; }
    }
  </style>
</head>
<body>
<div class="wrap">

  <!-- Topbar -->
  <div class="topbar">
    <div class="brand">
      <div class="brand-icon">⚡</div>
      <div>
        <div class="brand-name">Token Savior</div>
        <div class="brand-tagline" id="statsPath"></div>
      </div>
    </div>
    <div class="status-pill">
      <div class="dot"></div>
      <span id="liveLabel">Live</span>
    </div>
  </div>

  <!-- Hero -->
  <div class="hero">
    <div class="hero-eyebrow">
      <span>workspace token efficiency</span>
    </div>
    <div class="hero-number" id="heroSavingsPct">—</div>
    <div class="hero-label">of tokens saved across the workspace</div>
    <div class="hero-sub" id="heroSub">Loading…</div>
  </div>

  <!-- Stats row -->
  <div class="stats-row">
    <div class="stat-card s-emerald">
      <div class="stat-label">Tokens saved</div>
      <div class="stat-value c-emerald" id="sTokSaved">—</div>
      <div class="stat-hint" id="sCharsSaved">— chars</div>
    </div>
    <div class="stat-card s-cyan">
      <div class="stat-label">Tokens used</div>
      <div class="stat-value c-cyan" id="sTokUsed">—</div>
      <div class="stat-hint" id="sTokNaive">Naive —</div>
    </div>
    <div class="stat-card s-violet">
      <div class="stat-label">Total queries</div>
      <div class="stat-value c-violet" id="sQueries">—</div>
      <div class="stat-hint" id="sSessions">— sessions</div>
    </div>
    <div class="stat-card s-amber">
      <div class="stat-label">$ Saved (est.)</div>
      <div class="stat-value c-amber" id="sSavingsUsd">—</div>
      <div class="stat-hint" id="sSavingsHint">@ $3/M input tokens</div>
    </div>
  </div>

  <!-- Main grid -->
  <div class="main-grid">

    <!-- Left: projects -->
    <div class="left-col">
      <div class="section">
        <div class="section-head">
          <div>
            <div class="section-title">Projects</div>
            <div class="section-sub">Ranked by tokens saved · only used projects appear</div>
          </div>
          <span class="count-badge" id="projCount">0</span>
        </div>
        <div class="section-body">
          <input class="search-bar" id="projSearch" type="search" placeholder="Filter by name, path, or client…">
          <div class="proj-list" id="projList"></div>
        </div>
      </div>
    </div>

    <!-- Right sidebar -->
    <div class="right-col">

      <!-- Clients -->
      <div class="section">
        <div class="section-head">
          <div>
            <div class="section-title">Clients</div>
            <div class="section-sub">Sessions per client</div>
          </div>
        </div>
        <div class="section-body">
          <div class="client-list" id="clientList"></div>
        </div>
      </div>

      <!-- Top tools -->
      <div class="section">
        <div class="section-head">
          <div>
            <div class="section-title">Top tools</div>
            <div class="section-sub">Most called across workspace</div>
          </div>
        </div>
        <div class="section-body">
          <div class="tool-list" id="toolList"></div>
        </div>
      </div>

      <!-- Recent sessions -->
      <div class="section">
        <div class="section-head">
          <div>
            <div class="section-title">Recent sessions</div>
            <div class="section-sub">Latest 25 snapshots</div>
          </div>
        </div>
        <div class="section-body">
          <div class="sess-list" id="sessList"></div>
        </div>
      </div>

    </div>
  </div>

  <div class="footer" id="footer"></div>

</div>
<script>
  const S = { data: null, ts: null };

  // ── Format helpers ──────────────────────────────────────
  function fmt(n) {
    n = Number(n || 0);
    if (n >= 1e9)  return (n / 1e9).toFixed(2).replace(/\\.?0+$/, '') + 'B';
    if (n >= 1e6)  return (n / 1e6).toFixed(2).replace(/\\.?0+$/, '') + 'M';
    if (n >= 1e3)  return (n / 1e3).toFixed(1).replace(/\\.?0+$/, '') + 'K';
    return String(n);
  }
  function fmtFull(n) {
    return new Intl.NumberFormat('en-US').format(Number(n || 0));
  }
  function fmtPct(v) {
    const n = Number(v || 0);
    return n.toFixed(n >= 10 ? 1 : 2) + '%';
  }
  function fmtDate(v) {
    if (!v) return '—';
    const d = new Date(v);
    if (isNaN(d)) return String(v).slice(0, 16).replace('T', ' ');
    return d.toLocaleString('en-GB', { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' });
  }
  function ago(iso) {
    if (!iso) return '';
    const s = Math.floor((Date.now() - new Date(iso)) / 1000);
    if (s < 10)  return 'just now';
    if (s < 60)  return s + 's ago';
    if (s < 3600) return Math.floor(s / 60) + 'm ago';
    if (s < 86400) return Math.floor(s / 3600) + 'h ago';
    return Math.floor(s / 86400) + 'd ago';
  }
  function esc(v) {
    return String(v || '')
      .replace(/&/g, '&amp;').replace(/</g, '&lt;')
      .replace(/>/g, '&gt;').replace(/"/g, '&quot;');
  }
  function pctColor(v) {
    const p = Number(v || 0);
    if (p >= 80) return 'var(--emerald)';
    if (p >= 55) return 'var(--amber)';
    return 'var(--rose)';
  }
  function pctClass(v) {
    const p = Number(v || 0);
    if (p >= 80) return 'c-emerald';
    if (p >= 55) return 'c-amber';
    return 'c-rose';
  }

  // ── Render ──────────────────────────────────────────────
  function set(id, v) { document.getElementById(id).textContent = v; }

  function renderHero(d) {
    const t = d.totals;
    set('heroSavingsPct', fmtPct(t.savings_pct));
    set('heroSub', fmtFull(t.tokens_saved) + ' tokens saved · ' + fmtFull(t.tokens_used) + ' used vs ' + fmtFull(t.tokens_naive) + ' naive');
    set('statsPath', d.stats_dir || '');
  }

  function renderStats(d) {
    const t = d.totals;
    set('sTokSaved',  fmt(t.tokens_saved));
    set('sCharsSaved', fmtFull(t.chars_saved) + ' chars');
    set('sTokUsed',   fmt(t.tokens_used));
    set('sTokNaive',  'Naive ' + fmt(t.tokens_naive));
    set('sQueries',   fmt(t.queries));
    set('sSessions',  fmtFull(d.total_sessions || 0) + ' sessions');
    const usd = (t.estimated_savings_usd || 0);
    set('sSavingsUsd', '$' + (usd >= 1 ? usd.toFixed(2) : usd.toFixed(3)));
    set('sSavingsHint', '@ $3/M input tokens · ' + (d.project_count || 0) + ' projects');
    set('projCount',  String(d.project_count || 0));
  }

  function renderProjects(d) {
    const q = (document.getElementById('projSearch').value || '').trim().toLowerCase();
    const rows = (d.projects || []).filter(r => {
      if (!q) return true;
      return [r.project, r.project_root, r.last_client,
              Object.keys(r.client_counts || {}).join(' ')].join(' ').toLowerCase().includes(q);
    });
    if (!rows.length) {
      document.getElementById('projList').innerHTML = '<div class="empty">No projects match this filter.</div>';
      return;
    }
    document.getElementById('projList').innerHTML = rows.map(r => {
      const pct = Math.max(0, Math.min(100, Number(r.savings_pct || 0)));
      const col = pctColor(r.savings_pct);
      const cls = pctClass(r.savings_pct);
      const chips = Object.entries(r.client_counts || {})
        .map(([c, n]) => `<span class="chip">${esc(c)} ${fmt(n)}</span>`).join('');
      return `
        <div class="proj-row">
          <div>
            <div class="proj-name">${esc(r.project)}</div>
            ${r.project_root ? `<div class="proj-path">${esc(r.project_root)}</div>` : ''}
            <div class="proj-nums">
              <div class="pn-item"><div class="pn-label">Saved</div><div class="pn-value c-emerald">${fmt(r.tokens_saved)}</div></div>
              <div class="pn-item"><div class="pn-label">Used</div><div class="pn-value c-cyan">${fmt(r.tokens_used)}</div></div>
              <div class="pn-item"><div class="pn-label">Queries</div><div class="pn-value">${fmt(r.queries)}</div></div>
              <div class="pn-item"><div class="pn-label">Sessions</div><div class="pn-value">${fmt(r.sessions)}</div></div>
              <div class="pn-item"><div class="pn-label">Last seen</div><div class="pn-value c-muted" style="font-size:12px">${fmtDate(r.last_session)}</div></div>
            </div>
            <div class="proj-bar-wrap"><div class="proj-bar" style="width:${pct}%"></div></div>
          </div>
          <div class="proj-right">
            <div class="pct-badge ${cls}">${fmtPct(r.savings_pct)}</div>
            <div class="proj-chips">${chips}</div>
          </div>
        </div>`;
    }).join('');
  }

  function renderClients(d) {
    const rows = d.clients || [];
    if (!rows.length) {
      document.getElementById('clientList').innerHTML = '<div class="empty">No data yet.</div>';
      return;
    }
    const max = Math.max(...rows.map(r => r.sessions));
    document.getElementById('clientList').innerHTML = rows.map(r => {
      const w = max > 0 ? Math.round((r.sessions / max) * 100) : 0;
      return `
        <div class="client-row">
          <div class="client-name">${esc(r.client)}</div>
          <div class="client-track"><div class="client-fill" style="width:${w}%"></div></div>
          <div class="client-n">${fmtFull(r.sessions)}</div>
        </div>`;
    }).join('');
  }

  function renderTools(d) {
    const rows = d.top_tools || [];
    if (!rows.length) {
      document.getElementById('toolList').innerHTML = '<div class="empty">No data yet.</div>';
      return;
    }
    const max = rows[0].count;
    document.getElementById('toolList').innerHTML = rows.map(r => {
      const w = max > 0 ? Math.round((r.count / max) * 100) : 0;
      return `
        <div class="tool-row">
          <div class="tool-name">${esc(r.tool)}</div>
          <div class="tool-count">${fmt(r.count)}</div>
          <div class="tool-bar-wrap"><div class="tool-bar" style="width:${w}%"></div></div>
        </div>`;
    }).join('');
  }

  function renderSessions(d) {
    const rows = d.recent_sessions || [];
    if (!rows.length) {
      document.getElementById('sessList').innerHTML = '<div class="empty">No sessions yet.</div>';
      return;
    }
    document.getElementById('sessList').innerHTML = rows.map(r => {
      const cls = pctClass(r.savings_pct);
      const col = pctColor(r.savings_pct);
      return `
        <div class="sess-row">
          <div class="sess-head">
            <div class="sess-project">${esc(r.project)}</div>
            <span class="sess-badge ${cls}">${fmtPct(r.savings_pct)}</span>
          </div>
          <div class="sess-meta">
            <div><div class="sm-label">Client</div><div class="sm-value">${esc(r.client_name || '—')}</div></div>
            <div><div class="sm-label">Used</div><div class="sm-value">${fmt(r.tokens_used)}</div></div>
            <div><div class="sm-label">Naive</div><div class="sm-value">${fmt(r.tokens_naive)}</div></div>
            <div><div class="sm-label">When</div><div class="sm-value">${fmtDate(r.timestamp)}</div></div>
          </div>
        </div>`;
    }).join('');
  }

  function renderFooter(d) {
    set('footer', 'Updated ' + ago(S.ts) + ' · ' + (d.stats_dir || ''));
  }

  function render(d) {
    S.data = d;
    S.ts   = new Date().toISOString();
    renderHero(d);
    renderStats(d);
    renderProjects(d);
    renderClients(d);
    renderTools(d);
    renderSessions(d);
    renderFooter(d);
    document.getElementById('liveLabel').textContent = 'Live · ' + ago(S.ts);
  }

  // ── Fetch ───────────────────────────────────────────────
  async function refresh() {
    const r = await fetch('./api/status', { cache: 'no-store' });
    if (!r.ok) throw new Error('HTTP ' + r.status);
    render(await r.json());
  }

  async function tick() {
    try { await refresh(); } catch (e) {
      document.getElementById('liveLabel').textContent = 'Error: ' + (e.message || e);
    }
    if (S.data) renderFooter(S.data);
  }

  document.getElementById('projSearch').addEventListener('input', () => {
    if (S.data) renderProjects(S.data);
  });

  // Update "X ago" label every 15s without re-fetching
  setInterval(() => {
    document.getElementById('liveLabel').textContent = 'Live · ' + ago(S.ts);
    if (S.data) renderFooter(S.data);
  }, 15000);

  tick();
  setInterval(tick, 5000);
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
