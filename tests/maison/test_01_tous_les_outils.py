"""Les 69 outils, appeles comme un agent les appelle.

Trois garanties, dans cet ordre d'importance :

1. **Aucun outil declare ne reste jamais exerce.** Le test de couverture
   echoue si un outil apparait dans TOOL_SCHEMAS sans entree ici. C'est ce qui
   rend "on a teste tous les outils" verifiable au lieu d'affirme.
2. Chaque outil repond quelque chose d'exploitable sur un projet reel : pas de
   trace Python, pas de KeyError brute, pas de reponse vide.
3. Un argument obligatoire absent produit un message qui nomme l'argument.

Ce que ce fichier ne fait pas : verifier que la reponse est *juste*. C'est
l'objet de test_02_comportement_par_outil.py. Un outil peut passer ici en
rendant une betise polie.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from token_savior.tool_schemas import TOOL_SCHEMAS

# Outils qui modifient le projet : ils tournent sur une copie jetable.
OUTILS_MUTANTS = {
    "replace_symbol_source",
    "insert_near_symbol",
    "move_symbol",
    "add_field_to_model",
    "edit_lines_in_symbol",
    "checkpoint",
    "reindex",
    "capture_purge",
    "memory_delete",
    "memory_admin",
    "corpus_build",
}

# Outils qui lancent des processus : on les borne pour ne pas transformer la
# suite en execution de code arbitraire.
OUTILS_A_PROCESSUS = {"run_project_action", "run_impacted_tests", "ts_execute"}

ARGUMENTS_REALISTES: dict[str, dict] = {
    # --- navigation ----------------------------------------------------
    "find_symbol": {"name": "calculer_total"},
    "get_function_source": {"name": "calculer_total"},
    "get_class_source": {"name": "Panier"},
    "get_full_context": {"name": "calculer_total"},
    "get_edit_context": {"name": "calculer_total"},
    "get_structure_summary": {"file_path": "boutique/panier.py"},
    "get_functions": {"file_path": "boutique/panier.py"},
    "get_classes": {"file_path": "boutique/panier.py"},
    "get_imports": {"file_path": "boutique/panier.py"},
    "list_files": {"max_results": 10},
    "get_project_summary": {},
    "search_codebase": {"pattern": "calculer_total"},
    "search_in_symbols": {"pattern": "remise"},
    # --- graphe --------------------------------------------------------
    "get_dependencies": {"name": "calculer_total"},
    "get_dependents": {"name": "appliquer_remise"},
    "get_call_chain": {"from_name": "calculer_total", "to_name": "appliquer_remise"},
    "get_change_impact": {"name": "appliquer_remise"},
    "get_file_dependencies": {"file_path": "boutique/panier.py"},
    "get_file_dependents": {"file_path": "boutique/remises.py"},
    "get_entry_points": {},
    "get_feature_files": {"keyword": "panier"},
    # --- analyses ------------------------------------------------------
    "find_dead_code": {},
    "find_hotspots": {},
    "find_import_cycles": {},
    "find_semantic_duplicates": {"max_groups": 5},
    "detect_breaking_changes": {"ref": "HEAD~1"},
    "find_impacted_test_files": {"symbol_names": ["calculer_total"]},
    "get_routes": {},
    "get_env_usage": {"var_name": "BOUTIQUE_DB_URL"},
    "analyze_config": {"checks": ["orphans"]},
    "analyze_docker": {},
    "get_db_schema": {},
    # --- git -----------------------------------------------------------
    "get_diagnostics": {},
    "get_git_status": {},
    "get_changed_symbols": {},
    "build_commit_summary": {"changed_files": ["boutique/panier.py"]},
    "checkpoint": {"label": "essai-maison"},
    # --- projet --------------------------------------------------------
    "list_projects": {},
    "switch_project": {"name": None},  # rempli au moment du test
    "set_project_root": {"path": None},  # rempli au moment du test
    "reindex": {},
    "get_stats": {},
    "discover_project_actions": {},
    "run_project_action": {"action_id": "action_qui_n_existe_pas"},
    "run_impacted_tests": {"name": "fonction_jamais_appelee"},
    # --- edition -------------------------------------------------------
    "replace_symbol_source": {
        "symbol_name": "compter_articles",
        "file_path": "boutique/panier.py",
        "new_source": (
            "def compter_articles(lignes):\n"
            '    """Nombre total d\'articles, toutes lignes confondues."""\n'
            "    return sum(ligne[\"quantite\"] for ligne in lignes)\n"
        ),
    },
    "insert_near_symbol": {
        "symbol_name": "compter_articles",
        "file_path": "boutique/panier.py",
        "position": "after",
        "content": "\n\ndef ajoutee_par_le_test():\n    \"\"\"Inseree.\"\"\"\n    return 1\n",
    },
    "move_symbol": {
        "symbol": "compter_articles",
        "target_file": "boutique/remises.py",
    },
    "add_field_to_model": {
        "model": "Panier",
        "field_name": "devise",
        "field_type": "str",
        "file_path": "boutique/panier.py",
    },
    "edit_lines_in_symbol": {
        "symbol_name": "compter_articles",
        "file_path": "boutique/panier.py",
        "old_string": "toutes lignes confondues",
        "new_string": "toutes lignes reunies",
    },
    # --- memoire -------------------------------------------------------
    "memory_save": {
        "type": "project",
        "title": "Essai serie maison",
        "content": "Observation ecrite par la serie maison.",
    },
    "memory_index": {"query": "essai"},
    "memory_search": {"query": "essai"},
    "memory_get": {"ids": []},  # rempli au moment du test
    "memory_delete": {"id": None},  # rempli au moment du test
    "memory_admin": {"action": "stats"},
    # --- captures ------------------------------------------------------
    "capture_put": {"tool_name": "Bash", "output": "sortie capturee par la serie maison"},
    "capture_list": {},
    "capture_search": {"query": "capturee"},
    "capture_get": {"id": None},  # rempli au moment du test
    "capture_aggregate": {},
    "capture_purge": {"older_than_days": 3650},
    # --- raisonnement --------------------------------------------------
    "reasoning_save": {"title": "Essai", "content": "Un raisonnement d'essai."},
    "reasoning_list": {},
    "reasoning_search": {"query": "essai"},
    # --- corpus --------------------------------------------------------
    "corpus_build": {"name": "cobaye"},
    "corpus_query": {"name": "cobaye", "question": "a quoi sert le panier ?"},
    # --- meta ----------------------------------------------------------
    "ts_search": {"query": "trouver un symbole"},
    "ts_discover": {},
    "ts_stale_context": {},
    "ts_execute": {"script": "return 1 + 1;"},
}

