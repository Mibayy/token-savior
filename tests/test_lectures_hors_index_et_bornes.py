"""Trois echecs mesures sur 30 jours de transcriptions (06/10/2026).

1. read_lines sur un fichier present sur disque mais hors index : 17 des 19
   echecs de l'outil. On lit desormais ce que `sed -n` aurait lu.
2. search_codebase(max_results=0) : 5 reponses de 107 000 a 192 000
   caracteres, refusees par Claude Code. On coupe au budget et on le dit.
3. Le pied « Found in project » colle a une lecture reussie des que le texte
   lu contenait « not found in index ».
"""
from __future__ import annotations

from pathlib import Path

from token_savior.project_indexer import ProjectIndexer
from token_savior.query_api import create_project_query_functions
from token_savior.server_handlers.code_nav import QFN_HANDLERS


def _q(tmp_path: Path) -> dict:
    (tmp_path / "app.py").write_text(
        "def f():\n    return 'file not found in index'\n", encoding="utf-8")
    return create_project_query_functions(ProjectIndexer(str(tmp_path)).index())


def test_read_lines_lit_un_fichier_hors_index(tmp_path: Path) -> None:
    q = _q(tmp_path)
    (tmp_path / "schema.sql").write_text("create table t(\n  id int\n);\n", encoding="utf-8")
    out = QFN_HANDLERS["read_lines"](q, {"file_path": "schema.sql", "start": 2, "end": 3})
    assert "id int" in out and ");" in out
    assert "hors index" in out


def test_read_lines_lit_un_chemin_absolu_hors_projet(tmp_path: Path) -> None:
    (tmp_path / "projet").mkdir()
    q = _q(tmp_path / "projet")
    ailleurs = tmp_path / "ailleurs" / "decodage.mjs"
    ailleurs.parent.mkdir()
    ailleurs.write_text("export function d() {\n  return 1;\n}\n", encoding="utf-8")
    out = QFN_HANDLERS["read_lines"](q, {"file_path": str(ailleurs), "start": 1})
    assert "export function d()" in out


def test_read_lines_refuse_un_binaire_et_un_absent(tmp_path: Path) -> None:
    q = _q(tmp_path)
    (tmp_path / "image.bin").write_bytes(b"\x89PNG\x00\x00\x01")
    assert QFN_HANDLERS["read_lines"](q, {"file_path": "image.bin", "start": 1}).startswith("Error")
    assert QFN_HANDLERS["read_lines"](q, {"file_path": "absent.py", "start": 1}).startswith("Error")


def test_search_sans_borne_est_coupe_au_budget(tmp_path: Path) -> None:
    for n in range(40):
        (tmp_path / f"m{n}.py").write_text(("x = 'aiguille " + "z" * 140 + "'\n") * 40,
                                          encoding="utf-8")
    q = create_project_query_functions(ProjectIndexer(str(tmp_path)).index())
    out = QFN_HANDLERS["search_codebase"](q, {"pattern": "aiguille", "max_results": 0})
    assert isinstance(out, dict) and "_tronque" in out
    assert 0 < len(out["matches"]) < 1600
    borne = QFN_HANDLERS["search_codebase"](q, {"pattern": "aiguille", "max_results": 5})
    assert isinstance(borne, list) and len(borne) == 5


def test_une_lecture_reussie_ne_recoit_pas_de_piste(tmp_path: Path) -> None:
    from token_savior.server import _est_erreur_hors_index
    q = _q(tmp_path)
    lu = QFN_HANDLERS["read_lines"](q, {"file_path": "app.py", "start": 1})
    assert "not found in index" in lu
    assert not _est_erreur_hors_index(lu)
    assert _est_erreur_hors_index("Error: file 'x.py' not found in index. Only indexed")
