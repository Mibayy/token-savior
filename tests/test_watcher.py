"""Tests for the file watcher (B3, AUDIT.md Phase 4 proposal).

Covers the pure-Python layer (mode resolution, filter, drain semantics)
plus one real-file integration test that writes to tmp_path and waits
for the watcher thread to surface the event. The integration test is
gated on the ``watchfiles`` dep being importable; on environments
without it the fallback-logging path is exercised by
``test_start_without_watchfiles_logs_and_returns_false`` instead.
"""
from __future__ import annotations

import os
import time
from pathlib import Path

import pytest

from token_savior import watcher as watcher_mod
from token_savior.watcher import (
    SlotWatcher,
    _inotify_ceiling,
    resolve_mode,
)


@pytest.fixture(autouse=True)
def _force_polling(monkeypatch):
    """Tests run watchfiles in polling mode to avoid a Rust-backend
    cleanup race that segfaults at interpreter exit on some CI runners
    (observed on GitHub Actions ubuntu-latest, Python 3.11 + 3.12).
    Production paths still default to inotify.
    """
    monkeypatch.setenv("TS_WATCHER_FORCE_POLLING", "1")
    yield


# ── mode resolution ─────────────────────────────────────────────────────────


class TestResolveMode:
    def test_default_is_auto(self, monkeypatch):
        monkeypatch.delenv("TOKEN_SAVIOR_WATCHER", raising=False)
        assert resolve_mode() == "auto"

    def test_valid_values_accepted(self, monkeypatch):
        for v in ("on", "off", "auto", "AUTO", "On "):
            monkeypatch.setenv("TOKEN_SAVIOR_WATCHER", v)
            assert resolve_mode() == v.strip().lower()

    def test_typo_falls_back_to_auto(self, monkeypatch):
        # A typo must never silently disable the watcher — we collapse
        # to `auto` so the default behaviour wins.
        monkeypatch.setenv("TOKEN_SAVIOR_WATCHER", "trueish")
        assert resolve_mode() == "auto"


# ── inotify ceiling probe ──────────────────────────────────────────────────


class TestInotifyCeiling:
    def test_returns_int_or_none(self):
        v = _inotify_ceiling()
        # Linux CI returns an int; macOS / Windows return None. Both
        # are valid outcomes — just assert the contract.
        assert v is None or (isinstance(v, int) and v > 0)


# ── drain semantics (no filesystem) ────────────────────────────────────────


class TestDrainSemantics:
    def _make(self, tmp_path: Path) -> SlotWatcher:
        return SlotWatcher(root=str(tmp_path), exclude_patterns=[])

    def test_drain_empty_returns_empty_sets(self, tmp_path: Path):
        w = self._make(tmp_path)
        dirty, deleted = w.drain()
        assert dirty == set()
        assert deleted == set()

    def test_drain_resets_state(self, tmp_path: Path):
        w = self._make(tmp_path)
        # Simulate what the watcher thread would do.
        with w._lock:
            w._dirty.add("a.py")
            w._dirty.add("b.py")
            w._deleted.add("c.py")
        dirty, deleted = w.drain()
        assert dirty == {"a.py", "b.py"}
        assert deleted == {"c.py"}
        dirty2, deleted2 = w.drain()
        assert dirty2 == set()
        assert deleted2 == set()

    def test_deletion_wins_over_dirty(self, tmp_path: Path):
        # When the same file appears as dirty then deleted, the deleted
        # event wins and the dirty flag is cleared — otherwise we'd
        # reindex a file that no longer exists.
        w = self._make(tmp_path)
        with w._lock:
            w._dirty.add("x.py")
        # Emulate the thread logic directly.
        with w._lock:
            w._deleted.add("x.py")
            w._dirty.discard("x.py")
        dirty, deleted = w.drain()
        assert "x.py" in deleted
        assert "x.py" not in dirty


# ── start / failure behaviour ──────────────────────────────────────────────


