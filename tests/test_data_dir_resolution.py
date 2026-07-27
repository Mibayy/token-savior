"""`TOKEN_SAVIOR_DATA_DIR` must be read when the database is opened.

v4.20.0 made the memory database honour `TOKEN_SAVIOR_DATA_DIR` and
`XDG_DATA_HOME`, which it previously ignored -- but froze the answer at import:

    MEMORY_DB_PATH = _resoudre_repertoire_donnees() / "memory.db"

So the variables only count if they are already set when
`token_savior.db_core` is first imported. Anything setting them afterwards --
a wrapper script that configures the environment after importing the package,
an embedding host doing it per project -- is silently ignored and the engine
writes to `~/.local/share/token-savior` instead. Same shape as the defect that
change fixed: the value is readable, and not read at the moment it matters.

Run in a clean interpreter rather than by touching module state: the suite
rebinds `MEMORY_DB_PATH` session-wide for isolation, and that rebinding is an
override which must keep winning. What is under test is the case where nobody
overrode anything, which only a fresh process has.
"""
from __future__ import annotations

import os
import subprocess
import sys


def _dans_un_interprete_neuf(programme: str) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    env.pop("TOKEN_SAVIOR_DATA_DIR", None)
    env.pop("XDG_DATA_HOME", None)
    return subprocess.run(
        [sys.executable, "-c", programme],
        capture_output=True, text=True, env=env, timeout=120, check=False,
    )


def test_data_dir_set_after_import_is_honoured(tmp_path) -> None:
    cible = tmp_path / "ailleurs"
    programme = (
        "import os\n"
        "from token_savior import memory_db\n"
        f"os.environ['TOKEN_SAVIOR_DATA_DIR'] = {str(cible)!r}\n"
        "memory_db.get_db().close()\n"
    )
    resultat = _dans_un_interprete_neuf(programme)
    assert resultat.returncode == 0, resultat.stderr
    assert (cible / "memory.db").exists(), resultat.stderr


def test_xdg_data_home_set_after_import_is_honoured(tmp_path) -> None:
    cible = tmp_path / "xdg"
    programme = (
        "import os\n"
        "from token_savior import memory_db\n"
        f"os.environ['XDG_DATA_HOME'] = {str(cible)!r}\n"
        "memory_db.get_db().close()\n"
    )
    resultat = _dans_un_interprete_neuf(programme)
    assert resultat.returncode == 0, resultat.stderr
    assert (cible / "token-savior" / "memory.db").exists(), resultat.stderr


def test_an_explicit_rebinding_still_wins(tmp_path) -> None:
    """The override the whole test suite depends on must keep winning.

    `conftest` rebinds `MEMORY_DB_PATH` for the session, and 31 test modules
    rebind it per case. Resolving the environment at use time must not quietly
    take that away, or the suite goes back to writing into the user's real
    database.
    """
    impose = tmp_path / "impose.db"
    ignore = tmp_path / "ignore"
    programme = (
        "import os\n"
        "from token_savior import db_core, memory_db\n"
        f"os.environ['TOKEN_SAVIOR_DATA_DIR'] = {str(ignore)!r}\n"
        f"db_core.MEMORY_DB_PATH = {str(impose)!r}\n"
        f"memory_db.MEMORY_DB_PATH = {str(impose)!r}\n"
        "memory_db.get_db().close()\n"
    )
    resultat = _dans_un_interprete_neuf(programme)
    assert resultat.returncode == 0, resultat.stderr
    assert impose.exists(), resultat.stderr
    assert not (ignore / "memory.db").exists(), "the environment overrode an override"
