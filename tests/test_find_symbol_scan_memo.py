"""`find_symbol` ne fait qu'un appel : le regime scan l'amortit lui-meme.

`_fichiers_definissant` a deux regimes : un scan memoise tant qu'on reste sous
`_SEUIL_INDEX_DEFINISSEURS` noms distincts (appels cumulatifs), l'index complet
apres. Le seuil suppose qu'un appelant demande plusieurs noms — vrai pour
`get_dependents`, faux pour `find_symbol`, qui n'en demande qu'UN par requete.

Optimisation : pour un seul appel, un scan court (75ms) coute moins cher que
construire l'index entier (201ms). Chaque message Telegram = nouveau processus =
nouvel engine, donc le memo ne s'accumule jamais. `find_symbol` reste donc en
regime scan, qui est optimal pour lui.

Ce test verifie le chemin emprunte : apres UN seul appel a `find_symbol` sur
une instance fraiche, le memo doit etre rempli mais l'index ne doit PAS exister.
Le service rendu (signaler les homonymes) reste identique.
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
def engine(indexeur):
    """L'engine lui-meme, pas le dict `as_dict()` : le test regarde son cache."""
    from token_savior.query_api import ProjectQueryEngine

    return ProjectQueryEngine(indexeur._project_index)


def test_un_seul_find_symbol_reste_en_scan_memoise(engine) -> None:
    assert engine._fichiers_definissant_index is None, (
        "l'instance doit etre fraiche avant le premier appel"
    )

    resultat = engine.find_symbol(name="foo")

    assert engine._fichiers_definissant_index is None, (
        "apres un unique find_symbol, l'engine doit rester dans le regime scan "
        "memoise (index None, memo non vide). Un seul nom demande ne franchit "
        "jamais le seuil _SEUIL_INDEX_DEFINISSEURS, donc l'index ne se "
        "construit pas. C'est l'optimisation : scan court (75ms) au lieu de "
        "construction d'index (201ms) pour find_symbol qui ne fait qu'un appel."
    )
    
    assert len(engine._fichiers_definissant_memo) > 0, (
        "Le nom 'foo' doit etre en memo (scan court effectue)"
    )

    # Le service rendu ne change pas : l'homonyme reste signale.
    assert resultat.get("autres_definitions"), (
        "a.py et b.py definissent tous deux `foo` : l'ambiguite doit rester "
        f"signalee (rendu : {str(resultat)[:300]})"
    )
