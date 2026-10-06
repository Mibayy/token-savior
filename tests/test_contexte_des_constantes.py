"""Une constante a un contexte, comme une fonction.

Releve du 06/10/2026 sur 30 jours : `get_edit_context("POSTES_ESTALLE_MP")`
rendait « function 'POSTES_ESTALLE_MP' not found », alors que la constante
etait indexee. find_symbol ne cherchait pas les variables par defaut, et
get_full_context / get_edit_context ne savaient lire que fonctions et classes.
"""
from __future__ import annotations

from pathlib import Path

from token_savior.project_indexer import ProjectIndexer
from token_savior.query_api import create_project_query_functions
from token_savior.server_handlers.code_nav import _q_get_edit_context

TS = """export const POSTES = [
  { label: '9h Installation', heure: '9h' },
  { label: '10h Accueil', heure: '10h' },
];

export function compter() {
  return POSTES.length;
}
"""

PY = """SENDER = "bot"
TABLE = {
    "a": 1,
    "b": 2,
}


def envoyer():
    return SENDER
"""


def _qfns(tmp_path: Path) -> dict:
    (tmp_path / "postes.ts").write_text(TS, encoding="utf-8")
    (tmp_path / "envoi.py").write_text(PY, encoding="utf-8")
    return create_project_query_functions(ProjectIndexer(str(tmp_path)).index())


def test_find_symbol_trouve_une_constante_sans_kinds(tmp_path: Path) -> None:
    q = _qfns(tmp_path)
    r = q["find_symbol"]("TABLE")
    assert r["file"] == "envoi.py" and r["type"] == "constant"


def test_la_source_d_une_constante_couvre_toute_sa_valeur(tmp_path: Path) -> None:
    q = _qfns(tmp_path)
    src = q["get_variable_source"]("TABLE")
    assert '"b": 2,' in src and src.rstrip().endswith("}")
    assert "def envoyer" not in src
    ts = q["get_variable_source"]("POSTES")
    assert "10h Accueil" in ts and ts.rstrip().endswith("];")


def test_get_full_context_rend_la_source_d_une_constante(tmp_path: Path) -> None:
    q = _qfns(tmp_path)
    ctx = q["get_full_context"]("SENDER", depth=0)
    assert 'SENDER = "bot"' in ctx["source"]


def test_get_edit_context_rend_la_source_d_une_constante(tmp_path: Path) -> None:
    q = _qfns(tmp_path)
    ctx = _q_get_edit_context(q, {"name": "POSTES"})
    assert "9h Installation" in ctx["source"]
    assert "not found" not in str(ctx["source"])
