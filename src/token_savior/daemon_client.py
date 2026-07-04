"""Minimal Unix-socket client to the Token Savior daemon (`ts _daemon-serve`).

Lets the stdio MCP server delegate a single tool call to the persistent
daemon. Used to bridge the `ts_search` cold start: the daemon keeps the Nomic
embedding model warm across sessions, so the first `ts_search` in a fresh
stdio process can borrow it instead of paying the ~5s in-process model load.

Wire protocol (mirrors cli.py): a 4-byte big-endian length prefix followed by
a JSON body. Request ``{"cmd": "call", "tool": ..., "args": ...}``; response
``{"ok": true, "text": ...}`` or ``{"ok": false, "error": ...}``.

Best-effort by contract: any failure (no socket, timeout, bad frame, daemon
error) returns ``None`` so the caller falls back to the in-process path.
"""
from __future__ import annotations

import json
import os
import socket
import struct
from typing import Any

_SOCK_PATH = os.environ.get("TS_SOCK", "/tmp/ts.sock")


def _send_frame(sock: socket.socket, obj: Any) -> None:
    data = json.dumps(obj).encode("utf-8")
    sock.sendall(struct.pack(">I", len(data)) + data)


def _recv_frame(sock: socket.socket, timeout: float) -> Any:
    sock.settimeout(timeout)
    hdr = b""
    while len(hdr) < 4:
        chunk = sock.recv(4 - len(hdr))
        if not chunk:
            return None
        hdr += chunk
    (length,) = struct.unpack(">I", hdr)
    buf = b""
    while len(buf) < length:
        chunk = sock.recv(min(65536, length - len(buf)))
        if not chunk:
            return None
        buf += chunk
    return json.loads(buf.decode("utf-8"))


def call_daemon(
    tool: str,
    args: dict[str, Any],
    *,
    timeout: float = 10.0,
    sock_path: str | None = None,
) -> str | None:
    """Return the daemon's text output for ``tool``/``args``, or None on any failure."""
    path = sock_path or _SOCK_PATH
    if not path or not os.path.exists(path):
        return None
    sock: socket.socket | None = None
    try:
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(2.0)
        sock.connect(path)
        _send_frame(sock, {"cmd": "call", "tool": tool, "args": args})
        resp = _recv_frame(sock, timeout)
        if not isinstance(resp, dict) or not resp.get("ok"):
            return None
        text = resp.get("text")
        return text if isinstance(text, str) else None
    except (OSError, ValueError):
        return None
    finally:
        if sock is not None:
            try:
                sock.close()
            except OSError:
                pass
