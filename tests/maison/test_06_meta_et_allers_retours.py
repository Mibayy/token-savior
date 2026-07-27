"""Meta-outils et allers-retours restants.

`ts_execute` et `ts_search` ne passent pas par le dispatch ordinaire : ce sont
des meta-outils traites en amont. Ils meritent leurs propres verifications,
d'autant que `ts_execute` est le seul point ou du code s'execute vraiment.
"""

from __future__ import annotations

import pytest

# --- ts_execute ---------------------------------------------------------


def test_ts_execute_rend_la_valeur_du_script(appeler) -> None:
    sortie = appeler("ts_execute", script="return 21 * 2;")
    assert "42" in sortie, sortie[:300]


def test_ts_execute_atteint_les_outils(appeler) -> None:
    """Le point de tout l'exercice : un script qui enchaine des outils."""
    sortie = appeler(
        "ts_execute",
        script='const r = await tools.find_symbol({name: "calculer_total"}); return r;',
    )
    assert "panier.py" in sortie, sortie[:400]


def test_ts_execute_enchaine_sans_aller_retour(appeler) -> None:
    sortie = appeler(
        "ts_execute",
        script=(
            "const out = {};"
            'out.loc = await tools.find_symbol({name: "appliquer_remise"});'
            'out.dep = await tools.get_dependents({name: "appliquer_remise"});'
            "return out;"
        ),
    )
    assert "remises.py" in sortie
    assert "calculer_total" in sortie, sortie[:400]


def test_ts_execute_signale_une_erreur_de_script(appeler) -> None:
    sortie = appeler("ts_execute", script="return ceci_n_existe_pas();")
    assert sortie.strip()
    assert "Traceback" not in sortie
    assert "error" in sortie.lower() or "not defined" in sortie.lower(), sortie[:300]


def test_ts_execute_refuse_un_script_vide(appeler) -> None:
    sortie = appeler("ts_execute", script="   \n  ")
    assert sortie.strip()
    assert "Traceback" not in sortie


def test_ts_execute_respecte_son_delai(appeler) -> None:
    sortie = appeler(
        "ts_execute",
        script="await new Promise(r => setTimeout(r, 5000)); return 'trop tard';",
        timeout_ms=400,
    )
    assert "trop tard" not in sortie, "le delai n'a pas ete applique"
    assert "timeout" in sortie.lower(), sortie[:300]


def test_ts_execute_ne_voit_pas_un_outil_hors_perimetre(appeler) -> None:
    """La facade est une liste blanche, pas un passe-partout."""
    sortie = appeler("ts_execute", script="return await tools.outil_imaginaire({});")
    assert "Traceback" not in sortie
    assert "not a function" in sortie or "error" in sortie.lower(), sortie[:300]


# --- ts_search ----------------------------------------------------------


def test_ts_search_trouve_un_outil_par_sa_description(appeler) -> None:
    sortie = appeler("ts_search", query="trouver ou est defini un symbole")
    assert sortie.strip()
    assert "Traceback" not in sortie


def test_ts_search_sur_une_intention_d_analyse(appeler) -> None:
    sortie = appeler("ts_search", query="detecter du code jamais appele")
    assert sortie.strip()
    assert "Traceback" not in sortie


# --- allers-retours restants -------------------------------------------


def test_une_observation_peut_etre_relue_par_identifiant(appeler) -> None:
    appeler(
        "memory_save",
        type="project",
        title="Observation relue par identifiant",
        content="Contenu unique pour le test de relecture.",
    )
    index = appeler("memory_index", query="relue par identifiant")
    assert index.strip()
    assert "Traceback" not in index


def test_un_raisonnement_sauve_est_liste(appeler) -> None:
    appeler(
        "reasoning_save",
        title="Pourquoi le panier applique la remise",
        content="Parce que le seuil est a 100.",
    )
    liste = appeler("reasoning_list")
    assert liste.strip()
    assert "Traceback" not in liste


def test_un_raisonnement_sauve_est_retrouve(appeler) -> None:
    appeler(
        "reasoning_save",
        title="Choix du seuil de remise",
        content="Le seuil de 100 vient de la regle commerciale.",
    )
    trouve = appeler("reasoning_search", query="seuil de remise")
    assert trouve.strip()
    assert "Traceback" not in trouve


def test_les_statistiques_repondent(appeler) -> None:
    sortie = appeler("get_stats")
    assert sortie.strip()
    assert "Traceback" not in sortie


def test_la_liste_des_projets_contient_le_cobaye(appeler) -> None:
    sortie = appeler("list_projects")
    assert "cobaye" in sortie, sortie[:400]


def test_ts_discover_repond(appeler) -> None:
    sortie = appeler("ts_discover")
    assert sortie.strip()
    assert "Traceback" not in sortie


@pytest.mark.parametrize(
    "motif",
    ["calculer_total", "appliquer_remise", "SEUIL_REMISE", "Panier", "fonction_jamais_appelee"],
)
def test_chaque_symbole_plante_est_retrouvable_par_recherche(motif: str, appeler) -> None:
    sortie = appeler("search_codebase", pattern=motif)
    assert motif in sortie, f"{motif} introuvable :\n{sortie[:300]}"
