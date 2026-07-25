"""Startup banner must not write to stderr by default (#44).

PowerShell and several MCP clients on Windows surface anything on stderr as an
error, so an informational banner printed on every start made a working server
look broken. The banner is now opt-in.
"""
from __future__ import annotations

import io

from token_savior import server


def test_silent_by_default(monkeypatch):
    monkeypatch.delenv("TOKEN_SAVIOR_BANNER", raising=False)
    buf = io.StringIO()
    server._emit_startup_banner(buf, profile="full", tools=66, total=69, explicit=False)
    assert buf.getvalue() == ""


def test_emitted_when_opted_in(monkeypatch):
    monkeypatch.setenv("TOKEN_SAVIOR_BANNER", "1")
    buf = io.StringIO()
    server._emit_startup_banner(buf, profile="lean", tools=51, total=69, explicit=True)
    out = buf.getvalue()
    assert "profile=lean" in out
    assert "51/69" in out


def test_profile_hint_lists_the_documented_profiles(monkeypatch):
    """The hint used to omit `optimized`, which the README recommends."""
    monkeypatch.setenv("TOKEN_SAVIOR_BANNER", "1")
    buf = io.StringIO()
    server._emit_startup_banner(buf, profile="full", tools=66, total=69, explicit=False)
    out = buf.getvalue()
    for name in ("optimized", "lean", "ultra"):
        assert name in out


def test_no_hint_when_the_profile_was_chosen(monkeypatch):
    monkeypatch.setenv("TOKEN_SAVIOR_BANNER", "1")
    buf = io.StringIO()
    server._emit_startup_banner(buf, profile="full", tools=66, total=69, explicit=True)
    assert "reduce manifest cost" not in buf.getvalue()


def test_only_the_literal_one_enables_it(monkeypatch):
    for value in ("0", "true", "yes", ""):
        monkeypatch.setenv("TOKEN_SAVIOR_BANNER", value)
        buf = io.StringIO()
        server._emit_startup_banner(buf, profile="full", tools=66, total=69, explicit=False)
        assert buf.getvalue() == "", value
