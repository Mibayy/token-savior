"""daemon_client: minimal Unix-socket client used to bridge the ts_search
cold start by borrowing the daemon's warm Nomic model. Best-effort contract:
any failure returns None so the caller falls back to the in-process path.
"""

from __future__ import annotations

import json
import socket
import struct
import threading

from token_savior import daemon_client


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
    ready.wait(timeout=5)
    return t


def test_no_socket_returns_none(tmp_path):
    assert daemon_client.call_daemon("ts_search", {"query": "x"}, sock_path=str(tmp_path / "absent.sock")) is None


def test_successful_call_returns_text(tmp_path):
    sock_path = str(tmp_path / "ts.sock")
    t = _run_server(sock_path, {"ok": True, "text": "DAEMON_RESULT"})
    out = daemon_client.call_daemon("ts_search", {"query": "find deps"}, sock_path=sock_path)
    t.join(timeout=5)
    assert out == "DAEMON_RESULT"


def test_error_response_returns_none(tmp_path):
    sock_path = str(tmp_path / "ts.sock")
    t = _run_server(sock_path, {"ok": False, "error": "boom"})
    out = daemon_client.call_daemon("ts_search", {"query": "x"}, sock_path=sock_path)
    t.join(timeout=5)
    assert out is None


def test_non_string_text_returns_none(tmp_path):
    sock_path = str(tmp_path / "ts.sock")
    t = _run_server(sock_path, {"ok": True, "text": {"not": "a string"}})
    out = daemon_client.call_daemon("ts_search", {"query": "x"}, sock_path=sock_path)
    t.join(timeout=5)
    assert out is None
