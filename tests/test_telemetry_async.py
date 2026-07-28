"""L'ecriture differee de la telemetrie doit agreger, et ne rien perdre.

Ce que ces tests defendent :
  - le chemin de dispatch ne paie plus le read-modify-write sous flock ;
  - N appels en rafale ne produisent PAS N ecritures ;
  - ce qui est en file au moment de la sortie du processus est bien compte,
    sinon les sessions courtes seraient systematiquement sous-comptees, ce
    qui est un biais et pas une simple perte.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest


@pytest.fixture
def stats_dir(tmp_path, monkeypatch):
    d = tmp_path / "stats"
    d.mkdir()
    monkeypatch.setenv("TOKEN_SAVIOR_STATS_DIR", str(d))
    return d


def _counts(module) -> dict[str, int]:
    """Somme par outil, tous clients confondus."""
    etat = module._load()
    total: dict[str, int] = {}
    for bucket in etat.get("counts", {}).values():
        for nom, n in bucket.items():
            total[nom] = total.get(nom, 0) + n
    return total


def _attendre(predicat, delai=5.0):
    fin = time.time() + delai
    while time.time() < fin:
        if predicat():
            return True
        time.sleep(0.02)
    return False


def test_async_finit_par_compter(stats_dir):
    from token_savior import telemetry

    telemetry.record_tool_call_async("find_symbol")
    assert _attendre(lambda: _counts(telemetry).get("find_symbol") == 1), _counts(
        telemetry
    )


def test_rafale_totalement_comptee(stats_dir):
    """Cinquante appels donnent cinquante, quel que soit le decoupage en lots."""
    from token_savior import telemetry

    for _ in range(50):
        telemetry.record_tool_call_async("get_full_context")
    assert _attendre(
        lambda: _counts(telemetry).get("get_full_context") == 50
    ), _counts(telemetry)


def test_agregation_reduit_le_nombre_d_ecritures(stats_dir, monkeypatch):
    """Le coeur de la correction : N appels ne font pas N ecritures.

    Sans agregation ce compteur vaudrait 200. On exige nettement moins ; la
    borne est large a dessein pour ne pas dependre de l'ordonnancement.
    """
    from token_savior import telemetry

    ecritures = {"n": 0}
    vrai_save = telemetry._save

    def _save_compte(data):
        ecritures["n"] += 1
        return vrai_save(data)

    monkeypatch.setattr(telemetry, "_save", _save_compte)

    for _ in range(200):
        telemetry.record_tool_call_async("search_codebase")

    assert _attendre(
        lambda: _counts(telemetry).get("search_codebase") == 200
    ), _counts(telemetry)
    assert ecritures["n"] < 100, (
        f"{ecritures['n']} ecritures pour 200 appels : l'agregation ne joue pas"
    )


def test_appel_synchrone_reste_durable(stats_dir):
    """`record_tool_call` doit rester lisible immediatement apres l'appel.

    C'est le contrat de ses appelants directs : seul le point de dispatch
    devient fire-and-forget.
    """
    from token_savior import telemetry

    telemetry.record_tool_call("reindex")
    assert _counts(telemetry).get("reindex") == 1


def test_nom_vide_ignore(stats_dir):
    from token_savior import telemetry

    telemetry.record_tool_call_async("")
    time.sleep(0.2)
    assert "" not in _counts(telemetry)


def test_flush_a_la_sortie_du_processus(tmp_path):
    """Un processus qui se termine juste apres l'appel compte quand meme.

    Le vrai test du flush : un sous-processus reel, pas un atexit simule. Sans
    `_flush_a_la_sortie`, la file part avec le processus et le compteur reste
    a zero.
    """
    d = tmp_path / "stats"
    d.mkdir()
    env = dict(os.environ, TOKEN_SAVIOR_STATS_DIR=str(d))
    racine = Path(__file__).resolve().parents[1] / "src"
    env["PYTHONPATH"] = str(racine) + os.pathsep + env.get("PYTHONPATH", "")

    code = (
        "from token_savior import telemetry\n"
        "for _ in range(5):\n"
        "    telemetry.record_tool_call_async('get_git_status')\n"
    )
    r = subprocess.run(
        [sys.executable, "-c", code],
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert r.returncode == 0, r.stderr

    fichiers = list(d.glob("*.json"))
    assert fichiers, f"aucun compteur ecrit : {list(d.iterdir())}"
    total = 0
    for f in fichiers:
        data = json.loads(f.read_text())
        for bucket in data.get("counts", {}).values():
            total += bucket.get("get_git_status", 0)
    assert total == 5, f"attendu 5, obtenu {total} — le flush de sortie a perdu des appels"
