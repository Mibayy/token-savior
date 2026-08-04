"""Un `switch_project` d'un sous-agent ne doit pas repointer les autres en silence.

Le 05/08/2026, une recherche lancée sur ce dépôt a rendu les fichiers d'un
concurrent : un sous-agent parallèle venait d'appeler `switch_project`, et
`active_root` vit sur le *processus*, partagé par la session et tous ses
sous-agents. Réponse bien formée, entièrement fausse, aucun avertissement.

Deux propriétés sont vérifiées ici, et aucune n'existait avant :

1. `TS_STICKY_ACTIVE` couvre `switch_project`. Il ne gelait que la promotion
   implicite d'un indice `project=`, ce qui laissait le basculement explicite
   passer à côté de la barrière.
2. Le processus se souvient d'avoir vu plusieurs racines, ce qui permet de
   nommer le projet qui a répondu.
"""

from __future__ import annotations

import pytest


@pytest.fixture
def etat(monkeypatch):
    """État isolé, sans recharger le module.

    Un `importlib.reload(server_state)` remet bien le jeu de racines à zéro,
    et il remplace aussi les objets partagés que d'autres suites tiennent déjà
    par référence : trois tests sans rapport tombaient, dans la suite complète
    seulement, jamais en isolation. Mesuré le 05/08/2026. On remplace donc les
    deux attributs, que monkeypatch restaure ensuite.
    """
    from token_savior import server_state

    monkeypatch.setattr(server_state, "_racines_actives_vues", set(), raising=True)
    monkeypatch.setattr(server_state._slot_mgr, "active_root", "/depots/le-mien", raising=False)
    return server_state


def test_sans_sticky_le_basculement_sapplique(etat):
    gele = etat.noter_racine_active("/depots/celui-du-sous-agent")

    assert gele is False
    assert etat._slot_mgr.active_root == "/depots/celui-du-sous-agent"


def test_avec_sticky_la_racine_partagee_ne_bouge_pas(etat, monkeypatch):
    monkeypatch.setattr(etat, "_STICKY_ACTIVE", True, raising=True)

    gele = etat.noter_racine_active("/depots/celui-du-sous-agent")

    assert gele is True, "sticky doit refuser le basculement, pas seulement la promotion"
    assert etat._slot_mgr.active_root == "/depots/le-mien"


def test_le_refus_est_quand_meme_enregistre(etat, monkeypatch):
    """Sinon la réponse ne saurait pas qu'un second projet était en jeu."""
    monkeypatch.setattr(etat, "_STICKY_ACTIVE", True, raising=True)

    etat.noter_racine_active("/depots/le-mien")
    assert etat.racines_multiples() is False

    etat.noter_racine_active("/depots/celui-du-sous-agent")
    assert etat.racines_multiples() is True


def test_une_seule_racine_ne_declenche_aucune_etiquette(etat):
    etat.noter_racine_active("/depots/le-mien")
    etat.noter_racine_active("/depots/le-mien")

    assert etat.racines_multiples() is False, (
        "étiqueter chaque réponse d'une session mono-projet serait du bruit payé "
        "à chaque appel"
    )
