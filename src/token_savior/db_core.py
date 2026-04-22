"""Token Savior Memory Engine — core DB primitives and shared utils.

Owns: schema/migrations, connection factory, small epoch/json/hash helpers.
Kept deliberately dependency-free so higher-level memory modules can import
from here without pulling the full memory_db facade.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import sqlite3
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

MEMORY_DB_PATH = Path.home() / ".local" / "share" / "token-savior" / "memory.db"

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
    except (sqlite3.OperationalError, AttributeError, Exception) as exc:
        if not _vector_warning_emitted:
            _logger.warning(
                "[token-savior:memory] sqlite-vec load failed (%s); "
                "vector search disabled.", exc,
            )
            _vector_warning_emitted = True
        return False


def run_migrations(db_path: Path | str | None = None) -> None:
    """Apply schema + ALTER TABLE migrations once per database path.

    Idempotent. Called explicitly at MCP startup to keep get_db() hot-path
    free of schema inspection; also invoked lazily from get_db() as a
    safety net (e.g. for tests that patch MEMORY_DB_PATH).
    """
    path = Path(db_path) if db_path else MEMORY_DB_PATH
    path_str = str(path)
    if path_str in _migrated_paths:
        return

    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path_str)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA foreign_keys = ON")

    try:
        pre_cols = [r[1] for r in conn.execute("PRAGMA table_info(user_prompts)").fetchall()]
        if pre_cols and "project_root" not in pre_cols:
            conn.execute("ALTER TABLE user_prompts ADD COLUMN project_root TEXT")

        schema_sql = _SCHEMA_PATH.read_text(encoding="utf-8")
        conn.executescript(schema_sql)

        sess_cols = [r[1] for r in conn.execute("PRAGMA table_info(sessions)").fetchall()]
        if "end_type" not in sess_cols:
            conn.execute("ALTER TABLE sessions ADD COLUMN end_type TEXT")
        if "tokens_injected" not in sess_cols:
            conn.execute("ALTER TABLE sessions ADD COLUMN tokens_injected INTEGER DEFAULT 0")
        if "tokens_saved_est" not in sess_cols:
            conn.execute("ALTER TABLE sessions ADD COLUMN tokens_saved_est INTEGER DEFAULT 0")

        obs_cols = [r[1] for r in conn.execute("PRAGMA table_info(observations)").fetchall()]
        if "decay_immune" not in obs_cols:
            conn.execute("ALTER TABLE observations ADD COLUMN decay_immune INTEGER NOT NULL DEFAULT 0")
        if "last_accessed_epoch" not in obs_cols:
            conn.execute("ALTER TABLE observations ADD COLUMN last_accessed_epoch INTEGER")
        if "is_global" not in obs_cols:
            conn.execute("ALTER TABLE observations ADD COLUMN is_global INTEGER NOT NULL DEFAULT 0")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_obs_global ON observations(is_global)")
        if "context" not in obs_cols:
            conn.execute("ALTER TABLE observations ADD COLUMN context TEXT")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_obs_context ON observations(context)")
        if "expires_at_epoch" not in obs_cols:
            conn.execute("ALTER TABLE observations ADD COLUMN expires_at_epoch INTEGER")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_obs_expires ON observations(expires_at_epoch)")
        if "agent_id" not in obs_cols:
            conn.execute("ALTER TABLE observations ADD COLUMN agent_id TEXT")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_obs_agent ON observations(agent_id)")
        # A5: narrative/facts/concepts — non-destructive column adds.
        # The FTS5 virtual table rebuild below picks them up.
        obs_cols_a5 = {"narrative", "facts", "concepts"} - set(obs_cols)
        for col in ("narrative", "facts", "concepts"):
            if col in obs_cols_a5:
                conn.execute(f"ALTER TABLE observations ADD COLUMN {col} TEXT")

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
    path = db_path or MEMORY_DB_PATH
    run_migrations(path)
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode = WAL")
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
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


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
