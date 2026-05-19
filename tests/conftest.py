"""Global test config: isolate runtime state from `~/.local/share/token-savior`.

Pytest loads this file before collecting/importing any test module, so setting
`TOKEN_SAVIOR_STATS_DIR` here ensures `token_savior.server` and
`token_savior.slot_manager` pick up the isolated path when they first import.
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

import pytest

# Prefer this checkout's `src/` over any installed token_savior (e.g. an
# editable install pointing at a sibling worktree on the same machine).
_REPO_SRC = Path(__file__).resolve().parent.parent / "src"
if str(_REPO_SRC) not in sys.path:
    sys.path.insert(0, str(_REPO_SRC))

_ISOLATED_STATS_DIR = Path(tempfile.mkdtemp(prefix="ts-test-stats-"))
os.environ["TOKEN_SAVIOR_STATS_DIR"] = str(_ISOLATED_STATS_DIR)

# Default the watcher to OFF during the test suite so importing
# `token_savior.slot_manager` (transitively via server) never triggers
# a `from watchfiles import watch` inside a spawned thread. The watcher
# tests explicitly flip it back to "auto" via an autouse fixture in
# tests/test_watcher.py (which also sets TS_WATCHER_FORCE_POLLING).
#
# Why this matters on CI: the watchfiles Rust CPython extension holds
# inotify resources whose destructor can segfault at interpreter
# shutdown on GitHub Actions Python 3.11 + 3.12 runners (SIGABRT /
# SIGSEGV) — even when no watcher thread ever ran. Keeping the import
# deferred by default means 99 % of the suite never loads the .so.
os.environ.setdefault("TOKEN_SAVIOR_WATCHER", "off")


# Redirect latency.py's tool_latency table to an isolated SQLite file so
# pytest never writes telemetry into the developer's prod memory.db. This
# is what was polluting the memory_viewer FTS tests in full-suite runs.


@pytest.fixture(autouse=True, scope="session")
def _isolate_latency_db():
    """Point token_savior.latency at a per-session temp DB."""
    from token_savior import latency
    temp_db = _ISOLATED_STATS_DIR / "tool_latency.db"
    original_open = latency._open_conn

    def patched_open(db_path=None):
        return original_open(db_path or temp_db)

    latency._open_conn = patched_open
    latency.reset_for_tests()
    yield
    latency.reset_for_tests()
