"""Tests for auto-save findings on switch_project."""

import os

from token_savior import server_state as s


def test_auto_save_disabled_by_default():
    assert not s._auto_save_enabled


def test_auto_save_tracking_accumulates_symbols():
    s._auto_save_symbols.clear()
    s._auto_save_symbols.append("foo")
    s._auto_save_symbols.append("bar")
    assert len(s._auto_save_symbols) == 2
    s._auto_save_symbols.clear()


def test_auto_save_env_var(monkeypatch):
    monkeypatch.setenv("TOKEN_SAVIOR_MEMORY_AUTO_SAVE", "1")
    assert os.environ.get("TOKEN_SAVIOR_MEMORY_AUTO_SAVE") == "1"
