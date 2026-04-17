"""A2-1: optional web viewer backend on 127.0.0.1.

Off by default — starts only when ``TS_VIEWER_PORT`` is set in the
environment. When disabled, every public entry point is a cheap no-op
and heavy modules (``http.server``, ``queue``, ``urllib``) are never
imported at module load time. This keeps the baseline server cold-start
and footprint unchanged for the common case.

Endpoints (all JSON except ``/`` and ``/stream``):

- ``GET /``             → minimal HTML dashboard (htmx, inline, stdlib)
- ``GET /obs/{id}``     → full observation row
- ``GET /search?q=...`` → Layer 1 recent-index rows (default 15)
- ``GET /status``       → Layer-status JSON (obs counts, vectors, etc.)
- ``GET /stream``       → text/event-stream — one ``save`` event per
                          observation_save call (pushed via
                          :func:`notify_observation_saved`).

Binds ``127.0.0.1`` only; the server is explicitly not exposed to the
outside world. Operators who want a public view are expected to add
their own reverse-proxy with auth in front of it.
"""

from __future__ import annotations

import logging
import os
import threading
from typing import Any

_logger = logging.getLogger(__name__)

ENV_PORT = "TS_VIEWER_PORT"
_SSE_HEARTBEAT_SEC = 30

# ── module-level state ────────────────────────────────────────────────────
_lock = threading.Lock()
_started = False
_server: Any | None = None
_server_thread: threading.Thread | None = None
_subscribers_lock = threading.Lock()
_subscribers: list[Any] = []  # list[queue.Queue[str]]


def _parse_port() -> int | None:
    raw = os.environ.get(ENV_PORT, "").strip()
    if not raw:
        return None
    try:
        port = int(raw)
    except ValueError:
        _logger.warning("[token-savior:viewer] invalid %s=%r", ENV_PORT, raw)
        return None
    if not (1024 <= port <= 65535):
        _logger.warning("[token-savior:viewer] port out of range: %s", port)
        return None
    return port


def is_enabled() -> bool:
    """True when the viewer env var is set to a valid port."""
    return _parse_port() is not None


def is_running() -> bool:
    """True when the viewer HTTP thread has actually started."""
    return _started


def get_port() -> int | None:
    return _parse_port()


def start_if_configured() -> bool:
    """Boot the viewer thread iff ``TS_VIEWER_PORT`` is set. Idempotent.

    Returns True when the server is running after the call (either
    because it was already running, or it just started).
    """
    global _started, _server, _server_thread
    with _lock:
        if _started:
            return True
        port = _parse_port()
        if port is None:
            return False
        try:
            import http.server  # noqa: F401 — deferred import
            handler_cls = _build_handler()
            server = http.server.ThreadingHTTPServer(("127.0.0.1", port), handler_cls)
        except OSError as exc:
            _logger.warning("[token-savior:viewer] bind failed on :%s → %s", port, exc)
            return False
        except Exception as exc:
            _logger.warning("[token-savior:viewer] startup failed: %s", exc)
            return False
        thread = threading.Thread(
            target=server.serve_forever, name="ts-viewer", daemon=True,
        )
        thread.start()
        _server = server
        _server_thread = thread
        _started = True
        _logger.info("[token-savior:viewer] listening on http://127.0.0.1:%s", port)
        return True


def stop() -> None:
    """Shut the viewer down. Used mostly by tests."""
    global _started, _server, _server_thread
    with _lock:
        srv = _server
        _server = None
        _server_thread = None
        _started = False
    if srv is not None:
        try:
            srv.shutdown()
            srv.server_close()
        except Exception:
            pass
    with _subscribers_lock:
        _subscribers.clear()


def notify_observation_saved(obs_id: int) -> None:
    """Fan out a save event to every active SSE subscriber. No-op when off."""
    if not _started or obs_id is None:
        return
    import json
    payload = json.dumps({"event": "save", "obs_id": int(obs_id)})
    with _subscribers_lock:
        dead: list[Any] = []
        for q in _subscribers:
            try:
                q.put_nowait(payload)
            except Exception:
                dead.append(q)
        for q in dead:
            try:
                _subscribers.remove(q)
            except ValueError:
                pass


