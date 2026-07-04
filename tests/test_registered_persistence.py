"""Persisted project registry: survive stdio restarts so switch_project can
reach a project registered in a prior session without a fresh set_project_root
reindex.

Audit 2026-07-04: set_project_root was called 51x in 5.5 weeks (p95 1.8s, one
14.6s outlier), collector-crypt-scanner reindexed 20x -- the in-memory registry
was lost on every stdio respawn.
"""

from __future__ import annotations

import json

from token_savior import slot_manager as sm


def test_persist_and_load_roundtrip(tmp_path, monkeypatch):
    monkeypatch.setattr(sm, "_REGISTERED_ROOTS_FILE", str(tmp_path / "reg.json"))
    monkeypatch.setattr(sm, "_STATS_DIR", str(tmp_path))
    proj = tmp_path / "proj"
    proj.mkdir()

    sm._persist_registered_root(str(proj))
    sm._persist_registered_root(str(proj))  # idempotent -- no duplicate

    assert sm._load_registered_roots() == [str(proj)]


def test_load_filters_missing_dirs(tmp_path, monkeypatch):
    monkeypatch.setattr(sm, "_REGISTERED_ROOTS_FILE", str(tmp_path / "reg.json"))
    monkeypatch.setattr(sm, "_STATS_DIR", str(tmp_path))
    live = tmp_path / "live"
    live.mkdir()
    (tmp_path / "reg.json").write_text(
        json.dumps(["/nonexistent/gone", str(live)]), encoding="utf-8"
    )
    assert sm._load_registered_roots() == [str(live)]


def test_load_missing_file_returns_empty(tmp_path, monkeypatch):
    monkeypatch.setattr(sm, "_REGISTERED_ROOTS_FILE", str(tmp_path / "absent.json"))
    monkeypatch.setattr(sm, "_STATS_DIR", str(tmp_path))
    assert sm._load_registered_roots() == []


def test_load_corrupt_file_returns_empty(tmp_path, monkeypatch):
    monkeypatch.setattr(sm, "_REGISTERED_ROOTS_FILE", str(tmp_path / "reg.json"))
    monkeypatch.setattr(sm, "_STATS_DIR", str(tmp_path))
    (tmp_path / "reg.json").write_text("{not json", encoding="utf-8")
    assert sm._load_registered_roots() == []


def test_resolve_unregistered_by_path_and_basename(tmp_path, monkeypatch):
    """switch_project must reach a persisted project by basename, so the agent
    never needs a fresh set_project_root reindex in a new session."""
    from token_savior.server_handlers import project as proj

    monkeypatch.setattr(sm, "_REGISTERED_ROOTS_FILE", str(tmp_path / "reg.json"))
    monkeypatch.setattr(sm, "_STATS_DIR", str(tmp_path))
    d = tmp_path / "myproj"
    d.mkdir()

    # Direct directory path resolves without any persisted entry.
    assert proj._resolve_unregistered(str(d)) == str(d)

    # Basename resolves once the project has been persisted.
    sm._persist_registered_root(str(d))
    assert proj._resolve_unregistered("myproj") == str(d)
    assert proj._resolve_unregistered("MYPROJ") == str(d)  # case-insensitive

    # Unknown hint stays unresolved.
    assert proj._resolve_unregistered("nope-xyz-123") is None
