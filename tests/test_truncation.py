"""Une reponse coupee par sa borne doit le dire.

Le faux negatif qu'on ferme : un outil rend pile `max_results` elements,
l'appelant compte, et croit tenir un total. Deux comptages qui saturent tous
les deux la borne se lisent comme deux valeurs egales alors que ce sont deux
troncatures.
"""

from __future__ import annotations

import pytest

from token_savior.tool_schemas import TOOL_SCHEMAS
from token_savior.truncation import (
    BORNES_DE_TAILLE,
    NOMS_DE_BORNE,
    notice_de_troncature,
)


def test_liste_saturee_est_signalee():
    notice = notice_de_troncature({"max_results": 3}, ["a", "b", "c"])
    assert notice is not None
    assert "tronque" in notice
    assert "3" in notice


def test_liste_non_saturee_est_muette():
    assert notice_de_troncature({"max_results": 10}, ["a", "b", "c"]) is None


def test_sans_borne_pas_de_bruit():
    """Un appel sans borne explicite n'a rien a signaler."""
    assert notice_de_troncature({}, ["a", "b", "c"]) is None


def test_liste_imbriquee_dans_un_dict():
    notice = notice_de_troncature({"limit": 2}, {"matches": [1, 2], "total_files": 9})
    assert notice is not None
    assert "matches" in notice


def test_la_plus_longue_liste_gagne():
    """La borne gouverne la collection principale, pas un detail annexe."""
    resultat = {"symbols": [1, 2, 3, 4], "warnings": ["x"]}
    notice = notice_de_troncature({"max_results": 4}, resultat)
    assert notice is not None and "symbols" in notice


def test_profondeur_bornee():
    """Une liste enfouie a trois niveaux est un detail interne, pas la collection.

    Sans cette borne de profondeur, une liste de lignes ou de tokens dont la
    longueur coincide avec `limit` produirait une alerte a chaque appel, et une
    alerte qui crie tout le temps ne se lit plus.
    """
    profond = {"a": {"b": {"c": [1, 2, 3]}}}
    assert notice_de_troncature({"limit": 3}, profond) is None


def test_borne_booleenne_ignoree():
    """`True` vaut 1 en Python : un flag ne doit pas passer pour une borne."""
    assert notice_de_troncature({"limit": True}, ["seul"]) is None


def test_borne_en_chaine_acceptee():
    """Les arguments arrivent parfois en chaine depuis le protocole."""
    assert notice_de_troncature({"limit": "2"}, ["a", "b"]) is not None


def test_borne_negative_ou_nulle_ignoree():
    assert notice_de_troncature({"limit": 0}, []) is None
    assert notice_de_troncature({"limit": -5}, []) is None


def test_resultat_sans_liste():
    assert notice_de_troncature({"limit": 3}, {"source": "def f(): pass"}) is None
    assert notice_de_troncature({"limit": 3}, "texte brut") is None


def test_bornes_couvrent_les_schemas():
    """Aucun outil ne doit introduire une borne sous un nom inconnu d'ici.

    Sans ce test, un futur `max_items` tronquerait en silence : le module ne le
    reconnaitrait pas comme une borne et ne signalerait jamais rien.
    """
    connues = set(NOMS_DE_BORNE) | set(BORNES_DE_TAILLE)
    suspects = {}
    for nom, schema in TOOL_SCHEMAS.items():
        props = (schema.get("inputSchema") or {}).get("properties") or {}
        for cle in props:
            c = cle.lower()
            if c in connues:
                continue
            if c.startswith("max_") or c in {"limit", "top_k", "count", "n_results"}:
                suspects.setdefault(nom, []).append(cle)
    assert not suspects, (
        f"bornes non reconnues par truncation.py : {suspects}. Classe-les : "
        "dans NOMS_DE_BORNE si elles bornent un NOMBRE d'elements, dans "
        "BORNES_DE_TAILLE si elles bornent des octets ou des lignes. Sinon ces "
        "outils tronqueront en silence."
    )


def test_bornes_de_taille_ne_declenchent_rien():
    """Une borne d'octets ou de lignes n'a rien a voir avec un nombre d'elements."""
    for cle in BORNES_DE_TAILLE:
        assert notice_de_troncature({cle: 3}, ["a", "b", "c"]) is None, cle


def test_plusieurs_bornes_dans_le_meme_appel():
    """`get_change_impact` porte max_direct ET max_transitive.

    N'en retenir qu'une ferait manquer la troncature gouvernee par l'autre.
    """
    assert notice_de_troncature(
        {"max_direct": 10, "max_transitive": 2}, {"transitive": ["a", "b"]}
    ) is not None


@pytest.mark.parametrize("cle", NOMS_DE_BORNE)
def test_chaque_nom_de_borne_fonctionne(cle):
    assert notice_de_troncature({cle: 2}, ["a", "b"]) is not None
