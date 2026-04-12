from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import urlparse


DEFAULT_STATS_DIR = Path(
    os.environ.get("TOKEN_SAVIOR_STATS_DIR", "~/.local/share/token-savior")
).expanduser()
HOST = os.environ.get("TOKEN_SAVIOR_DASHBOARD_HOST", "127.0.0.1")
PORT = int(os.environ.get("TOKEN_SAVIOR_DASHBOARD_PORT", "8921"))
INCLUDE_TMP_PROJECTS = os.environ.get("TOKEN_SAVIOR_INCLUDE_TMP_PROJECTS", "").lower() in {
    "1",
    "true",
    "yes",
}
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
    raw = payload.get("project")
    if isinstance(raw, str) and raw.strip():
        base = raw.rstrip("/").split("/")[-1]
        return base or path.stem
    return path.stem


def _display_project_root(value: str) -> str:
    if not isinstance(value, str):
        return ""
    return value


def _safe_int(payload: dict, key: str) -> int:
    try:
        return int(payload.get(key) or 0)
    except Exception:
        return 0


def _recent_sessions(payload: dict, project_name: str) -> list[dict]:
    entries = payload.get("recent_sessions") or payload.get("history") or []
    out = []
    for entry in entries:
        if isinstance(entry, dict):
            item = dict(entry)
            item["project"] = project_name
            out.append(item)
    return out


def _client_name(value) -> str:
    return str(value).strip() if value else "unknown"


def _project_client_counts(payload: dict) -> dict[str, int]:
    counts: dict[str, int] = {}
    raw = payload.get("client_counts") or payload.get("clients")
    if isinstance(raw, dict):
        for name, value in raw.items():
            try:
                counts[_client_name(name)] = int(value or 0)
            except Exception:
                continue
    elif isinstance(raw, list):
        for entry in raw:
            if isinstance(entry, dict):
                name = _client_name(entry.get("client"))
                try:
                    counts[name] = int(entry.get("sessions") or 0)
                except Exception:
                    continue
    return counts


def _should_include_project(payload: dict, path: Path) -> bool:
    if INCLUDE_TMP_PROJECTS:
        return True
    root = payload.get("project") or ""
    if isinstance(root, str) and root.startswith("/tmp/"):
        return False
    return True


VALID_OBS_TYPES = (
    "bugfix",
    "decision",
    "convention",
    "warning",
    "guardrail",
    "error_pattern",
    "note",
    "command",
    "research",
    "infra",
    "config",
    "idea",
)


