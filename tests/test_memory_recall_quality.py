"""Le recall rend-il la BONNE observation, pas seulement une reponse.

« Recall » est dans le nom du produit et sa qualite n'avait jamais ete mesuree,
seulement son absence d'erreur. Premiere mesure : **4 requetes sur 6**.

FTS5 fait un ET implicite entre les mots d'une requete nue. Une phrase en
langage naturel -- exactement la facon dont un agent formule -- exigeait donc
que chaque mot figure dans l'observation, mots outils compris.
« supprimer des donnees en prod » ne rendait rien face a une observation
contenant supprimer, donnees et prod, parce qu'elle ne contenait pas « des ».

Trois essais du plus precis au plus large, chacun tente seulement si le
precedent n'a rien rendu : une requete qui marchait rend exactement la meme
chose qu'avant.
"""
from __future__ import annotations

import pytest

from token_savior import memory_db

OBSERVATIONS = [
    ("command", "Redemarrer nginx", "systemctl restart nginx apres modification du vhost"),
    ("guardrail", "Ne jamais supprimer en masse en prod",
     "DELETE sans WHERE a efface de vraies donnees"),
    ("infra", "Certificat SSL expire", "certbot renew echoue si le port 80 est occupe"),
    ("convention", "Nommage des branches", "prefixe feat ou fix suivi du ticket"),
    ("command", "Relancer les tests", "pytest -q dans le venv du projet"),
]


@pytest.fixture
def base(tmp_path, monkeypatch):
    """Base isolee.

    Poser TOKEN_SAVIOR_DATA_DIR ne suffit pas : le chemin est resolu a
    l'import du module, bien avant qu'un test ne s'execute. Sans ce patch, la
    suite ecrivait dans la base reelle de l'utilisateur -- constate en
    retrouvant 25 observations de test dedans -- et les resultats dependaient
    des executions precedentes.
    """
    from token_savior import db_core
    from token_savior import memory_db as md

    # Les deux noms : `memory_db` garde une copie prise a l'import et la passe
    # explicitement a db_core. Ne patcher qu'un des deux laisse le test
    # partager la base des autres, et ses assertions « ne doit rien rendre »
    # voient alors les observations des voisins.
    cible = tmp_path / "memoire.db"
    monkeypatch.setattr(db_core, "MEMORY_DB_PATH", cible)
    monkeypatch.setattr(md, "MEMORY_DB_PATH", cible)
    racine = str(tmp_path / "projet")
    for type_, titre, contenu in OBSERVATIONS:
        memory_db.observation_save(None, racine, type_, titre, contenu)
    return racine


def _titres(racine, requete):
    return [o.get("title", "") for o in (memory_db.observation_search(racine, requete, limit=3) or [])]


@pytest.mark.parametrize("requete,attendu", [
    ("nginx", "Redemarrer nginx"),
    ("pytest", "Relancer les tests"),
    ("certificat ssl", "Certificat SSL expire"),
])
def test_les_requetes_directes_marchent_toujours(base, requete, attendu) -> None:
    """Non-regression : ce qui marchait avant doit marcher a l'identique."""
    assert _titres(base, requete)[:1] == [attendu]


@pytest.mark.parametrize("requete,attendu", [
    ("supprimer des donnees en prod", "Ne jamais supprimer en masse en prod"),
    ("lancer la suite de tests", "Relancer les tests"),
])
def test_une_phrase_en_langage_naturel_trouve(base, requete, attendu) -> None:
    """Le defaut d'origine : un mot outil manquant annulait toute la requete."""
    titres = _titres(base, requete)
    assert titres and titres[0] == attendu, f"{requete!r} -> {titres}"


@pytest.mark.parametrize("requete,attendu", [
    ("comment nommer une branche", "Nommage des branches"),
    ("renouveler le certificat", "Certificat SSL expire"),
])
def test_une_autre_forme_du_mot_trouve(base, requete, attendu) -> None:
    """FTS ne racinise pas le francais : `nommer` ne joint pas `Nommage`."""
    titres = _titres(base, requete)
    assert titres and titres[0] == attendu, f"{requete!r} -> {titres}"


def test_une_requete_absente_ne_rend_rien(base) -> None:
    """Elargir la recherche ne doit pas la transformer en generateur de bruit."""
    assert _titres(base, "kubernetes helm istio zzz") == []


def test_une_requete_faite_que_de_mots_outils_ne_declenche_pas_le_repli(base) -> None:
    """Elargir sur des mots outils ramenerait toute la base.

    Note : « des » seul rend legitimement un resultat, il figure tel quel dans
    une observation. Ce qui est teste ici, c'est que le REPLI ne s'emballe pas
    quand la requete ne contient aucun terme porteur.
    """
    assert _titres(base, "le la de du et ou") == []


@pytest.mark.parametrize("requete", ["nginx (", 'ssl "', "tests -", "a^b:c"])
def test_la_ponctuation_ne_leve_pas_dexception(base, requete) -> None:
    """Une requete refusee par la syntaxe FTS doit rendre une liste, pas
    remonter une OperationalError a l'appelant."""
    assert isinstance(memory_db.observation_search(base, requete, limit=3), list)
