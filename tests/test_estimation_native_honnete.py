"""Le coût « natif » d'un appel est celui de la vraie alternative (07/10/2026).

L'ancienne estimation prenait une fraction du projet entier par appel (15 %
pour une recherche) : `ts gain` affichait 98,7 % d'économie quand le banc A/B
mesurait -14 % de coût et -25 % de jetons neufs.
"""
from __future__ import annotations

from types import SimpleNamespace

from token_savior.project_indexer import ProjectIndexer
from token_savior.server_runtime import _estimate_naive_chars_for_call


def _slot(tmp_path):
    (tmp_path / "gros.py").write_text("def cible():\n    return 1\n" + "x = 1\n" * 2000, encoding="utf-8")
    (tmp_path / "autre.py").write_text("y = 2\n" * 5000, encoding="utf-8")
    index = ProjectIndexer(str(tmp_path)).index()
    return SimpleNamespace(indexer=SimpleNamespace(_project_index=index)), index


def test_une_recherche_ne_revendique_aucune_economie(tmp_path) -> None:
    slot, _ = _slot(tmp_path)
    res = [{"file": "gros.py", "line_number": 1, "content": "def cible():"}]
    import json
    assert _estimate_naive_chars_for_call(slot, "search_codebase", {"pattern": "cible"}, res) == len(json.dumps(res, separators=(",", ":")))


def test_lire_un_symbole_compte_son_fichier_pas_le_projet(tmp_path) -> None:
    slot, index = _slot(tmp_path)
    src = "def cible():\n    return 1"
    naif = _estimate_naive_chars_for_call(slot, "get_function_source", {"name": "cible", "file_path": "gros.py"}, src)
    assert naif == index.files["gros.py"].total_chars
    assert naif < sum(m.total_chars for m in index.files.values())
