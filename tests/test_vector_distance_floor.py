"""Un k-NN sans plancher rend toujours k resultats, aussi loin soient-ils.

Mesure sur des observations francaises, distances renvoyees par sqlite-vec :

    voisins PERTINENTS      0,85 a 0,99
    voisins SANS RAPPORT    0,97 a 1,07

Les bandes se recouvrent presque entierement. Pire, « redemarrer le serveur
web » classait `Certificat SSL expire` (0,928) DEVANT `Redemarrer nginx`
(0,989), et une requete absurde obtenait un meilleur score (0,973) que la bonne
reponse d'une requete legitime.

Consequence sans plancher : toute requete rendait quelque chose. Une memoire
qui repond toujours n'est plus une memoire, c'est un generateur de souvenirs
plausibles, et ce qu'elle rend est injecte au modele comme s'il l'avait vecu.

Le vecteur ne contribue donc que lorsqu'il est franchement confiant. Ailleurs,
c'est le lexical qui sait.
"""
from __future__ import annotations

import pytest

from token_savior import memory_db
from token_savior.memory.search import _DISTANCE_MAX_FUSION, _DISTANCE_MAX_SEULE

OBSERVATIONS = [
    ("command", "Redemarrer nginx", "systemctl restart nginx apres modification du vhost"),
    ("guardrail", "Ne jamais supprimer en masse en prod", "DELETE sans WHERE a efface des donnees"),
    ("infra", "Certificat SSL expire", "certbot renew echoue si le port 80 est occupe"),
    ("convention", "Nommage des branches", "prefixe feat ou fix suivi du ticket"),
    ("command", "Relancer les tests", "pytest -q dans le venv du projet"),
]


@pytest.fixture
def base(tmp_path, monkeypatch):
    from token_savior import db_core
    from token_savior import memory_db as md

    cible = tmp_path / "memoire.db"
    monkeypatch.setattr(db_core, "MEMORY_DB_PATH", cible)
    monkeypatch.setattr(md, "MEMORY_DB_PATH", cible)
    racine = str(tmp_path / "projet")
    for type_, titre, contenu in OBSERVATIONS:
        memory_db.observation_save(None, racine, type_, titre, contenu)
    return racine


def _titres(racine, requete):
    return [o["title"] for o in (memory_db.observation_search(racine, requete, limit=3) or [])]


@pytest.mark.parametrize("absurde", [
    "kubernetes helm istio zzz",
    "banane pamplemousse",
    "quantum blockchain synergy",
])
def test_une_requete_sans_rapport_ne_rend_rien(base, absurde) -> None:
    """Le defaut d'origine : le k-NN rendait les moins eloignees, donc tout."""
    assert _titres(base, absurde) == [], absurde


@pytest.mark.parametrize("requete,attendu", [
    ("nginx", "Redemarrer nginx"),
    ("certificat qui expire", "Certificat SSL expire"),
    ("comment nommer une branche", "Nommage des branches"),
])
def test_les_vraies_questions_trouvent_toujours(base, requete, attendu) -> None:
    """Resserrer ne doit pas rendre la memoire muette."""
    titres = _titres(base, requete)
    assert titres and titres[0] == attendu, f"{requete!r} -> {titres}"


def test_une_question_pertinente_ne_traine_pas_de_bruit(base) -> None:
    """Trois reponses dont deux sans rapport coutent plus qu'elles n'apportent."""
    assert _titres(base, "certificat qui expire") == ["Certificat SSL expire"]


def test_les_seuils_restent_dans_la_bande_mesuree() -> None:
    """Un seuil au-dessus de 0,95 laisserait repasser la bande du bruit.

    Ce test protege une valeur MESUREE, pas choisie : s'il tombe, c'est que
    quelqu'un a relache le seuil sans refaire la mesure.
    """
    assert _DISTANCE_MAX_SEULE <= 0.95, _DISTANCE_MAX_SEULE
    assert _DISTANCE_MAX_FUSION <= 0.95, _DISTANCE_MAX_FUSION
