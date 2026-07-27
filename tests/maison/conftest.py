"""Projet-cobaye et harnais pour la serie maison.

Ce que cette serie teste, et que la suite historique ne testait pas : les
outils tels qu'un agent les appelle reellement, c'est-a-dire a travers
`_dispatch_tool`, avec des arguments realistes, sur un projet qui contient
vraiment ce que les outils pretendent trouver.

Le projet-cobaye est construit avec des defauts *plantes exprès* : un cycle
d'import, une fonction morte, un doublon semantique, une fonction complexe,
une variable d'environnement orpheline, une route, un Dockerfile. Un outil qui
rend "rien trouve" sur ce projet a un probleme, et le test le dit.

Il a aussi un historique git a deux commits, sans quoi `get_git_status`,
`get_changed_symbols`, `checkpoint` et `detect_breaking_changes` n'ont rien a
se mettre sous la dent.
"""

from __future__ import annotations

import subprocess
import textwrap
from pathlib import Path

import pytest

# Racine active AVANT que cette serie n'enregistre quoi que ce soit. Prise a
# l'import du module, donc avant toute fixture : la fixture `appeler` est de
# portee session et enregistre le cobaye des sa creation, si bien qu'un
# instantane pris dans une fixture par test capturait deja un etat pollue et
# "restaurait" le cobaye.
#
# Pourquoi cela compte : la recherche du viewer de memoire est filtree par le
# projet actif -- son propre test le dit en commentaire. Laisser le cobaye
# actif lui faisait rendre zero resultat.
try:
    from token_savior import server_state as _etat_initial
    _RACINE_PRISTINE = getattr(_etat_initial._slot_mgr, "active_root", None)
except Exception:  # pragma: no cover - defensif
    _RACINE_PRISTINE = None


def _ecrire(racine: Path, rel: str, contenu: str) -> Path:
    chemin = racine / rel
    chemin.parent.mkdir(parents=True, exist_ok=True)
    chemin.write_text(textwrap.dedent(contenu).lstrip("\n"), encoding="utf-8")
    return chemin


def _git(racine: Path, *args: str) -> None:
    subprocess.run(
        ["git", *args],
        cwd=racine,
        check=True,
        capture_output=True,
        env={
            "PATH": "/usr/bin:/bin",
            "HOME": str(racine),
            "GIT_AUTHOR_NAME": "essai",
            "GIT_AUTHOR_EMAIL": "essai@example.invalid",
            "GIT_COMMITTER_NAME": "essai",
            "GIT_COMMITTER_EMAIL": "essai@example.invalid",
        },
    )


