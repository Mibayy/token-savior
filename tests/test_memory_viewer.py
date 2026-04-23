"""A2-1: optional web viewer backend on 127.0.0.1.

Tests use a real ephemeral TCP port and run the viewer against an
isolated tmp memory DB. Every test that starts a viewer tears it down
via the module-level ``stop()`` so state never leaks between cases.

The off-path test asserts that with ``TS_VIEWER_PORT`` unset, the
module's public entry points are cheap no-ops and no thread is
started.
"""

from __future__ import annotations

import json
import socket
import time
import urllib.request
from pathlib import Path
from unittest.mock import patch

import pytest

from token_savior import memory_db
from token_savior.memory import viewer
from token_savior.server_handlers.memory import _mh_memory_doctor

PROJECT = "/tmp/test-project-a2-1"


def _free_port() -> int:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]
    finally:
        s.close()


def _wait_until_serving(port: int, deadline_s: float = 15.0) -> None:
    t0 = time.monotonic()
    while time.monotonic() - t0 < deadline_s:
        try:
            with urllib.request.urlopen(
                f"http://127.0.0.1:{port}/status", timeout=0.5,
            ) as r:
                r.read()
                return
        except Exception:
            time.sleep(0.05)
    raise RuntimeError(f"viewer did not come up on :{port}")


@pytest.fixture
def _memory_tmpdb(tmp_path: Path):
    db_path = tmp_path / "memory.db"
    with patch.object(memory_db, "MEMORY_DB_PATH", db_path):
        yield db_path


@pytest.fixture
def _viewer_running(monkeypatch, _memory_tmpdb):
    port = _free_port()
    monkeypatch.setenv("TS_VIEWER_PORT", str(port))
    assert viewer.start_if_configured() is True
    _wait_until_serving(port)
    try:
        yield port
    finally:
        viewer.stop()


# ── off path ──────────────────────────────────────────────────────────────


class TestDisabledByDefault:
    def test_env_absent_means_disabled(self, monkeypatch):
        monkeypatch.delenv("TS_VIEWER_PORT", raising=False)
        assert viewer.is_enabled() is False
        assert viewer.get_port() is None
        assert viewer.start_if_configured() is False
        assert viewer.is_running() is False

    def test_notify_is_noop_when_disabled(self, monkeypatch):
        monkeypatch.delenv("TS_VIEWER_PORT", raising=False)
        # Must not crash, must not start anything.
        viewer.notify_observation_saved(123)
        assert viewer.is_running() is False

    def test_invalid_port_is_rejected(self, monkeypatch):
        monkeypatch.setenv("TS_VIEWER_PORT", "not-a-number")
        assert viewer.get_port() is None
        assert viewer.start_if_configured() is False

    def test_port_out_of_range_rejected(self, monkeypatch):
        monkeypatch.setenv("TS_VIEWER_PORT", "42")
        assert viewer.get_port() is None
        assert viewer.start_if_configured() is False


# ── lifecycle ────────────────────────────────────────────────────────────


class TestStartStop:
    def test_start_is_idempotent(self, monkeypatch, _memory_tmpdb):
        port = _free_port()
        monkeypatch.setenv("TS_VIEWER_PORT", str(port))
        try:
            assert viewer.start_if_configured() is True
            # Second call is a no-op but returns True (running).
            assert viewer.start_if_configured() is True
        finally:
            viewer.stop()
        assert viewer.is_running() is False


# ── endpoints ────────────────────────────────────────────────────────────


class TestEndpointsJson:
    def test_root_serves_html(self, _viewer_running):
        port = _viewer_running
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/") as r:
            body = r.read().decode("utf-8")
            ctype = r.headers.get("Content-Type", "")
        assert "text/html" in ctype
        assert "<html" in body
        assert "Token Savior" in body
        # Must wire EventSource to /stream.
        assert "/stream" in body

    def test_status_endpoint_shape(self, _viewer_running):
        port = _viewer_running
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/status") as r:
            data = json.loads(r.read())
        assert "obs_active" in data
        assert "obs_archived" in data
        assert "sessions" in data
        assert "vectors" in data
        assert data["viewer_port"] == port

    def test_obs_endpoint_returns_row(self, _viewer_running):
        port = _viewer_running
        sid = memory_db.session_start(PROJECT)
        oid = memory_db.observation_save(
            sid, PROJECT, "convention", "viewer-obs", "body content",
        )
        assert oid is not None
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/obs/{oid}") as r:
            data = json.loads(r.read())
        assert data["id"] == oid
        assert data["title"] == "viewer-obs"

    def test_obs_endpoint_404(self, _viewer_running):
        port = _viewer_running
        req = urllib.request.Request(f"http://127.0.0.1:{port}/obs/999999")
        with pytest.raises(Exception) as exc_info:
            urllib.request.urlopen(req)
        assert "404" in str(exc_info.value)

    def test_search_without_query_returns_recent(self, _viewer_running):
        port = _viewer_running
        sid = memory_db.session_start(PROJECT)
        for i in range(3):
            memory_db.observation_save(
                sid, PROJECT, "convention", f"row {i}", f"body {i}",
            )
        with urllib.request.urlopen(
            f"http://127.0.0.1:{port}/search"
        ) as r:
            data = json.loads(r.read())
        assert "results" in data
        # get_recent_index may include all projects' obs; just ensure some rows.
        assert isinstance(data["results"], list)

    def test_search_with_query_uses_fts(self, _viewer_running):
        port = _viewer_running
        sid = memory_db.session_start(PROJECT)
        memory_db.observation_save(
            sid, PROJECT, "convention", "alpha", "uniqwordCCC body",
        )
        memory_db.observation_save(
            sid, PROJECT, "convention", "beta", "other body no match",
        )
        # Active project influences results; resolve via module helper.
        with urllib.request.urlopen(
            f"http://127.0.0.1:{port}/search?q=uniqwordCCC"
        ) as r:
            data = json.loads(r.read())
        titles = [h["title"] for h in data["results"]]
        assert "alpha" in titles

    def test_unknown_path_returns_404(self, _viewer_running):
        port = _viewer_running
        req = urllib.request.Request(f"http://127.0.0.1:{port}/nope")
        with pytest.raises(Exception) as exc_info:
            urllib.request.urlopen(req)
        assert "404" in str(exc_info.value)


