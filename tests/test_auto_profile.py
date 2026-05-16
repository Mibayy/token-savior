"""Tests for the adaptive `auto` profile."""
from __future__ import annotations

import importlib
import sys


def _reload_server(monkeypatch, profile: str | None):
    """Reload token_savior.server with a specific profile env var."""
    if profile is None:
        monkeypatch.delenv("TOKEN_SAVIOR_PROFILE", raising=False)
    else:
        monkeypatch.setenv("TOKEN_SAVIOR_PROFILE", profile)
    if "token_savior.server" in sys.modules:
        del sys.modules["token_savior.server"]
    import token_savior.server as srv
    importlib.reload(srv) if "token_savior.server" in sys.modules else None
    return srv


def test_auto_profile_registered():
    """auto must be in the profile dict alongside full/code_mode/etc."""
    from token_savior.server import _PROFILE_EXCLUDES
    assert "auto" in _PROFILE_EXCLUDES


def test_auto_cold_start_falls_back_to_warm_baseline():
    """With no telemetry, auto should expose tiny_plus + essentials, not the empty set."""
    from token_savior import server as srv

    # Patch aggregate_counts to return nothing (cold start)
    from token_savior import telemetry
    orig = telemetry.aggregate_counts
    telemetry.aggregate_counts = lambda: {}
    try:
        includes = srv._auto_includes()
    finally:
        telemetry.aggregate_counts = orig

    assert "ts_search" in includes
    assert "ts_execute" in includes
    assert "switch_project" in includes
    assert "find_symbol" in includes  # via tiny_plus baseline


def test_auto_promotes_top_K_from_telemetry():
    """When telemetry has data, the top-K calls should land in the manifest."""
    from token_savior import server as srv
    from token_savior import telemetry

    fake_counts = {
        "find_symbol": 500,
        "get_function_source": 400,
        "search_codebase": 300,
        "get_full_context": 250,
        "replace_symbol_source": 200,
        "find_dead_code": 150,
        "get_git_status": 140,
        "analyze_config": 100,
        "get_dependencies": 90,
        "get_dependents": 80,
        "find_hotspots": 70,
        "memory_admin": 1,
    }
    orig = telemetry.aggregate_counts
    telemetry.aggregate_counts = lambda: fake_counts
    try:
        includes = srv._auto_includes()
    finally:
        telemetry.aggregate_counts = orig

    # Always-on essentials
    for must in ("ts_search", "ts_execute", "switch_project", "list_projects"):
        assert must in includes, f"missing essential: {must}"
    # Top-10 hot tools (excluding essentials already in the set)
    for hot in ("find_symbol", "get_function_source", "search_codebase",
                "get_full_context", "replace_symbol_source"):
        assert hot in includes, f"missing hot tool: {hot}"
    # Manifest stays small: essentials (5) + top-K (10) = 15-ish
    assert len(includes) <= 20, f"manifest too big: {len(includes)}"


def test_auto_skips_unknown_tools():
    """Telemetry entries for renamed/removed tools must not crash auto."""
    from token_savior import server as srv
    from token_savior import telemetry

    orig = telemetry.aggregate_counts
    telemetry.aggregate_counts = lambda: {
        "tool_that_no_longer_exists": 10_000,
        "find_symbol": 1,
    }
    try:
        includes = srv._auto_includes()
    finally:
        telemetry.aggregate_counts = orig

    assert "tool_that_no_longer_exists" not in includes
    assert "find_symbol" in includes


def test_deprecated_profile_warns_on_stderr(capsys, monkeypatch):
    """Setting one of the legacy profiles should print a deprecation notice."""
    srv = _reload_server(monkeypatch, "lean")
    captured = capsys.readouterr()
    assert "DEPRECATED" in captured.err
    assert "auto" in captured.err
    # And the profile still works (TOOLS is non-empty)
    assert len(srv.TOOLS) > 0


def test_auto_profile_does_not_emit_deprecation(capsys, monkeypatch):
    """auto is the recommended path — never warn."""
    _reload_server(monkeypatch, "auto")
    captured = capsys.readouterr()
    assert "DEPRECATED" not in captured.err


def test_full_profile_does_not_emit_deprecation(capsys, monkeypatch):
    """full stays a first-class profile for debug / power users."""
    _reload_server(monkeypatch, "full")
    captured = capsys.readouterr()
    assert "DEPRECATED" not in captured.err