def check_health() -> dict[str, Any]:
    """Doctor probe. Returns ``{enabled, status, port, reason}``."""
    port = _parse_port()
    if port is None:
        return {"enabled": False, "status": "disabled", "port": None}
    if not _started:
        return {"enabled": True, "status": "down", "port": port,
                "reason": "thread not started"}
    import urllib.error
    import urllib.request
    try:
        with urllib.request.urlopen(
            f"http://127.0.0.1:{port}/status", timeout=1.0,
        ) as resp:
            code = getattr(resp, "status", 200)
            if 200 <= code < 300:
                return {"enabled": True, "status": "ok", "port": port}
            return {"enabled": True, "status": "bad", "port": port, "code": code}
    except Exception as exc:
        return {"enabled": True, "status": "down", "port": port,
                "reason": str(exc)}


# ── handler builder (lazy imports) ────────────────────────────────────────


_HTML = """<!doctype html>
<html lang="en"><head>
<meta charset="utf-8">
<title>Token Savior — memory viewer</title>
<script src="https://unpkg.com/htmx.org@1.9.12" defer></script>
<style>
 :root { color-scheme: dark; }
 body { font: 14px/1.5 ui-monospace, SFMono-Regular, Menlo, monospace;
        background:#0b0d10; color:#d7dde3; margin:0; padding:20px;
        max-width: 960px; }
 h1 { font-size:16px; margin:0 0 12px 0; color:#8ab4f8; }
 h2 { font-size:14px; margin:16px 0 6px 0; color:#f0a36c; }
 pre,code { background:#16191e; padding:8px 10px; border-radius:6px;
            white-space:pre-wrap; word-break:break-word; }
 .row { display:flex; gap:16px; }
 .col { flex:1; min-width:0; }
 input[type=text] { width:100%; padding:6px 8px; background:#16191e;
                    color:#d7dde3; border:1px solid #2a2f36; border-radius:4px;
                    font:inherit; }
 button { padding:6px 10px; background:#2a5cff; color:white; border:0;
          border-radius:4px; cursor:pointer; font:inherit; }
 .dim { color:#6a7380; font-size:12px; }
 .evt { margin:4px 0; padding:4px 8px; background:#132018; border-radius:4px; }
 #events { max-height:240px; overflow-y:auto; }
</style></head>
<body>
<h1>🧠 Token Savior — memory viewer (127.0.0.1)</h1>
<p class="dim">Off by default. Set <code>TS_VIEWER_PORT</code> to enable.</p>

<div class="row">
  <div class="col">
    <h2>Status</h2>
    <pre id="status" hx-get="/status" hx-trigger="load, every 5s">loading…</pre>
  </div>
  <div class="col">
    <h2>Live save events</h2>
    <div id="events" class="dim">waiting for /stream …</div>
  </div>
</div>

<h2>Recent index</h2>
<form hx-get="/search" hx-target="#search-out" hx-swap="innerHTML">
  <input type="text" name="q" placeholder="query (optional — blank = 15 most recent)">
  <button type="submit">Search</button>
</form>
<pre id="search-out" hx-get="/search" hx-trigger="load">loading…</pre>

<script>
 const evtBox = document.getElementById('events');
 try {
   const src = new EventSource('/stream');
   src.addEventListener('hello', () => {
     evtBox.textContent = '🟢 connected';
   });
   src.addEventListener('save', (e) => {
     const d = JSON.parse(e.data);
     const row = document.createElement('div');
     row.className = 'evt';
     row.textContent = new Date().toISOString().slice(11, 19)
                       + '  obs #' + d.obs_id + ' saved';
     evtBox.prepend(row);
     while (evtBox.childElementCount > 50) evtBox.lastChild.remove();
   });
   src.onerror = () => { evtBox.textContent = '🔴 stream disconnected'; };
 } catch (e) { evtBox.textContent = '🔴 SSE unsupported: ' + e; }
</script>
</body></html>
"""


def _active_project_root() -> str:
    """Resolve the project to describe in /status and /search."""
    try:
        from token_savior import server_state as state
        root = state._slot_mgr.active_root
        if root:
            return root
    except Exception:
        pass
    try:
        from token_savior import memory_db
        with memory_db.db_session() as conn:
            row = conn.execute(
                "SELECT project_root FROM observations "
                "GROUP BY project_root ORDER BY COUNT(*) DESC LIMIT 1"
            ).fetchone()
            if row:
                return row[0]
    except Exception:
        pass
    return ""