# ── SSE stream ───────────────────────────────────────────────────────────


class TestSseStream:
    def test_save_pushes_event_to_subscriber(self, _viewer_running):
        port = _viewer_running
        # Open the stream in streaming mode.
        req = urllib.request.Request(f"http://127.0.0.1:{port}/stream")
        resp = urllib.request.urlopen(req, timeout=5)
        try:
            assert "text/event-stream" in resp.headers.get("Content-Type", "")
            # Read the hello frame first.
            hello = b""
            while b"\n\n" not in hello:
                chunk = resp.read(1)
                if not chunk:
                    break
                hello += chunk
            assert b"event: hello" in hello

            # Give the handler a tick to register the subscriber.
            time.sleep(0.05)

            sid = memory_db.session_start(PROJECT)
            oid = memory_db.observation_save(
                sid, PROJECT, "convention",
                "sse-pushed", "this triggers an event",
            )
            assert oid is not None

            # Read the save frame.
            buf = b""
            t0 = time.monotonic()
            while time.monotonic() - t0 < 3.0:
                chunk = resp.read(1)
                if not chunk:
                    break
                buf += chunk
                if b"event: save" in buf and b"\n\n" in buf:
                    break
            assert b"event: save" in buf
            # Extract the data line and parse it.
            for line in buf.split(b"\n"):
                if line.startswith(b"data: "):
                    payload = json.loads(line[len(b"data: "):].decode("utf-8"))
                    if payload.get("event") == "save":
                        assert payload["obs_id"] == oid
                        return
            pytest.fail("save event data not parsed")
        finally:
            resp.close()


# ── health / doctor integration ──────────────────────────────────────────


class TestCheckHealth:
    def test_disabled_when_env_absent(self, monkeypatch):
        monkeypatch.delenv("TS_VIEWER_PORT", raising=False)
        res = viewer.check_health()
        assert res["enabled"] is False
        assert res["status"] == "disabled"

    def test_ok_when_running(self, _viewer_running):
        res = viewer.check_health()
        assert res["enabled"] is True
        assert res["status"] == "ok"
        assert res["port"] == _viewer_running

    def test_down_when_env_set_but_not_started(self, monkeypatch):
        port = _free_port()
        monkeypatch.setenv("TS_VIEWER_PORT", str(port))
        # Explicitly do NOT start. Status should be "down".
        res = viewer.check_health()
        assert res["enabled"] is True
        assert res["status"] == "down"


class TestDoctorRendersViewer:
    def test_disabled_line(self, monkeypatch, _memory_tmpdb):
        monkeypatch.delenv("TS_VIEWER_PORT", raising=False)
        out = _mh_memory_doctor({"project": PROJECT})
        assert "Viewer: disabled" in out

    def test_ok_line(self, _viewer_running):
        out = _mh_memory_doctor({"project": PROJECT})
        assert "Viewer: ok" in out
        assert f"127.0.0.1:{_viewer_running}" in out



# ── A2-2: HTML fragments for htmx ─────────────────────────────────────────


class TestHtmxRootPage:
    def test_root_has_dashboard_wiring(self, _viewer_running):
        port = _viewer_running
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/") as r:
            body = r.read().decode("utf-8")
        # Header + stack.
        assert "Token Savior Recall" in body
        assert "Memory Viewer" in body
        assert "htmx.org" in body
        # Search wired via htmx with 300ms debounce.
        assert 'hx-get="/search?format=html"' in body
        assert "keyup changed delay:300ms" in body
        assert 'hx-target="#results"' in body
        # Live SSE section.
        assert 'id="live"' in body
        assert "EventSource('/stream')" in body
        # Status bar at bottom wired to /status fragment.
        assert 'id="statusbar"' in body
        assert 'hx-get="/status?format=html"' in body
        assert "every 5s" in body


