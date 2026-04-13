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
