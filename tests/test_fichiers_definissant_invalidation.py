"""Un cache qui survit a l'edition ment sur ce que le projet definit.

`_fichiers_definissant` balayait tout le projet a chaque appel, et
`get_dependents` l'appelle deux fois par dependance : p95 mesure a 3805 ms
contre 382 ms habituellement. Le correctif construit l'index une fois et le
garde sur l'instance — or `slot_manager` garde justement un engine par projet
et le reutilise pour tous les appels suivants.

Donc, sans invalidation, la premiere reponse fige la liste des definisseurs
pour le reste de la session : apres un `replace_symbol_source` qui renomme une
fonction, `find_symbol` continue de citer le fichier qui ne la definit plus, et
ne voit pas le nouveau nom. C'est exactement le bug que le cache etait cense
eviter (choisir un fichier au hasard), reintroduit par la porte de derriere.

Sens de l'erreur a preferer : reconstruire un index inutilement coute un
balayage ; rendre un fichier qui ne definit plus le symbole coute une edition
au mauvais endroit.
"""

from __future__ import annotations

import pytest


@pytest.fixture
def projet(tmp_path):
    """Deux fichiers definissent `foo` — l'ambiguite est reelle au depart."""
    (tmp_path / "a.py").write_text(
        "def foo(x):\n"
        '    """Definition A."""\n'
        "    return x + 1\n",
        encoding="utf-8",
    )
    (tmp_path / "b.py").write_text(
        "def foo(x):\n"
        '    """Definition B."""\n'
        "    return x + 2\n",
        encoding="utf-8",
    )
    return tmp_path


@pytest.fixture
def indexeur(projet):
    from token_savior.project_indexer import ProjectIndexer

    idx = ProjectIndexer(str(projet))
    idx.index()
    return idx


@pytest.fixture
def qf(indexeur):
    """Un seul jeu de fonctions pour tout le test, comme le fait slot_manager."""
    from token_savior.query_api import create_project_query_functions

    return create_project_query_functions(indexeur._project_index)


def test_le_cache_suit_un_renommage_apres_reindex(qf, indexeur, projet) -> None:
    # 1. Premier appel : il chauffe le cache et voit bien les deux homonymes.
    avant = qf["find_symbol"](name="foo")
    assert avant.get("autres_definitions"), (
        "a.py et b.py definissent tous deux `foo` : l'ambiguite devrait etre "
        f"signalee des le premier appel (rendu : {str(avant)[:300]})"
    )

    # 2. Une edition renomme foo -> bar dans a.py, puis reindexe ce fichier,
    #    ce que workflow_ops fait apres chaque replace_symbol_source.
    (projet / "a.py").write_text(
        "def bar(x):\n"
        '    """Definition A, renommee."""\n'
        "    return x + 1\n",
        encoding="utf-8",
    )
    indexeur.reindex_file(str(projet / "a.py"))

    # 3. `foo` n'est plus defini que dans b.py.
    apres = qf["find_symbol"](name="foo")
    assert "b.py" in str(apres.get("file", "")), (
        "le cache perime designe encore a.py, qui ne definit plus `foo` ; "
        f"l'edition suivante irait dans le mauvais fichier (rendu : {str(apres)[:300]})"
    )
    assert not apres.get("autres_definitions"), (
        "a.py est toujours annonce comme autre definisseur de `foo` alors "
        f"qu'il ne l'est plus (rendu : {str(apres)[:300]})"
    )

    # 4. Et le nouveau nom, lui, est visible.
    renomme = qf["find_symbol"](name="bar")
    assert "a.py" in str(renomme.get("file", "")), (
        "le cache perime ne connait pas `bar`, ajoute par l'edition : "
        f"{str(renomme)[:300]}"
    )
