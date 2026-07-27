"""Token Savior Memory Engine — core DB primitives and shared utils.

Owns: schema/migrations, connection factory, small epoch/json/hash helpers.
Kept deliberately dependency-free so higher-level memory modules can import
from here without pulling the full memory_db facade.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import sqlite3
import time
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path


def _resoudre_repertoire_donnees() -> Path:
    """Repertoire de donnees, selon la convention suivie par le reste du produit.

    La base memoire etait le seul element a ignorer `TOKEN_SAVIOR_DATA_DIR` et
    `XDG_DATA_HOME` : elle prenait `~/.local/share` en dur. Un utilisateur qui
    deplace ses donnees retrouvait donc ses captures et son etat au nouvel
    endroit, et sa memoire a l'ancien, sans que rien ne le signale.

    Consequence secondaire, mesuree : les tests memoire ecrivaient dans la base
    reelle de l'utilisateur, faute de pouvoir la rediriger.
    """
    explicite = os.environ.get("TOKEN_SAVIOR_DATA_DIR", "").strip()
    if explicite:
        return Path(explicite)
    xdg = os.environ.get("XDG_DATA_HOME", "").strip()
    base = Path(xdg) if xdg else Path.home() / ".local" / "share"
    return base / "token-savior"


# Point de surcharge : la suite de tests le rebinde pour toute la session, et
# 31 modules de test le rebindent au cas par cas. C'est le mecanisme documente
# en tete de `memory_db`, il doit continuer de gagner.
_CHEMIN_INITIAL = _resoudre_repertoire_donnees() / "memory.db"
MEMORY_DB_PATH = _CHEMIN_INITIAL


def chemin_memoire() -> Path:
    """Ou vit la base memoire, demande au moment ou on l'ouvre.

    `TOKEN_SAVIOR_DATA_DIR` et `XDG_DATA_HOME` etaient lus une fois, a
    l'import. Ils ne comptaient donc que s'ils etaient deja poses avant le
    premier `import token_savior.db_core` : un script d'enrobage qui configure
    l'environnement apres avoir importe le paquet, un hote qui le fait par
    projet, etaient ignores en silence et la memoire partait dans
    `~/.local/share/token-savior`. Meme forme que le defaut corrige en 4.20.0 :
    la valeur est lisible, et pas lue au moment ou elle compte.

    Une surcharge explicite l'emporte ; a defaut on redemande a
    l'environnement plutot que de rejouer une reponse datee de l'import.
    """
    if MEMORY_DB_PATH != _CHEMIN_INITIAL:
        return Path(MEMORY_DB_PATH)
    return _resoudre_repertoire_donnees() / "memory.db"

_SCHEMA_PATH = Path(__file__).parent / "memory_schema.sql"

# Migrations run once per DB path (tests use per-tmp_path DBs).
_migrated_paths: set[str] = set()

_logger = logging.getLogger(__name__)

# A1-1: optional sqlite-vec integration. Absent by default — the base
# memory engine keeps working without it and VECTOR_SEARCH_AVAILABLE
# stays False. Install the extra with:
#   pip install 'token-savior-recall[memory-vector]'
try:
    import sqlite_vec as _sqlite_vec  # type: ignore[import-not-found]
    VECTOR_SEARCH_AVAILABLE = True
except ImportError:
    _sqlite_vec = None  # type: ignore[assignment]
    VECTOR_SEARCH_AVAILABLE = False

_vector_warning_emitted = False


def _maybe_load_sqlite_vec(conn: sqlite3.Connection) -> bool:
    """Load the sqlite-vec extension into ``conn`` when available.

    Returns True if the extension is loaded and vec0 tables can be used,
    False otherwise. A single warning is emitted per process — callers
    are expected to call this on every new connection, so we keep noise
    low. Failure modes covered:
      * sqlite-vec package not installed (ImportError at module import)
      * Python's sqlite3 compiled without extension-loading support
      * vec extension load raised at runtime
    """
    global _vector_warning_emitted
    if not VECTOR_SEARCH_AVAILABLE:
        if not _vector_warning_emitted:
            _logger.warning(
                "[token-savior:memory] sqlite-vec not installed; vector "
                "search disabled. Install with: "
                "pip install 'token-savior-recall[memory-vector]'"
            )
            _vector_warning_emitted = True
        return False
    try:
        conn.enable_load_extension(True)
        _sqlite_vec.load(conn)
        conn.enable_load_extension(False)
        return True
    except Exception as exc:  # `Exception` already subsumed the narrower arms
        if not _vector_warning_emitted:
            _logger.warning(
                "[token-savior:memory] sqlite-vec load failed (%s); "
                "vector search disabled.", exc,
            )
            _vector_warning_emitted = True
        return False


def _activer_wal(conn: sqlite3.Connection, *, patience_s: float = 5.0) -> None:
    """Put the connection in WAL mode, waiting out a concurrent writer.

    Changing the journal mode needs an exclusive lock, and SQLite refuses it
    with SQLITE_BUSY **without calling the busy handler** when another
    connection holds a write lock. The connection ``timeout=`` is therefore
    never consulted and the refusal is instantaneous. Measured on 3.14.6:

        another connection holds a read lock   -> BUSY after the full timeout
        another connection holds a write lock  -> BUSY after 0.00s

    Two clients starting on a *fresh* database hit the second case: the one
    that reaches the pragma while the other is inside its migration
    transaction is refused outright, the exception escapes ``get_db`` and that
    client cannot start. Six processes on a barrier, fresh data dir: three
    failed, each after 0.00s, and raising the timeout from 5s to 60s changed
    nothing.

    Retrying is the whole fix. As soon as any one client completes the switch
    the database is already in WAL, so the pragma stops being a transition,
    stops needing the exclusive lock, and succeeds for everyone else. That is
    also why a warm database never showed this.

    The last attempt is left to raise: a database we cannot put in WAL is a
    real problem, not something to swallow.
    """
    limite = time.monotonic() + patience_s
    while True:
        try:
            conn.execute("PRAGMA journal_mode = WAL")
            return
        except sqlite3.OperationalError:
            if time.monotonic() >= limite:
                raise
            time.sleep(0.02)


def _ajouter_colonne(conn: sqlite3.Connection, table: str, colonne: str, decl: str) -> bool:
    """Add a column unless it is already there. True if this call added it.

    Asking ``PRAGMA table_info`` first and issuing ``ALTER TABLE`` second is a
    check that goes stale between the question and the answer: two clients
    both read "missing", both add, and the loser gets

        sqlite3.OperationalError: duplicate column name: decay_immune

    which nothing caught, so the exception escaped ``run_migrations`` and that
    client could not start. Measured with twelve processes released on a
    barrier against a fresh data dir -- rare enough to read as a flake (2 runs
    out of 10), certain enough to happen on someone's first day.

    Letting SQLite arbitrate is the only check that cannot be stale by the
    time it is acted on. A missing table is treated the same way: the schema
    script that follows creates it with the column already declared.
    """
    try:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {colonne} {decl}")
        return True
    except sqlite3.OperationalError as exc:
        message = str(exc).lower()
        if "duplicate column name" in message or "no such table" in message:
            return False
        raise


def run_migrations(db_path: Path | str | None = None) -> None:
    """Apply schema + ALTER TABLE migrations once per database path.

    Idempotent. Called explicitly at MCP startup to keep get_db() hot-path
    free of schema inspection; also invoked lazily from get_db() as a
    safety net (e.g. for tests that patch MEMORY_DB_PATH).
    """
    path = Path(db_path) if db_path else chemin_memoire()
    path_str = str(path)
    if path_str in _migrated_paths:
        return

    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path_str)
    conn.row_factory = sqlite3.Row
    _activer_wal(conn)
    conn.execute("PRAGMA foreign_keys = ON")

    try:
        _ajouter_colonne(conn, "user_prompts", "project_root", "TEXT")

        schema_sql = _SCHEMA_PATH.read_text(encoding="utf-8")
        conn.executescript(schema_sql)

        _ajouter_colonne(conn, "sessions", "end_type", "TEXT")
        _ajouter_colonne(conn, "sessions", "tokens_injected", "INTEGER DEFAULT 0")
        _ajouter_colonne(conn, "sessions", "tokens_saved_est", "INTEGER DEFAULT 0")

        _ajouter_colonne(conn, "observations", "decay_immune", "INTEGER NOT NULL DEFAULT 0")
        _ajouter_colonne(conn, "observations", "last_accessed_epoch", "INTEGER")
        _ajouter_colonne(conn, "observations", "is_global", "INTEGER NOT NULL DEFAULT 0")
        _ajouter_colonne(conn, "observations", "context", "TEXT")
        _ajouter_colonne(conn, "observations", "expires_at_epoch", "INTEGER")
        _ajouter_colonne(conn, "observations", "agent_id", "TEXT")
        # Peremption. Une observation rendue fausse par une plus recente est
        # archivee, jamais supprimee, et garde le lien vers celle qui la
        # remplace : on peut repondre "qu'est-ce qui etait vrai en avril" et
        # defaire un remplacement errone.
        _ajouter_colonne(conn, "observations", "superseded_by", "INTEGER")
        # A5: narrative/facts/concepts — non-destructive column adds.
        # The FTS5 virtual table rebuild below picks them up.
        for col in ("narrative", "facts", "concepts"):
            _ajouter_colonne(conn, "observations", col, "TEXT")

        # Un index par colonne, hors de toute condition. `IF NOT EXISTS` est
        # deja la bonne question, et la poser sans condition supprime la
        # derniere facon pour un index de manquer : `idx_obs_agent` etait cree
        # dans la branche de `superseded_by`, donc plus du tout le jour ou
        # cette colonne rejoindrait memory_schema.sql.
        conn.execute("CREATE INDEX IF NOT EXISTS idx_obs_global ON observations(is_global)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_obs_context ON observations(context)")
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_obs_expires ON observations(expires_at_epoch)")
        # Partiel, comme dans memory_schema.sql : les deux definitions se
        # contredisaient, et laquelle gagnait dependait de l'age de la base.
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_obs_agent ON observations(agent_id) "
            "WHERE agent_id IS NOT NULL")

        # A5: observations_fts doesn't support ALTER to add columns. If the
        # existing virtual table is missing the new columns, rebuild it +
        # its triggers and repopulate from the base table via 'rebuild'.
        fts_row = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='observations_fts'"
        ).fetchone()
        fts_sql = (fts_row[0] or "") if fts_row else ""
        needs_fts_rebuild = fts_row is not None and not all(
            col in fts_sql for col in ("narrative", "facts", "concepts")
        )
        if needs_fts_rebuild:
            for trig in ("obs_fts_insert", "obs_fts_delete", "obs_fts_update"):
                conn.execute(f"DROP TRIGGER IF EXISTS {trig}")
            conn.execute("DROP TABLE IF EXISTS observations_fts")
            conn.execute(
                "CREATE VIRTUAL TABLE observations_fts USING fts5("
                "  title, content, why, how_to_apply, tags,"
                "  narrative, facts, concepts,"
                "  content='observations', content_rowid='id'"
                ")"
            )
            conn.execute(
                "CREATE TRIGGER obs_fts_insert AFTER INSERT ON observations BEGIN "
                "  INSERT INTO observations_fts(rowid, title, content, why, how_to_apply, tags, narrative, facts, concepts) "
                "  VALUES (new.id, new.title, new.content, new.why, new.how_to_apply, new.tags, new.narrative, new.facts, new.concepts); "
                "END"
            )
            conn.execute(
                "CREATE TRIGGER obs_fts_delete AFTER DELETE ON observations BEGIN "
                "  INSERT INTO observations_fts(observations_fts, rowid, title, content, why, how_to_apply, tags, narrative, facts, concepts) "
                "  VALUES ('delete', old.id, old.title, old.content, old.why, old.how_to_apply, old.tags, old.narrative, old.facts, old.concepts); "
                "END"
            )
            conn.execute(
                "CREATE TRIGGER obs_fts_update AFTER UPDATE ON observations BEGIN "
                "  INSERT INTO observations_fts(observations_fts, rowid, title, content, why, how_to_apply, tags, narrative, facts, concepts) "
                "  VALUES ('delete', old.id, old.title, old.content, old.why, old.how_to_apply, old.tags, old.narrative, old.facts, old.concepts); "
                "  INSERT INTO observations_fts(rowid, title, content, why, how_to_apply, tags, narrative, facts, concepts) "
                "  VALUES (new.id, new.title, new.content, new.why, new.how_to_apply, new.tags, new.narrative, new.facts, new.concepts); "
                "END"
            )
            conn.execute("INSERT INTO observations_fts(observations_fts) VALUES ('rebuild')")

        conn.execute(
            "CREATE TABLE IF NOT EXISTS adaptive_lattice ("
            "  context_type TEXT NOT NULL,"
            "  level INTEGER NOT NULL,"
            "  alpha REAL NOT NULL DEFAULT 1.0,"
            "  beta REAL NOT NULL DEFAULT 1.0,"
            "  updated_at_epoch INTEGER NOT NULL,"
            "  PRIMARY KEY (context_type, level)"
            ")"
        )
        conn.execute(
            "CREATE TABLE IF NOT EXISTS consistency_scores ("
            "  obs_id INTEGER PRIMARY KEY,"
            "  validity_alpha REAL NOT NULL DEFAULT 2.0,"
            "  validity_beta REAL NOT NULL DEFAULT 1.0,"
            "  last_checked_epoch INTEGER,"
            "  stale_suspected INTEGER NOT NULL DEFAULT 0,"
            "  quarantine INTEGER NOT NULL DEFAULT 0,"
            "  FOREIGN KEY(obs_id) REFERENCES observations(id) ON DELETE CASCADE"
            ")"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_consistency_quarantine "
            "ON consistency_scores(quarantine)"
        )

        conn.execute(
            "CREATE TABLE IF NOT EXISTS ledger_events ("
            " id INTEGER PRIMARY KEY AUTOINCREMENT,"
            " ts_epoch INTEGER NOT NULL,"
            " event_type TEXT NOT NULL,"
            " subject TEXT,"
            " session_id TEXT,"
            " project_root TEXT,"
            " cost_tokens INTEGER DEFAULT 0,"
            " latency_ms INTEGER DEFAULT 0,"
            " acted_on INTEGER,"
            " prevented_error INTEGER,"
            " ignored INTEGER,"
            " block_justified INTEGER,"
            " was_visible INTEGER,"
            " meta_json TEXT"
            ")"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_ledger_type_ts "
            "ON ledger_events(event_type, ts_epoch)"
        )

        # A1-1: create the vec0 virtual table when sqlite-vec is loadable.
        # FLOAT[768] matches the FastEmbed nomic-embed-text-v1.5-Q output
        # used by memory/embeddings.py. If a legacy FLOAT[384] table is
        # present from a pre-2.8 install, drop it so the new schema can be
        # created — vectors are rebuilt on demand by backfill_obs_vectors.
        if _maybe_load_sqlite_vec(conn):
            try:
                row = conn.execute(
                    "SELECT sql FROM sqlite_master "
                    "WHERE type='table' AND name='obs_vectors'"
                ).fetchone()
                if row and "FLOAT[384]" in (row[0] or ""):
                    _logger.warning(
                        "[token-savior:memory] legacy FLOAT[384] obs_vectors "
                        "detected; dropping to rebuild in FLOAT[768].",
                    )
                    conn.execute("DROP TABLE obs_vectors")
                conn.execute(
                    "CREATE VIRTUAL TABLE IF NOT EXISTS obs_vectors USING vec0("
                    "  obs_id INTEGER PRIMARY KEY,"
                    "  embedding FLOAT[768]"
                    ")"
                )
            except sqlite3.OperationalError as exc:
                _logger.warning(
                    "[token-savior:memory] obs_vectors create failed (%s); "
                    "vector search disabled.", exc,
                )

            # Feature 1: symbol-level vector index for search_codebase(semantic=True).
            # vec0 demands INTEGER primary keys and a dense FLOAT[N] column, so
            # symbol metadata (project + key) lives in a sibling mapping table
            # and the vec row is joined via symbol_id.
            try:
                conn.execute(
                    "CREATE TABLE IF NOT EXISTS symbols("
                    "  id INTEGER PRIMARY KEY AUTOINCREMENT,"
                    "  project_root TEXT NOT NULL,"
                    "  symbol_key TEXT NOT NULL,"
                    "  file_path TEXT NOT NULL,"
                    "  lineno INTEGER NOT NULL,"
                    "  kind TEXT NOT NULL,"
                    "  signature TEXT,"
                    "  docstring_head TEXT,"
                    "  content_hash TEXT NOT NULL,"
                    "  updated_at_epoch INTEGER NOT NULL,"
                    "  UNIQUE(project_root, symbol_key)"
                    ")"
                )
                conn.execute(
                    "CREATE INDEX IF NOT EXISTS idx_symbols_proj "
                    "ON symbols(project_root)"
                )
                conn.execute(
                    "CREATE VIRTUAL TABLE IF NOT EXISTS symbol_vectors USING vec0("
                    "  symbol_id INTEGER PRIMARY KEY,"
                    "  embedding FLOAT[768]"
                    ")"
                )
            except sqlite3.OperationalError as exc:
                _logger.warning(
                    "[token-savior:memory] symbol_vectors create failed (%s); "
                    "semantic code search disabled.", exc,
                )

        conn.commit()
    finally:
        conn.close()

    _migrated_paths.add(path_str)


def get_db(db_path: Path | None = None) -> sqlite3.Connection:
    """Open a WAL-mode SQLite connection. Migrations run once per path.

    If sqlite-vec is installed, the extension is loaded on every new
    connection so that the obs_vectors vec0 table is queryable. Missing
    extension is silent (warning emitted once, see _maybe_load_sqlite_vec).
    """
    path = db_path or chemin_memoire()
    run_migrations(path)
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    _activer_wal(conn)
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA synchronous = NORMAL")
    if VECTOR_SEARCH_AVAILABLE:
        _maybe_load_sqlite_vec(conn)
    return conn


@contextmanager
def db_session(db_path: Path | None = None):
    """Context manager for SQLite connections — guarantees close on exit."""
    conn = get_db(db_path)
    try:
        yield conn
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Shared utils (epoch/json/hash/text)
# ---------------------------------------------------------------------------


def _now_iso() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _now_epoch() -> int:
    return int(time.time())


def _json_dumps(value: list | dict | None) -> str | None:
    if value is None:
        return None
    return json.dumps(value, ensure_ascii=False)


def observation_hash(project_root: str, title: str, content: str) -> str:
    """Legacy composite hash — kept for reasoning/distillation call sites that
    key on derived fields other than observation content. Do not use for
    observation dedup; use :func:`content_hash` instead."""
    raw = f"{project_root}:{title}:{content}"
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


def content_hash(content: str | None) -> str | None:
    """SHA-256 of normalized observation content (``strip().lower()``).

    Used as the canonical dedup key stored in ``observations.content_hash``.
    Returns ``None`` for empty/whitespace-only content so dedup skips rather
    than collapsing every blank row onto one hash.
    """
    if content is None:
        return None
    norm = content.strip().lower()
    if not norm:
        return None
    return hashlib.sha256(norm.encode("utf-8")).hexdigest()


_PRIVATE_RE = re.compile(r"<private>.*?</private>", re.IGNORECASE | re.DOTALL)


def strip_private(text: str | None) -> str | None:
    """Replace <private>...</private> spans with [PRIVATE]."""
    if text is None:
        return None
    return _PRIVATE_RE.sub("[PRIVATE]", text).strip()


def relative_age(epoch: int | None) -> str:
    """Readable relative age ('3d ago', '2w ago', ...) from a unix epoch."""
    if not epoch:
        return "?"
    delta = int(time.time()) - int(epoch)
    if delta < 0:
        return "just now"
    if delta < 3600:
        return f"{max(1, delta // 60)}m ago"
    if delta < 86400:
        return f"{delta // 3600}h ago"
    if delta < 7 * 86400:
        return f"{delta // 86400}d ago"
    if delta < 30 * 86400:
        return f"{delta // (7 * 86400)}w ago"
    if delta < 365 * 86400:
        return f"{delta // (30 * 86400)}mo ago"
    return f"{delta // (365 * 86400)}y ago"


def _fts5_safe_query(text: str, max_tokens: int = 12) -> str:
    """Build an FTS5 OR query from alphanumeric tokens (>=3 chars)."""
    toks = re.findall(r"[A-Za-zÀ-ÿ0-9_]{3,}", text or "")
    stop = {
        "que", "qui", "les", "des", "une", "aux", "pour", "avec", "dans",
        "sur", "par", "est", "sont", "the", "and", "for", "with", "this",
        "that", "you", "are", "how", "what", "can", "will", "from",
    }
    toks = [t for t in toks if t.lower() not in stop][:max_tokens]
    return " OR ".join(f'"{t}"' for t in toks)
