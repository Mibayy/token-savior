"""Le plafond des homonymes s'appliquait aux fonctions, pas aux classes.

`db4bc36` puis le correctif de bornage ont introduit `_MAX_HOMONYMES = 5` et
l'ont cable sur les trois chemins connus : `autres_definitions` (find_symbol
sur une fonction), `fichiers_candidats` (get_dependents), et la voie memoisee
de `_classes_definissant`.

Le repli « search all files » de `_resolve_symbol_info`, herite de `fd4cc01`,
est anterieur a la constante et plafonnait a 10 en dur — pour `candidates`
(classe homonyme) comme pour `normalized_candidates` (repli par nom
normalise). Un projet ou la meme classe est definie par plus de cinq
fournisseurs rendait donc une liste deux fois plus longue que l'equivalent
fonction, pour la meme information.

Comme dans `test_homonymes_bornes.py`, le plafond est recopie en dur ici : un
test qui importe la constante qu'il verifie passerait quelle que soit sa
valeur. Et comme la-bas, le sens de l'erreur compte : la liste est bornee,
mais le compte reel doit rester dans le message, sinon l'ambiguite redevient
invisible au-dela du plafond.
"""

from __future__ import annotations

import pytest

from token_savior.project_indexer import ProjectIndexer
from token_savior.query_api import create_project_query_functions

_MAX_HOMONYMES = 5


def _qfns(racine):
    return create_project_query_functions(ProjectIndexer(str(racine)).index())


@pytest.fixture
def projet_fournisseurs(tmp_path):
    """Huit modules definissent chacun la meme classe `Fournisseur`."""
    for i in range(8):
        paquet = tmp_path / "fournisseurs" / f"prestataire{i:02d}"
        paquet.mkdir(parents=True)
        (paquet / "client.py").write_text(
            "class Fournisseur:\n"
            "    def envoyer(self, doc):\n"
            f"        return {{'prestataire': {i}}}\n",
            encoding="utf-8",
        )
    return tmp_path


def test_les_fichiers_candidats_d_une_classe_sont_bornes(projet_fournisseurs) -> None:
    res = _qfns(projet_fournisseurs)["find_symbol"]("Fournisseur")
    candidats = res.get("candidates")

    assert candidats, f"l'ambiguite de classe n'est plus signalee : {str(res)[:300]}"
    assert len(candidats) <= _MAX_HOMONYMES, (
        f"{len(candidats)} fichiers listes pour une classe homonyme, alors que "
        "le meme cas sur une fonction en liste 5 : le repli 'search all files' "
        "ne passe pas par le plafond partage"
    )


def test_l_ambiguite_de_classe_dit_le_compte_reel(projet_fournisseurs) -> None:
    """Tronquer en silence serait pire que ne pas tronquer."""
    res = _qfns(projet_fournisseurs)["find_symbol"]("Fournisseur")
    message = res.get("error", "")

    assert "8" in message, (
        "le message ne porte pas le nombre total de definitions ; un agent qui "
        f"lit 5 candidats croira qu'il n'y en a que 5 (message : {message!r})"
    )


@pytest.fixture
def projet_casse_variee(tmp_path):
    """Sept noms distincts qui se normalisent tous en `userservice`."""
    noms = [
        "user_service",
        "userService",
        "USER_SERVICE",
        "User_Service",
        "userservice",
        "Userservice",
        "UserService",
    ]
    (tmp_path / "src").mkdir()
    for i, nom in enumerate(noms):
        (tmp_path / "src" / f"module{i:02d}.py").write_text(
            f"def {nom}(req):\n"
            f"    return {i}\n",
            encoding="utf-8",
        )
    return tmp_path


def test_les_candidats_normalises_sont_bornes(projet_casse_variee) -> None:
    """Meme defaut, meme methode, meme plafond : le repli par nom normalise."""
    res = _qfns(projet_casse_variee)["find_symbol"]("user-service")
    candidats = res.get("normalized_candidates")

    assert candidats, f"le repli normalise ne propose plus rien : {str(res)[:300]}"
    assert len(candidats) <= _MAX_HOMONYMES, (
        f"{len(candidats)} candidats normalises rendus ; la liste doit etre "
        "bornee comme les autres listes d'homonymes"
    )