def collect_memory_engine_data() -> dict:
    """Memory Engine stats across projects, isolated from token stats.

    Filters observations to VALID_OBS_TYPES only — legacy `project`/`reference`/
    `user`/`feedback` entries from auto-memory are excluded from the dashboard
    so the type breakdown reflects coding-session signal only.
    """
    try:
        from token_savior import memory_db
    except Exception:
        return {"available": False}

    type_placeholders = ",".join("?" for _ in VALID_OBS_TYPES)

    try:
        conn = memory_db.get_db()
        try:
            totals_row = conn.execute(
                "SELECT COUNT(*) AS total, "
                "COUNT(DISTINCT project_root) AS projects, "
                "COUNT(DISTINCT session_id) AS sessions, "
                "SUM(CASE WHEN symbol IS NOT NULL THEN 1 ELSE 0 END) AS with_symbol "
                f"FROM observations WHERE archived=0 AND type IN ({type_placeholders})",
                VALID_OBS_TYPES,
            ).fetchone()
            archived_count = conn.execute(
                f"SELECT COUNT(*) FROM observations "
                f"WHERE archived=1 AND type IN ({type_placeholders})",
                VALID_OBS_TYPES,
            ).fetchone()[0]
            by_type = [
                {"type": r["type"], "count": r["count"]}
                for r in conn.execute(
                    "SELECT type, COUNT(*) AS count FROM observations "
                    f"WHERE archived=0 AND type IN ({type_placeholders}) "
                    "GROUP BY type ORDER BY count DESC",
                    VALID_OBS_TYPES,
                ).fetchall()
            ]
            prompts_count = conn.execute("SELECT COUNT(*) FROM user_prompts").fetchone()[0]
            summaries_count = conn.execute("SELECT COUNT(*) FROM summaries").fetchone()[0]
            sessions_total = conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0]
            sessions_completed = conn.execute(
                "SELECT COUNT(*) FROM sessions WHERE end_type='completed'"
            ).fetchone()[0]
            sessions_interrupted = conn.execute(
                "SELECT COUNT(*) FROM sessions WHERE end_type='interrupted'"
            ).fetchone()[0]

            recent_obs_rows = conn.execute(
                "SELECT id, type, title, content, symbol, file_path, importance, "
                "why, how_to_apply, tags, project_root, is_global, "
                "created_at, created_at_epoch "
                f"FROM observations WHERE archived=0 AND type IN ({type_placeholders}) "
                "ORDER BY created_at_epoch DESC LIMIT 10",
                VALID_OBS_TYPES,
            ).fetchall()
            recent_obs = []
            for r in recent_obs_rows:
                d = dict(r)
                d["age"] = memory_db.relative_age(d.get("created_at_epoch"))
                if d.get("symbol") and d.get("project_root"):
                    d["stale"] = memory_db.check_symbol_staleness(
                        d["project_root"], d["symbol"], d.get("created_at_epoch") or 0
                    )
                else:
                    d["stale"] = False
                recent_obs.append(d)
            global_count = conn.execute(
                "SELECT COUNT(*) FROM observations WHERE archived=0 AND is_global=1"
            ).fetchone()[0]

            top_sessions_rows = conn.execute(
                "SELECT s.id, s.project_root, s.status, s.end_type, s.summary, "
                "s.created_at, s.completed_at, s.created_at_epoch, s.completed_at_epoch, "
                "(s.completed_at_epoch - s.created_at_epoch) AS duration_sec, "
                "(SELECT COUNT(*) FROM observations o WHERE o.session_id=s.id AND o.archived=0) AS obs_count "
                "FROM sessions s "
                "ORDER BY s.id DESC LIMIT 5"
            ).fetchall()
            top_sessions = []
            for r in top_sessions_rows:
                row = dict(r)
                # duration_sec is NULL when session is still active → "en cours"
                if row.get("completed_at_epoch") is None:
                    row["duration_s"] = None
                else:
                    row["duration_s"] = max(0, row.get("duration_sec") or 0)
                top_sessions.append(row)

            search_rows = conn.execute(
                "SELECT id, type, title, symbol, created_at, project_root "
                f"FROM observations WHERE archived=0 AND type IN ({type_placeholders}) "
                "ORDER BY created_at_epoch DESC LIMIT 300",
                VALID_OBS_TYPES,
            ).fetchall()
            all_obs = [dict(r) for r in search_rows]
        finally:
            conn.close()
    except Exception:
        return {"available": False}

    try:
        mode = memory_db.get_current_mode()
    except Exception:
        mode = None

    # Continuity score for the most populated project
    try:
        conn2 = memory_db.get_db()
        top_proj = conn2.execute(
            "SELECT project_root FROM observations WHERE archived=0 "
            "GROUP BY project_root ORDER BY COUNT(*) DESC LIMIT 1"
        ).fetchone()
        conn2.close()
        continuity = memory_db.compute_continuity_score(top_proj[0]) if top_proj else None
    except Exception:
        continuity = None

    return {
        "available": True,
        "mode": mode,
        "continuity": continuity,
        "global_count": global_count,
        "totals": {
            "observations": totals_row["total"] or 0,
            "archived": archived_count,
            "projects": totals_row["projects"] or 0,
            "sessions_with_obs": totals_row["sessions"] or 0,
            "with_symbol": totals_row["with_symbol"] or 0,
            "sessions": sessions_total,
            "sessions_completed": sessions_completed,
            "sessions_interrupted": sessions_interrupted,
            "summaries": summaries_count,
            "prompts": prompts_count,
        },
        "by_type": by_type,
        "recent": recent_obs,
        "top_sessions": top_sessions,
        "all_obs": all_obs,
    }


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
        if not project_client_counts and _safe_int(payload, "sessions") > 0:
            project_client_counts = {"unknown": _safe_int(payload, "sessions")}
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
            "last_client": _client_name(
                payload.get("last_client") or next(iter(project_client_counts), "")
            ),
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
            "savings_pct": round((1 - total_chars_used / total_chars_naive) * 100, 2)
            if total_chars_naive > 0
            else 0.0,
            "estimated_savings_usd": round(
                max(total_chars_naive - total_chars_used, 0) / 4 * 3.0 / 1_000_000, 2
            ),
        },
    }
    for client_name, session_count in client_totals.items():
        result[client_name] = {"active": True, "sessions": session_count}
    result["memory_engine"] = collect_memory_engine_data()
    return result


# ---------------------------------------------------------------------------
# HTML template — data injected as window.__DATA__ JSON at render time.
# ---------------------------------------------------------------------------
HTML_TEMPLATE = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=1200">
<title>Token Savior — Dashboard</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&family=JetBrains+Mono:wght@400;500;600;700&display=swap" rel="stylesheet">
<style>
:root {
  --bg: #0a0a0f;
  --surface: #111118;
  --surface-hi: #17171f;
  --border: #1e1e2e;
  --border-hi: #2a2a3e;
  --text: #e2e8f0;
  --muted: #64748b;
  --cyan: #00d4ff;
  --orange: #ff6b35;
  --violet: #7c3aed;
  --success: #10b981;
  --warning: #f59e0b;
  --danger: #ef4444;
  --type-guardrail: #ef4444;
  --type-convention: #7c3aed;
  --type-warning: #f59e0b;
  --type-bugfix: #10b981;
  --type-decision: #00d4ff;
  --type-error_pattern: #ff6b35;
  --type-note: #64748b;
  --type-project: #3b82f6;
  --type-reference: #8b8b9e;
  --type-user: #ec4899;
  --type-command: #06b6d4;
  --type-research: #8b5cf6;
  --type-infra: #84cc16;
  --type-config: #f97316;
  --type-idea: #ec4899;
  --type-feedback: #14b8a6;
  --shadow: 0 0 0 1px rgba(255,255,255,0.05), 0 4px 24px rgba(0,0,0,0.4);
}

