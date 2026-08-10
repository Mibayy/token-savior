"""Signaler l'ambiguite, oui — la payer proportionnellement au bruit, non.

`db4bc36` a corrige deux vrais bugs : find_symbol qui choisissait un homonyme
en silence, get_dependents qui rendait un appelant qui n'appelle rien. Mais les
deux correctifs listaient TOUS les fichiers candidats, sans plafond.

Sur un nom courant, ca n'est plus une note, c'est un mur. Un projet Next.js
compte une trentaine de fonctions `POST` ; mesure du 08/08/2026 sur
n2-decision-tree, l'appel passait de 173.9 a 344.6 tokens.

Les deux tests de regression existants montent des fixtures a deux fichiers :
ils prouvent que l'ambiguite est dite, jamais qu'elle reste bornee.

Sens de l'erreur a preferer : tronquer sans le dire serait pire que ne pas
tronquer — l'ambiguite redeviendrait invisible au-dela du plafond. Donc la
liste est bornee ET le compte reel voyage dans la note.
"""

from __future__ import annotations

import pytest

# Le plafond est ecrit ici en dur, pas importe de l'implementation : un test qui
# lit la constante qu'il verifie passerait quelle que soit sa valeur.
_MAX_HOMONYMES = 5


def _qf(racine):
    from token_savior.project_indexer import ProjectIndexer
    from token_savior.query_api import create_project_query_functions

    idx = ProjectIndexer(str(racine))
    idx.index()
    return create_project_query_functions(idx._project_index)


@pytest.fixture
def projet_next(tmp_path):
    """Vingt routes definissent `POST`. Le motif reel d'un projet Next.js."""
    for i in range(20):
        route = tmp_path / "app" / "api" / f"ressource{i:02d}"
        route.mkdir(parents=True)
        (route / "route.py").write_text(
            "def POST(req):\n"
            f"    return {{'ressource': {i}}}\n",
            encoding="utf-8",
        )
    return tmp_path


def test_find_symbol_borne_la_liste_des_homonymes(projet_next) -> None:
    out = _qf(projet_next)["find_symbol"](name="POST")
    autres = out.get("autres_definitions")

    assert autres, f"l'ambiguite n'est plus signalee du tout : {str(out)[:300]}"
    assert len(autres) <= _MAX_HOMONYMES, (
        f"{len(autres)} definitions listees ; sur un nom courant cette liste "
        "double le cout de l'appel sans rien apprendre de plus"
    )


def test_find_symbol_dit_le_compte_reel_quand_il_tronque(projet_next) -> None:
    """Tronquer en silence rendrait l'ambiguite invisible : le pire cas."""
    out = _qf(projet_next)["find_symbol"](name="POST")
    note = out.get("note", "")

    assert "19" in note, (
        "la note ne porte pas le nombre total de definitions ; un agent qui "
        f"lit 5 candidats croira qu'il n'y en a que 5 (note : {note!r})"
    )


@pytest.fixture
def projet_appelants(tmp_path):
    """Vingt fichiers definissent `handler`, tous importent la cible."""
    (tmp_path / "lib").mkdir()
    (tmp_path / "lib" / "signature.py").write_text(
        "def creer_demande_signature(doc):\n"
        '    """Envoie un document en signature."""\n'
        "    return {'id': 1}\n",
        encoding="utf-8",
    )
    for i in range(20):
        (tmp_path / f"appelant{i:02d}.py").write_text(
            "from lib.signature import creer_demande_signature\n"
            "\n"
            "def handler(req):\n"
            "    return creer_demande_signature(req)\n",
            encoding="utf-8",
        )
    return tmp_path


def _entrees_ambigues(entries) -> list[dict]:
    return [
        e for e in entries
        if isinstance(e, dict) and e.get("ambigu") and e.get("fichiers_candidats")
    ]


def test_get_dependents_borne_les_fichiers_candidats(projet_appelants) -> None:
    entries = _qf(projet_appelants)["get_dependents"]("creer_demande_signature")
    ambigues = _entrees_ambigues(entries)

    assert ambigues, (
        "aucune entree ambigue : vingt `handler` homonymes devraient etre "
        f"signales plutot que tranches en silence (rendu : {str(entries)[:300]})"
    )
    for entree in ambigues:
        assert len(entree["fichiers_candidats"]) <= _MAX_HOMONYMES, (
            f"{len(entree['fichiers_candidats'])} candidats listes pour "
            f"'{entree.get('name')}' ; la liste non bornee est ce qui a double "
            "le cout par appel"
        )


def test_get_dependents_dit_le_compte_reel_quand_il_tronque(projet_appelants) -> None:
    entries = _qf(projet_appelants)["get_dependents"]("creer_demande_signature")
    ambigues = _entrees_ambigues(entries)

    for entree in ambigues:
        note = entree.get("note", "")
        assert "20" in note, (
            "la note ne porte pas le nombre total de candidats ; l'ambiguite "
            f"redevient invisible au-dela du plafond (note : {note!r})"
        )
