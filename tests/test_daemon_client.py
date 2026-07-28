"""daemon_client: minimal Unix-socket client used to bridge the ts_search
cold start by borrowing the daemon's warm Nomic model. Best-effort contract:
any failure returns None so the caller falls back to the in-process path.
"""

from __future__ import annotations

import json
import os
import shutil
import socket
import struct
import tempfile
import threading

import pytest

from token_savior import daemon_client


@pytest.fixture
def sock_dir():
    # pytest's tmp_path can exceed the AF_UNIX sun_path limit (104 bytes on
    # macOS/BSD, 108 on Linux) and make bind() fail — which turned the
    # expect-None tests below into vacuous passes. mkdtemp under the system
    # temp dir stays short enough to bind everywhere.
    d = tempfile.mkdtemp(prefix="tsd.")
    yield d
    shutil.rmtree(d, ignore_errors=True)


def _serve_one(sock_path: str, response: dict | None, *, ready: threading.Event):
    """Accept a single connection, read one framed request, reply once."""
    srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    srv.bind(sock_path)
    srv.listen(1)
    ready.set()
    conn, _ = srv.accept()
    # Read the request frame (length-prefixed JSON) but ignore its content.
    hdr = b""
    while len(hdr) < 4:
        hdr += conn.recv(4 - len(hdr))
    (length,) = struct.unpack(">I", hdr)
    buf = b""
    while len(buf) < length:
        buf += conn.recv(length - len(buf))
    if response is not None:
        data = json.dumps(response).encode("utf-8")
        conn.sendall(struct.pack(">I", len(data)) + data)
    conn.close()
    srv.close()


def _run_server(sock_path, response):
    ready = threading.Event()
    t = threading.Thread(target=_serve_one, args=(sock_path, response), kwargs={"ready": ready}, daemon=True)
    t.start()
    if not ready.wait(timeout=5):
        # A dead server thread must fail the test loudly — a silent timeout
        # makes every expect-None assertion below pass vacuously.
        raise RuntimeError(f"test daemon failed to bind/listen on {sock_path}")
    return t


def test_no_socket_returns_none(sock_dir):
    assert daemon_client.call_daemon("ts_search", {"query": "x"}, sock_path=os.path.join(sock_dir, "absent.sock")) is None


def test_successful_call_returns_text(sock_dir):
    sock_path = os.path.join(sock_dir, "ts.sock")
    t = _run_server(sock_path, {"ok": True, "text": "DAEMON_RESULT"})
    out = daemon_client.call_daemon("ts_search", {"query": "find deps"}, sock_path=sock_path)
    t.join(timeout=5)
    assert out == "DAEMON_RESULT"


def test_error_response_returns_none(sock_dir):
    sock_path = os.path.join(sock_dir, "ts.sock")
    t = _run_server(sock_path, {"ok": False, "error": "boom"})
    out = daemon_client.call_daemon("ts_search", {"query": "x"}, sock_path=sock_path)
    t.join(timeout=5)
    assert out is None


def test_non_string_text_returns_none(sock_dir):
    sock_path = os.path.join(sock_dir, "ts.sock")
    t = _run_server(sock_path, {"ok": True, "text": {"not": "a string"}})
    out = daemon_client.call_daemon("ts_search", {"query": "x"}, sock_path=sock_path)
    t.join(timeout=5)
    assert out is None


# --- socket path hardening (#98) ---
def test_default_sock_path_prefers_xdg_runtime(monkeypatch):
    """Socket defaults out of world-writable /tmp into a per-user dir (#98)."""
    from token_savior import daemon_client
    monkeypatch.setenv("XDG_RUNTIME_DIR", "/run/user/1000")
    assert daemon_client._default_sock_path() == "/run/user/1000/ts.sock"


def test_default_sock_path_falls_back_to_config(monkeypatch):
    from token_savior import daemon_client
    monkeypatch.delenv("XDG_RUNTIME_DIR", raising=False)
    monkeypatch.setenv("XDG_CONFIG_HOME", "/home/u/.config")
    assert daemon_client._default_sock_path() == "/home/u/.config/ts/ts.sock"


def test_default_sock_path_never_uses_tmp(monkeypatch):
    from token_savior import daemon_client
    monkeypatch.delenv("XDG_RUNTIME_DIR", raising=False)
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    assert not daemon_client._default_sock_path().startswith("/tmp/")
