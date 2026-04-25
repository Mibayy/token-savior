"""Tests for TOKEN_SAVIOR_PROFILE tool-filtering."""

import importlib
import sys

import pytest

from token_savior.server_handlers import (
    META_HANDLERS,
    MEMORY_HANDLERS,
    QFN_HANDLERS,
    SLOT_HANDLERS,
)
from token_savior.tool_schemas import TOOL_SCHEMAS

_TOTAL = len(TOOL_SCHEMAS)


def _reload_with_profile(monkeypatch, value):
    if value is None:
        monkeypatch.delenv("TOKEN_SAVIOR_PROFILE", raising=False)
    else:
        monkeypatch.setenv("TOKEN_SAVIOR_PROFILE", value)
    sys.modules.pop("token_savior.server", None)
    return importlib.import_module("token_savior.server")


@pytest.fixture(autouse=True)
def _restore_server():
    yield
    sys.modules.pop("token_savior.server", None)
    importlib.import_module("token_savior.server")


def test_full_profile_exposes_all_tools(monkeypatch):
    srv = _reload_with_profile(monkeypatch, "full")
    assert len(srv.TOOLS) == _TOTAL


def test_unset_defaults_to_ultra(monkeypatch):
    """Default flipped from 'full' to 'ultra' in v3 (Round 6 cleanup)."""
    srv = _reload_with_profile(monkeypatch, None)
    # ultra exposes a curated 33-tool subset + the ts_extended proxy.
    # The exact count depends on _ULTRA_INCLUDES; just verify it's
    # significantly smaller than full and contains ts_extended.
    names = {t.name for t in srv.TOOLS}
    assert len(names) < _TOTAL
    assert "ts_extended" in names
    assert "find_symbol" in names  # canary: a hot tool stays visible


def test_core_profile_excludes_memory_and_meta(monkeypatch):
    srv = _reload_with_profile(monkeypatch, "core")
    names = {t.name for t in srv.TOOLS}
    assert len(names) < _TOTAL
    assert names.isdisjoint(MEMORY_HANDLERS)
    assert names.isdisjoint(META_HANDLERS)
    # QFN + SLOT should still be present
    assert set(QFN_HANDLERS).issubset(names)
    assert set(SLOT_HANDLERS).issubset(names)


def test_nav_profile_is_subset_of_core(monkeypatch):
    srv_core = _reload_with_profile(monkeypatch, "core")
    core_names = {t.name for t in srv_core.TOOLS}
    srv_nav = _reload_with_profile(monkeypatch, "nav")
    nav_names = {t.name for t in srv_nav.TOOLS}
    assert nav_names < core_names
    assert nav_names == set(QFN_HANDLERS)


def test_invalid_profile_falls_back_to_ultra(monkeypatch, capsys):
    srv = _reload_with_profile(monkeypatch, "bogus")
    # Falls back to the new default (ultra) — see test_unset_defaults_to_ultra.
    names = {t.name for t in srv.TOOLS}
    assert len(names) < _TOTAL
    assert "ts_extended" in names
    err = capsys.readouterr().err
    assert "unknown profile 'bogus'" in err


def test_profile_is_case_insensitive(monkeypatch):
    srv = _reload_with_profile(monkeypatch, "CORE")
    names = {t.name for t in srv.TOOLS}
    assert names.isdisjoint(MEMORY_HANDLERS)
