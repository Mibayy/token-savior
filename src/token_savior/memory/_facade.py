"""Lazy handle on the `memory_db` facade, to break an import cycle.

Nineteen modules of this subpackage opened their connections through
`from token_savior import memory_db`, and `memory_db` re-exports their names
back with `from token_savior.memory.X import (...)`. That cycle resolved in
one direction only:

* facade first -- `memory_db` runs, reaches its import of X, X runs to
  completion, the facade collects its names;
* submodule first -- X stops at its import line to run `memory_db`, which
  immediately asks X for names it has not defined yet, and raises
  ``ImportError: cannot import name ... from partially initialized module``.

So `python -c "import token_savior.memory.observations"` failed while
`import token_savior.memory_db` first made it work. Invisible through the MCP
server, which always goes through the facade; it bit a test module, a script,
`python -m`, a debugger session, and read as a broken install rather than an
import-order constraint.

The cycle is cut here rather than in nineteen call sites. Importing this
module pulls nothing: the facade is resolved on first attribute access, by
which time both modules exist. Resolving per access also keeps the override
working -- rebinding `memory_db.MEMORY_DB_PATH`, which the test suite does
session-wide and thirty-one test modules do per case, still reaches every
submodule, because nothing here holds a copy of anything.
"""
from __future__ import annotations

from typing import Any


class _FacadeMemoire:
    """Forwards attribute access to `token_savior.memory_db`, on demand."""

    __slots__ = ()

    def __getattr__(self, nom: str) -> Any:
        from token_savior import memory_db as _reel

        return getattr(_reel, nom)

    def __repr__(self) -> str:  # pragma: no cover - diagnostics only
        return "<token_savior.memory_db (lazy)>"


memory_db = _FacadeMemoire()
