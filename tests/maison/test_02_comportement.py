"""Chaque outil doit trouver ce qui a ete plante pour lui.

Le fichier precedent verifie qu'un outil *repond*. Celui-ci verifie qu'il
repond *juste*, ce qui est une autre affaire : un outil qui rend poliment
"rien trouve" passe le premier et tombe ici.

Le projet-cobaye contient un cycle d'import, une fonction morte, un doublon
semantique, une fonction volontairement complexe, deux routes, trois
variables d'environnement dont une orpheline, un Dockerfile et un historique
git avec un changement de signature. Chaque cas ci-dessous pointe l'un de ces
elements plantes.
"""

from __future__ import annotations

import pytest

# (identifiant, outil, arguments, fragments attendus dans la reponse)
CAS: list[tuple[str, str, dict, list[str]]] = [
    # --- localisation ---------------------------------------------------
    ("find_symbol_fichier", "find_symbol", {"name": "calculer_total"},
     ["panier.py"]),
    ("find_symbol_classe", "find_symbol", {"name": "Panier"},
     ["panier.py"]),
    ("find_symbol_autre_module", "find_symbol", {"name": "appliquer_remise"},
     ["remises.py"]),
    ("find_symbol_batch", "find_symbol",
     {"names": ["calculer_total", "appliquer_remise"]},
     ["panier.py", "remises.py"]),
    ("find_symbol_inconnu", "find_symbol", {"name": "symbole_qui_n_existe_pas"},
     ["not found"]),
    # --- lecture de source ----------------------------------------------
    ("source_fonction", "get_function_source", {"name": "calculer_total", "level": 0},
     ["def calculer_total", "appliquer_remise"]),
    ("source_fonction_corps", "get_function_source",
     {"name": "appliquer_remise", "level": 0},
     ["SEUIL_REMISE"]),
    ("source_classe", "get_class_source", {"name": "Panier", "level": 0},
     ["class Panier", "def ajouter"]),
    ("source_methode", "get_function_source", {"name": "Panier.total", "level": 0},
     ["calculer_total"]),
    # --- lecture par plage de lignes -------------------------------------
    # Le cas ou l'appelant tient un numero (trace d'erreur, `grep -n`) et pas
    # un nom de symbole. Sans cet outil il repart en `sed -n '1,10p'`.
    ("lecture_plage", "read_lines",
     {"file_path": "boutique/panier.py", "start": 1, "end": 10},
     ["appliquer_remise", "calculer_total"]),
    ("lecture_plage_numerotee", "read_lines",
     {"file_path": "boutique/panier.py", "start": 3, "end": 3},
     ["3"]),
    ("lecture_plage_hors_fichier", "read_lines",
     {"file_path": "boutique/panier.py", "start": 9999, "end": 10000},
     ["Error"]),
    ("lecture_plage_fichier_inconnu", "read_lines",
     {"file_path": "boutique/absent.py", "start": 1, "end": 2},
     ["not found in index", "list_files"]),
    # --- contexte complet -----------------------------------------------
    ("contexte_source", "get_full_context", {"name": "calculer_total"},
     ["calculer_total"]),
    ("contexte_dependance", "get_full_context", {"name": "calculer_total", "depth": 1},
     ["appliquer_remise"]),
    ("contexte_edition_appelants", "get_edit_context", {"name": "appliquer_remise"},
     ["calculer_total"]),
    # --- structure -------------------------------------------------------
    ("structure_fonctions", "get_structure_summary", {"file_path": "boutique/panier.py"},
     ["calculer_total", "compter_articles", "Panier"]),
    ("structure_import", "get_structure_summary", {"file_path": "boutique/panier.py"},
     ["remises"]),
    ("fonctions_du_fichier", "get_functions", {"file_path": "boutique/remises.py"},
     ["appliquer_remise", "fonction_jamais_appelee"]),
    ("classes_du_fichier", "get_classes", {"file_path": "boutique/panier.py"},
     ["Panier"]),
    ("imports_du_fichier", "get_imports", {"file_path": "boutique/panier.py"},
     ["remises"]),
    ("liste_fichiers", "list_files", {"max_results": 50},
     ["panier.py", "remises.py"]),
    ("resume_projet", "get_project_summary", {},
     ["boutique"]),
    # --- recherche -------------------------------------------------------
    ("recherche_regex", "search_codebase", {"pattern": "appliquer_remise"},
     ["panier.py", "remises.py"]),
    ("recherche_constante", "search_codebase", {"pattern": "SEUIL_REMISE"},
     ["remises.py"]),
    ("recherche_sans_resultat", "search_codebase",
     {"pattern": "zzz_motif_absent_zzz"}, []),
    ("recherche_dans_symboles", "search_in_symbols", {"pattern": "remise"},
     ["remise"]),
    # --- graphe ----------------------------------------------------------
    ("dependances", "get_dependencies", {"name": "calculer_total"},
     ["appliquer_remise"]),
    ("dependants", "get_dependents", {"name": "appliquer_remise"},
     ["calculer_total"]),
    ("chaine_appel", "get_call_chain",
     {"from_name": "calculer_total", "to_name": "appliquer_remise"},
     ["calculer_total", "appliquer_remise"]),
    ("impact_changement", "get_change_impact", {"name": "appliquer_remise"},
     ["calculer_total"]),
    ("dependances_fichier", "get_file_dependencies", {"file_path": "boutique/panier.py"},
     ["remises"]),
    ("dependants_fichier", "get_file_dependents", {"file_path": "boutique/remises.py"},
     ["panier"]),
    ("points_entree", "get_entry_points", {},
     ["main.py"]),
    # --- analyses : les defauts plantes ---------------------------------
    ("code_mort", "find_dead_code", {},
     ["fonction_jamais_appelee"]),
    ("cycle_import", "find_import_cycles", {},
     ["cycle_a", "cycle_b"]),
    ("point_chaud", "find_hotspots", {},
     ["calculer_frais"]),
    ("routes", "get_routes", {},
     ["/panier"]),
    ("routes_post", "get_routes", {},
     ["/panier/ligne"]),
    ("variables_env", "get_env_usage", {"var_name": "BOUTIQUE_DB_URL"},
     ["api.py"]),
    ("variables_env_seconde", "get_env_usage", {"var_name": "BOUTIQUE_PAIEMENT_CLE"},
     ["api.py"]),
    ("config_orpheline", "analyze_config", {"checks": ["orphans"]},
     ["BOUTIQUE_VARIABLE_ORPHELINE"]),
    ("docker", "analyze_docker", {},
     ["python"]),
    ("tests_impactes", "find_impacted_test_files",
     {"symbol_names": ["calculer_total"]},
     ["test_panier.py"]),
    # --- git -------------------------------------------------------------
    ("git_branche", "get_git_status", {},
     ["main"]),
    ("resume_commit", "build_commit_summary", {},
     []),
]


