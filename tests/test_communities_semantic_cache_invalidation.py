"""Deux caches de plus qui survivaient a l'edition, comme `_fichiers_definissant`.

`_communities` et `_semantic_hash_cache` etaient construits au premier appel
puis jamais reconstruits. Or `slot_manager` garde un engine par projet et le
reutilise pour tous les appels suivants : la premiere reponse figeait donc le
decoupage en communautes et l'empreinte des sources pour le reste de la
session.

Consequence pour l'agent : apres un `replace_symbol_source` ou un
`move_symbol`, `get_symbol_cluster` rend le voisinage d'AVANT l'edition et
`find_semantic_duplicates` compare des sources qui n'existent plus. Une reponse
perimee sans rien qui la signale coute plus cher qu'un recalcul : l'agent la
prend pour vraie et repart chercher lui-meme, en natif.

Sens de l'erreur a preferer, comme pour le jumeau : reconstruire inutilement
coute un balayage ; rendre un voisinage perime coute une edition au mauvais
endroit.
"""

from __future__ import annotations

import pytest


@pytest.fixture
def projet(tmp_path):
    """Au depart, `uniq_a` et `uniq_b` n'ont ni la meme forme ni de lien."""
    (tmp_path / "a.py").write_text(
        "def uniq_a(x):\n"
        "    return x * 2 + 7\n"
        "\n"
        "\n"
        "def helper_a(v):\n"
        "    return v + 1\n"
        "\n"
        "\n"
        "def caller(x):\n"
        "    return helper_a(x)\n",
        encoding="utf-8",
    )
    (tmp_path / "b.py").write_text(
        "def uniq_b(y):\n"
        "    return y - 3\n"
        "\n"
        "\n"
        "def lonely(z):\n"
        "    return z\n",
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


def _groupes(rendu: str) -> list[str]:
    """Les lignes `  hash ...: a, b` de find_semantic_duplicates."""
    return [ligne for ligne in str(rendu).splitlines() if ligne.strip().startswith("hash ")]


def test_find_semantic_duplicates_suit_une_edition_apres_reindex(qf, indexeur, projet) -> None:
    # 1. Premier appel : il chauffe le cache d'empreintes. Les deux corps
    #    different, donc aucun groupe ne doit reunir uniq_a et uniq_b.
    avant = qf["find_semantic_duplicates"](min_lines=1)
    assert not [g for g in _groupes(avant) if "uniq_a" in g and "uniq_b" in g], (
        "uniq_a (`x * 2 + 7`) et uniq_b (`y - 3`) n'ont pas la meme forme : "
        f"ils ne devraient pas etre groupes (rendu : {str(avant)[:400]})"
    )

    # 2. Une edition donne a uniq_b la meme forme que uniq_a, puis reindexe le
    #    fichier, ce que workflow_ops fait apres chaque replace_symbol_source.
    (projet / "b.py").write_text(
        "def uniq_b(z):\n"
        "    return z * 2 + 7\n"
        "\n"
        "\n"
        "def lonely(z):\n"
        "    return z\n",
        encoding="utf-8",
    )
    indexeur.reindex_file(str(projet / "b.py"))

    # 3. Le doublon existe maintenant vraiment ; le cache doit l'avoir vu.
    apres = qf["find_semantic_duplicates"](min_lines=1)
    assert [g for g in _groupes(apres) if "uniq_a" in g and "uniq_b" in g], (
        "le cache d'empreintes est reste fige sur les sources d'avant "
        "l'edition : uniq_b a desormais le corps de uniq_a et le doublon "
        f"n'est pas signale (rendu : {str(apres)[:400]})"
    )


def test_get_symbol_cluster_suit_une_edition_apres_reindex(qf, indexeur, projet) -> None:
    # 1. Premier appel : il chauffe le decoupage en communautes. `caller`
    #    n'appelle que helper_a, donc helper_b n'existe pas encore.
    avant = qf["get_symbol_cluster"](name="caller")
    assert "helper_b" not in str(avant), (
        "helper_b n'est pas encore defini : il ne peut pas etre dans le "
        f"cluster de caller (rendu : {str(avant)[:400]})"
    )

    # 2. L'edition ajoute helper_b et le fait appeler par caller — le graphe de
    #    dependances change, donc le decoupage en communautes aussi.
    (projet / "a.py").write_text(
        "def uniq_a(x):\n"
        "    return x * 2 + 7\n"
        "\n"
        "\n"
        "def helper_a(v):\n"
        "    return v + 1\n"
        "\n"
        "\n"
        "def helper_b(v):\n"
        "    return v * 3\n"
        "\n"
        "\n"
        "def caller(x):\n"
        "    return helper_a(x) + helper_b(x)\n",
        encoding="utf-8",
    )
    indexeur.reindex_file(str(projet / "a.py"))

    # 3. helper_b fait partie du voisinage de caller.
    apres = qf["get_symbol_cluster"](name="caller")
    membres = [m.get("name", "") for m in (apres.get("members") or [])]
    assert any("helper_b" in m for m in membres), (
        "le decoupage en communautes est reste fige sur le graphe d'avant "
        "l'edition : helper_b, appele par caller, n'apparait pas dans son "
        f"cluster (membres : {membres}, rendu : {str(apres)[:400]})"
    )
