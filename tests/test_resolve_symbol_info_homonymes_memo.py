"""Le comptage des classes homonymes passe par un cache, comme les fonctions.

`_resolve_symbol_info` a besoin de savoir combien de fichiers definissent une
CLASSE d'un nom donne : sans ce comptage, il rendrait le premier match d'un
projet polyglotte comme s'il etait le seul. Ce comptage se faisait en clair,
`sorted(index.files.items())` + balayage de toutes les classes, a chaque appel —
donc a chaque `find_symbol`, et une fois par dependance dans `get_dependents`,
`get_dependencies` et `get_call_chain`. Le cache pose sur
`_fichiers_definissant` ne l'atteignait pas.

Ce test verifie le chemin emprunte (cache dedie rempli, index complet non
construit sous le seuil) ET le service rendu (l'ambiguite de classe reste
signalee, avec les memes fichiers dans le meme ordre).
"""

from __future__ import annotations

import pytest


@pytest.fixture
def projet(tmp_path):
    """Deux fichiers definissent la classe `Tarif` — ambiguite reelle."""
    (tmp_path / "a.py").write_text(
        "class Tarif:\n"
        '    """Definition A."""\n'
        "    def montant(self):\n"
        "        return 1\n",
        encoding="utf-8",
    )
    (tmp_path / "b.py").write_text(
        "class Tarif:\n"
        '    """Definition B."""\n'
        "    def montant(self):\n"
        "        return 2\n",
        encoding="utf-8",
    )
    (tmp_path / "c.py").write_text(
        "class Facture:\n"
        '    """Seule de son nom."""\n'
        "    def total(self):\n"
        "        return 3\n",
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


def test_comptage_homonymes_de_classe_est_memoise(engine) -> None:
    assert engine._classes_homonymes_index is None, (
        "l'instance doit etre fraiche avant le premier appel"
    )
    assert engine._classes_homonymes_memo == {}

    engine.find_symbol(name="Facture")

    assert len(engine._classes_homonymes_memo) > 0, (
        "le comptage des classes homonymes doit passer par un cache dedie, "
        "pas par un balayage refait a chaque appel"
    )
    assert engine._classes_homonymes_index is None, (
        "un seul nom demande ne franchit pas _SEUIL_INDEX_DEFINISSEURS : "
        "l'index complet du projet ne doit pas etre reconstruit"
    )

    # Deuxieme appel sur le meme nom : servi par le memo, aucun scan de plus.
    avant = dict(engine._classes_homonymes_memo)
    engine.find_symbol(name="Facture")
    assert engine._classes_homonymes_memo == avant


def test_le_service_rendu_ne_change_pas(engine) -> None:
    """L'ambiguite entre `Tarif` de a.py et `Tarif` de b.py reste signalee."""
    resultat = engine.find_symbol(name="Tarif")

    rendu = str(resultat)
    assert "a.py" in rendu and "b.py" in rendu, (
        "les deux fichiers definissant `Tarif` doivent etre nommes dans la "
        f"reponse (rendu : {rendu[:400]})"
    )

    homonymes = engine._classes_definissant("Tarif")
    assert homonymes == sorted(homonymes), "ordre alphabetique attendu"
    assert [h.split("/")[-1] for h in homonymes] == ["a.py", "b.py"]

    # Un nom sans homonyme reste resolu normalement, pas signale comme ambigu.
    seule = engine.find_symbol(name="Facture")
    assert "ambiguous" not in str(seule)


def test_le_cache_tombe_quand_les_fichiers_changent(engine, projet, indexeur) -> None:
    """Meme invalidation que `_fichiers_definissant` : contre `files_version`."""
    assert engine._classes_definissant("Tarif")

    (projet / "b.py").write_text(
        "class TarifRenomme:\n"
        '    """Plus d\'homonyme."""\n'
        "    def montant(self):\n"
        "        return 2\n",
        encoding="utf-8",
    )
    indexeur.reindex_file(str(projet / "b.py"))

    apres = engine._classes_definissant("Tarif")
    assert [h.split("/")[-1] for h in apres] == ["a.py"], (
        "apres reindexation, le cache doit tomber : `Tarif` n'est plus defini "
        f"que dans a.py (rendu : {apres})"
    )


def test_sorted_paths_perime_est_repare_par_classes_definissant(engine) -> None:
    """Meme reparation que `_fichiers_definissant` quand `sorted_paths` arrive
    vide/perime (cache, deserialisation) alors que des fichiers existent :
    sinon chaque appel en regime scan retrie tout le projet a chaque fois.
    """
    assert engine.index.files, "le fixture doit avoir des fichiers"
    engine.index.sorted_paths = []

    engine._classes_definissant("Tarif")

    assert engine.index.sorted_paths, (
        "sorted_paths perime aurait du etre repare sur l'objet partage, "
        "comme le fait _fichiers_definissant, pour eviter un retri a chaque appel"
    )
    assert engine.index.sorted_paths == sorted(engine.index.files)
