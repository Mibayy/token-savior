"""Le texte tape par l'utilisateur ne doit jamais etre lu comme de la syntaxe FTS5.

Trouve le 27/07/2026 en presentant a chaque outil une entree impossible et en
regardant ce qu'il repond, plutot qu'en relisant le code.

`session_summary_search` et `prompt_search` passaient la requete brute a
`MATCH`. FTS5 y voit alors des operateurs : le trait d'union, la parenthese,
le deux-points et le mot NOT declenchent une `sqlite3.Error`. Elle etait
attrapee, imprimee sur stderr, et traduite en `[]`. L'appelant lisait
« rien trouve » la ou il fallait lire « la recherche a echoue ».

Mesure avant correction, cinq cas sur dix cassaient :

    'certificat-ssl'  -> erreur SQL avalee
    'port-80'         -> erreur SQL avalee
    'a:b'             -> erreur SQL avalee
    'NOT certificat'  -> erreur SQL avalee
    '(certificat'     -> erreur SQL avalee

Ce ne sont pas des cas tordus. C'est ce qu'on tape tous les jours :
`token-savior`, `claude-code`, `post-mortem`, `port-80`.

Le desinfectant `_fts5_safe_query` existait deja dans le depot et servait a
`reasoning_search` et a `tool_capture`. Il manquait a ces deux-la, ce qui est
la forme la plus banale du defaut : la bonne solution etait a l'interieur de
la maison.
"""
from __future__ import annotations

import io
from contextlib import redirect_stderr
from pathlib import Path

import pytest

# Les tournures qui cassaient, plus des temoins qui marchaient deja.
REQUETES = [
    "certificat",            # temoin, aucun caractere special
    "certificat-ssl",        # trait d'union
    "port-80",               # trait d'union et chiffres
    "(certificat",           # parenthese non fermee
    "NOT certificat",        # mot-cle FTS5
    "a:b",                   # deux-points, syntaxe de colonne
    'certificat "ssl"',      # guillemets
    "cert*",                 # etoile
    "certificat OR nginx",   # operateur
    "nginx systemctl",       # temoin multi-mot
]


@pytest.fixture
def memoire(tmp_path: Path, monkeypatch):
    from token_savior import db_core
    from token_savior import memory_db as md

    cible = tmp_path / "memoire.db"
    monkeypatch.setattr(db_core, "MEMORY_DB_PATH", cible)
    monkeypatch.setattr(md, "MEMORY_DB_PATH", cible)
    racine = str(tmp_path / "projet")

    sid = md.session_start(racine)
    md.session_end(
        sid,
        request="renouveler le certificat ssl du port 80",
        learned="certbot echoue quand nginx occupe le port",
        end_type="completed",
    )
    md.prompt_save(None, racine, "comment renouveler le certificat ssl sur le port 80 ?")
    return racine


def _sans_erreur_sql(fonction, racine: str, requete: str):
    """Appelle et rend (resultats, erreurs SQL imprimees sur stderr)."""
    tampon = io.StringIO()
    with redirect_stderr(tampon):
        resultats = fonction(racine, requete)
    bruit = [
        ligne for ligne in tampon.getvalue().splitlines()
        if "error" in ligne.lower() and "sqlite-vec" not in ligne
    ]
    return resultats, bruit


@pytest.mark.parametrize("requete", REQUETES)
def test_session_summary_search_ne_casse_sur_aucune_requete(memoire, requete) -> None:
    from token_savior import memory_db as md

    _, erreurs = _sans_erreur_sql(md.session_summary_search, memoire, requete)
    assert not erreurs, (
        f"{requete!r} a produit une erreur SQL avalee : {erreurs}. "
        "La requete de l'utilisateur atteint MATCH sans passer par "
        "_fts5_safe_query."
    )


@pytest.mark.parametrize("requete", REQUETES)
def test_prompt_search_ne_casse_sur_aucune_requete(memoire, requete) -> None:
    from token_savior import memory_db as md

    _, erreurs = _sans_erreur_sql(md.prompt_search, memoire, requete)
    assert not erreurs, (
        f"{requete!r} a produit une erreur SQL avalee : {erreurs}."
    )


def test_le_trait_d_union_trouve_quand_meme_la_reponse(memoire) -> None:
    """Ne pas casser ne suffit pas : il faut encore trouver.

    Un correctif qui se contenterait d'avaler l'erreur plus proprement
    passerait le test precedent tout en ne rendant toujours rien. On exige
    donc que la requete a trait d'union ramene la ligne qui contient
    reellement la reponse.
    """
    from token_savior import memory_db as md

    resultats, erreurs = _sans_erreur_sql(md.session_summary_search, memoire, "certificat-ssl")
    assert not erreurs, erreurs
    assert resultats, "'certificat-ssl' ne ramene rien alors que le resume parle de certificat ssl"


def test_une_requete_sans_rien_de_cherchable_rend_une_liste_vide(memoire) -> None:
    """`_fts5_safe_query` peut ne rien laisser : il ne faut pas passer ca a MATCH.

    Une chaine vide dans un MATCH leve. Le garde doit s'arreter avant, et
    rendre une liste vide sans bruit.
    """
    from token_savior import memory_db as md

    for requete in ("a:b", "!! ??", "de la"):
        resultats, erreurs = _sans_erreur_sql(md.session_summary_search, memoire, requete)
        assert resultats == [], requete
        assert not erreurs, (requete, erreurs)