TRACES_INTERDITES = ("Traceback (most recent call last)", "KeyError:", "AttributeError:")


@pytest.fixture
def copie_projet(projet_cobaye: Path, tmp_path: Path) -> Path:
    """Copie jetable, pour tout outil qui modifie le projet."""
    cible = tmp_path / "cobaye"
    shutil.copytree(projet_cobaye, cible)
    return cible


def _arguments_pour(nom: str, appeler, copie: Path | None) -> dict:
    args = dict(ARGUMENTS_REALISTES[nom])
    if nom == "switch_project":
        args["name"] = appeler.racine
    if nom == "set_project_root":
        args["path"] = appeler.racine
    if nom in {"memory_get", "memory_delete", "capture_get"}:
        # Ces trois-la ont besoin d'un identifiant existant : on en fabrique un.
        if nom == "capture_get":
            appeler(
                "capture_put",
                tool_name="Bash",
                output="contenu fabrique pour capture_get",
            )
            args["id"] = 1
        else:
            appeler(
                "memory_save",
                type="project",
                title="Pour memory_get",
                content="Contenu d'essai.",
            )
            if nom == "memory_get":
                args["ids"] = [1]
            else:
                args["id"] = 1
    if copie is not None:
        args["project"] = str(copie)
    return args


def test_la_table_couvre_tous_les_outils_declares() -> None:
    """La garantie qui rend "tous les outils" verifiable.

    Si un outil est ajoute a TOOL_SCHEMAS sans entree ici, ce test tombe. Sans
    lui, "on teste tous les outils" resterait une affirmation invérifiable qui
    se perime au premier ajout.
    """
    declares = set(TOOL_SCHEMAS)
    couverts = set(ARGUMENTS_REALISTES)
    manquants = sorted(declares - couverts)
    en_trop = sorted(couverts - declares)
    assert not manquants, f"outils declares jamais exerces : {manquants}"
    assert not en_trop, f"outils exerces mais plus declares : {en_trop}"


@pytest.mark.parametrize("nom", sorted(ARGUMENTS_REALISTES))
def test_chaque_outil_repond_sans_trace(nom: str, appeler, copie_projet: Path) -> None:
    """Aucun outil ne doit rendre une trace Python a un client."""
    copie = copie_projet if nom in OUTILS_MUTANTS or nom in OUTILS_A_PROCESSUS else None
    sortie = appeler(nom, **_arguments_pour(nom, appeler, copie))
    assert isinstance(sortie, str)
    assert sortie.strip(), f"{nom} rend une reponse vide"
    for trace in TRACES_INTERDITES:
        assert trace not in sortie, f"{nom} laisse fuir une trace :\n{sortie[:600]}"
    # Appele avec des arguments realistes, un outil ne doit pas repondre qu'il
    # lui en manque : ce serait la table ci-dessus qui est fausse. Sans cette
    # ligne, un message d'erreur passait pour une reponse valide -- c'est
    # exactement ainsi que trois entrees erronees ont survecu au premier jet.
    assert "requires '" not in sortie, (
        f"{nom} : les arguments de la table sont incorrects.\n{sortie[:400]}"
    )


OUTILS_AVEC_OBLIGATOIRES = sorted(
    nom
    for nom in TOOL_SCHEMAS
    if (TOOL_SCHEMAS[nom].get("inputSchema") or {}).get("required")
)


@pytest.mark.parametrize("nom", OUTILS_AVEC_OBLIGATOIRES)
def test_un_argument_obligatoire_absent_est_nomme(nom: str, appeler) -> None:
    """Appele sans ses arguments obligatoires, un outil doit le dire.

    Le defaut corrige le 27/07/2026 : `get_call_chain` rendait
    `KeyError: 'from_name'`, le repr d'une exception Python. Pour un client
    LLM c'est le pire message possible, il ne nomme ni l'argument manquant ni
    comment l'obtenir, donc l'appelant retente a l'aveugle.

    On passe par l'appel brut : `set_project_root` et `switch_project`
    acceptent `project` comme alias de leur argument requis, donc l'injecter
    rendrait l'appel valide et le test ne prouverait rien.
    """
    requis = (TOOL_SCHEMAS[nom].get("inputSchema") or {})["required"]
    sortie = appeler.brut(nom)
    assert sortie.strip(), f"{nom} ne dit rien quand il manque {requis}"
    assert "Traceback" not in sortie, f"{nom} :\n{sortie[:400]}"
    nomme = any(argument in sortie for argument in requis)
    assert nomme, (
        f"{nom} ne nomme aucun de ses arguments obligatoires {requis} "
        f"dans sa reponse :\n{sortie[:400]}"
    )
