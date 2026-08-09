"""Une absence doit dire OU l'on a cherche, pas seulement QUOI on n'a pas trouve.

Panne vecue le 09/08/2026. Le bot Telegram relance un processus `claude -p` a
chaque message, donc un serveur MCP neuf, donc un projet actif remis au defaut.
`find_symbol('run_script_async')` reussissait a un tour, echouait au suivant :

    {"error": "symbol 'run_script_async' not found", "scanned_files": 8, ...}

Le symbole existait bien, dans un projet de 464 fichiers. L'index fouille en
portait 8, ceux d'un autre projet. Le message etait exact et inutilisable : il
a envoye chercher un bug dans `get_edit_context` pendant que la cause etait un
`switch_project` a refaire.

C'est le meme motif que le faux appelant de `get_dependents` : une reponse qui
a l'air precise et ne l'est pas coute plus cher qu'une absence de reponse.
"""

from __future__ import annotations

import json

import pytest


def _moteur(racine):
    from token_savior.project_indexer import ProjectIndexer
    from token_savior.query_api import create_project_query_functions

    idx = ProjectIndexer(str(racine))
    idx.index()
    return create_project_query_functions(idx._project_index)


@pytest.fixture
def api(tmp_path):
    """Un projet minuscule : l'exacte situation d'un index tombe au defaut."""
    (tmp_path / "seul.py").write_text("def present():\n    return 1\n", encoding="utf-8")
    return _moteur(tmp_path)


class TestAbsenceExplicite:
    def test_l_absence_nomme_le_projet_fouille(self, api, tmp_path) -> None:
        r = api["find_symbol"](name="absent_partout")
        assert "error" in r
        assert r.get("projet_actif") not in (None, "", "?"), (
            "sans le chemin reel du projet fouille, l'agent ne peut pas distinguer "
            "« ce symbole n'existe pas » de « je regarde au mauvais endroit » ; "
            "un « ? » donne l'illusion d'une information"
        )
        assert str(tmp_path) in str(r["projet_actif"])

    def test_un_index_minuscule_declenche_la_suggestion(self, api) -> None:
        r = api["find_symbol"](name="absent_partout")
        assert "_suggestion_projet" in r
        assert "switch_project" in r["_suggestion_projet"], (
            "la suggestion doit nommer le geste qui repare, pas seulement le symptome"
        )

    def test_un_symbole_present_reste_trouve(self, api) -> None:
        """Le garde-fou ne doit pas transformer une reussite en avertissement."""
        r = api["find_symbol"](name="present")
        assert "error" not in r
        assert "_suggestion_projet" not in r


class TestPasDeBruitSurUnGrosProjet:
    def test_un_gros_index_ne_suggere_pas_de_changer_de_projet(self, tmp_path) -> None:
        """Sur un projet complet, une absence est une vraie absence.

        Suggerer `switch_project` a chaque symbole manquant d'un projet de 500
        fichiers serait du bruit, et le bruit finit par etre ignore : c'est
        exactement ce qui rend un avertissement inutile le jour ou il compte.
        """
        for i in range(40):
            (tmp_path / f"m{i}.py").write_text(f"def f{i}():\n    return {i}\n", encoding="utf-8")
        r = _moteur(tmp_path)["find_symbol"](name="absent_partout")
        assert "error" in r
        assert "_suggestion_projet" not in r
        assert str(tmp_path) in str(r.get("projet_actif")), (
            "le projet reste nomme, meme sans suggestion")
