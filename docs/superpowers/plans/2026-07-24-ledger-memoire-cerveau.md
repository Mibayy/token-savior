# Ledger d'efficacité (Phase 1 — Mémoire cerveau) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Construire le `ledger` : une table SQLite qui enregistre tout ce que le système de mémoire fait, avec capture automatique des ratés (corrections de Louis) et calcul de valeur nette pour détecter les mécanismes contre-productifs.

**Architecture:** Un module `ledger.py` jumeau de `tool_capture.py` (même DB `db_core.get_db()`, même style d'API put/query/aggregate). La table `ledger_events` est créée idempotemment dans `db_core.run_migrations()`. Un détecteur de corrections pur alimente le hook `memory-userprompt.sh`. La valeur nette s'agrège en pur Python.

**Tech Stack:** Python 3.12, sqlite3 via `token_savior.db_core`, pytest. Aucune nouvelle dépendance.

## Global Constraints

- Connexion DB : toujours `db_core.get_db()` (jamais ouvrir sqlite3 en direct). Import : `from token_savior import db_core`. Copié verbatim du pattern `tool_capture.py`.
- **Isolation DB en test (mécanisme confirmé, `db_core.py:20,285`)** : `get_db()` utilise le global `db_core.MEMORY_DB_PATH` (pas d'env var). Les tests ciblent une DB temp via `monkeypatch.setattr(db_core, "MEMORY_DB_PATH", tmp_path / "m.sqlite")` puis `db_core.run_migrations(<ce path>)`. C'est le pattern de la fixture `isolated_db` de `tests/test_tool_capture.py:11`. Réutiliser cette fixture (copiée en tête de chaque fichier de test ci-dessous).
- Booléens stockés en `INTEGER` (0 / 1 / NULL), idiome du codebase.
- Vocabulaire `event_type` (code, ASCII snake_case) : `injection` | `soft_remind` | `hard_block` | `silence` | `miss` | `false_positive`. Mapping vers le spec FR : miss=raté, false_positive=faux positif, soft_remind=rappel_soft, hard_block=blocage_hard.
- Kill-switch : tout hook doit court-circuiter si `TS_MEMORY_DISABLE=1` (pattern existant dans `hooks/memory-posttooluse.sh`).
- Édition de code `.py` existant : utiliser `replace_symbol_source` / `insert_near_symbol` (TS), puis `reindex`. Nouveaux fichiers : `Write` autorisé.
- Ne jamais laisser une exception d'un hook casser la session : toujours try/except large côté hook, `exit 0`.

---

### Task 1: Table `ledger_events` (migration)

**Files:**
- Modify: `src/token_savior/db_core.py` (fonction `run_migrations`, ~L79-260)
- Test: `tests/test_ledger_schema.py`

**Interfaces:**
- Consumes: `db_core.get_db(db_path)`, `db_core.run_migrations(db_path)` (existants)
- Produces: table `ledger_events` avec colonnes : `id INTEGER PRIMARY KEY AUTOINCREMENT`, `ts_epoch INTEGER NOT NULL`, `event_type TEXT NOT NULL`, `subject TEXT`, `session_id TEXT`, `project_root TEXT`, `cost_tokens INTEGER DEFAULT 0`, `latency_ms INTEGER DEFAULT 0`, `acted_on INTEGER`, `prevented_error INTEGER`, `ignored INTEGER`, `block_justified INTEGER`, `was_visible INTEGER`, `meta_json TEXT`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_ledger_schema.py
from pathlib import Path
from token_savior import db_core


def test_ledger_events_table_created(tmp_path):
    db = tmp_path / "m.sqlite"
    db_core.run_migrations(db)
    conn = db_core.get_db(db)
    cols = {r[1] for r in conn.execute("PRAGMA table_info(ledger_events)")}
    conn.close()
    assert {"id", "ts_epoch", "event_type", "subject", "cost_tokens",
            "acted_on", "was_visible", "meta_json"} <= cols
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /root/token-savior && /root/.local/token-savior-venv/bin/python -m pytest tests/test_ledger_schema.py -v`
Expected: FAIL — `PRAGMA table_info(ledger_events)` returns no rows, set comparison fails.

- [ ] **Step 3: Add the CREATE TABLE block in `run_migrations`**

Insert alongside the existing `CREATE TABLE IF NOT EXISTS adaptive_lattice (...)` / `consistency_scores (...)` blocks (same idempotent style, same connection/cursor already open in `run_migrations`). Use `insert_near_symbol` on `run_migrations` or `replace_symbol_source`. The SQL:

```python
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
```

After editing, run `reindex` (TS) so the symbol index reflects the change.

- [ ] **Step 4: Run test to verify it passes**

Run: `cd /root/token-savior && /root/.local/token-savior-venv/bin/python -m pytest tests/test_ledger_schema.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/token_savior/db_core.py tests/test_ledger_schema.py
git commit -m "feat(ledger): ledger_events table + index in run_migrations"
```

---

### Task 2: `ledger_put` — écrire un événement

**Files:**
- Create: `src/token_savior/memory/ledger.py`
- Test: `tests/test_ledger_put.py`

**Interfaces:**
- Consumes: `db_core.get_db()`, table `ledger_events` (Task 1)
- Produces:
  `ledger_put(event_type: str, *, subject: str | None = None, session_id: str | None = None, project_root: str | None = None, cost_tokens: int = 0, latency_ms: int = 0, outcome: dict[str, Any] | None = None, meta: dict[str, Any] | None = None) -> dict[str, Any]` returning `{"id": int, "uri": "ts://ledger/{id}"}`.
  `outcome` keys (all optional, bool→int): `acted_on`, `prevented_error`, `ignored`, `block_justified`, `was_visible`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_ledger_put.py
import pytest
from token_savior import db_core
from token_savior.memory import ledger


@pytest.fixture
def isolated_db(monkeypatch, tmp_path):
    db_path = tmp_path / "m.sqlite"
    monkeypatch.setattr(db_core, "MEMORY_DB_PATH", db_path)
    db_core.run_migrations(db_path)
    yield db_path


def test_ledger_put_inserts_row(isolated_db):
    res = ledger.ledger_put("miss", subject="ts://obs/42",
                            meta={"phrase": "je t'ai déjà dit"},
                            outcome={"was_visible": True})
    assert res["id"] > 0
    assert res["uri"] == f"ts://ledger/{res['id']}"

    conn = db_core.get_db()  # picks up patched MEMORY_DB_PATH
    row = conn.execute(
        "SELECT event_type, subject, was_visible FROM ledger_events WHERE id=?",
        (res["id"],)).fetchone()
    conn.close()
    assert row[0] == "miss"
    assert row[1] == "ts://obs/42"
    assert row[2] == 1  # bool True -> int 1
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /root/token-savior && /root/.local/token-savior-venv/bin/python -m pytest tests/test_ledger_put.py -v`
Expected: FAIL — `ModuleNotFoundError: token_savior.memory.ledger`

- [ ] **Step 3: Write `ledger.py` with `ledger_put`**

```python
# src/token_savior/memory/ledger.py
"""Effectiveness ledger — records every action the memory system takes.

Twin of tool_capture.py: same DB (db_core.get_db), same put/query/aggregate
shape. Rows let the reflection loop compute net value per rule/mechanism and
flag anything counterproductive (net < 0).
"""
from __future__ import annotations

import json
import sqlite3
import sys
import time
from typing import Any

from token_savior import db_core

EVENT_TYPES = {
    "injection", "soft_remind", "hard_block", "silence", "miss", "false_positive",
}

_OUTCOME_KEYS = ("acted_on", "prevented_error", "ignored", "block_justified", "was_visible")


def _b(v: Any) -> int | None:
    if v is None:
        return None
    return 1 if v else 0


def ledger_put(
    event_type: str,
    *,
    subject: str | None = None,
    session_id: str | None = None,
    project_root: str | None = None,
    cost_tokens: int = 0,
    latency_ms: int = 0,
    outcome: dict[str, Any] | None = None,
    meta: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Persist one ledger event. Returns {id, uri}."""
    outcome = outcome or {}
    vals = {k: _b(outcome.get(k)) for k in _OUTCOME_KEYS}
    epoch = int(time.time())
    try:
        conn = db_core.get_db()
        cur = conn.execute(
            "INSERT INTO ledger_events "
            "(ts_epoch, event_type, subject, session_id, project_root, "
            " cost_tokens, latency_ms, acted_on, prevented_error, ignored, "
            " block_justified, was_visible, meta_json) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                epoch, event_type, subject, session_id, project_root,
                int(cost_tokens), int(latency_ms),
                vals["acted_on"], vals["prevented_error"], vals["ignored"],
                vals["block_justified"], vals["was_visible"],
                json.dumps(meta) if meta else None,
            ),
        )
        row_id = cur.lastrowid
        conn.commit()
        conn.close()
    except sqlite3.Error as exc:
        print(f"[token-savior:ledger] put error: {exc}", file=sys.stderr)
        return {"id": None, "uri": None, "error": str(exc)}
    return {"id": row_id, "uri": f"ts://ledger/{row_id}"}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd /root/token-savior && /root/.local/token-savior-venv/bin/python -m pytest tests/test_ledger_put.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/token_savior/memory/ledger.py tests/test_ledger_put.py
git commit -m "feat(ledger): ledger_put writes events to ledger_events"
```

---

### Task 3: `ledger_query` — relire les événements

**Files:**
- Modify: `src/token_savior/memory/ledger.py`
- Test: `tests/test_ledger_query.py`

**Interfaces:**
- Consumes: `ledger_put` (Task 2)
- Produces: `ledger_query(*, event_type: str | None = None, session_id: str | None = None, since_epoch: int | None = None, limit: int = 100) -> list[dict[str, Any]]` — rows newest-first, each a dict with all columns + `outcome` reassembled as a sub-dict.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_ledger_query.py
import pytest
from token_savior import db_core
from token_savior.memory import ledger


@pytest.fixture
def isolated_db(monkeypatch, tmp_path):
    db_path = tmp_path / "m.sqlite"
    monkeypatch.setattr(db_core, "MEMORY_DB_PATH", db_path)
    db_core.run_migrations(db_path)
    yield db_path


def test_query_filters_by_type(isolated_db):
    ledger.ledger_put("miss", subject="a")
    ledger.ledger_put("injection", subject="b", cost_tokens=120)
    ledger.ledger_put("miss", subject="c")

    misses = ledger.ledger_query(event_type="miss")
    assert len(misses) == 2
    assert {m["subject"] for m in misses} == {"a", "c"}
    assert all(m["event_type"] == "miss" for m in misses)

    inj = ledger.ledger_query(event_type="injection")
    assert inj[0]["cost_tokens"] == 120
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /root/token-savior && /root/.local/token-savior-venv/bin/python -m pytest tests/test_ledger_query.py -v`
Expected: FAIL — `AttributeError: module ... has no attribute 'ledger_query'`

- [ ] **Step 3: Add `ledger_query` to `ledger.py`**

Use `insert_near_symbol` after `ledger_put`.

```python
def ledger_query(
    *,
    event_type: str | None = None,
    session_id: str | None = None,
    since_epoch: int | None = None,
    limit: int = 100,
) -> list[dict[str, Any]]:
    """Return ledger rows newest-first, filtered."""
    clauses: list[str] = []
    params: list[Any] = []
    if event_type is not None:
        clauses.append("event_type = ?")
        params.append(event_type)
    if session_id is not None:
        clauses.append("session_id = ?")
        params.append(session_id)
    if since_epoch is not None:
        clauses.append("ts_epoch >= ?")
        params.append(int(since_epoch))
    where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
    sql = (
        "SELECT id, ts_epoch, event_type, subject, session_id, project_root, "
        " cost_tokens, latency_ms, acted_on, prevented_error, ignored, "
        " block_justified, was_visible, meta_json "
        "FROM ledger_events" + where + " ORDER BY ts_epoch DESC, id DESC LIMIT ?"
    )
    params.append(int(limit))
    conn = db_core.get_db()
    rows = conn.execute(sql, params).fetchall()
    conn.close()
    out: list[dict[str, Any]] = []
    for r in rows:
        out.append({
            "id": r[0], "ts_epoch": r[1], "event_type": r[2], "subject": r[3],
            "session_id": r[4], "project_root": r[5], "cost_tokens": r[6],
            "latency_ms": r[7],
            "outcome": {
                "acted_on": r[8], "prevented_error": r[9], "ignored": r[10],
                "block_justified": r[11], "was_visible": r[12],
            },
            "meta": json.loads(r[13]) if r[13] else None,
        })
    return out
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd /root/token-savior && /root/.local/token-savior-venv/bin/python -m pytest tests/test_ledger_query.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/token_savior/memory/ledger.py tests/test_ledger_query.py
git commit -m "feat(ledger): ledger_query with filters"
```

---

### Task 4: `detect_correction` — repérer un rappel de Louis

**Files:**
- Modify: `src/token_savior/memory/ledger.py`
- Test: `tests/test_ledger_detect.py`

**Interfaces:**
- Consumes: rien
- Produces: `detect_correction(text: str) -> str | None` — returns the matched trigger phrase (lowercased) or `None`. Pure function, no DB.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_ledger_detect.py
import pytest
from token_savior.memory import ledger


@pytest.mark.parametrize("text", [
    "je t'ai déjà dit de regarder les logs",
    "Tu devais vérifier avant de push",
    "je te rappelle qu'on utilise Token Savior",
    "combien de fois je dois te le dire",
    "encore une fois tu as oublié",
])
def test_detects_corrections(text):
    assert ledger.detect_correction(text) is not None


@pytest.mark.parametrize("text", [
    "peux-tu ajouter une fonction ici",
    "installe hermes sur le vps",
    "",
    "merci c'est parfait",
])
def test_ignores_non_corrections(text):
    assert ledger.detect_correction(text) is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /root/token-savior && /root/.local/token-savior-venv/bin/python -m pytest tests/test_ledger_detect.py -v`
Expected: FAIL — `AttributeError: ... 'detect_correction'`

- [ ] **Step 3: Add `detect_correction` to `ledger.py`**

Add `import re` at the top of the module (near the other imports). Use `insert_near_symbol`.

```python
_CORRECTION_PATTERNS = [
    r"je t'?ai déjà dit",
    r"je t'?avais dit",
    r"tu devais\b",
    r"je te rappelle",
    r"combien de fois",
    r"encore une fois",
    r"comme (?:je t'?ai dit|d'?habitude)",
    r"je te l'?ai dit",
]
_CORRECTION_RE = re.compile("|".join(_CORRECTION_PATTERNS), re.IGNORECASE)


def detect_correction(text: str) -> str | None:
    """Return the matched correction phrase (lowercased) or None."""
    if not text:
        return None
    m = _CORRECTION_RE.search(text)
    return m.group(0).lower() if m else None
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd /root/token-savior && /root/.local/token-savior-venv/bin/python -m pytest tests/test_ledger_detect.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/token_savior/memory/ledger.py tests/test_ledger_detect.py
git commit -m "feat(ledger): detect_correction heuristic (FR trigger phrases)"
```

---

### Task 5: `record_from_userprompt` — capturer un raté depuis un prompt

**Files:**
- Modify: `src/token_savior/memory/ledger.py`
- Test: `tests/test_ledger_record.py`

**Interfaces:**
- Consumes: `detect_correction` (Task 4), `ledger_put` (Task 2)
- Produces: `record_from_userprompt(payload: dict[str, Any], *, session_id: str | None = None, project_root: str | None = None) -> dict[str, Any] | None` — reads the user text from `payload["prompt"]` (fallback `payload["user_message"]`), and if a correction is detected, writes a `miss` event with `meta={"phrase": ..., "text": text[:500]}`. Returns the `ledger_put` result, or `None` if no correction.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_ledger_record.py
import pytest
from token_savior import db_core
from token_savior.memory import ledger


@pytest.fixture
def isolated_db(monkeypatch, tmp_path):
    db_path = tmp_path / "m.sqlite"
    monkeypatch.setattr(db_core, "MEMORY_DB_PATH", db_path)
    db_core.run_migrations(db_path)
    yield db_path


def test_record_writes_miss_on_correction(isolated_db):
    res = ledger.record_from_userprompt(
        {"prompt": "je t'ai déjà dit de regarder les logs"},
        session_id="s1")
    assert res is not None and res["id"] > 0

    misses = ledger.ledger_query(event_type="miss")
    assert len(misses) == 1
    assert misses[0]["meta"]["phrase"] == "je t'ai déjà dit"
    assert misses[0]["session_id"] == "s1"


def test_record_returns_none_without_correction(isolated_db):
    res = ledger.record_from_userprompt({"prompt": "ajoute une fonction"})
    assert res is None
    assert ledger.ledger_query(event_type="miss") == []
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /root/token-savior && /root/.local/token-savior-venv/bin/python -m pytest tests/test_ledger_record.py -v`
Expected: FAIL — `AttributeError: ... 'record_from_userprompt'`

- [ ] **Step 3: Add `record_from_userprompt` to `ledger.py`**

```python
def record_from_userprompt(
    payload: dict[str, Any],
    *,
    session_id: str | None = None,
    project_root: str | None = None,
) -> dict[str, Any] | None:
    """If the user text is a correction, log a 'miss' event. Else None."""
    text = (payload.get("prompt") or payload.get("user_message") or "")
    phrase = detect_correction(text)
    if not phrase:
        return None
    return ledger_put(
        "miss",
        session_id=session_id,
        project_root=project_root,
        meta={"phrase": phrase, "text": text[:500]},
    )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd /root/token-savior && /root/.local/token-savior-venv/bin/python -m pytest tests/test_ledger_record.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/token_savior/memory/ledger.py tests/test_ledger_record.py
git commit -m "feat(ledger): record_from_userprompt captures misses"
```

---

### Task 6: `ledger_net_value` — valeur nette + contre-productivité

**Files:**
- Modify: `src/token_savior/memory/ledger.py`
- Test: `tests/test_ledger_netvalue.py`

**Interfaces:**
- Consumes: `ledger_query` (Task 3)
- Produces: `ledger_net_value(*, since_epoch: int | None = None) -> dict[str, Any]` returning
  `{"by_subject": {subject: {"benefit": int, "cost": int, "net": int}}, "totals": {"benefit": int, "cost": int, "net": int}, "counterproductive": [subject, ...]}`.
  Rules (honest metric — count prevented errors and acted-on reminders, not activity):
  - benefit += 1 per event with `prevented_error == 1`
  - benefit += 1 per `soft_remind` with `acted_on == 1`
  - cost += `cost_tokens` (all events)
  - cost += 1 per `false_positive` event
  - cost += 1 per `hard_block` with `block_justified == 0`
  - net = benefit − cost ; a subject with net < 0 is `counterproductive`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_ledger_netvalue.py
import pytest
from token_savior import db_core
from token_savior.memory import ledger


@pytest.fixture
def isolated_db(monkeypatch, tmp_path):
    db_path = tmp_path / "m.sqlite"
    monkeypatch.setattr(db_core, "MEMORY_DB_PATH", db_path)
    db_core.run_migrations(db_path)
    yield db_path


def test_net_value_flags_counterproductive(isolated_db):
    # rule "good": prevented a real error once, no cost
    ledger.ledger_put("hard_block", subject="good",
                      outcome={"block_justified": True, "prevented_error": True})
    # rule "noise": 2 false positives, never prevented anything
    ledger.ledger_put("false_positive", subject="noise")
    ledger.ledger_put("false_positive", subject="noise")

    nv = ledger.ledger_net_value()
    assert nv["by_subject"]["good"]["net"] == 1
    assert nv["by_subject"]["noise"]["net"] == -2
    assert "noise" in nv["counterproductive"]
    assert "good" not in nv["counterproductive"]
    assert nv["totals"]["net"] == -1
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /root/token-savior && /root/.local/token-savior-venv/bin/python -m pytest tests/test_ledger_netvalue.py -v`
Expected: FAIL — `AttributeError: ... 'ledger_net_value'`

- [ ] **Step 3: Add `ledger_net_value` to `ledger.py`**

```python
def ledger_net_value(*, since_epoch: int | None = None) -> dict[str, Any]:
    """Aggregate benefit/cost/net per subject. Honest metric: counts real
    errors prevented and acted-on reminders, not raw activity."""
    rows = ledger_query(since_epoch=since_epoch, limit=1_000_000)
    agg: dict[str, dict[str, int]] = {}

    def bucket(subj: str | None) -> dict[str, int]:
        key = subj or "(none)"
        return agg.setdefault(key, {"benefit": 0, "cost": 0, "net": 0})

    for r in rows:
        b = bucket(r["subject"])
        o = r["outcome"]
        b["cost"] += int(r["cost_tokens"] or 0)
        if o.get("prevented_error") == 1:
            b["benefit"] += 1
        if r["event_type"] == "soft_remind" and o.get("acted_on") == 1:
            b["benefit"] += 1
        if r["event_type"] == "false_positive":
            b["cost"] += 1
        if r["event_type"] == "hard_block" and o.get("block_justified") == 0:
            b["cost"] += 1

    totals = {"benefit": 0, "cost": 0, "net": 0}
    counterproductive: list[str] = []
    for subj, b in agg.items():
        b["net"] = b["benefit"] - b["cost"]
        totals["benefit"] += b["benefit"]
        totals["cost"] += b["cost"]
        if b["net"] < 0:
            counterproductive.append(subj)
    totals["net"] = totals["benefit"] - totals["cost"]
    return {"by_subject": agg, "totals": totals, "counterproductive": counterproductive}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd /root/token-savior && /root/.local/token-savior-venv/bin/python -m pytest tests/test_ledger_netvalue.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/token_savior/memory/ledger.py tests/test_ledger_netvalue.py
git commit -m "feat(ledger): ledger_net_value + counterproductive flagging"
```

---

### Task 7: Câbler la capture des ratés dans le hook `memory-userprompt.sh`

**Files:**
- Modify: `hooks/memory-userprompt.sh`
- Test: `tests/test_ledger_hook_entrypoint.py`

**Interfaces:**
- Consumes: `record_from_userprompt` (Task 5)
- Produces: the hook calls a Python entrypoint that reads the hook JSON payload from stdin and calls `record_from_userprompt`. No change to what the hook injects (recall stays as-is); we only ADD the miss capture, non-blocking.

- [ ] **Step 1: Write the failing test (entrypoint tested in-process)**

The entrypoint reads stdin and uses `db_core.MEMORY_DB_PATH`, so we monkeypatch both — same pattern as `tests/test_tool_capture_hybrid.py:72` (`monkeypatch.setattr("sys.stdin", io.StringIO(...))`). No subprocess (a subprocess can't see the patched temp DB, since selection is by module global, not env var).

```python
# tests/test_ledger_hook_entrypoint.py
import io
import json
import pytest
from token_savior import db_core
from token_savior.memory import ledger, ledger_hook


@pytest.fixture
def isolated_db(monkeypatch, tmp_path):
    db_path = tmp_path / "m.sqlite"
    monkeypatch.setattr(db_core, "MEMORY_DB_PATH", db_path)
    db_core.run_migrations(db_path)
    yield db_path


def test_entrypoint_records_miss(isolated_db, monkeypatch):
    payload = {"prompt": "je t'ai déjà dit de checker les logs",
               "session_id": "sX"}
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(payload)))

    rc = ledger_hook.main()
    assert rc == 0  # never breaks the session

    misses = ledger.ledger_query(event_type="miss")
    assert len(misses) == 1 and misses[0]["session_id"] == "sX"


def test_entrypoint_survives_garbage_stdin(isolated_db, monkeypatch):
    monkeypatch.setattr("sys.stdin", io.StringIO("not json at all"))
    assert ledger_hook.main() == 0  # returns 0, writes nothing
    assert ledger.ledger_query(event_type="miss") == []
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /root/token-savior && /root/.local/token-savior-venv/bin/python -m pytest tests/test_ledger_hook_entrypoint.py -v`
Expected: FAIL — `No module named token_savior.memory.ledger_hook`

- [ ] **Step 3: Create the entrypoint module `ledger_hook.py`**

```python
# src/token_savior/memory/ledger_hook.py
"""Stdin entrypoint for the UserPromptSubmit hook: capture misses.

Reads the hook JSON payload on stdin, records a 'miss' if the user text is a
correction. Never raises: a hook must not break the session.
"""
from __future__ import annotations

import json
import sys


def main() -> int:
    try:
        raw = sys.stdin.read()
        payload = json.loads(raw) if raw.strip() else {}
    except Exception:
        return 0
    try:
        from token_savior.memory import ledger
        ledger.record_from_userprompt(
            payload,
            session_id=payload.get("session_id"),
            project_root=payload.get("cwd") or payload.get("project_root"),
        )
    except Exception as exc:  # never break the session
        print(f"[token-savior:ledger_hook] {exc}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd /root/token-savior && /root/.local/token-savior-venv/bin/python -m pytest tests/test_ledger_hook_entrypoint.py -v`
Expected: PASS

- [ ] **Step 5: Wire the entrypoint into `memory-userprompt.sh`**

Read `hooks/memory-userprompt.sh` first. It already captures stdin into a variable and runs a python block. Add the ledger call NON-destructively: after the existing recall logic, tee the same payload to the entrypoint. If the hook reads stdin once into `PAYLOAD`, reuse it:

```bash
# --- ledger: capture misses (non-blocking) ---------------------------------
if [ "$TS_MEMORY_DISABLE" != "1" ]; then
    printf '%s' "$PAYLOAD" | /root/.local/token-savior-venv/bin/python3 \
        -m token_savior.memory.ledger_hook >/dev/null 2>>"$ERR_LOG" || true
fi
```

Place it so it cannot alter the hook's stdout (the recall injection). If the current hook does not name the payload `PAYLOAD`, adapt to its variable, or capture stdin at the top once (`PAYLOAD=$(cat)`) and feed both consumers from it.

- [ ] **Step 6: Manual smoke — verify the wired hook writes a miss**

```bash
cd /root/token-savior
echo '{"prompt":"je t'"'"'ai déjà dit de regarder les logs","session_id":"smoke"}' \
  | bash hooks/memory-userprompt.sh >/dev/null 2>&1
/root/.local/token-savior-venv/bin/python -c "
from token_savior.memory import ledger
print([m['meta']['phrase'] for m in ledger.ledger_query(event_type='miss') if m['session_id']=='smoke'])
"
```
Expected: prints a list containing `je t'ai déjà dit` (against the real memory DB). If it prints `[]`, the payload variable name in the hook differs — fix the wiring.

- [ ] **Step 7: Commit**

```bash
git add hooks/memory-userprompt.sh src/token_savior/memory/ledger_hook.py tests/test_ledger_hook_entrypoint.py
git commit -m "feat(ledger): wire miss capture into UserPromptSubmit hook"
```

---

## Self-Review

**Spec coverage (Unité 3 — ledger):**
- « Tout ce que le système fait écrit une ligne » → `ledger_put` + `EVENT_TYPES` (Task 2). Les producteurs (injection/hard_block/soft_remind/silence) seront câblés dans les plans `rules`/`retrieval` ; le ledger accepte déjà leurs event_types. ✓
- Champ `était_visible` qui classe la panne → colonne `was_visible` + porté par `outcome` (Tasks 1-2). ✓
- Capture des ratés heuristique sur UserPromptSubmit → `detect_correction` + `record_from_userprompt` + hook (Tasks 4,5,7). ✓
- Valeur nette + « peut se recommander son propre rollback » → `ledger_net_value` + `counterproductive` (Task 6). ✓
- Métrique honnête (erreurs évitées, pas activité) → règles d'agrégation Task 6, encodées dans le test. ✓
- Attribution async du `résultat` → colonnes outcome nullable, remplies plus tard par la boucle `reflection` (hors périmètre phase 1, colonnes prêtes). ✓

**Hors périmètre (assumé, autres plans) :** production des events injection/soft_remind/hard_block/silence (plans `rules` et `retrieval`), backtest sur les 493 tool captures, boucle `reflection`. Le ledger est le socle testable qu'ils consommeront.

**Placeholder scan :** aucun TODO/TBD ; tout le code est fourni. Mécanisme d'isolation DB **vérifié et corrigé** avant publication (`monkeypatch.setattr(db_core, "MEMORY_DB_PATH", tmp)`, confirmé sur `db_core.py:20,285` et `tests/test_tool_capture.py:11`). Reste un seul point à inspecter au runtime, signalé avec la marche à suivre (pas laissé en blanc) : le nom de variable du payload dans `memory-userprompt.sh` (Task 7, Step 5).

**Type consistency :** `ledger_put` / `ledger_query` / `record_from_userprompt` / `ledger_net_value` / `detect_correction` cohérents entre Tasks 2-7. `outcome` porte partout les mêmes 5 clés (`_OUTCOME_KEYS`). event_types identiques partout (`EVENT_TYPES`). Fixture `isolated_db` identique dans les 6 fichiers de test.
