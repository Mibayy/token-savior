"""`get_routes` rendait `[]` sur un projet FastAPI ou Flask.

Il ne connaissait que Next.js App Router et Spring. Sur les deux frameworks
web Python les plus repandus il rendait une liste vide, ce qu'un appelant lit
comme « ce projet n'a pas de routes ». Un silence qui se confond avec une
reponse est pire qu'une erreur : rien ne signale qu'il faut chercher ailleurs.

Trouve en exigeant le contenu attendu au lieu de l'absence d'erreur.
"""
from __future__ import annotations

import pytest

from token_savior.project_indexer import ProjectIndexer
from token_savior.query_api import create_project_query_functions


@pytest.fixture(scope="module")
def routes(tmp_path_factory):
    r = tmp_path_factory.mktemp("web")
    (r / "app").mkdir()
    (r / "app" / "api.py").write_text(
        "from fastapi import APIRouter, FastAPI\n\n"
        "app = FastAPI()\nrouter = APIRouter()\n\n\n"
        '@app.get("/produits")\ndef lister():\n    return []\n\n\n'
        '@app.post("/panier/valider", status_code=201)\ndef valider():\n    return 1\n\n\n'
        '@router.delete("/produits/{pid}")\ndef supprimer(pid: int):\n    return None\n\n\n'
        "def pas_une_route(x):\n    return x\n", encoding="utf-8")
    (r / "app" / "web.py").write_text(
        "from flask import Flask\n\napp = Flask(__name__)\n\n\n"
        '@app.route("/health", methods=["GET", "HEAD"])\ndef health():\n    return "ok"\n\n\n'
        '@app.route("/ping")\ndef ping():\n    return "pong"\n', encoding="utf-8")
    q = create_project_query_functions(ProjectIndexer(str(r)).index())
    return q["get_routes"]()


def _par_chemin(routes):
    return {r["route"]: r for r in routes}


@pytest.mark.parametrize("chemin,methode", [
    ("/produits", "GET"),
    ("/panier/valider", "POST"),
    ("/produits/{pid}", "DELETE"),
])
def test_fastapi_est_reconnu(routes, chemin, methode) -> None:
    trouvees = _par_chemin(routes)
    assert chemin in trouvees, f"{chemin} absent de {sorted(trouvees)}"
    assert methode in trouvees[chemin]["methods"], trouvees[chemin]


def test_un_router_compte_autant_qu_une_app(routes) -> None:
    """`@router.delete` est aussi courant que `@app.get` : ne reconnaitre que
    l'objet nomme `app` raterait la moitie d'un projet structure."""
    assert "/produits/{pid}" in _par_chemin(routes)


def test_flask_route_avec_methodes_explicites(routes) -> None:
    sante = _par_chemin(routes).get("/health")
    assert sante is not None, sorted(_par_chemin(routes))
    assert set(sante["methods"]) == {"GET", "HEAD"}, sante


def test_flask_route_sans_methodes_vaut_get(routes) -> None:
    ping = _par_chemin(routes).get("/ping")
    assert ping is not None
    assert ping["methods"] == ["GET"], ping


def test_une_fonction_non_decoree_nest_pas_une_route(routes) -> None:
    """Un faux positif ici inventerait une API qui n'existe pas."""
    assert all("pas_une_route" not in str(r) for r in routes), routes


def test_le_fichier_et_la_ligne_sont_rendus(routes) -> None:
    """Sans eux la reponse n'est pas actionnable."""
    for r in routes:
        assert r["file"].endswith(".py"), r
        assert r["line"] >= 1, r