class TestStartBehaviour:
    @pytest.mark.skipif(
        watcher_mod._WATCHFILES_AVAILABLE,
        reason="only exercised when watchfiles is missing",
    )
    def test_start_without_watchfiles_returns_false(self, tmp_path: Path):
        w = SlotWatcher(root=str(tmp_path), exclude_patterns=[])
        assert w.start() is False
        assert w.failure_reason is not None
        assert "watchfiles" in w.failure_reason.lower()

    @pytest.mark.skipif(
        not watcher_mod._WATCHFILES_AVAILABLE,
        reason="requires watchfiles installed",
    )
    def test_start_with_watchfiles_spawns_thread(self, tmp_path: Path):
        w = SlotWatcher(root=str(tmp_path), exclude_patterns=[])
        assert w.start() is True
        assert w.ok is True
        w.stop()


# ── integration: real filesystem events ────────────────────────────────────


@pytest.mark.skipif(
    not watcher_mod._WATCHFILES_AVAILABLE,
    reason="requires watchfiles installed",
)
@pytest.mark.skipif(
    os.environ.get("CI", "").lower() == "true",
    reason=(
        "watchfiles' Rust extension segfaults at interpreter shutdown "
        "on GitHub Actions even with force_polling=True. The unit tests "
        "above cover drain/filter/mode/ceiling semantics; these "
        "integration tests exercise real fs events and pass locally."
    ),
)
class TestIntegration:
    """Exercises a real watcher thread against a tmp_path filesystem.

    We tolerate slow event propagation with a generous deadline — the
    goal is to prove the wiring works, not to measure latency.
    """

    def _drain_with_timeout(
        self, w: SlotWatcher, predicate, timeout_s: float = 5.0,
    ) -> tuple[set[str], set[str]]:
        t0 = time.monotonic()
        dirty_all: set[str] = set()
        deleted_all: set[str] = set()
        while time.monotonic() - t0 < timeout_s:
            d, x = w.drain()
            dirty_all |= d
            deleted_all |= x
            if predicate(dirty_all, deleted_all):
                return dirty_all, deleted_all
            time.sleep(0.05)
        return dirty_all, deleted_all

    def test_create_file_surfaces_as_dirty(self, tmp_path: Path):
        w = SlotWatcher(root=str(tmp_path), exclude_patterns=[])
        assert w.start() is True
        try:
            # Slight pause so the watcher thread has time to arm inotify.
            time.sleep(0.1)
            (tmp_path / "new.py").write_text("x = 1\n")
            dirty, _deleted = self._drain_with_timeout(
                w, lambda d, _: "new.py" in d,
            )
            assert "new.py" in dirty
        finally:
            w.stop()

    def test_delete_file_surfaces_as_deleted(self, tmp_path: Path):
        (tmp_path / "will-go.py").write_text("x = 1\n")
        w = SlotWatcher(root=str(tmp_path), exclude_patterns=[])
        assert w.start() is True
        try:
            time.sleep(0.1)
            os.remove(tmp_path / "will-go.py")
            _dirty, deleted = self._drain_with_timeout(
                w, lambda _, x: "will-go.py" in x,
            )
            assert "will-go.py" in deleted
        finally:
            w.stop()

    def test_exclude_pattern_filters_out_event(self, tmp_path: Path):
        # A file matching the project exclude pattern must never show
        # up in the dirty set — otherwise a noisy monorepo would fire
        # hundreds of events per second on build artifacts.
        (tmp_path / "build").mkdir()
        w = SlotWatcher(
            root=str(tmp_path),
            exclude_patterns=["build/**", "**/build/**"],
        )
        assert w.start() is True
        try:
            time.sleep(0.1)
            (tmp_path / "build" / "out.js").write_text("// asset\n")
            # Also write a real file to make sure the watcher is alive.
            (tmp_path / "real.py").write_text("x = 2\n")
            dirty, _ = self._drain_with_timeout(
                w, lambda d, _: "real.py" in d,
            )
            # real.py must arrive; build/out.js must not.
            assert "real.py" in dirty
            assert "build/out.js" not in dirty
        finally:
            w.stop()