* { box-sizing: border-box; margin: 0; padding: 0; }
html, body { background: var(--bg); color: var(--text); font-family: 'Inter', system-ui, sans-serif; font-size: 14px; line-height: 1.5; }
body { min-height: 100vh; }

.mono { font-family: 'JetBrains Mono', ui-monospace, monospace; }
.muted { color: var(--muted); }

/* ---------- Header ---------- */
.hdr {
  position: sticky; top: 0; z-index: 10;
  display: flex; align-items: center; justify-content: space-between;
  padding: 14px 32px;
  background: rgba(10,10,15,0.85);
  backdrop-filter: blur(12px);
  border-bottom: 1px solid var(--border);
}
.brand { font-family: 'JetBrains Mono', monospace; font-weight: 700; font-size: 16px; letter-spacing: -0.02em; }
.brand .bolt { color: var(--cyan); }
.tabs { display: flex; gap: 4px; background: var(--surface); border: 1px solid var(--border); border-radius: 8px; padding: 4px; }
.tab-btn {
  appearance: none; border: 0; background: transparent; color: var(--muted);
  padding: 8px 18px; border-radius: 6px; font: 500 13px 'Inter', sans-serif;
  cursor: pointer; transition: all 0.15s ease;
  font-family: 'JetBrains Mono', monospace;
}
.tab-btn:hover { color: var(--text); }
.tab-btn.active { background: var(--bg); color: var(--text); box-shadow: inset 0 0 0 1px var(--border-hi); }
.tab-btn[data-tab="memory"].active { color: var(--orange); }
.tab-btn[data-tab="tokens"].active { color: var(--cyan); }
.live-badge {
  display: inline-flex; align-items: center; gap: 8px;
  color: var(--muted); font-family: 'JetBrains Mono', monospace; font-size: 12px;
}
.dot { width: 8px; height: 8px; border-radius: 50%; background: var(--success); box-shadow: 0 0 8px var(--success); }

/* ---------- Layout ---------- */
.wrap { max-width: 1400px; margin: 0 auto; padding: 32px; }
.tab-panel { display: none; }
.tab-panel.active { display: block; animation: fadeIn 0.2s ease; }
@keyframes fadeIn { from { opacity: 0; transform: translateY(4px);} to { opacity: 1; transform: none; } }

/* ---------- Hero ---------- */
.hero { text-align: center; padding: 48px 0 40px; }
.hero-pct { font-family: 'JetBrains Mono', monospace; font-weight: 700; font-size: 96px; line-height: 1; color: var(--cyan); letter-spacing: -0.04em; }
.hero-sub { margin-top: 12px; color: var(--muted); font-family: 'JetBrains Mono', monospace; font-size: 14px; }

/* ---------- KPI cards ---------- */
.kpi-row { display: grid; grid-template-columns: repeat(4, 1fr); gap: 16px; margin-bottom: 24px; }
.kpi {
  background: var(--surface); border: 1px solid var(--border); border-radius: 8px;
  padding: 20px; box-shadow: var(--shadow);
  transition: border-color 0.15s;
}
.kpi:hover { border-color: var(--border-hi); }
.kpi-label { font-size: 11px; font-weight: 600; text-transform: uppercase; letter-spacing: 0.08em; color: var(--muted); margin-bottom: 10px; }
.kpi-value { font-family: 'JetBrains Mono', monospace; font-weight: 700; font-size: 28px; color: var(--text); letter-spacing: -0.02em; }
.kpi-hint { margin-top: 6px; font-size: 12px; color: var(--muted); font-family: 'JetBrains Mono', monospace; }
.kpi.accent-orange .kpi-value { color: var(--orange); }
.kpi.accent-cyan .kpi-value { color: var(--cyan); }
.kpi.accent-violet .kpi-value { color: var(--violet); }

/* ---------- Cards ---------- */
.grid2 { display: grid; grid-template-columns: 1.2fr 0.8fr; gap: 16px; }
.card {
  background: var(--surface); border: 1px solid var(--border); border-radius: 8px;
  padding: 20px; box-shadow: var(--shadow);
}
.card + .card { margin-top: 16px; }
.card-hdr { display: flex; align-items: center; justify-content: space-between; margin-bottom: 14px; gap: 12px; }
.card-title { font-family: 'JetBrains Mono', monospace; font-weight: 600; font-size: 12px; text-transform: uppercase; letter-spacing: 0.1em; color: var(--muted); }
.card-count { font-family: 'JetBrains Mono', monospace; font-size: 11px; color: var(--muted); }

/* ---------- Input ---------- */
.input {
  appearance: none; width: 100%; background: var(--bg); border: 1px solid var(--border);
  color: var(--text); padding: 8px 12px; border-radius: 6px; font: 13px 'JetBrains Mono', monospace;
  transition: border-color 0.15s;
}
.input:focus { outline: none; border-color: var(--border-hi); }
.search { max-width: 260px; }

