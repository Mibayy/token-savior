"""Global test config: isolate runtime state from `~/.local/share/token-savior`.

Pytest loads this file before collecting/importing any test module, so setting
`TOKEN_SAVIOR_STATS_DIR` here ensures `token_savior.server` and
`token_savior.slot_manager` pick up the isolated path when they first import.
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

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
