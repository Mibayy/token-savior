"""Les enchainements reels, ceux que le CLAUDE.md du depot prescrit.

Un outil peut etre juste isolement et inutile en chaine. Ces tests rejouent
les sequences que la doc du projet impose, et verifient qu'elles tiennent
vraiment : contexte avant edition, analyses avant commit, verification avant
deploiement.

On y verifie aussi le determinisme : deux appels identiques sur un projet qui
n'a pas bouge doivent rendre la meme chose. Un outil de lecture qui varie
d'un appel a l'autre est inutilisable comme fondation.
"""

from __future__ import annotations

import pytest

# --- Determinisme -------------------------------------------------------

OUTILS_DE_LECTURE = [
    ("find_symbol", {"name": "calculer_total"}),
    ("get_function_source", {"name": "calculer_total", "level": 0, "force_full": True}),
    ("get_class_source", {"name": "Panier", "level": 0, "force_full": True}),
    ("get_structure_summary", {"file_path": "boutique/panier.py"}),
    ("get_functions", {"file_path": "boutique/panier.py"}),
    ("get_classes", {"file_path": "boutique/panier.py"}),
    ("get_imports", {"file_path": "boutique/panier.py"}),
    ("get_dependencies", {"name": "calculer_total"}),
    ("get_dependents", {"name": "appliquer_remise"}),
    ("get_file_dependencies", {"file_path": "boutique/panier.py"}),
    ("get_file_dependents", {"file_path": "boutique/remises.py"}),
    ("search_codebase", {"pattern": "appliquer_remise"}),
    ("find_dead_code", {}),
    ("find_import_cycles", {}),
    ("find_hotspots", {}),
    ("get_routes", {}),
    ("get_entry_points", {}),
    ("list_files", {"max_results": 20}),
    ("get_project_summary", {}),
    ("get_git_status", {}),
]


@pytest.mark.parametrize(
    ("outil", "arguments"), OUTILS_DE_LECTURE, ids=[o for o, _ in OUTILS_DE_LECTURE],
)
def test_un_outil_de_lecture_est_deterministe(outil: str, arguments: dict, appeler) -> None:
    """Deux fois la meme question, deux fois la meme reponse."""
    premier = appeler(outil, **arguments)
    second = appeler(outil, **arguments)
    assert premier == second, (
        f"{outil} varie entre deux appels identiques.\n"
        f"1er : {premier[:250]}\n2e  : {second[:250]}"
    )


@pytest.mark.parametrize(
    ("outil", "arguments"), OUTILS_DE_LECTURE, ids=[o for o, _ in OUTILS_DE_LECTURE],
)
def test_un_outil_de_lecture_ne_modifie_pas_le_projet(
    outil: str, arguments: dict, appeler, projet_cobaye,
) -> None:
    """Lire ne doit rien ecrire : empreinte du projet inchangee."""
    def empreinte() -> list[tuple[str, int]]:
        return sorted(
            (str(p.relative_to(projet_cobaye)), p.stat().st_size)
            for p in projet_cobaye.rglob("*.py")
        )

    avant = empreinte()
    appeler(outil, **arguments)
    assert empreinte() == avant, f"{outil} a modifie des fichiers du projet"


# --- Les enchainements prescrits ---------------------------------------


def test_chaine_avant_edition(appeler) -> None:
    """La regle : get_edit_context avant toute edition, jamais l'inverse.

    On verifie que l'appel unique rend bien ce que la chaine longue aurait
    donne : la source, les appelants et les tests impactes.
    """
    contexte = appeler("get_edit_context", name="appliquer_remise")
    assert "appliquer_remise" in contexte, "pas de source"
    assert "calculer_total" in contexte, "pas d'appelant"


def test_chaine_un_appel_au_lieu_de_trois(appeler) -> None:
    """get_full_context doit remplacer find_symbol + source + dependances."""
    complet = appeler("get_full_context", name="calculer_total", depth=1)
    localisation = appeler("find_symbol", name="calculer_total")
    dependances = appeler("get_dependencies", name="calculer_total")

    assert "panier.py" in localisation
    assert "appliquer_remise" in dependances
    # Le contexte unique doit contenir l'essentiel des deux autres.
    assert "calculer_total" in complet
    assert "appliquer_remise" in complet, (
        "get_full_context n'apporte pas les dependances, la chaine longue "
        f"reste necessaire :\n{complet[:400]}"
    )


def test_chaine_avant_commit(appeler) -> None:
    """Avant un commit : etat git, symboles changes, ruptures d'API."""
    etat = appeler("get_git_status")
    assert "main" in etat

    changes = appeler("get_changed_symbols")
    assert changes.strip()

    ruptures = appeler("detect_breaking_changes", ref="HEAD~1")
    assert ruptures.strip()
    assert "Traceback" not in ruptures


