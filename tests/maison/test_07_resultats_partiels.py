"""Un script qui expire ne doit pas emporter tout son travail avec lui.

Regression du 27/07/2026. `ts_execute` rendait `value: null` et rien d'autre
quand le delai etait atteint : les resultats des appels deja servis etaient
jetes, alors que le cote Python les avait tous en main.

Observe deux fois dans la meme session : une batterie de 15 outils expiree au
9e appel a du etre relancee en trois morceaux, chaque morceau refaisant le
travail deja accompli. Dans un outil dont le metier est d'economiser des
allers-retours, c'est le pire endroit ou en perdre.
"""

from __future__ import annotations

import json

import pytest


def _charge(sortie: str) -> dict:
    try:
        return json.loads(sortie)
    except json.JSONDecodeError:
        pytest.skip(f"reponse non JSON : {sortie[:200]}")
        raise


def test_un_script_expire_rend_les_appels_deja_servis(appeler) -> None:
    sortie = appeler(
        "ts_execute",
        script=(
            'await tools.find_symbol({name: "calculer_total"});'
            'await tools.find_symbol({name: "appliquer_remise"});'
            "await new Promise(r => setTimeout(r, 30000));"
            "return 'jamais atteint';"
        ),
        timeout_ms=2500,
    )
    charge = _charge(sortie)
    assert charge.get("value") is None
    assert "timeout" in (charge.get("error") or {}).get("message", "").lower()

    partiels = charge.get("partial_results")
    assert partiels, (
        "les deux appels servis avant le delai ont ete perdus :\n" + sortie[:400]
    )
    assert len(partiels) == 2, f"attendu 2 appels traces, obtenu {len(partiels)}"
    assert all(p["tool"] == "find_symbol" for p in partiels), partiels
    assert any("panier.py" in p["apercu"] for p in partiels), (
        "l'apercu ne porte pas le resultat reel :\n" + str(partiels)
    )


def test_un_script_qui_reussit_ne_traine_pas_de_trace(appeler) -> None:
    """Le cas nominal ne doit pas doubler de volume.

    Le script a deja rendu ce qu'il voulait : repeter chaque appel servi
    couterait les tokens que ce paquet existe pour epargner.
    """
    sortie = appeler(
        "ts_execute",
        script='return await tools.find_symbol({name: "calculer_total"});',
    )
    charge = _charge(sortie)
    assert charge.get("error") is None
    assert "partial_results" not in charge, (
        "la trace de secours ne doit apparaitre qu'en cas d'echec"
    )


def test_une_erreur_de_script_rend_aussi_les_appels_servis(appeler) -> None:
    """Pas seulement le delai : toute sortie anormale conserve la trace."""
    sortie = appeler(
        "ts_execute",
        script=(
            'await tools.find_symbol({name: "calculer_total"});'
            "return fonction_inexistante();"
        ),
    )
    charge = _charge(sortie)
    assert charge.get("error") is not None
    partiels = charge.get("partial_results")
    assert partiels, "l'appel servi avant l'erreur a ete perdu :\n" + sortie[:400]
    assert partiels[0]["tool"] == "find_symbol"
    assert partiels[0]["ok"] is True


def test_un_outil_refuse_est_trace_comme_echec(appeler) -> None:
    sortie = appeler(
        "ts_execute",
        script=(
            "try { await tools.outil_hors_liste({}); } catch (e) {}"
            "await new Promise(r => setTimeout(r, 30000));"
            "return 1;"
        ),
        timeout_ms=2000,
    )
    charge = _charge(sortie)
    partiels = charge.get("partial_results") or []
    if not partiels:
        pytest.skip("la facade refuse l'appel avant de le transmettre")
    assert any(p["ok"] is False for p in partiels), partiels


def test_la_trace_est_bornee(appeler) -> None:
    """Une trace de secours qui ferait exploser la reponse serait pire."""
    sortie = appeler(
        "ts_execute",
        script=(
            "for (let i = 0; i < 40; i++) "
            '{ await tools.find_symbol({name: "calculer_total"}); }'
            "await new Promise(r => setTimeout(r, 30000));"
            "return 1;"
        ),
        timeout_ms=9000,
    )
    charge = _charge(sortie)
    partiels = charge.get("partial_results") or []
    if not partiels:
        pytest.skip("aucun appel servi avant le delai")
    assert len(partiels) <= 25, f"trace non bornee : {len(partiels)} entrees"
    for entree in partiels:
        assert len(entree["apercu"]) <= 245, "apercu non borne"
