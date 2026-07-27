"""Le compteur de telemetrie doit rester exact quand plusieurs processus ecrivent.

Ce fichier existe parce qu'une suite verte ne voyait pas le defaut. Il n'y
avait ni exception, ni processus en echec, ni fichier corrompu : juste des
nombres trop petits. `record_tool_call` gardait un instantane ABSOLU du
compteur en memoire et le reecrivait entierement a chaque incrementation, donc
le dernier ecrivain effacait le travail des autres.

Mesure du 27/07/2026 avant correction, temoin mono-processus exact a 400/400 :

     2 processus x 200 -> 212 / 400 comptees   (47 % perdues)
     8 processus x  50 -> 112, 104, 74 / 400   (72 a 81 % perdues)

Le compromis etait documente dans le module, avec cette justification : « this
is acceptable for the v2.8/v3.0 goal of "is this tool ever called" ». Il l'etait
peut-etre pour une question binaire. Il ne l'est plus depuis que ces compteurs
alimentent `ts gain`, les statistiques par projet, le dimensionnement du profil
`auto` et `scripts/ts_audit.py` : la question est devenue quantitative, et un
chiffre faux de moitie n'y repond pas.

Un test qui n'ecrirait que depuis un seul processus passerait avant comme
apres. C'est pour cela qu'on lance ici de vrais sous-processus.
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest


def _incrementer(stats_dir: Path, processus: int, par_processus: int) -> int:
    """Lance N processus qui incrementent, rend le total effectivement compte."""
    programme = (
        "from token_savior import telemetry\n"
        f"for _ in range({par_processus}):\n"
        "    telemetry.record_tool_call('find_symbol')\n"
    )
    env = {**dict(__import__("os").environ), "TOKEN_SAVIOR_STATS_DIR": str(stats_dir)}
    lances = [
        subprocess.Popen(
            [sys.executable, "-c", programme],
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
        )
        for _ in range(processus)
    ]
    for p in lances:
        _, err = p.communicate(timeout=300)
        assert p.returncode == 0, f"un processus a echoue : {err[-400:]}"

    fichier = stats_dir / "tool-calls.json"
    assert fichier.exists(), "aucun fichier de compteur n'a ete ecrit"
    charge = json.loads(fichier.read_text(encoding="utf-8"))
    return charge.get("counts", {}).get("unknown", {}).get("find_symbol", 0)


def test_un_seul_processus_compte_juste(tmp_path: Path) -> None:
    """Temoin. Sans lui, un echec du test suivant ne dirait pas ou est le probleme.

    Si celui-ci tombe, le defaut n'est pas dans la concurrence mais dans
    l'incrementation elle-meme, et il faut chercher ailleurs.
    """
    assert _incrementer(tmp_path, processus=1, par_processus=200) == 200


@pytest.mark.parametrize("processus,par_processus", [(2, 100), (8, 25)])
def test_aucune_incrementation_perdue_entre_processus(
    tmp_path: Path, processus: int, par_processus: int
) -> None:
    """Le total doit etre exact, pas seulement proche.

    Deux processus suffisaient a perdre pres de la moitie des incrementations :
    ce n'est pas un cas limite exotique, c'est le mode de travail normal des le
    moment ou un sous-agent tourne en meme temps que la session principale.
    """
    attendu = processus * par_processus
    compte = _incrementer(tmp_path, processus, par_processus)
    assert compte == attendu, (
        f"{attendu - compte} incrementation(s) perdue(s) sur {attendu} "
        f"avec {processus} processus concurrents. Le verrou de "
        f"telemetry._verrou n'a pas joue son role, ou un cache absolu a ete "
        f"reintroduit dans record_tool_call."
    )


def test_le_verrou_ne_fait_jamais_echouer_l_appelant(tmp_path: Path) -> None:
    """Un repertoire de stats impossible a verrouiller ne doit rien casser.

    La telemetrie est appelee dans le chemin de dispatch des outils. Elle a le
    droit de perdre une incrementation, jamais de lever une exception : un
    compteur ne doit pas pouvoir faire echouer l'outil qu'il compte.
    """
    from token_savior import telemetry

    inexistant = tmp_path / "chemin" / "qui" / "sera" / "cree"
    with telemetry._verrou(inexistant / "compteur.json"):
        pass  # ne doit pas lever

    fichier = tmp_path / "compteur.json"
    fichier.write_text("{}", encoding="utf-8")
    with telemetry._verrou(fichier):
        pass  # ne doit pas lever