class TestSearchHtmlFragment:
    def test_search_html_returns_fragment_with_badges(self, _viewer_running):
        port = _viewer_running
        sid = memory_db.session_start(PROJECT)
        memory_db.observation_save(
            sid, PROJECT, "convention", "alpha htmx", "uniqmarkerDDD body",
        )
        req = urllib.request.Request(
            f"http://127.0.0.1:{port}/search?q=uniqmarkerDDD&format=html"
        )
        with urllib.request.urlopen(req) as r:
            body = r.read().decode("utf-8")
            ctype = r.headers.get("Content-Type", "")
        assert "text/html" in ctype
        # No <html>/<body> wrapper — a plain fragment.
        assert "<html" not in body
        assert '<li class="r"' in body
        assert '<span class="badge' in body
        assert "convention" in body
        assert "alpha htmx" in body
        # Citation button wired to expand inline.
        assert 'hx-get="/obs/' in body
        assert "ts://obs/" in body

    def test_search_html_empty_results(self, _viewer_running):
        port = _viewer_running
        req = urllib.request.Request(
            f"http://127.0.0.1:{port}/search?q=zzznosuchqueryzzz&format=html"
        )
        with urllib.request.urlopen(req) as r:
            body = r.read().decode("utf-8")
        assert 'class="empty"' in body
        assert "no results" in body

    def test_search_without_format_still_json(self, _viewer_running):
        """Back-compat: no ?format=html still yields JSON."""
        port = _viewer_running
        with urllib.request.urlopen(
            f"http://127.0.0.1:{port}/search"
        ) as r:
            ctype = r.headers.get("Content-Type", "")
            data = json.loads(r.read())
        assert "application/json" in ctype
        assert "results" in data


class TestObsHtmlFragment:
    def test_obs_html_renders_detail(self, _viewer_running):
        port = _viewer_running
        sid = memory_db.session_start(PROJECT)
        oid = memory_db.observation_save(
            sid, PROJECT, "pattern", "htmx-detail", "frag body",
        )
        req = urllib.request.Request(
            f"http://127.0.0.1:{port}/obs/{oid}?format=html"
        )
        with urllib.request.urlopen(req) as r:
            body = r.read().decode("utf-8")
            ctype = r.headers.get("Content-Type", "")
        assert "text/html" in ctype
        assert f'id="d-{oid}"' in body
        assert "<pre>" in body
        assert "frag body" in body
        assert "pattern" in body

    def test_obs_html_escapes_content(self, _viewer_running):
        port = _viewer_running
        sid = memory_db.session_start(PROJECT)
        oid = memory_db.observation_save(
            sid, PROJECT, "convention", "esc",
            "<script>alert(1)</script>",
        )
        req = urllib.request.Request(
            f"http://127.0.0.1:{port}/obs/{oid}?format=html"
        )
        with urllib.request.urlopen(req) as r:
            body = r.read().decode("utf-8")
        assert "<script>alert(1)</script>" not in body
        assert "&lt;script&gt;" in body

    def test_obs_html_404_returns_fragment(self, _viewer_running):
        port = _viewer_running
        req = urllib.request.Request(
            f"http://127.0.0.1:{port}/obs/999999?format=html"
        )
        with pytest.raises(Exception) as exc_info:
            urllib.request.urlopen(req)
        # Still a 404, even in html mode.
        assert "404" in str(exc_info.value)


class TestStatusHtmlFragment:
    def test_status_html_has_pills_and_continuity(self, _viewer_running):
        port = _viewer_running
        sid = memory_db.session_start(PROJECT)
        memory_db.observation_save(
            sid, PROJECT, "convention", "status-probe", "body",
        )
        req = urllib.request.Request(
            f"http://127.0.0.1:{port}/status?format=html"
        )
        with urllib.request.urlopen(req) as r:
            body = r.read().decode("utf-8")
            ctype = r.headers.get("Content-Type", "")
        assert "text/html" in ctype
        assert 'class="pill' in body
        assert "vectors:" in body
        assert "obs:" in body
        assert "continuity:" in body
        # No full document wrapper in a fragment.
        assert "<html" not in body

    def test_status_html_vectors_off_label(self, _viewer_running, monkeypatch):
        """When VECTOR_SEARCH_AVAILABLE is False, pill reads 'off'."""
        port = _viewer_running
        from token_savior import db_core
        monkeypatch.setattr(db_core, "VECTOR_SEARCH_AVAILABLE", False)
        req = urllib.request.Request(
            f"http://127.0.0.1:{port}/status?format=html"
        )
        with urllib.request.urlopen(req) as r:
            body = r.read().decode("utf-8")
        assert "vectors: off" in body

    def test_status_without_format_still_json(self, _viewer_running):
        port = _viewer_running
        with urllib.request.urlopen(
            f"http://127.0.0.1:{port}/status"
        ) as r:
            ctype = r.headers.get("Content-Type", "")
            data = json.loads(r.read())
        assert "application/json" in ctype
        # A2-2 adds continuity to the JSON payload too.
        assert "continuity" in data
        assert "score" in data["continuity"]
