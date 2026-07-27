"""Every `token_savior.memory.*` module must import on its own.

    $ python -c "import token_savior.memory.observations"
    ImportError: cannot import name '_CORRUPTION_MARKERS' from partially
    initialized module 'token_savior.memory.observations'
    (most likely due to a circular import)

The cycle is `memory.X` -> `memory_db` -> `memory.X`, and it resolves in one
direction only. Facade first: `memory_db` runs, reaches its
`from token_savior.memory.X import (...)`, X runs to completion -- its own
`from token_savior import memory_db` binds the partially initialised *module
object*, which is fine since it is only dereferenced at call time -- and the
facade then collects its names. Submodule first: X stops at its import line to
run `memory_db`, which immediately asks X for names it has not defined yet.

So the direction that fails is the one where the import is `from X import
name` rather than `import X`. Nineteen modules are affected, not one: every
submodule `memory_db` re-exports by name.

Invisible through the MCP server, which always imports the facade. It bites a
test module, a script, `python -m`, a debugger session, and any future change
that drops the facade import -- and reads as a broken install rather than an
import-order constraint.
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

_MEMOIRE = Path(__file__).resolve().parents[1] / "src" / "token_savior" / "memory"
MODULES = sorted(
    p.stem for p in _MEMOIRE.glob("*.py")
    if p.stem != "__init__"
)


def test_the_module_list_is_not_empty() -> None:
    """Guards the parametrisation itself: an empty glob would pass silently."""
    assert len(MODULES) > 15, MODULES


@pytest.mark.parametrize("module", MODULES)
def test_submodule_imports_without_the_facade(module: str) -> None:
    resultat = subprocess.run(
        [sys.executable, "-c", f"import token_savior.memory.{module}"],
        capture_output=True, text=True, env=dict(os.environ), timeout=120,
        check=False,
    )
    assert resultat.returncode == 0, resultat.stderr.strip().splitlines()[-1:]


def test_the_facade_still_re_exports_everything() -> None:
    """Breaking the cycle must not empty `memory_db`.

    The facade exists so that rebinding `memory_db.MEMORY_DB_PATH` reaches
    every submodule that opens a connection. A fix that made the submodules
    importable by cutting the re-exports would take that away.
    """
    programme = (
        "from token_savior import memory_db\n"
        "for nom in ('observation_save', 'observation_search', 'session_end',\n"
        "            'get_stats', 'event_save', 'MEMORY_DB_PATH', 'get_db'):\n"
        "    assert hasattr(memory_db, nom), nom\n"
    )
    resultat = subprocess.run(
        [sys.executable, "-c", programme],
        capture_output=True, text=True, env=dict(os.environ), timeout=120,
        check=False,
    )
    assert resultat.returncode == 0, resultat.stderr


def test_the_path_override_still_reaches_a_submodule(tmp_path) -> None:
    """Rebinding the facade's path must still steer a submodule's connection."""
    cible = tmp_path / "impose.db"
    programme = (
        "from token_savior import memory_db\n"
        "from token_savior.memory import observations\n"
        f"memory_db.MEMORY_DB_PATH = {str(cible)!r}\n"
        "observations.observation_save(None, '/tmp/p', 'insight', 't', 'c')\n"
    )
    resultat = subprocess.run(
        [sys.executable, "-c", programme],
        capture_output=True, text=True, env=dict(os.environ), timeout=120,
        check=False,
    )
    assert resultat.returncode == 0, resultat.stderr
    assert cible.exists(), resultat.stderr
