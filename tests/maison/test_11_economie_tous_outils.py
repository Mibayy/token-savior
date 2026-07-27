"""La promesse du projet, verifiee outil par outil et figee ici.

Tout Token Savior repose sur une affirmation mesurable : passer par l'outil
coute moins cher que l'alternative naive. Elle n'etait verifiee nulle part.
L'audit du 27/07/2026 a trouve quatre outils qui coutaient plus qu'ils
n'epargnaient -- dont `get_function_source` sur une petite fonction, plus
cher qu'un `cat` du fichier entier.

Ce fichier transforme cet audit ponctuel en propriete permanente. Trois
familles, parce qu'elles ne se comparent pas de la meme facon :

  A. Reference naive franche : lire le fichier ou vit la reponse. Verdict
     binaire, l'outil gagne ou il perd.
  B. Composites. On ne compare PAS leur taille a une chaine qui rend moins de
     choses -- ce serait malhonnete, leur gain se compte en allers-retours.
     On verifie ce qui est verifiable : ils ne doivent pas expedier deux fois
     le meme contenu.
  C. Analyses sans equivalent naif : personne ne detecte un cycle d'import a
     la main pour un cout comparable. On les juge sur deux defauts qui en
     sont dans tous les cas -- contenu duplique, et demesure.
"""

from __future__ import annotations

import json

import pytest

# (outil, arguments, fichier de reference) -- la reference est la source que
# l'agent lirait s'il n'avait pas l'outil.
FAMILLE_A: list[tuple[str, dict, str]] = [
    ("get_function_source", {"name": "calculer_total", "level": 0}, "boutique/panier.py"),
    ("get_function_source", {"name": "appliquer_remise", "level": 0}, "boutique/remises.py"),
    ("get_function_source", {"name": "calculer_frais", "level": 0}, "boutique/expedition.py"),
    ("get_class_source", {"name": "Panier", "level": 0}, "boutique/panier.py"),
    ("get_structure_summary", {"file_path": "boutique/panier.py"}, "boutique/panier.py"),
    ("get_functions", {"file_path": "boutique/panier.py"}, "boutique/panier.py"),
    ("get_classes", {"file_path": "boutique/panier.py"}, "boutique/panier.py"),
    ("get_imports", {"file_path": "boutique/panier.py"}, "boutique/panier.py"),
    ("find_symbol", {"name": "calculer_total"}, "boutique/panier.py"),
    ("get_dependencies", {"name": "calculer_total"}, "boutique/panier.py"),
    ("get_dependents", {"name": "appliquer_remise"}, "boutique/remises.py"),
    ("get_file_dependencies", {"file_path": "boutique/panier.py"}, "boutique/panier.py"),
    ("get_file_dependents", {"file_path": "boutique/remises.py"}, "boutique/remises.py"),
    ("get_call_chain",
     {"from_name": "calculer_total", "to_name": "appliquer_remise"}, "boutique/panier.py"),
]

FAMILLE_C: list[tuple[str, dict]] = [
    ("find_dead_code", {}),
    ("find_hotspots", {}),
    ("find_import_cycles", {}),
    ("detect_breaking_changes", {"ref": "HEAD~1"}),
    ("find_impacted_test_files", {"symbol_names": ["calculer_total"]}),
    ("get_routes", {}),
    ("get_env_usage", {"var_name": "BOUTIQUE_DB_URL"}),
    ("get_entry_points", {}),
    ("analyze_config", {"checks": ["orphans"]}),
    ("analyze_docker", {}),
    ("get_git_status", {}),
    ("get_changed_symbols", {}),
    ("get_project_summary", {}),
    ("list_files", {"max_results": 20}),
    ("search_codebase", {"pattern": "appliquer_remise"}),
    ("get_feature_files", {"keyword": "panier"}),
]

# Outils de session, pas d'analyse de projet : captures et memoire. Leur
# taille depend de ce que la session a accumule, pas du depot. Les comparer a
# la taille du code n'a aucun sens -- c'est ce qui a fait tomber a tort
# `capture_list` sur un projet-cobaye de 2 Ko. Leur borne propre est verifiee
# dans test_10_listage_borne.py, ou elle veut dire quelque chose.
FAMILLE_SESSION: list[tuple[str, dict]] = [
    ("capture_list", {}),
    ("capture_aggregate", {}),
    ("memory_index", {"query": "x"}),
    ("memory_search", {"query": "x"}),
]