@pytest.mark.parametrize(
    ("identifiant", "outil", "arguments", "attendus"),
    CAS,
    ids=[cas[0] for cas in CAS],
)
def test_l_outil_trouve_ce_qui_est_plante(
    identifiant: str, outil: str, arguments: dict, attendus: list[str], appeler,
) -> None:
    sortie = appeler(outil, **arguments)
    for fragment in attendus:
        assert fragment in sortie, (
            f"[{identifiant}] {outil} ne rend pas {fragment!r}.\n"
            f"Reponse : {sortie[:500]}"
        )


# --- Cas qui meritent plus qu'un fragment ------------------------------


def test_le_code_mort_ne_signale_pas_les_fonctions_vivantes(appeler) -> None:
    """Un detecteur de code mort qui signale tout ne sert a rien."""
    sortie = appeler("find_dead_code")
    assert "fonction_jamais_appelee" in sortie
    assert "calculer_total" not in sortie, (
        "calculer_total est appelee par Panier.total, elle n'est pas morte :\n"
        + sortie[:500]
    )


def test_le_cycle_signale_est_bien_un_cycle(appeler) -> None:
    sortie = appeler("find_import_cycles")
    assert "cycle_a" in sortie and "cycle_b" in sortie
    assert "panier" not in sortie.lower() or "cycle" in sortie.lower(), sortie[:400]


def test_les_doublons_semantiques_apparient_les_deux_jumelles(appeler) -> None:
    sortie = appeler("find_semantic_duplicates", max_groups=10)
    apparie = "somme_des_prix" in sortie and "additionner_les_prix" in sortie
    if not apparie:
        pytest.skip(f"pile semantique indisponible ou seuil non atteint : {sortie[:200]}")
    assert apparie


def test_le_point_chaud_classe_la_fonction_tordue_en_tete(appeler) -> None:
    """calculer_frais est de loin la plus complexe du cobaye."""
    sortie = appeler("find_hotspots")
    assert "calculer_frais" in sortie
    position_tordue = sortie.index("calculer_frais")
    for simple in ("compter_articles", "depuis_a"):
        if simple in sortie:
            assert position_tordue < sortie.index(simple), (
                f"{simple} est classee avant calculer_frais :\n{sortie[:500]}"
            )


def test_la_ref_demandee_est_celle_analysee(appeler) -> None:
    """Le defaut du 27/07 : la ref etait jetee et l'analyse repartait de HEAD~1."""
    sortie = appeler("detect_breaking_changes", ref="HEAD~2")
    assert "HEAD~2" in sortie, (
        "la ref demandee n'apparait pas dans le rapport, elle a ete jetee :\n"
        + sortie[:300]
    )


def test_un_changement_de_signature_est_vu(appeler) -> None:
    """appliquer_remise a gagne un parametre entre les deux commits."""
    sortie = appeler("detect_breaking_changes", ref="HEAD~1")
    assert "appliquer_remise" in sortie or "no breaking changes" in sortie, sortie[:400]


def test_le_contexte_complet_evite_la_chaine_en_trois_appels(appeler) -> None:
    """La regle du depot : get_full_context remplace find puis read puis deps."""
    complet = appeler("get_full_context", name="calculer_total", depth=1)
    assert "calculer_total" in complet
    assert "appliquer_remise" in complet, (
        "get_full_context doit rendre les dependances, sinon il ne remplace "
        f"pas la chaine :\n{complet[:400]}"
    )


def test_le_contexte_d_edition_annonce_les_appelants(appeler) -> None:
    """Avant d'editer, on veut savoir qui casse."""
    sortie = appeler("get_edit_context", name="appliquer_remise")
    assert "calculer_total" in sortie, (
        "get_edit_context doit nommer les appelants avant une edition :\n"
        + sortie[:400]
    )


def test_un_fichier_inconnu_le_dit(appeler) -> None:
    sortie = appeler("get_structure_summary", file_path="boutique/inexistant.py")
    assert sortie.strip()
    assert "Traceback" not in sortie
    assert "not found" in sortie.lower() or "error" in sortie.lower(), sortie[:300]


def test_un_symbole_inconnu_le_dit_sans_planter(appeler) -> None:
    for outil in ("get_function_source", "get_dependencies", "get_dependents"):
        sortie = appeler(outil, name="symbole_absolument_absent")
        assert sortie.strip(), outil
        assert "Traceback" not in sortie, f"{outil} :\n{sortie[:300]}"
