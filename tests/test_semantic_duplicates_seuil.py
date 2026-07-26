"""`min_lines` semblait sans effet, et le cache repondait pour un autre seuil.

Deux defauts trouves en exigeant un resultat connu d'avance plutot que
l'absence d'erreur.

**Un plancher cache.** En plus de `min_lines`, un `len(source) < 50` non
documente ecartait les corps courts. Un appelant qui demandait explicitement
`min_lines=1` voyait donc son parametre sans effet et en concluait, a raison
de son point de vue, que l'outil ne marchait pas.

**Un cache perime.** Le cache de hash etait construit une seule fois, avec le
`min_lines` du PREMIER appel, puis reutilise tel quel. Tout appel ulterieur
avec un autre seuil recevait un resultat calcule pour l'ancien, sans rien qui
le signale. C'est la famille de defaut la plus couteuse : une reponse
plausible, fausse, et stable.
"""
from __future__ import annotations

import pytest

from token_savior.project_indexer import ProjectIndexer
from token_savior.query_api import create_project_query_functions

CORPS_COURT = "    return x + y * 2\n"
CORPS_LONG = ("    total = x + y\n    total = total * 2\n"
              "    if total > 10:\n        total -= 1\n    return total\n")


def _projet(tmp_path, corps):
    (tmp_path / "a.py").write_text(f"def calc(x, y):\n{corps}", encoding="utf-8")
    (tmp_path / "b.py").write_text(f"def calc_bis(x, y):\n{corps}", encoding="utf-8")
    return create_project_query_functions(ProjectIndexer(str(tmp_path)).index())


def test_min_lines_1_leve_le_plancher(tmp_path) -> None:
    """Le cas qui a revele le defaut : deux corps d'une ligne, identiques."""
    q = _projet(tmp_path, CORPS_COURT)
    res = str(q["find_semantic_duplicates"](min_lines=1))
    assert "calc" in res and "calc_bis" in res, res[:250]


def test_le_defaut_filtre_toujours_le_bruit(tmp_path) -> None:
    """Sans plancher, tout getter d'une ligne collisionne avec tous les autres.
    Le corriger ne doit pas rendre l'outil inutilisable par defaut."""
    q = _projet(tmp_path, CORPS_COURT)
    assert "none found" in str(q["find_semantic_duplicates"]()).lower()


def test_un_vrai_doublon_est_vu_meme_par_defaut(tmp_path) -> None:
    q = _projet(tmp_path, CORPS_LONG)
    res = str(q["find_semantic_duplicates"]())
    assert "calc" in res and "calc_bis" in res, res[:250]


def test_le_cache_suit_le_seuil_demande(tmp_path) -> None:
    """Le defaut le plus sournois : deux appels differents, meme reponse.

    L'ordre compte. Si le cache reste sur le premier seuil, le second appel
    ment sans que rien ne le signale.
    """
    q = _projet(tmp_path, CORPS_COURT)

    defaut_dabord = str(q["find_semantic_duplicates"]())
    puis_explicite = str(q["find_semantic_duplicates"](min_lines=1))
    retour_au_defaut = str(q["find_semantic_duplicates"]())

    assert "none found" in defaut_dabord.lower(), defaut_dabord[:200]
    assert "calc_bis" in puis_explicite, (
        f"le cache du premier appel a survecu : {puis_explicite[:200]}")
    assert "none found" in retour_au_defaut.lower(), (
        f"le cache du deuxieme appel a survecu : {retour_au_defaut[:200]}")


@pytest.mark.parametrize("ordre", [(1, 2), (2, 1)])
def test_les_deux_ordres_donnent_le_meme_verdict(tmp_path, ordre) -> None:
    """Un resultat qui depend de l'ordre des appels n'est pas un resultat."""
    q = _projet(tmp_path, CORPS_COURT)
    resultats = {seuil: str(q["find_semantic_duplicates"](min_lines=seuil))
                 for seuil in ordre}
    assert "calc_bis" in resultats[1], resultats[1][:200]
    assert "none found" in resultats[2].lower(), resultats[2][:200]
