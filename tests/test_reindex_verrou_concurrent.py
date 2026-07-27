"""Le reindex semantique ne doit pas confisquer la base memoire.

Regression du 26/07/2026. `search_codebase(semantic=True)` declenche un
reindex complet des symboles dans le fil de la requete. L'embedding est
volontairement sequentiel (~100 ms par symbole, voir `_embed_batch` : la
version par lot materialise faisait exploser la RSS et declenchait l'OOM
killer). Le commit n'arrivant qu'a la toute fin, le premier INSERT ouvrait
une transaction d'ecriture tenue pendant toute la boucle.

Effet mesure sur ce VPS : plus de 25 minutes de verrou d'ecriture sur la
base memoire partagee. Pendant ce temps aucun autre processus ne pouvait y
ecrire, un second client Token Savior ne demarrait plus du tout parce que
`run_migrations()` echouait sur "database is locked", et le WAL passait de
8 a 15 Mo sans pouvoir etre resorbe.

Ce test tente une ecriture tierce a chaque lot. Avant le correctif, la
tentative du deuxieme lot echoue.
"""

from __future__ import annotations

import sqlite3
import textwrap
from pathlib import Path

import pytest


def _ecrire(racine: Path, rel: str, contenu: str) -> None:
    chemin = racine / rel
    chemin.parent.mkdir(parents=True, exist_ok=True)
    chemin.write_text(textwrap.dedent(contenu), encoding="utf-8")


def _pile_vectorielle_prete() -> bool:
    try:
        from token_savior.db_core import VECTOR_SEARCH_AVAILABLE
        from token_savior.memory.embeddings import is_available
    except Exception:
        return False
    return bool(VECTOR_SEARCH_AVAILABLE and is_available())


@pytest.fixture
def projet(tmp_path: Path) -> Path:
    _ecrire(tmp_path, "pkg/__init__.py", "")
    _ecrire(tmp_path, "pkg/tri.py", '''
        """Aides au tri."""

        def trier_resultats(candidats):
            """Trie les candidats par score decroissant."""
            return sorted(candidats, key=lambda c: c["score"], reverse=True)

        def fusionner_listes(a, b):
            """Fusionne deux listes classees."""
            return list(a) + list(b)
    ''')
    _ecrire(tmp_path, "pkg/graphe.py", '''
        """Aides au graphe."""

        class GrapheImports:
            """Graphe oriente des imports."""

            def trouver_cycles(self):
                """Detecte les composantes fortement connexes."""
                return []

            def ajouter_arete(self, a, b):
                """Enregistre un import de a vers b."""
    ''')
    return tmp_path


@pytest.mark.skipif(
    not _pile_vectorielle_prete(),
    reason="sqlite-vec ou le modele d'embedding est indisponible",
)
def test_le_verrou_n_est_pas_tenu_pendant_l_embedding(
    projet: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from token_savior import db_core
    from token_savior.memory import symbol_embeddings as se

    base = tmp_path / "memoire.db"
    db_core.run_migrations(base)

    ecritures_tierces: list[bool] = []
    embed_original = se._embed_batch

    def embed_espion(docs):
        # Un tiers tente d'ecrire pendant que ce lot s'embedde. Au deuxieme
        # lot, le premier a deja insere : si sa transaction n'a pas ete
        # validee, cette ecriture echoue.
        conn = sqlite3.connect(str(base), timeout=1.0)
        try:
            conn.execute("CREATE TABLE IF NOT EXISTS sonde(x INTEGER)")
            conn.execute("INSERT INTO sonde VALUES (1)")
            conn.commit()
            ecritures_tierces.append(True)
        except sqlite3.OperationalError:
            ecritures_tierces.append(False)
        finally:
            conn.close()
        return embed_original(docs)

    monkeypatch.setattr(se, "_embed_batch", embed_espion)

    # batch_size=1 pour garantir plusieurs lots : c'est la tentative du
    # deuxieme lot qui discrimine, celle du premier passe meme avec le bug.
    resultat = se.reindex_project_symbols(projet, db_path=base, batch_size=1)

    assert resultat["status"] == "ok"
    assert len(ecritures_tierces) >= 2, (
        "il faut au moins deux lots pour que ce test prouve quoi que ce soit, "
        f"obtenu {len(ecritures_tierces)}"
    )
    assert all(ecritures_tierces), (
        "la base est restee verrouillee pendant l'embedding, lot par lot : "
        f"{ecritures_tierces}"
    )


@pytest.mark.skipif(
    not _pile_vectorielle_prete(),
    reason="sqlite-vec ou le modele d'embedding est indisponible",
)
def test_un_symbole_modifie_se_reindexe_apres_un_commit(
    projet: Path, tmp_path: Path,
) -> None:
    """Reecrire un vecteur deja committe doit passer.

    `symbol_vectors` est une table virtuelle vec0. L'ancien
    "INSERT OR REPLACE" ne tenait que parce que la ligne visee venait d'etre
    inseree dans la meme transaction non validee. Des que le commit se fait
    par lot, la ligne est committee et vec0 rend "UNIQUE constraint failed
    on symbol_vectors primary key".

    La suite ne le voyait pas : elle ne testait l'idempotence que sur des
    fichiers *inchanges*, cas ou le content_hash fait sauter le re-embed. Il
    faut un symbole reellement modifie pour toucher le chemin de reecriture.
    Attrape en CI par le bench code_retrieval, pas par les tests.
    """
    from token_savior import db_core
    from token_savior.memory import symbol_embeddings as se

    base = tmp_path / "memoire.db"
    db_core.run_migrations(base)

    premier = se.reindex_project_symbols(projet, db_path=base, batch_size=1)
    assert premier["status"] == "ok"
    assert premier["indexed"] >= 2

    # Le corps change, donc le content_hash change, donc on re-embedde une
    # ligne vectorielle qui existe deja et qui est committee.
    _ecrire(projet, "pkg/tri.py", '''
        """Aides au tri, revisees."""

        def trier_resultats(candidats):
            """Trie les candidats par score croissant, cette fois."""
            return sorted(candidats, key=lambda c: c["score"])

        def fusionner_listes(a, b):
            """Concatene deux listes classees en une seule."""
            return [*a, *b]
    ''')

    second = se.reindex_project_symbols(projet, db_path=base, batch_size=1)
    assert second["status"] == "ok"
    assert second["indexed"] >= 2, (
        "les symboles modifies auraient du etre re-embeddes, "
        f"obtenu {second}"
    )

    # L'index reste coherent apres reecriture : autant de symboles qu'au
    # premier passage, pas de doublon laisse par le DELETE + INSERT.
    with sqlite3.connect(str(base)) as conn:
        total = conn.execute(
            "SELECT COUNT(*) FROM symbols WHERE project_root=?",
            (str(projet.resolve()),),
        ).fetchone()[0]
    assert total == premier["total"]
