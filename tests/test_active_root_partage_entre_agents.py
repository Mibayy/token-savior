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


@pytest.fixture
def cobaye(tmp_path):
    """Un projet réel et minuscule : le chemin QFN exige un index construit."""
    (tmp_path / "boutique.py").write_text(
        '"""Boutique."""\n'
        "\n"
        "\n"
        "def calculer_total(lignes):\n"
        '    """Somme les lignes."""\n'
        "    return sum(lignes)\n",
        encoding="utf-8",
    )
    return tmp_path


@pytest.fixture
def session_multi_projets(etat, cobaye, monkeypatch):
    """Un processus qui a déjà vu deux racines, servant le cobaye par défaut."""
    from token_savior.slot_manager import SlotManager

    gestionnaire = SlotManager(cache_version=1)
    gestionnaire.register_roots([str(cobaye)])
    gestionnaire.active_root = str(cobaye)
    monkeypatch.setattr(etat, "_slot_mgr", gestionnaire)
    monkeypatch.setattr(
        etat, "_racines_actives_vues", {str(cobaye), "/depots/celui-du-sous-agent"}
    )
    return cobaye


def _texte(sortie) -> str:
    return "".join(getattr(bloc, "text", "") or "" for bloc in sortie)


@pytest.mark.parametrize(
    "outil, arguments",
    [
        ("get_git_status", {}),
        ("find_symbol", {"name": "calculer_total"}),
    ],
)
def test_letiquette_couvre_les_deux_tables_de_dispatch(
    session_multi_projets, outil, arguments
):
    """L'étiquette doit être posée quelle que soit la table qui a servi l'appel.

    Elle ne vivait que sur la branche `_SLOT_HANDLERS`. Les outils de
    navigation -- `find_symbol`, `search_codebase`, `get_full_context` --
    passent par `_QFN_HANDLERS` et rendaient donc une réponse muette sur sa
    source, c'est-à-dire l'incident même que l'étiquette devait signaler, sur
    la majorité des appels réels.
    """
    import token_savior.server as srv

    sortie = _texte(srv._dispatch_tool(outil, dict(arguments), ""))

    etiquette = f"[project: {session_multi_projets.name}]"
    assert etiquette in sortie, (
        f"appel sans indice servi par '{outil}' : la réponse ne nomme pas le "
        f"projet qui a répondu"
    )
    assert sortie.count("[project: ") == 1, "une seule étiquette, pas deux"


@pytest.fixture
def dossier_etranger(tmp_path_factory):
    """Un dossier hors de tout projet enregistré.

    Il doit être créé ailleurs que sous le cobaye : un sous-répertoire d'un
    projet enregistré résout vers ce projet, et le test passerait alors sans
    rien prouver.
    """
    racine = tmp_path_factory.mktemp("hors-projet")
    (racine / "jetable.py").write_text(
        "def alpha(x):\n    return x\n\n\ndef beta(x):\n    return x\n", encoding="utf-8"
    )
    return racine


def test_sticky_couvre_aussi_un_chemin_jamais_enregistre(
    session_multi_projets, etat, monkeypatch, dossier_etranger
):
    """Le trou mesuré le 09/08/2026, en cherchant à lire un fichier de /tmp.

    `TS_STICKY_ACTIVE` était appliqué dans `server_state.noter_racine_active`,
    au niveau du dispatch. Mais `SlotManager.resolve` écrivait `active_root`
    lui-même sur une branche : celle du chemin réel que personne n'a
    enregistré -- exactement le dépôt cloné dans /tmp, le dossier d'export,
    le fichier de travail. Le gel ne tenait donc pas là où le besoin est le
    plus fréquent : l'appel suivant, sans `project=`, repartait sur le
    mauvais dépôt en silence.
    """
    import token_savior.server as srv

    monkeypatch.setattr(etat, "_STICKY_ACTIVE", True, raising=True)

    srv._dispatch_tool(
        "find_symbol", {"name": "alpha", "project": str(dossier_etranger)}, ""
    )

    assert etat._slot_mgr.active_root == str(session_multi_projets), (
        "un indice vers un dossier non enregistré a déplacé le défaut partagé "
        "malgré TS_STICKY_ACTIVE"
    )


def test_sans_sticky_un_chemin_inconnu_promeut_toujours(
    session_multi_projets, etat, dossier_etranger
):
    """Le comportement historique reste le défaut : seul le gel le change."""
    import token_savior.server as srv

    srv._dispatch_tool(
        "find_symbol", {"name": "beta", "project": str(dossier_etranger)}, ""
    )

    assert etat._slot_mgr.active_root == str(dossier_etranger)


def test_un_indice_explicite_ne_declenche_pas_letiquette(session_multi_projets):
    """Un appel qui nomme son projet sait déjà d'où vient la réponse."""
    import token_savior.server as srv

    sortie = _texte(
        srv._dispatch_tool(
            "find_symbol",
            {"name": "calculer_total", "project": str(session_multi_projets)},
            "",
        )
    )

    assert "[project: " not in sortie