/* ---------- Project rows ---------- */
.proj-list { max-height: 640px; overflow-y: auto; }
.proj-list::-webkit-scrollbar { width: 8px; }
.proj-list::-webkit-scrollbar-thumb { background: var(--border-hi); border-radius: 4px; }
.proj-row {
  display: grid; grid-template-columns: 1fr auto auto; align-items: center; gap: 16px;
  padding: 12px 0; border-bottom: 1px solid var(--border);
}
.proj-row:last-child { border-bottom: 0; }
.proj-name { font-family: 'JetBrains Mono', monospace; font-weight: 600; font-size: 13px; }
.proj-path { font-family: 'JetBrains Mono', monospace; font-size: 11px; color: var(--muted); margin-top: 2px; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; max-width: 360px; }
.proj-stats { font-family: 'JetBrains Mono', monospace; font-size: 11px; color: var(--muted); text-align: right; white-space: nowrap; }
.proj-stats .key { color: var(--muted); }
.proj-stats .val { color: var(--text); }
.proj-savings { font-family: 'JetBrains Mono', monospace; font-weight: 700; font-size: 16px; color: var(--cyan); min-width: 72px; text-align: right; }
.proj-bar { grid-column: 1 / -1; height: 3px; background: var(--border); border-radius: 2px; overflow: hidden; margin-top: 6px; }
.proj-bar-fill { height: 100%; background: linear-gradient(90deg, var(--cyan), var(--violet)); }
.proj-clients { display: flex; gap: 4px; flex-wrap: wrap; margin-top: 4px; }
.client-pill { font-family: 'JetBrains Mono', monospace; font-size: 10px; color: var(--muted); background: var(--bg); border: 1px solid var(--border); padding: 2px 8px; border-radius: 4px; }

/* ---------- Horizontal bars ---------- */
.hbar-list { display: flex; flex-direction: column; gap: 10px; }
.hbar { position: relative; }
.hbar-lbl { display: flex; justify-content: space-between; font-family: 'JetBrains Mono', monospace; font-size: 12px; margin-bottom: 4px; }
.hbar-lbl .name { color: var(--text); }
.hbar-lbl .count { color: var(--muted); }
.hbar-track { height: 6px; background: var(--bg); border-radius: 3px; overflow: hidden; border: 1px solid var(--border); }
.hbar-fill { height: 100%; background: var(--cyan); border-radius: 3px; transition: width 0.3s ease; }

/* ---------- Memory status bar ---------- */
.mem-status {
  display: flex; gap: 24px; align-items: center; flex-wrap: wrap;
  background: var(--surface); border: 1px solid var(--border); border-radius: 8px;
  padding: 14px 20px; margin-bottom: 24px;
  font-family: 'JetBrains Mono', monospace; font-size: 12px;
}
.mem-status .chip {
  display: inline-flex; align-items: center; gap: 8px;
  color: var(--muted);
}
.mem-status .chip .val { color: var(--text); font-weight: 600; }
.mode-badge {
  display: inline-flex; align-items: center; gap: 6px;
  background: rgba(255,107,53,0.1); color: var(--orange);
  padding: 4px 10px; border-radius: 4px; border: 1px solid rgba(255,107,53,0.3);
  font-weight: 600; text-transform: uppercase; letter-spacing: 0.05em; font-size: 11px;
}

/* ---------- Type badges ---------- */
.type-badge {
  display: inline-flex; align-items: center;
  font-family: 'JetBrains Mono', monospace; font-size: 10px; font-weight: 600;
  text-transform: uppercase; letter-spacing: 0.05em;
  padding: 3px 8px; border-radius: 4px;
  background: rgba(255,255,255,0.04); border: 1px solid var(--border);
}
.type-badge[data-type="guardrail"]     { color: var(--type-guardrail);     border-color: rgba(239,68,68,0.3);  background: rgba(239,68,68,0.08); }
.type-badge[data-type="convention"]    { color: var(--type-convention);    border-color: rgba(124,58,237,0.3); background: rgba(124,58,237,0.08); }
.type-badge[data-type="warning"]       { color: var(--type-warning);       border-color: rgba(245,158,11,0.3); background: rgba(245,158,11,0.08); }
.type-badge[data-type="bugfix"]        { color: var(--type-bugfix);        border-color: rgba(16,185,129,0.3); background: rgba(16,185,129,0.08); }
.type-badge[data-type="decision"]      { color: var(--type-decision);      border-color: rgba(0,212,255,0.3);  background: rgba(0,212,255,0.08); }
.type-badge[data-type="error_pattern"] { color: var(--type-error_pattern); border-color: rgba(255,107,53,0.3); background: rgba(255,107,53,0.08); }
.type-badge[data-type="note"]          { color: var(--type-note);          border-color: rgba(100,116,139,0.3);background: rgba(100,116,139,0.08); }
.type-badge[data-type="project"]       { color: var(--type-project);       border-color: rgba(59,130,246,0.3); background: rgba(59,130,246,0.08); }