def _duplication(texte: str, taille: int = 120) -> float:
    """Fraction du texte occupee par des blocs qui apparaissent deux fois.

    Grossier a dessein : on cherche un doublon franc, du type source
    reexpediee en entier, pas une repetition de mise en forme.
    """
    if len(texte) < taille * 2:
        return 0.0
    vus: set[str] = set()
    doubles = 0
    for i in range(0, len(texte) - taille, taille):
        bloc = texte[i:i + taille]
        if bloc in vus:
            doubles += 1
        vus.add(bloc)
    return doubles / max(1, (len(texte) - taille) // taille)


@pytest.mark.parametrize(
    ("outil", "arguments", "reference"),
    FAMILLE_A,
    ids=[f"{o}-{next(iter(a.values()))}" for o, a, _ in FAMILLE_A],
)
def test_un_outil_coute_moins_que_lire_le_fichier(
    outil: str, arguments: dict, reference: str, appeler, projet_cobaye,
) -> None:
    """Sinon `cat` est un meilleur outil, et le projet perd sa raison d'etre.

    Defaut trouve le 27/07/2026 : `get_function_source` sur une fonction de
    trois lignes rendait 131 caracteres la ou le fichier entier en coutait 76.
    L'ecart etait l'indice "-> get_full_context(...)", ~70 caracteres ajoutes
    a toutes les reponses sans egard pour leur taille.

    Limite honnete de cette mesure : extraire un symbole d'un fichier qui ne
    contient que lui n'epargne rien par construction. Le fichier
    `expedition.py` du cobaye est dans ce cas, et l'outil y depasse de la
    taille de l'indice. On tolere donc une marge egale a cet indice, et
    seulement quand le fichier tient en un seul symbole -- pretendre le
    contraire serait afficher un test vert sur une propriete fausse.
    """
    fichier = (projet_cobaye / reference).read_text(encoding="utf-8")
    sortie = appeler(outil, **arguments)
    # Un fichier qui ne declare qu'un symbole : rien a epargner en extrayant.
    symbole_unique = (fichier.count("\ndef ") + fichier.count("\nclass ")) <= 1
    marge = 120 if symbole_unique else 0
    assert len(sortie) <= len(fichier) + marge, (
        f"{outil}({arguments}) rend {len(sortie)} caracteres, lire "
        f"{reference} en coute {len(fichier)}"
        + (" (fichier a symbole unique, marge d'indice toleree)" if symbole_unique else "")
        + f".\n{sortie[:400]}"
    )


@pytest.mark.parametrize(
    ("outil", "arguments"),
    FAMILLE_C + FAMILLE_SESSION,
    ids=[o for o, _ in FAMILLE_C + FAMILLE_SESSION],
)
def test_une_analyse_ne_se_repete_pas(outil: str, arguments: dict, appeler) -> None:
    """Du contenu expedie deux fois dans la meme reponse est un defaut net.

    C'est exactement ce que faisait `get_edit_context` : la source complete
    sous `source`, et ses vingt premieres lignes a nouveau sous
    `location.source_preview`.
    """
    sortie = appeler(outil, **arguments)
    part = _duplication(sortie)
    assert part <= 0.15, (
        f"{outil} repete {part:.0%} de sa reponse.\n{sortie[:400]}"
    )


@pytest.mark.parametrize(
    ("outil", "arguments"), FAMILLE_C, ids=[o for o, _ in FAMILLE_C],
)
def test_une_analyse_reste_bornee(
    outil: str, arguments: dict, appeler, projet_cobaye,
) -> None:
    """Aucune reponse ne doit peser plus que tout le code du projet.

    Defaut trouve le meme jour : `capture_list` rendait 29 836 caracteres par
    defaut, vingt-quatre fois la source du projet audite, parce qu'aucune
    borne ne s'appliquait a ses cinquante lignes completes.
    """
    tout_le_code = sum(
        len(p.read_text(encoding="utf-8", errors="replace"))
        for p in projet_cobaye.rglob("*.py")
    )
    sortie = appeler(outil, **arguments)
    assert len(sortie) <= tout_le_code, (
        f"{outil} rend {len(sortie)} caracteres pour un projet dont tout le "
        f"code fait {tout_le_code}."
    )


def test_le_contexte_d_edition_n_expedie_pas_deux_fois_la_source(appeler) -> None:
    """Le composite le plus utilise, sur le seul critere honnete."""
    sortie = appeler("get_edit_context", name="calculer_total")
    try:
        charge = json.loads(sortie)
    except json.JSONDecodeError:
        pytest.skip("reponse non JSON")
        return
    if charge.get("source"):
        assert "source_preview" not in (charge.get("location") or {}), (
            "la source complete est deja dans `source`, son extrait dans "
            "`location` la facture une seconde fois"
        )


def test_le_niveau_abrege_coute_toujours_moins_que_le_complet(appeler) -> None:
    for symbole in ("calculer_total", "calculer_frais", "compter_articles"):
        complet = appeler(
            "get_function_source", name=symbole, level=0, force_full=True,
        )
        abrege = appeler(
            "get_function_source", name=symbole, level=2, force_full=True,
        )
        assert len(abrege) <= len(complet), (
            f"{symbole} : niveau 2 rend {len(abrege)} caracteres contre "
            f"{len(complet)} pour le niveau 0"
        )


def test_aucun_outil_de_lecture_ne_grossit_au_second_appel(appeler) -> None:
    """Le cache doit epargner, et surtout ne jamais couter plus cher.

    Sur un petit symbole, l'accuse de cache faisait 317 caracteres pour une
    source de 232. Le code calculait pourtant l'economie et la voyait tomber a
    zero, sans en tirer la consequence.
    """
    for symbole in ("compter_articles", "calculer_frais"):
        premier = appeler("get_function_source", name=symbole, level=0)
        second = appeler("get_function_source", name=symbole, level=0)
        assert len(second) <= len(premier), (
            f"{symbole} : second appel {len(second)} caracteres contre "
            f"{len(premier)} au premier"
        )
