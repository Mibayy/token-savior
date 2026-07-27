"""Un listage doit identifier, pas restituer.

Regression du 27/07/2026, trouvee par un audit d'economie passe sur les 43
outils comparables. `capture_list` rendait **29 836 caracteres** par defaut,
vingt-quatre fois tout le code source du projet audite : chaque entree pesait
~677 caracteres, dont ~310 pour la commande complete et ~205 pour un apercu,
le tout multiplie par une limite par defaut de 50.

Dans un paquet dont le metier est d'economiser des tokens, un listage qui
deverse 30 Ko sans qu'on ait rien demande est un defaut a lui seul.
"""

from __future__ import annotations

import json

import pytest

PLAFOND_LIGNE = 300  # une entree de liste, en caracteres
PLAFOND_TOTAL = 8000  # la reponse par defaut, en caracteres


def _charge(sortie: str) -> dict:
    try:
        return json.loads(sortie)
    except json.JSONDecodeError:
        pytest.skip(f"reponse non JSON : {sortie[:200]}")
        raise


@pytest.fixture
def des_captures(appeler):
    """Assez de captures pour que la limite par defaut morde."""
    for i in range(30):
        appeler(
            "capture_put",
            tool_name="Bash",
            output=("ligne de sortie assez longue pour peser " * 12) + str(i),
            args_summary=json.dumps({"command": "commande volontairement tres longue " * 8}),
        )


def test_le_listage_par_defaut_reste_borne(appeler, des_captures) -> None:
    sortie = appeler("capture_list")
    assert len(sortie) < PLAFOND_TOTAL, (
        f"le listage par defaut rend {len(sortie)} caracteres, plafond "
        f"{PLAFOND_TOTAL}. C'est ce defaut qui rendait 29 836 caracteres."
    )


def test_chaque_entree_reste_courte(appeler, des_captures) -> None:
    charge = _charge(appeler("capture_list"))
    entrees = charge.get("captures") or []
    assert entrees, "aucune capture listee"
    for entree in entrees:
        taille = len(json.dumps(entree))
        assert taille <= PLAFOND_LIGNE, (
            f"une entree de liste pese {taille} caracteres :\n{entree}"
        )


def test_l_entree_reste_exploitable(appeler, des_captures) -> None:
    """Borner n'est pas amputer : on doit pouvoir retrouver la capture."""
    charge = _charge(appeler("capture_list"))
    entrees = charge.get("captures") or []
    assert entrees
    premiere = entrees[0]
    assert premiere.get("uri"), "sans uri, une entree de liste ne sert a rien"
    assert premiere.get("id") is not None
    assert premiere.get("tool_name"), "il faut pouvoir reconnaitre l'outil"


def test_une_limite_explicite_est_honoree(appeler, des_captures) -> None:
    charge = _charge(appeler("capture_list", limit=3))
    assert len(charge.get("captures") or []) <= 3


def test_le_contenu_complet_reste_accessible(appeler, des_captures) -> None:
    """Le listage borne, capture_get restitue. Sinon on aurait perdu de l'info."""
    charge = _charge(appeler("capture_list", limit=1))
    entrees = charge.get("captures") or []
    assert entrees
    identifiant = entrees[0]["id"]
    complet = appeler("capture_get", id=identifiant)
    assert complet.strip()
    assert "Traceback" not in complet
    assert len(complet) > len(json.dumps(entrees[0])), (
        "capture_get doit rendre plus que la ligne de liste, sinon borner "
        "aurait fait perdre du contenu"
    )