/* ---------- Type breakdown bars ---------- */
.type-bars { display: flex; flex-direction: column; gap: 8px; }
.type-row { display: grid; grid-template-columns: 120px 1fr 48px; align-items: center; gap: 12px; }
.type-row .t-track { height: 8px; background: var(--bg); border: 1px solid var(--border); border-radius: 4px; overflow: hidden; }
.type-row .t-fill { height: 100%; border-radius: 4px; }
.type-row .t-count { font-family: 'JetBrains Mono', monospace; font-size: 12px; color: var(--muted); text-align: right; }

/* ---------- Obs list ---------- */
.obs-list { display: flex; flex-direction: column; }
.obs-row {
  display: grid; grid-template-columns: 40px auto 1fr auto auto; align-items: center; gap: 10px;
  padding: 10px 0; border-bottom: 1px solid var(--border); cursor: pointer;
  transition: background 0.1s;
}
.obs-row:hover { background: rgba(255,255,255,0.02); }
.obs-row:last-child { border-bottom: 0; }
.obs-id { font-family: 'JetBrains Mono', monospace; font-size: 11px; color: var(--muted); }
.obs-title { font-size: 13px; color: var(--text); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.obs-sym { font-family: 'JetBrains Mono', monospace; font-size: 11px; color: var(--violet); }
.obs-stale { margin-left: 4px; font-size: 11px; color: var(--orange); }
.obs-global { margin-right: 4px; font-size: 11px; }
.obs-date { font-family: 'JetBrains Mono', monospace; font-size: 11px; color: var(--muted); }
.obs-content {
  grid-column: 1 / -1; font-family: 'JetBrains Mono', monospace; font-size: 12px;
  color: var(--muted); background: var(--bg); border: 1px solid var(--border);
  border-radius: 4px; padding: 12px; margin-top: 8px; white-space: pre-wrap; line-height: 1.55;
  max-height: 320px; overflow-y: auto;
}
.hidden { display: none !important; }

/* ---------- Session list ---------- */
.sess-row {
  padding: 12px 0; border-bottom: 1px solid var(--border); cursor: pointer;
}
.sess-row:last-child { border-bottom: 0; }
.sess-row:hover { background: rgba(255,255,255,0.02); }
.sess-head { display: flex; align-items: center; gap: 10px; flex-wrap: wrap; }
.sess-id { font-family: 'JetBrains Mono', monospace; font-size: 12px; color: var(--muted); }
.sess-badge {
  font-family: 'JetBrains Mono', monospace; font-size: 10px; font-weight: 600;
  padding: 3px 8px; border-radius: 4px;
  text-transform: uppercase; letter-spacing: 0.05em;
}
.sess-badge[data-end="completed"]   { color: var(--success); background: rgba(16,185,129,0.1); border: 1px solid rgba(16,185,129,0.3); }
.sess-badge[data-end="interrupted"] { color: var(--warning); background: rgba(245,158,11,0.1); border: 1px solid rgba(245,158,11,0.3); }
.sess-badge[data-end="active"]      { color: var(--cyan); background: rgba(0,212,255,0.1); border: 1px solid rgba(0,212,255,0.3); }
.sess-meta { font-family: 'JetBrains Mono', monospace; font-size: 11px; color: var(--muted); margin-left: auto; }
.sess-project { font-family: 'JetBrains Mono', monospace; font-size: 11px; color: var(--muted); margin-top: 4px; }
.sess-summary {
  margin-top: 10px; padding: 10px 12px; background: var(--bg); border: 1px solid var(--border);
  border-radius: 4px; font-family: 'JetBrains Mono', monospace; font-size: 12px;
  color: var(--muted); white-space: pre-wrap; max-height: 260px; overflow-y: auto; line-height: 1.55;
}

.empty { color: var(--muted); font-family: 'JetBrains Mono', monospace; font-size: 12px; padding: 14px; text-align: center; }

footer { text-align: center; padding: 32px; color: var(--muted); font-family: 'JetBrains Mono', monospace; font-size: 11px; }
</style>
</head>
<body>
<header class="hdr">
  <div class="brand"><span class="bolt">⚡</span> Token Savior</div>
  <div class="tabs" role="tablist">
    <button class="tab-btn active" data-tab-btn="tokens">Tokens</button>
    <button class="tab-btn" data-tab-btn="memory">Memory</button>
  </div>
  <div class="live-badge"><span class="dot"></span><span id="liveStamp">● Live</span></div>
</header>

<div class="wrap">
  <section class="tab-panel active" data-tab="tokens">
    <div class="hero">
      <div class="hero-pct" id="heroPct">—</div>
      <div class="hero-sub" id="heroSub">loading…</div>
    </div>

    <div class="kpi-row">
      <div class="kpi accent-cyan"><div class="kpi-label">Tokens Saved</div><div class="kpi-value" id="kpiSaved">—</div><div class="kpi-hint" id="kpiSavedHint"></div></div>
      <div class="kpi"><div class="kpi-label">Tokens Used</div><div class="kpi-value" id="kpiUsed">—</div><div class="kpi-hint" id="kpiUsedHint"></div></div>
      <div class="kpi"><div class="kpi-label">Total Queries</div><div class="kpi-value" id="kpiCalls">—</div><div class="kpi-hint" id="kpiCallsHint"></div></div>
      <div class="kpi accent-orange"><div class="kpi-label">$ Saved (est.)</div><div class="kpi-value" id="kpiUsd">—</div><div class="kpi-hint">vs naive reads</div></div>
    </div>

    <div class="grid2">
      <div class="card">
        <div class="card-hdr">
          <div class="card-title">Projects</div>
          <input class="input search" id="projFilter" type="search" placeholder="filter projects…">
        </div>
        <div class="proj-list" id="projList"></div>
      </div>
      <div>
        <div class="card">
          <div class="card-hdr"><div class="card-title">Clients</div><div class="card-count" id="clientCount"></div></div>
          <div class="hbar-list" id="clientList"></div>
        </div>
        <div class="card">
          <div class="card-hdr"><div class="card-title">Top Tools</div><div class="card-count" id="toolCount"></div></div>
          <div class="hbar-list" id="toolList"></div>
        </div>
      </div>
    </div>
  </section>

  <section class="tab-panel" data-tab="memory">
    <div class="mem-status" id="memStatus"><span class="muted">loading memory…</span></div>

    <div class="kpi-row">
      <div class="kpi accent-orange"><div class="kpi-label">Observations</div><div class="kpi-value" id="memObs">—</div><div class="kpi-hint" id="memObsHint"></div></div>
      <div class="kpi"><div class="kpi-label">Sessions Completed</div><div class="kpi-value" id="memSessCompl">—</div><div class="kpi-hint" id="memSessHint"></div></div>
      <div class="kpi accent-violet"><div class="kpi-label">Summaries</div><div class="kpi-value" id="memSum">—</div><div class="kpi-hint">auto-generated</div></div>
      <div class="kpi"><div class="kpi-label">Prompts Archived</div><div class="kpi-value" id="memPrompts">—</div><div class="kpi-hint">user prompts</div></div>
    </div>

    <div class="card">
      <div class="card-hdr"><div class="card-title">Type Breakdown</div><div class="card-count" id="typeCount"></div></div>
      <div class="type-bars" id="typeBars"></div>
    </div>

    <div style="height:16px"></div>

    <div class="grid2">
      <div class="card">
        <div class="card-hdr">
          <div class="card-title">Recent Observations</div>
          <input class="input search" id="obsSearch" type="search" placeholder="search observations…">
        </div>
        <div class="obs-list" id="obsList"></div>
      </div>
      <div class="card">
        <div class="card-hdr"><div class="card-title">Recent Sessions</div><div class="card-count" id="sessCount"></div></div>
        <div id="sessList"></div>
      </div>
    </div>
  </section>
</div>

<footer>Token Savior · generated <span id="genAt"></span></footer>

<script>
window.__DATA__ = __DATA_JSON__;

// ---------- Helpers ----------
const $ = (id) => document.getElementById(id);
const fmt = (n) => (n == null ? "—" : Number(n).toLocaleString("en-US"));
const basename = (p) => (p || "").replace(/\/+$/, "").split("/").pop() || p || "—";
const esc = (s) => String(s == null ? "" : s).replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const shortDate = (iso) => (iso || "").slice(0, 10);
const fmtDur = (s) => {
  if (s == null) return "en cours";
  s = Number(s);
  if (s < 60) return s + "s";
  if (s < 3600) return Math.floor(s/60) + "m " + (s%60) + "s";
  const h = Math.floor(s/3600), m = Math.floor((s%3600)/60);
  return h + "h " + m + "m";
};

// ---------- Tab switching ----------
document.querySelectorAll("[data-tab-btn]").forEach(btn => {
  btn.addEventListener("click", () => {
    const target = btn.dataset.tabBtn;
    document.querySelectorAll("[data-tab-btn]").forEach(b => b.classList.toggle("active", b === btn));
    document.querySelectorAll(".tab-panel").forEach(p => p.classList.toggle("active", p.dataset.tab === target));
  });
});

// ---------- Renderers ----------
function renderTokens(d) {
  const t = d.totals || {};
  $("heroPct").textContent = (t.savings_pct || 0).toFixed(1) + "%";
  $("heroSub").textContent = `${fmt(t.tokens_saved)} saved · ${fmt(t.tokens_used)} used vs ${fmt(t.tokens_naive)} naive`;

  $("kpiSaved").textContent = fmt(t.tokens_saved);
  $("kpiSavedHint").textContent = (t.savings_pct || 0).toFixed(1) + "% of naive";
  $("kpiUsed").textContent = fmt(t.tokens_used);
  $("kpiUsedHint").textContent = fmt(t.chars_used) + " chars";
  $("kpiCalls").textContent = fmt(t.queries);
  $("kpiCallsHint").textContent = (d.project_count || 0) + " projects";
  $("kpiUsd").textContent = "$" + (t.estimated_savings_usd || 0).toFixed(2);

  // Projects list
  const list = $("projList");
  const projects = (d.projects || []).filter(p => p.queries > 0 || p.tokens_saved > 0);
  if (!projects.length) {
    list.innerHTML = '<div class="empty">no project activity yet</div>';
  } else {
    list.innerHTML = projects.map(p => {
      const clients = Object.entries(p.client_counts || {})
        .sort((a,b) => b[1]-a[1])
        .map(([c,n]) => `<span class="client-pill">${esc(c)} · ${n}</span>`).join("");
      const pct = Math.min(100, p.savings_pct || 0);
      return `
        <div class="proj-row" data-project="${esc(p.project.toLowerCase())} ${esc((p.project_root||'').toLowerCase())}">
          <div>
            <div class="proj-name">${esc(p.project)}</div>
            <div class="proj-path">${esc(p.project_root || '')}</div>
            <div class="proj-clients">${clients}</div>
          </div>
          <div class="proj-stats">
            <span class="key">saved</span> <span class="val">${fmt(p.tokens_saved)}</span> ·
            <span class="key">used</span> <span class="val">${fmt(p.tokens_used)}</span><br>
            <span class="key">queries</span> <span class="val">${fmt(p.queries)}</span> ·
            <span class="key">sessions</span> <span class="val">${fmt(p.sessions)}</span>
          </div>
          <div class="proj-savings">${(p.savings_pct||0).toFixed(1)}%</div>
          <div class="proj-bar"><div class="proj-bar-fill" style="width:${pct}%"></div></div>
        </div>`;
    }).join("");
  }

  // Clients
  const clients = d.clients || [];
  $("clientCount").textContent = clients.length + " client" + (clients.length === 1 ? "" : "s");
  const maxC = Math.max(1, ...clients.map(c => c.sessions));
  $("clientList").innerHTML = clients.length
    ? clients.map(c => `
      <div class="hbar">
        <div class="hbar-lbl"><span class="name">${esc(c.client)}</span><span class="count">${fmt(c.sessions)}</span></div>
        <div class="hbar-track"><div class="hbar-fill" style="width:${(c.sessions/maxC)*100}%; background: var(--cyan);"></div></div>
      </div>`).join("")
    : '<div class="empty">no clients</div>';

  // Top tools
  const tools = d.top_tools || [];
  $("toolCount").textContent = tools.length + " tools";
  const maxT = Math.max(1, ...tools.map(t => t.count));
  $("toolList").innerHTML = tools.length
    ? tools.map(t => `
      <div class="hbar">
        <div class="hbar-lbl"><span class="name">${esc(t.tool)}</span><span class="count">${fmt(t.count)}</span></div>
        <div class="hbar-track"><div class="hbar-fill" style="width:${(t.count/maxT)*100}%; background: var(--violet);"></div></div>
      </div>`).join("")
    : '<div class="empty">no tool usage</div>';
}

function renderMemory(d) {
  const m = d.memory_engine || {available: false};
  if (!m.available) {
    $("memStatus").innerHTML = '<span class="empty">memory engine unavailable</span>';
    return;
  }
  const t = m.totals || {};
  const mode = (m.mode && m.mode.name) || 'code';
  const origin = (m.mode && m.mode.origin) || 'global';

  const cs = m.continuity || null;
  const contChip = cs && cs.total > 0
    ? `<span class="chip" title="${cs.valid}/${cs.total} valid · ${cs.recent} recent · ${cs.potentially_stale} potentially stale">continuity <span class="val">${cs.score}%</span> ${esc(cs.label)}</span>`
    : '';
  const globChip = m.global_count
    ? `<span class="chip">🌐 global <span class="val">${fmt(m.global_count)}</span></span>`
    : '';

  $("memStatus").innerHTML = `
    <span class="mode-badge">MODE: ${esc(mode)} · ${esc(origin)}</span>
    <span class="chip">obs <span class="val">${fmt(t.observations)}</span></span>
    ${globChip}
    ${contChip}
    <span class="chip">archived <span class="val">${fmt(t.archived)}</span></span>
    <span class="chip">sessions <span class="val">${fmt(t.sessions)}</span></span>
    <span class="chip">summaries <span class="val">${fmt(t.summaries)}</span></span>
    <span class="chip">prompts <span class="val">${fmt(t.prompts)}</span></span>
    <span class="chip">projects <span class="val">${fmt(t.projects)}</span></span>
  `;

  $("memObs").textContent = fmt(t.observations);
  $("memObsHint").textContent = `${fmt(t.with_symbol)} symbol-linked · ${fmt(t.archived)} archived`;
  $("memSessCompl").textContent = fmt(t.sessions_completed);
  $("memSessHint").textContent = `${fmt(t.sessions_interrupted)} interrupted / ${fmt(t.sessions)} total`;
  $("memSum").textContent = fmt(t.summaries);
  $("memPrompts").textContent = fmt(t.prompts);

  // Type breakdown
  const byType = m.by_type || [];
  $("typeCount").textContent = byType.length + " types";
  const maxType = Math.max(1, ...byType.map(x => x.count));
  $("typeBars").innerHTML = byType.length
    ? byType.map(x => {
        const color = `var(--type-${x.type}, var(--type-note))`;
        return `
          <div class="type-row">
            <span class="type-badge" data-type="${esc(x.type)}">${esc(x.type)}</span>
            <div class="t-track"><div class="t-fill" style="width:${(x.count/maxType)*100}%; background:${color};"></div></div>
            <div class="t-count">${fmt(x.count)}</div>
          </div>`;
      }).join("")
    : '<div class="empty">no observations</div>';

  // Recent obs
  const recent = m.recent || [];
  renderObsList(recent);

  // Sessions
  const sess = m.top_sessions || [];
  $("sessCount").textContent = sess.length + " sessions";
  $("sessList").innerHTML = sess.length
    ? sess.map(s => {
        const end = s.end_type || (s.status === 'active' ? 'active' : '?');
        const day = shortDate(s.completed_at || s.created_at);
        const summary = s.summary
          ? `<div class="sess-summary">${esc(s.summary)}</div>`
          : '';
        return `
          <div class="sess-row" data-sess-id="${s.id}">
            <div class="sess-head">
              <span class="sess-id">#${s.id}</span>
              <span class="sess-badge" data-end="${esc(end)}">${esc(end)}</span>
              <span class="sess-meta">${day} · ${fmt(s.obs_count)} obs · ${fmtDur(s.duration_s)}</span>
            </div>
            <div class="sess-project">${esc(basename(s.project_root))}</div>
            ${summary}
          </div>`;
      }).join("")
    : '<div class="empty">no sessions</div>';
}

function renderObsList(list) {
  const container = $("obsList");
  if (!list.length) {
    container.innerHTML = '<div class="empty">no observations</div>';
    return;
  }
  container.innerHTML = list.map(o => {
    const staleBadge = o.stale ? '<span class="obs-stale" title="Symbol modified after this observation was saved">⚠️</span>' : '';
    const sym = o.symbol ? `<span class="obs-sym">⚙ ${esc(o.symbol)}</span>${staleBadge}` : '';
    const glob = o.is_global ? '<span class="obs-global" title="Global — applies to all projects">🌐</span>' : '';
    const age = o.age || shortDate(o.created_at);
    const dateAttr = o.created_at ? ` title="${esc(o.created_at)}"` : '';
    const haystack = (String(o.title||'') + ' ' + String(o.content||'') + ' ' + String(o.symbol||'')).toLowerCase();
    return `
      <div class="obs-row" data-search="${esc(haystack)}">
        <span class="obs-id">#${o.id}</span>
        <span class="type-badge" data-type="${esc(o.type)}">${esc(o.type)}</span>
        ${glob}
        <span class="obs-title">${esc(o.title)}</span>
        ${sym}
        <span class="obs-date"${dateAttr}>${esc(age)}</span>
      </div>
      <div class="obs-content hidden">${esc(o.content || '(empty)')}</div>`;
  }).join("");
  container.querySelectorAll('.obs-row').forEach(row => {
    row.addEventListener('click', () => {
      const body = row.nextElementSibling;
      if (body && body.classList.contains('obs-content')) body.classList.toggle('hidden');
    });
  });
}

// ---------- Search / filter ----------
$("projFilter").addEventListener("input", (e) => {
  const q = e.target.value.trim().toLowerCase();
  document.querySelectorAll('.proj-row').forEach(r => {
    r.classList.toggle('hidden', q && !r.dataset.project.includes(q));
  });
});

$("obsSearch").addEventListener("input", (e) => {
  const q = e.target.value.trim().toLowerCase();
  document.querySelectorAll('#obsList .obs-row').forEach(r => {
    const match = !q || r.dataset.search.includes(q);
    r.classList.toggle('hidden', !match);
    const body = r.nextElementSibling;
    if (body && !match) body.classList.add('hidden');
  });
});

// ---------- Boot ----------
function render(d) {
  renderTokens(d);
  renderMemory(d);
  $("genAt").textContent = (d.generated_at || '').slice(0, 19).replace('T', ' ') + " UTC";
  $("liveStamp").textContent = "● Live · " + (d.generated_at || '').slice(11, 19);
}
render(window.__DATA__);
</script>
</body>
</html>
"""


def generate_dashboard(data: dict) -> str:
    """Return the full HTML page with data injected as window.__DATA__."""
    payload = json.dumps(data, default=str, ensure_ascii=False)
    # Escape </script in JSON so it can't close the <script> tag early.
    payload = payload.replace("</", "<\\/")
    return HTML_TEMPLATE.replace("__DATA_JSON__", payload)


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
            body = json.dumps(collect_dashboard_data(), indent=2, default=str).encode("utf-8")
            self._send(200, body, "application/json")
            return
        if path == "/":
            html = generate_dashboard(collect_dashboard_data())
            self._send(200, html.encode("utf-8"), "text/html; charset=utf-8")
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