def _build_handler() -> type:
    import http.server
    import json
    import queue
    import re
    import urllib.parse

    OBS_RE = re.compile(r"^/obs/(\d+)/?$")

    class _Handler(http.server.BaseHTTPRequestHandler):
        def log_message(self, format, *args):  # silence default stderr noise
            return

        def _send_json(self, code: int, data: Any) -> None:
            body = json.dumps(data, default=str).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def _send_html(self, code: int, html: str) -> None:
            body = html.encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):  # noqa: N802 (stdlib interface)
            try:
                parsed = urllib.parse.urlparse(self.path)
                path = parsed.path
                qs = urllib.parse.parse_qs(parsed.query)
                if path in ("", "/"):
                    self._send_html(200, _HTML)
                    return
                if path == "/status":
                    self._handle_status()
                    return
                if path == "/search":
                    q = (qs.get("q") or [""])[0]
                    try:
                        limit = int((qs.get("limit") or ["15"])[0])
                    except ValueError:
                        limit = 15
                    limit = max(1, min(limit, 100))
                    self._handle_search(q, limit)
                    return
                if path == "/stream":
                    self._handle_stream()
                    return
                m = OBS_RE.match(path)
                if m:
                    self._handle_obs(int(m.group(1)))
                    return
                self._send_json(404, {"error": "not found", "path": path})
            except BrokenPipeError:
                return
            except Exception as exc:
                try:
                    self._send_json(500, {"error": str(exc)})
                except Exception:
                    pass

        def _handle_obs(self, obs_id: int) -> None:
            from token_savior import memory_db
            rows = memory_db.observation_get([obs_id])
            if not rows:
                self._send_json(404, {"error": "not found", "id": obs_id})
                return
            obs = dict(rows[0])
            self._send_json(200, obs)

        def _handle_search(self, q: str, limit: int) -> None:
            from token_savior import memory_db
            project = _active_project_root()
            if q:
                rows = memory_db.observation_search(
                    project_root=project, query=q, limit=limit,
                )
            else:
                rows = memory_db.get_recent_index(
                    project_root=project, limit=limit,
                )
            self._send_json(200, {
                "project": project,
                "query": q,
                "limit": limit,
                "results": [dict(r) for r in rows],
            })

        def _handle_status(self) -> None:
            from token_savior import memory_db
            project = _active_project_root()
            db = memory_db.get_db()
            try:
                active = db.execute(
                    "SELECT COUNT(*) FROM observations "
                    "WHERE project_root=? AND archived=0",
                    [project],
                ).fetchone()[0]
                archived = db.execute(
                    "SELECT COUNT(*) FROM observations "
                    "WHERE project_root=? AND archived=1",
                    [project],
                ).fetchone()[0]
                sessions = db.execute(
                    "SELECT COUNT(*) FROM sessions WHERE project_root=?",
                    [project],
                ).fetchone()[0]
            finally:
                db.close()
            try:
                from token_savior.memory.embeddings import (
                    EMBED_DIM, vector_coverage,
                )
                vc = vector_coverage(project)
                vc["dim"] = EMBED_DIM
            except Exception:
                vc = {"total": 0, "indexed": 0, "percent": 0.0,
                      "available": False, "dim": 384}
            self._send_json(200, {
                "project": project,
                "obs_active": int(active or 0),
                "obs_archived": int(archived or 0),
                "sessions": int(sessions or 0),
                "vectors": vc,
                "viewer_port": _parse_port(),
            })

        def _handle_stream(self) -> None:
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "keep-alive")
            self.send_header("X-Accel-Buffering", "no")
            self.end_headers()
            sub: queue.Queue = queue.Queue(maxsize=64)
            with _subscribers_lock:
                _subscribers.append(sub)
            try:
                self.wfile.write(b"event: hello\ndata: {}\n\n")
                self.wfile.flush()
                while True:
                    try:
                        payload = sub.get(timeout=_SSE_HEARTBEAT_SEC)
                    except queue.Empty:
                        try:
                            self.wfile.write(b": heartbeat\n\n")
                            self.wfile.flush()
                        except Exception:
                            break
                        continue
                    try:
                        self.wfile.write(
                            b"event: save\ndata: " + payload.encode("utf-8") + b"\n\n"
                        )
                        self.wfile.flush()
                    except Exception:
                        break
            finally:
                with _subscribers_lock:
                    try:
                        _subscribers.remove(sub)
                    except ValueError:
                        pass

    return _Handler