def test_chaine_avant_deploiement(appeler) -> None:
    """Avant un deploiement : config puis Docker."""
    config = appeler("analyze_config", checks=["orphans"])
    assert "BOUTIQUE_VARIABLE_ORPHELINE" in config, (
        "la variable orpheline plantee doit remonter :\n" + config[:400]
    )
    docker = appeler("analyze_docker")
    assert docker.strip()
    assert "Traceback" not in docker


def test_chaine_debut_de_refactoring(appeler) -> None:
    """Avant un refactoring : ou est le code mort, ou sont les points chauds."""
    mort = appeler("find_dead_code")
    chauds = appeler("find_hotspots")
    assert "fonction_jamais_appelee" in mort
    assert "calculer_frais" in chauds


def test_chaine_impact_avant_de_toucher_a_un_symbole(appeler) -> None:
    """Qui casse si je touche a appliquer_remise ?"""
    impact = appeler("get_change_impact", name="appliquer_remise")
    assert "calculer_total" in impact, (
        "l'appelant direct doit apparaitre dans l'impact :\n" + impact[:400]
    )


def test_le_projet_actif_est_respecte(appeler, projet_cobaye) -> None:
    """Un `project` explicite ne doit pas etre ignore."""
    sortie = appeler("get_project_summary", project=str(projet_cobaye))
    assert "boutique" in sortie.lower()


def test_un_projet_inexistant_donne_une_erreur_lisible(appeler) -> None:
    sortie = appeler("get_project_summary", project="/chemin/qui/n/existe/pas")
    assert sortie.strip()
    assert "Traceback" not in sortie
    assert "error" in sortie.lower() or "not" in sortie.lower(), sortie[:300]


def test_les_alias_d_arguments_sont_honores(appeler) -> None:
    """`symbol` doit valoir `name` la ou l'alias est declare."""
    par_nom = appeler("find_symbol", name="calculer_total")
    par_alias = appeler("find_symbol", symbol="calculer_total")
    assert "panier.py" in par_nom
    assert "panier.py" in par_alias, (
        "l'alias `symbol` n'est pas honore :\n" + par_alias[:300]
    )


def test_le_batch_rend_autant_d_entrees_que_demande(appeler) -> None:
    sortie = appeler("find_symbol", names=["calculer_total", "appliquer_remise", "Panier"])
    for attendu in ("calculer_total", "appliquer_remise", "Panier"):
        assert attendu in sortie, f"{attendu} absent du batch :\n{sortie[:400]}"


def test_le_cache_de_source_rend_un_accuse_au_second_appel(appeler) -> None:
    """Le cache doit economiser, et surtout ne jamais couter plus cher.

    Mesure du 27/07/2026 : sur `compter_articles`, trois lignes, la source
    faisait 232 caracteres et l'accuse de cache 317. Le mecanisme cense
    epargner des tokens en coutait 85 de plus. Il le savait meme : `saved`
    tombait a 0 par un `max(0, ...)` sans que la consequence en soit tiree.

    Ailleurs dans cette serie le cache est vide avant chaque test pour que les
    outils soient mesures independamment. Ici on le laisse jouer, puisque
    c'est lui qu'on mesure.
    """
    premier = appeler("get_function_source", name="compter_articles", level=0)
    second = appeler("get_function_source", name="compter_articles", level=0)
    assert "def compter_articles" in premier
    assert len(second) <= len(premier), (
        "sur un petit symbole, l'accuse de cache coute plus cher que la "
        f"source :\n1er {len(premier)} car., 2e {len(second)} car."
    )


def test_le_cache_economise_reellement_sur_un_gros_symbole(appeler) -> None:
    """Le pendant : sur une fonction longue, l'accuse doit bien etre plus court."""
    premier = appeler("get_function_source", name="calculer_frais", level=0)
    second = appeler("get_function_source", name="calculer_frais", level=0)
    assert "def calculer_frais" in premier
    assert len(second) < len(premier), (
        "sur un gros symbole le cache doit raccourcir la reponse :\n"
        f"1er {len(premier)} car., 2e {len(second)} car."
    )


def test_force_full_reprend_le_corps_apres_un_accuse(appeler) -> None:
    appeler("get_function_source", name="compter_articles", level=0)
    appeler("get_function_source", name="compter_articles", level=0)
    force = appeler("get_function_source", name="compter_articles", level=0, force_full=True)
    assert "def compter_articles" in force, (
        "force_full doit toujours rendre le corps :\n" + force[:300]
    )
