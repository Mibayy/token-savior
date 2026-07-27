"""`run_migrations` must survive a concurrent writer holding the database.

Two clients starting on a *fresh* database raced and one of them died with
``sqlite3.OperationalError: database is locked``, thrown by

    conn.execute("PRAGMA journal_mode = WAL")

Measured with six processes released on a barrier against a fresh data dir:
three failed, each after 0.00s. Raising the connection timeout from 5s to 60s
changed neither the failure nor its instantaneity, which rules out "the timeout
is too short" and points at the one case where SQLite refuses without ever
calling the busy handler.

Switching the journal mode needs an exclusive lock. Measured on 3.14.6:

    another connection holds a read lock   -> BUSY after the full timeout (31.8s
                                              with timeout=30) -- handler runs
    another connection holds a write lock  -> BUSY after 0.00s -- handler skipped

The second line is the race: a client that reaches the pragma while another
client is inside its own migration transaction is refused immediately, with no
retry of any kind. Once any one client has completed the switch the mode is
already WAL, so the pragma stops being a transition and stops needing the lock
at all -- which is why a *warm* database gives 6/6 successes and why this only
ever bites on first run.

The test is single-process and deterministic: the real race only decides
whether a client meets a held write lock, never what happens once it does.
"""
from __future__ import annotations

import sqlite3
import threading

from token_savior import db_core


def test_migrations_survive_a_concurrent_write_lock(tmp_path) -> None:
    db = tmp_path / "memory.db"
    # The file must exist and must NOT be in WAL yet: the pragma only takes the
    # exclusive lock when it actually changes the mode.
    amorce = sqlite3.connect(str(db))
    amorce.execute("CREATE TABLE amorce(x)")
    amorce.commit()
    amorce.close()

    # isolation_level=None + BEGIN IMMEDIATE: a real RESERVED lock, held until
    # the timer fires. check_same_thread=False because that timer releases it.
    bloqueur = sqlite3.connect(str(db), isolation_level=None, check_same_thread=False)
    bloqueur.execute("BEGIN IMMEDIATE")
    liberation = threading.Timer(0.4, bloqueur.rollback)
    liberation.start()
    try:
        db_core.run_migrations(db)
    finally:
        liberation.cancel()
        try:
            bloqueur.rollback()
        finally:
            bloqueur.close()

    conn = sqlite3.connect(str(db))
    try:
        mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
    finally:
        conn.close()
    assert mode.lower() == "wal", mode


def test_migrations_survive_a_concurrent_migrator(tmp_path, monkeypatch) -> None:
    """A second client adding the same column must not kill this one.

    Every column add reads ``PRAGMA table_info`` and then issues its
    ``ALTER TABLE``. Between those two statements another client can add the
    same column, and the loser gets ``duplicate column name``, which nothing
    catches: the exception escapes ``run_migrations`` and the client cannot
    start. Same visible symptom as the journal-mode race above, different
    statement.

    The window is genuinely narrow -- twelve processes released on a barrier
    reproduced it in only 2 runs out of 10, which is far too unreliable to
    gate a regression on. So the interleaving is pinned rather than hoped for:
    a trace callback fires the competing ``ALTER`` immediately before ours
    runs. That is the real production path, with the timing made certain
    instead of lucky.
    """
    db = tmp_path / "memory.db"
    vrai_connect = sqlite3.connect
    concurrent = {"fait": False}

    def connect_espion(*args, **kwargs):
        conn = vrai_connect(*args, **kwargs)

        def trace(sql: str) -> None:
            if concurrent["fait"]:
                return
            if "ADD COLUMN decay_immune" not in sql:
                return
            concurrent["fait"] = True
            autre = vrai_connect(str(db))
            try:
                autre.execute(
                    "ALTER TABLE observations "
                    "ADD COLUMN decay_immune INTEGER NOT NULL DEFAULT 0"
                )
                autre.commit()
            finally:
                autre.close()

        conn.set_trace_callback(trace)
        return conn

    monkeypatch.setattr(sqlite3, "connect", connect_espion)
    db_core.run_migrations(db)
    assert concurrent["fait"], "the competing ALTER never fired; test is inert"

    monkeypatch.undo()
    conn = sqlite3.connect(str(db))
    try:
        colonnes = [r[1] for r in conn.execute("PRAGMA table_info(observations)")]
    finally:
        conn.close()
    assert "decay_immune" in colonnes
    assert "superseded_by" in colonnes, "migration stopped early at the losing ALTER"
