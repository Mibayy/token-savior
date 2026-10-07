"""Hook SessionStart : budget du bloc Memory Index (bornes, traces auto, types)."""

from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path
from unittest.mock import patch

import pytest

from token_savior import memory_db

REPO = Path(__file__).resolve().parent.parent
HOOK = REPO / "hooks" / "memory-session-start.sh"
PROJECT = "/tmp/test-project-session-index"


def _insert(conn, oid, type_, title, *, days=1, importance=5):
    now = int(time.time()) - days * 86400
    conn.execute(
        "INSERT INTO observations (id, project_root, type, title, content, importance, "
        "content_hash, created_at, created_at_epoch, updated_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?)",
        (oid, PROJECT, type_, title, "c", importance, f"h{oid}", "x", now, "x"),
    )


@pytest.fixture
def seeded(tmp_path: Path):
    db_path = tmp_path / "memory.db"
    with patch.object(memory_db, "MEMORY_DB_PATH", db_path):
        conn = memory_db.get_db()
        oid = 1
        for i in range(20):  # 20 guardrails : bien plus que la borne
            _insert(conn, oid, "guardrail", f"Garde-fou numero {i}", days=i % 5)
            oid += 1
        for t in ("chmod 600 sur la cle", "restart du service", "start nginx",
                  "stop du bot", "enable timer", "reload nginx", "chown root"):
            _insert(conn, oid, "guardrail", t, importance=10)
            oid += 1
        _insert(conn, oid, "warning", "Piege warning", importance=9)
        oid += 1
        _insert(conn, oid, "decision", "Choix decision", importance=9)
        oid += 1
        for t in ("convention", "note", "command", "bugfix"):
            _insert(conn, oid, t, f"Hors type {t}", importance=10)
            oid += 1
        conn.commit()
        conn.close()
    return tmp_path


def _run(data: Path, **env) -> str:
    e = {
        **os.environ,
        "HOME": str(data),
        "TOKEN_SAVIOR_DATA_DIR": str(data),
        "XDG_STATE_HOME": str(data / "state"),
        "XDG_CACHE_HOME": str(data / "cache"),
        "TOKEN_SAVIOR_PYTHON": sys.executable,
        "TOKEN_SAVIOR_SRC": str(REPO / "src"),
        "CLAUDE_PROJECT_ROOT": PROJECT,
        "TS_HOOK_MINIMAL": "1",
    }
    for k in list(e):
        if k.startswith("TS_SESSION_"):
            del e[k]
    e.update(env)
    out = subprocess.run(
        ["bash", str(HOOK)], input='{"session_id":"fake"}', capture_output=True,
        text=True, env=e, timeout=120, check=True,
    ).stdout
    # un second appel identique renverrait « unchanged » : on repart propre
    (data / "last_injected_state.json").unlink(missing_ok=True)
    return out


def _lines(out: str) -> list[str]:
    return [ln for ln in out.splitlines() if ln.lstrip().startswith("#") and "[" in ln
            and not ln.startswith("###")]


def test_default_caps_at_12_observations(seeded):
    out = _run(seeded)
    assert len(_lines(out)) == 12
    assert "12 obs" in out


def test_env_overrides_the_cap(seeded):
    assert len(_lines(_run(seeded, TS_SESSION_INDEX_MAX="5"))) == 5


def test_automatic_traces_are_excluded(seeded):
    out = _run(seeded, TS_SESSION_INDEX_MAX="100")
    for word in ("chmod", "restart", "start nginx", "stop du", "enable", "reload", "chown"):
        assert word not in out
    assert "Garde-fou numero 0" in out


def test_only_guardrail_warning_decision(seeded):
    out = _run(seeded, TS_SESSION_INDEX_MAX="100")
    types = {ln.split("[")[1].split("]")[0] for ln in _lines(out)}
    assert types == {"guardrail", "warning", "decision"}
    assert "Hors type" not in out


def test_types_are_configurable(seeded):
    out = _run(seeded, TS_SESSION_INDEX_MAX="100", TS_SESSION_INDEX_TYPES="decision")
    types = {ln.split("[")[1].split("]")[0] for ln in _lines(out)}
    assert types == {"decision"}


def test_recent_observations_come_first(seeded):
    # une vieille observation a fort score ne doit pas passer devant les recentes
    db_path = seeded / "memory.db"
    with patch.object(memory_db, "MEMORY_DB_PATH", db_path):
        conn = memory_db.get_db()
        _insert(conn, 900, "guardrail", "Vieille regle", days=60, importance=10)
        conn.commit()
        conn.close()
    out = _run(seeded, TS_SESSION_INDEX_MAX="12")
    assert "Vieille regle" not in out


def test_long_lines_are_dropped_when_limit_is_zero(seeded):
    out = _run(seeded, TS_SESSION_LINE_MAX="0")
    assert "Continuity" not in out
    assert "Tool Capture" not in out
