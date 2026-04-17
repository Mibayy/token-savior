"""Token Savior Memory Engine — core DB primitives and shared utils.

Owns: schema/migrations, connection factory, small epoch/json/hash helpers.
Kept deliberately dependency-free so higher-level memory modules can import
from here without pulling the full memory_db facade.
"""

from __future__ import annotations

import hashlib
import json
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

        conn.commit()
    finally:
        conn.close()

    _migrated_paths.add(path_str)


def get_db(db_path: Path | None = None) -> sqlite3.Connection:
    """Open a WAL-mode SQLite connection. Migrations run once per path."""
    path = db_path or MEMORY_DB_PATH
    run_migrations(path)
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA synchronous = NORMAL")
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
