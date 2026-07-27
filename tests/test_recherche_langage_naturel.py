"""Une question posee en langage naturel doit retrouver l'observation.

Trouve le 27/07/2026. En FTS5, une suite de mots bruts est un ET IMPLICITE :
la requete exige que TOUS les mots soient presents, mots de liaison compris.
Une question humaine ne satisfait jamais cette condition.

Mesure sur 45 observations reelles, jambe lexicale seule, requetes baties sur
des mots du CONTENU absents du TITRE :

    requete brute (ET implicite)  ->  0/45, et 45 requetes sur 45 rendant
                                      ZERO ligne
    _requete_elargie              -> 29/45, 16 erreurs SQL
    _requete_prefixe              -> 29/45, 16 erreurs SQL
    _fts5_safe_query              -> 45/45, aucune erreur

Les deux replis existants echouaient sur 36 % des requetes parce qu'ils ne
mettent pas leurs termes entre guillemets : une apostrophe, frequente en
francais, suffisait a faire lever FTS5. L'erreur etait attrapee, l'essai
suivant tente, et le silence complet passait pour une absence de resultats.

Bout en bout, apres correction :

    requete courte    45/45 trouvees, rang moyen 1,02  (inchange)
    requete naturelle 39/45 -> 45/45,  rang 2,08 -> 1,00

Aucun test existant ne pouvait attraper ca : ils interrogent tous avec des
mots-cles courts, c'est-a-dire le seul regime ou le ET implicite est
satisfaisable.
"""
from __future__ import annotations

import pytest

CORPUS = [
    ("infra", "Certificat SSL expire",
     "certbot renew echoue quand nginx occupe le port 80 pendant le challenge"),
    ("command", "Redemarrer nginx",
     "systemctl restart nginx apres modification du fichier vhost"),
    ("guardrail", "Ne jamais supprimer en masse en production",
     "un DELETE sans clause WHERE a efface des donnees clients reelles"),
    ("convention", "Nommage des branches",
     "prefixe feat ou fix suivi du numero de ticket jira"),
    ("decision", "Passer le cache en ecriture differee",
     "la latence venait des ecritures synchrones sur chaque acces"),
]

# Questions telles qu'un humain les pose : verbeuses, avec des mots de
# liaison et des apostrophes.
QUESTIONS = [
    ("comment on fait quand le certificat expire et que le renouvellement echoue",
     "Certificat SSL expire"),
    ("qu'est-ce qu'on avait dit sur la modification du fichier vhost",
     "Redemarrer nginx"),
    ("rappelle-moi ce qu'on sait des donnees clients effacees par erreur",
     "Ne jamais supprimer en masse en production"),
    ("je cherche la convention pour le numero de ticket jira",
     "Nommage des branches"),
    ("on avait dit quoi sur la latence des ecritures synchrones",
     "Passer le cache en ecriture differee"),
]


@pytest.fixture
def memoire(tmp_path, monkeypatch):
    """Corpus de test AVEC LA JAMBE VECTORIELLE COUPEE.

    Ce detail decide de la valeur de tout ce fichier. Une premiere version de
    ces tests laissait le vectoriel actif : ils passaient identiquement avec
    et sans le correctif, parce que sur cinq observations la jambe
    vectorielle retrouve la bonne reponse toute seule et masque l'echec
    complet de la jambe lexicale. Un test vert qui ne contraint rien.

    Couper le vectoriel n'est pas un artifice de laboratoire : c'est le
    regime par defaut. `pip install token-savior-recall` sans l'extra
    `memory-vector` n'a ni sqlite-vec ni fastembed, et c'etait aussi l'etat
    des hooks memoire de cette machine jusqu'au 27/07/2026.
    """
    from token_savior import db_core
    from token_savior import memory_db as md

    cible = tmp_path / "memoire.db"
    monkeypatch.setattr(db_core, "MEMORY_DB_PATH", cible)
    monkeypatch.setattr(md, "MEMORY_DB_PATH", cible)
    racine = str(tmp_path / "projet")
    for type_, titre, contenu in CORPUS:
        md.observation_save(None, racine, type_, titre, contenu)
    monkeypatch.setattr(db_core, "VECTOR_SEARCH_AVAILABLE", False)
    return racine


@pytest.mark.parametrize("question,attendu", QUESTIONS)
def test_une_question_humaine_retrouve_l_observation(memoire, question, attendu) -> None:
    """Le cas mesure : requete verbeuse, reponse presente en base.

    Avant correction, le ET implicite de FTS5 rendait zero ligne pour chacune
    de ces questions : aucun des mots de liaison n'est dans l'observation.
    """
    from token_savior import memory_db as md

    titres = [o["title"] for o in md.observation_search(memoire, question, limit=5)]
    assert titres, f"{question!r} ne ramene rien du tout"
    assert titres[0] == attendu, f"{question!r} -> {titres}"


def test_une_apostrophe_ne_fait_pas_taire_la_recherche(memoire) -> None:
    """Le francais en est plein, et c'est precisement ce qui cassait les replis.

    `_requete_elargie` et `_requete_prefixe` ne mettent pas leurs termes entre
    guillemets, donc `l'echec` reste tel quel et FTS5 repond
    `syntax error near "'"`. Mesure : cinq requetes sur six contenant une
    apostrophe levaient. `_fts5_safe_query` n'a pas le probleme, son
    extracteur de jetons ne garde pas l'apostrophe et met chaque terme entre
    guillemets.

    On exige de TROUVER, pas seulement de ne pas lever : les deux replis
    d'origine ne levaient pas non plus vu de l'appelant, l'erreur etait
    attrapee et le silence passait pour une absence de resultats.
    """
    from token_savior import memory_db as md

    for question, attendu in (
        ("l'echec du renouvellement certbot", "Certificat SSL expire"),
        ("qu'est-ce qu'on fait quand nginx occupe le port", "Certificat SSL expire"),
        ("c'est quoi la convention pour le ticket jira", "Nommage des branches"),
    ):
        titres = [o["title"] for o in md.observation_search(memoire, question, limit=5)]
        assert titres, f"{question!r} ne ramene rien : l'apostrophe a fait taire la recherche"
        assert titres[0] == attendu, f"{question!r} -> {titres}"


def test_une_requete_courte_donne_toujours_le_meme_resultat(memoire) -> None:
    """La chaine d'essais ne doit rien changer au regime qui marchait deja.

    Le premier essai reste le ET implicite d'origine. Une requete precise qui
    trouvait sa reponse hier doit trouver exactement la meme aujourd'hui,
    sinon la correction se paie en regressions invisibles.
    """
    from token_savior import memory_db as md

    titres = [o["title"] for o in md.observation_search(memoire, "certbot renew", limit=5)]
    assert titres and titres[0] == "Certificat SSL expire", titres


def test_une_question_sans_rapport_ne_ramene_rien(memoire) -> None:
    """Elargir le rappel ne doit pas revenir a tout rendre.

    Sans ce test, on pourrait faire passer les precedents en rendant la base
    entiere a chaque requete.
    """
    from token_savior import memory_db as md

    for absurde in ("banane pamplemousse zzz", "quantum blockchain synergy"):
        titres = [o["title"] for o in md.observation_search(memoire, absurde, limit=5)]
        assert titres == [], f"{absurde!r} -> {titres}"
