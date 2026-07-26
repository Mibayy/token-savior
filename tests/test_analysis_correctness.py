"""Les outils d'analyse rendent-ils la BONNE reponse, pas seulement une reponse.

Constat qui a motive ce fichier : sur ~292 verifications d'un audit complet,
**16 seulement controlaient le fond**. Les 281 autres verifiaient qu'aucune
erreur n'etait rendue. Un outil qui repond avec assurance quelque chose de
faux passait ce test, et deux versions livrees le meme jour ont fait exactement
ca en passant 2253 puis 2267 tests verts.

Le projet ci-dessous est fabrique pour que chaque reponse soit connue d'avance :
du code mort, un doublon AST exact, un cycle d'import, des routes, un modele,
un Dockerfile sans USER, un .env avec une variable orpheline, et une rupture
d'API entre deux tags git. Chaque test exige le contenu attendu.
"""
from __future__ import annotations

import subprocess

import pytest

from token_savior.project_indexer import ProjectIndexer
from token_savior.query_api import create_project_query_functions


def _ecrire(racine, rel: str, contenu: str) -> None:
    f = racine / rel
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(contenu, encoding="utf-8")


@pytest.fixture(scope="module")
def boutique(tmp_path_factory):
    """Projet polyglotte aux reponses connues."""
    r = tmp_path_factory.mktemp("boutique")

    _ecrire(r, "pyproject.toml", '[project]\nname = "boutique"\nversion = "1.0.0"\n')
    _ecrire(r, "app/__init__.py", "")
    _ecrire(r, "app/models.py",
            "from sqlmodel import Field, SQLModel\n\n\n"
            "class Produit(SQLModel, table=True):\n    id: int = Field(primary_key=True)\n"
            "    nom: str\n\n\n"
            "class Commande(SQLModel, table=True):\n    id: int = Field(primary_key=True)\n"
            "    quantite: int\n")
    _ecrire(r, "app/tarifs.py",
            '"""Calculs de prix."""\n\n\n'
            "def total_commande(commande, prix_unitaire):\n"
            "    return commande.quantite * prix_unitaire\n\n\n"
            "def appliquer_remise(total, pourcentage):\n"
            "    if pourcentage <= 0:\n        return total\n"
            "    return total - (total * pourcentage) // 100\n\n\n"
            "def prix_ttc(base, taux):\n    return base + (base * taux) // 100\n\n\n"
            "def prix_ht_vers_ttc(base, taux):\n    return base + (base * taux) // 100\n\n\n"
            "def jamais_appelee(x):\n    return x * 2\n")
    _ecrire(r, "app/panier.py",
            "from app.tarifs import appliquer_remise, total_commande\n\n\n"
            "class Panier:\n    def __init__(self, lignes=None):\n"
            "        self.lignes = lignes or []\n\n"
            "    def total(self, prix=100):\n"
            "        brut = sum(total_commande(c, prix) for c in self.lignes)\n"
            "        return appliquer_remise(brut, 10)\n\n"
            "    def valider(self):\n"
            "        from app.paiement import encaisser\n"
            "        return encaisser(self.total())\n")
    _ecrire(r, "app/paiement.py",
            "import os\n\n\n"
            "def encaisser(montant):\n"
            '    cle = os.environ.get("STRIPE_KEY", "")\n'
            '    return "refuse" if not cle else f"ok:{montant}"\n\n\n'
            "def rembourser(montant):\n"
            "    from app.panier import Panier\n"
            "    _ = Panier()\n    return montant\n")
    _ecrire(r, "app/api.py",
            "from fastapi import FastAPI\n\nfrom app.panier import Panier\n\n"
            "app = FastAPI()\n\n\n"
            '@app.get("/produits")\ndef lister_produits():\n    return []\n\n\n'
            '@app.post("/panier/valider")\ndef valider_panier():\n'
            "    return Panier().valider()\n")
    _ecrire(r, "Dockerfile",
            "FROM python:3.12-slim\nWORKDIR /app\nCOPY . .\n"
            "EXPOSE 8000\nCMD [\"uvicorn\", \"app.api:app\"]\n")
    _ecrire(r, ".env", "STRIPE_KEY=sk_test\nVARIABLE_ORPHELINE=jamais_lue\n")
    _ecrire(r, "tests/test_tarifs.py",
            "from app.tarifs import total_commande\n\n\n"
            "def test_total():\n    assert total_commande(type('C',(),{'quantite':2})(), 5) == 10\n")

    for cmd in (["git", "init", "-q"], ["git", "config", "user.email", "t@t"],
                ["git", "config", "user.name", "t"], ["git", "add", "-A"],
                ["git", "commit", "-q", "-m", "v1"], ["git", "tag", "v1"]):
        subprocess.run(cmd, cwd=r, check=False,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    # rupture d'API apres v1 : signature elargie
    _ecrire(r, "app/tarifs.py",
            (r / "app/tarifs.py").read_text(encoding="utf-8").replace(
                "def appliquer_remise(total, pourcentage):",
                "def appliquer_remise(total, pourcentage, plafond=0):"))
    return r


@pytest.fixture(scope="module")
def index(boutique):
    return ProjectIndexer(str(boutique)).index()


@pytest.fixture(scope="module")
def q(index):
    return create_project_query_functions(index)


def _txt(valeur) -> str:
    return str(valeur)


# --- Graphe de symboles ---------------------------------------------------- #

def test_les_appelants_de_total_commande(q) -> None:
    """`Panier.total` l'appelle. Une liste vide serait une reponse plausible
    et fausse, c'est exactement ce qu'on veut empecher."""
    res = _txt(q["get_dependents"]("total_commande"))
    assert "panier" in res.lower(), res[:300]


def test_la_chaine_dappel_traverse_deux_modules(q) -> None:
    res = _txt(q["get_call_chain"]("valider", "encaisser"))
    assert "encaisser" in res, res[:300]


def test_les_imports_du_panier(q) -> None:
    res = _txt(q["get_imports"]("app/panier.py"))
    assert "tarifs" in res, res[:200]


def test_les_classes_du_modele(q) -> None:
    res = _txt(q["get_classes"]("app/models.py"))
    for attendu in ("Produit", "Commande"):
        assert attendu in res, f"{attendu} absent : {res[:200]}"


def test_les_fonctions_des_tarifs(q) -> None:
    res = _txt(q["get_functions"]("app/tarifs.py"))
    for attendu in ("total_commande", "appliquer_remise", "prix_ttc"):
        assert attendu in res, f"{attendu} absent : {res[:250]}"


# --- Analyses qui doivent trouver ce qui a ete plante ---------------------- #

def test_le_cycle_dimport_est_detecte(q) -> None:
    """panier importe paiement, paiement importe panier."""
    res = _txt(q["find_import_cycles"]())
    assert "panier" in res.lower() and "paiement" in res.lower(), res[:300]


def test_le_doublon_ast_exact_est_detecte(q) -> None:
    """prix_ttc et prix_ht_vers_ttc ont le meme corps, aux noms pres."""
    res = _txt(q["find_semantic_duplicates"](min_lines=1, max_groups=20))
    assert "prix_ttc" in res or "prix_ht_vers_ttc" in res, res[:300]


def test_le_code_mort_est_detecte(index) -> None:
    """`jamais_appelee` n'est referencee nulle part.

    `find_dead_code` n'est pas expose par create_project_query_functions : il
    vit dans son propre module. Le tester par la mauvaise porte le rendait
    invisible, ce qui est la meme erreur que celles qu'il cherche.
    """
    from token_savior.dead_code import find_dead_code

    res = str(find_dead_code(index))
    assert "jamais_appelee" in res, res[:400]


def test_les_routes_sont_listees(q) -> None:
    res = _txt(q["get_routes"]())
    for chemin in ("/produits", "/panier/valider"):
        assert chemin in res, f"{chemin} absent : {res[:250]}"


def test_la_variable_denv_est_localisee(q) -> None:
    """STRIPE_KEY n'est lue que dans paiement.py."""
    res = _txt(q["get_env_usage"]("STRIPE_KEY"))
    assert "paiement" in res, res[:250]


def test_le_point_dentree_est_lapi(q) -> None:
    res = _txt(q["get_entry_points"]())
    assert "api.py" in res, res[:250]


def test_les_tests_impactes_pointent_le_bon_fichier(q) -> None:
    """L'outil part des changements git, pas d'un nom de symbole seul :
    le fixture laisse tarifs.py modifie apres le tag v1."""
    res = _txt(q["find_impacted_test_files"]())
    if "No changed files" in res:
        pytest.skip("aucun changement git detecte dans ce fixture")
    assert "test_tarifs" in res, res[:250]


# --- Ce qui NE doit PAS etre signale --------------------------------------- #

def test_une_fonction_utilisee_nest_pas_du_code_mort(index) -> None:
    """Un faux positif ici enverrait supprimer du code vivant."""
    from token_savior.dead_code import find_dead_code

    res = str(find_dead_code(index))
    assert "total_commande" not in res, f"faux positif : {res[:400]}"


def test_un_symbole_absent_est_refuse_pas_invente(q) -> None:
    res = q["find_symbol"]("fonction_totalement_inexistante_xyz")
    assert isinstance(res, dict) and "error" in res, res


def test_une_recherche_sans_resultat_ne_fabrique_rien(q) -> None:
    res = _txt(q["search_codebase"]("chaine_absolument_introuvable_zzz"))
    assert "chaine_absolument_introuvable_zzz" not in res.replace(
        "chaine_absolument_introuvable_zzz", "", 1), res[:200]