@pytest.fixture(scope="session")
def projet_cobaye(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """Un projet complet, avec des defauts plantes pour chaque analyse."""
    racine = tmp_path_factory.mktemp("cobaye")

    _ecrire(racine, "boutique/__init__.py", "")

    # --- Domaine : classes, methodes, dependances chainees -----------------
    _ecrire(racine, "boutique/panier.py", '''
        """Le panier et son calcul de total."""

        from boutique.remises import appliquer_remise


        def calculer_total(lignes):
            """Somme les lignes du panier, remise comprise."""
            brut = sum(ligne["prix"] * ligne["quantite"] for ligne in lignes)
            return appliquer_remise(brut)


        def compter_articles(lignes):
            """Nombre total d'articles, toutes lignes confondues."""
            return sum(ligne["quantite"] for ligne in lignes)


        class Panier:
            """Un panier client."""

            def __init__(self):
                self.lignes = []

            def ajouter(self, prix, quantite=1):
                """Ajoute une ligne au panier."""
                self.lignes.append({"prix": prix, "quantite": quantite})

            def total(self):
                """Total du panier."""
                return calculer_total(self.lignes)
    ''')

    _ecrire(racine, "boutique/remises.py", '''
        """Regles de remise."""

        SEUIL_REMISE = 100.0
        TAUX_REMISE = 0.9


        def appliquer_remise(montant):
            """Applique la remise au-dela du seuil."""
            if montant > SEUIL_REMISE:
                return montant * TAUX_REMISE
            return montant


        def fonction_jamais_appelee(x):
            """Code mort plante exprès : rien ne l'appelle."""
            return x * 2
    ''')

    # --- Doublon semantique plante exprès ----------------------------------
    _ecrire(racine, "boutique/doublons.py", '''
        """Deux fonctions qui font la meme chose, pour find_semantic_duplicates."""


        def somme_des_prix(lignes):
            """Additionne le prix de chaque ligne."""
            total = 0
            for ligne in lignes:
                total += ligne["prix"]
            return total


        def additionner_les_prix(entrees):
            """Additionne le prix de chaque entree."""
            cumul = 0
            for entree in entrees:
                cumul += entree["prix"]
            return cumul
    ''')

    # --- Fonction complexe plantee exprès (hotspot) ------------------------
    _ecrire(racine, "boutique/expedition.py", '''
        """Calcul de frais de port volontairement tordu, pour find_hotspots."""


        def calculer_frais(poids, pays, express, assure, client_pro, volume):
            """Frais de port. Complexite volontairement elevee."""
            frais = 0
            if pays == "FR":
                frais = 5 if poids < 1 else 8 if poids < 5 else 12
                if express:
                    frais *= 2
                    if assure:
                        frais += 3
                        if client_pro:
                            frais -= 1
                            if volume > 10:
                                frais -= 2
            elif pays in ("BE", "LU", "DE"):
                frais = 9 if poids < 1 else 14
                if express:
                    frais *= 2
                if assure:
                    frais += 5
                if client_pro and volume > 20:
                    frais *= 0.8
            else:
                frais = 25
                if poids > 10:
                    frais += 15
                if express:
                    frais += 30
                if not assure:
                    frais -= 2
            return max(frais, 0)
    ''')

    # --- Cycle d'import plante exprès --------------------------------------
    _ecrire(racine, "boutique/cycle_a.py", '''
        """Moitie A d'un cycle d'import volontaire."""

        from boutique import cycle_b


        def depuis_a():
            """Appelle B."""
            return cycle_b.depuis_b()
    ''')
    _ecrire(racine, "boutique/cycle_b.py", '''
        """Moitie B d'un cycle d'import volontaire."""

        from boutique import cycle_a


        def depuis_b():
            """Reference A, ce qui ferme le cycle."""
            return cycle_a
    ''')

    # --- Routes + variables d'environnement --------------------------------
    _ecrire(racine, "boutique/api.py", '''
        """API HTTP de la boutique."""

        import os

        from fastapi import FastAPI

        app = FastAPI()

        BASE_DONNEES = os.environ.get("BOUTIQUE_DB_URL", "sqlite:///boutique.db")
        CLE_PAIEMENT = os.getenv("BOUTIQUE_PAIEMENT_CLE")


        @app.get("/panier")
        def lire_panier():
            """Rend le panier courant."""
            return {"lignes": []}


        @app.post("/panier/ligne")
        def ajouter_ligne(prix: float, quantite: int = 1):
            """Ajoute une ligne au panier."""
            return {"ok": True, "prix": prix, "quantite": quantite}
    ''')

    # --- Point d'entree ----------------------------------------------------
    _ecrire(racine, "boutique/main.py", '''
        """Point d'entree du service."""

        from boutique.api import app


        def demarrer():
            """Demarre le service."""
            return app


        if __name__ == "__main__":
            demarrer()
    ''')

    # --- Tests du projet-cobaye (pour find_impacted_test_files) ------------
    _ecrire(racine, "tests/test_panier.py", '''
        """Tests du panier."""

        from boutique.panier import calculer_total, compter_articles


        def test_calculer_total():
            assert calculer_total([{"prix": 10, "quantite": 2}]) == 20


        def test_compter_articles():
            assert compter_articles([{"prix": 10, "quantite": 3}]) == 3
    ''')

    # --- Config : une variable orpheline plantee exprès --------------------
    _ecrire(racine, ".env", """
        BOUTIQUE_DB_URL=sqlite:///boutique.db
        BOUTIQUE_PAIEMENT_CLE=cle_de_test
        BOUTIQUE_VARIABLE_ORPHELINE=personne_ne_me_lit
    """)
    _ecrire(racine, "Dockerfile", """
        FROM python:3.12-slim
        WORKDIR /app
        COPY . .
        RUN pip install --no-cache-dir fastapi
        EXPOSE 8000
        CMD ["python", "-m", "boutique.main"]
    """)
    _ecrire(racine, "pyproject.toml", """
        [project]
        name = "boutique-cobaye"
        version = "1.0.0"
    """)
    _ecrire(racine, "README.md", "# Boutique cobaye\n\nProjet d'essai.\n")

    # --- Historique git : deux commits, sinon rien a diffuser --------------
    _git(racine, "init", "-q", "-b", "main")
    _git(racine, "add", "-A")
    _git(racine, "commit", "-q", "-m", "initial")
    # Second commit avec un changement de signature, pour detect_breaking_changes.
    _ecrire(racine, "boutique/remises.py", '''
        """Regles de remise."""

        SEUIL_REMISE = 100.0
        TAUX_REMISE = 0.9


        def appliquer_remise(montant, devise="EUR"):
            """Applique la remise au-dela du seuil. Signature elargie."""
            if montant > SEUIL_REMISE:
                return montant * TAUX_REMISE
            return montant


        def fonction_jamais_appelee(x):
            """Code mort plante exprès : rien ne l'appelle."""
            return x * 2
    ''')
    _git(racine, "add", "-A")
    _git(racine, "commit", "-q", "-m", "elargit la signature de appliquer_remise")

    return racine


@pytest.fixture(scope="session")
def appeler(projet_cobaye: Path):
    """Appelle un outil par le vrai chemin de dispatch, rend le texte.

    `ts_search` et `ts_execute` ne passent pas par `_dispatch_tool` : ce sont
    des meta-outils traites en amont, dans `call_tool`. Les router ici evite
    de conclure a tort qu'ils sont casses -- `_dispatch_tool` rend
    "unknown tool" pour eux, ce qui est le comportement attendu.
    """
    import asyncio

    from token_savior import server
    from token_savior import server_state as etat

    # Enregistre le cobaye comme projet actif, une fois pour la session.
    server._dispatch_tool("set_project_root", {"path": str(projet_cobaye)}, "")

    def _texte(sortie) -> str:
        if isinstance(sortie, str):
            return sortie
        return "".join(getattr(bloc, "text", "") or "" for bloc in sortie)

    def _meta(nom: str, arguments: dict):
        handler = {
            "ts_search": getattr(server, "_handle_ts_search", None),
            "ts_execute": getattr(server, "_handle_ts_execute", None),
        }[nom]
        if handler is None:
            return f"Error: {nom} sans point d'entree"
        resultat = handler(arguments)
        if asyncio.iscoroutine(resultat):
            resultat = asyncio.run(resultat)
        return resultat

    def _appel(nom: str, **arguments) -> str:
        if nom in ("ts_search", "ts_execute"):
            return _texte(_meta(nom, arguments))
        arguments.setdefault("project", str(projet_cobaye))
        sortie = server._dispatch_tool(nom, arguments, arguments.get("name", ""))
        return _texte(sortie)

    def _appel_brut(nom: str, **arguments) -> str:
        """Sans injection de `project`.

        Indispensable pour tester l'absence d'argument obligatoire :
        `set_project_root` et `switch_project` acceptent `project` comme
        alias de leur argument requis, donc l'injecter rendait l'appel valide
        et le test ne prouvait rien.
        """
        if nom in ("ts_search", "ts_execute"):
            return _texte(_meta(nom, arguments))
        return _texte(server._dispatch_tool(nom, arguments, ""))

    _appel.racine = str(projet_cobaye)
    _appel.etat = etat
    _appel.brut = _appel_brut
    return _appel


@pytest.fixture(autouse=True)
def _cache_de_session_vierge(projet_cobaye):
    """Isolation complete, appliquee a CHAQUE test de la serie.

    Trois fuites d'etat, corrigees ici plutot que dans chaque test.

    - Le cache CSC est volontairement persistant sur la duree d'une session
      MCP : redemander un symbole deja vu rend un accuse compact au lieu du
      corps. Dans une suite, cela fait dependre un test de ceux qui l'ont
      precede.
    - Le projet actif est global : les tests d'integrite enregistrent des
      projets temporaires, et `ts_execute` -- qui n'a pas de parametre
      `project` -- se retrouvait a travailler sur le dernier enregistre.
    - `server_state` porte des compteurs de session. Cette serie fait des
      centaines d'appels de navigation, ce qui fait basculer les indices de
      `find_symbol` vers le message d'arret. Quatre tests de test_query_api et
      test_memory_viewer passaient seuls et tombaient dans la suite complete,
      uniquement parce que `maison` est collecte avant eux.

    Portee `function` et non `session` ni `package` : une fixture de session
    ne se demonte qu'a la fin de TOUTE la suite, et `package` y retombe faute
    de `__init__.py` dans ce repertoire. Les deux laissaient la redirection
    active pour les tests suivants. Une suite qui casse ses voisines est une
    suite fausse, meme verte.
    """
    from token_savior import server
    from token_savior import server_state as etat

    # Pas de redirection de la base memoire ici : la conftest racine isole
    # deja toute la suite vers une base temporaire. Une seconde redirection,
    # posee et retiree a chaque test, laissait le viewer de memoire avec une
    # connexion ouverte sur une base disparue -- deux de ses tests rendaient
    # alors une recherche vide. La serie ecrit donc dans la meme base que ses
    # voisines, ce qui est le comportement voulu.

    # -- instantane des compteurs et conteneurs de session
    instantane = {}
    for nom in dir(etat):
        if nom.startswith("__"):
            continue
        valeur = getattr(etat, nom, None)
        if isinstance(valeur, bool):
            continue
        if isinstance(valeur, (int, str)) or valeur is None:
            instantane[nom] = ("scalaire", valeur)
        elif isinstance(valeur, (dict, list, set)):
            instantane[nom] = ("conteneur", valeur.copy())

    # La racine active est une chaine : elle etait sautee par l'instantane, qui
    # ne gardait qu'entiers et conteneurs. Le viewer de memoire filtre ses
    # observations par projet actif, et se retrouvait a chercher dans le
    # cobaye -- d'ou deux recherches vides, quel que soit le fichier de la
    # serie execute avant lui.

    for attribut in ("_session_symbol_cache", "_session_result_cache"):
        cache = getattr(etat, attribut, None)
        if hasattr(cache, "clear"):
            cache.clear()
    server._dispatch_tool("switch_project", {"name": str(projet_cobaye)}, "")

    try:
        yield
    finally:
        try:
            etat._slot_mgr.active_root = _RACINE_PRISTINE
        except Exception:
            pass
        for nom, (genre, valeur) in instantane.items():
            if genre == "scalaire":
                try:
                    setattr(etat, nom, valeur)
                except Exception:
                    pass
                continue
            courant = getattr(etat, nom, None)
            if not hasattr(courant, "clear"):
                continue
            courant.clear()
            if isinstance(valeur, dict):
                courant.update(valeur)
            elif isinstance(valeur, set):
                courant |= valeur
            else:
                courant.extend(valeur)
