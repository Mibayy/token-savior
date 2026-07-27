"""`--mode les-deux` : le temoin biaise a cote de la mesure honnete, en un run.

Le script sait deja construire les deux jeux de requetes, mais un seul par
execution. Le biais du mode `titre` ne se voit pourtant que par comparaison :
seul, un 100 % flatteur ressemble a un bon resultat. Deux commandes a lancer,
c'est bon marche ; ce n'est pas automatique, et c'est la difference qui a coute
une decision le 27/07/2026 -- une amelioration mesuree sur un corpus derive des
titres, jugee trop belle, ecartee au profit d'une variante qui ne changeait
rien.
"""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

from token_savior import memory_db

PROJET = "/opt/projet-eval"
RACINE = Path(__file__).resolve().parents[1]


@pytest.fixture
def script():
    spec = importlib.util.spec_from_file_location(
        "eval_rappel", RACINE / "scripts" / "eval_rappel.py",
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def base(tmp_path, monkeypatch):
    from token_savior import db_core
    from token_savior import memory_db as md

    cible = tmp_path / "memoire.db"
    monkeypatch.setattr(db_core, "MEMORY_DB_PATH", cible)
    monkeypatch.setattr(md, "MEMORY_DB_PATH", cible)
    # `construire_jeu` exige titre > 20 et contenu > 80 caracteres, et au moins
    # cinq jetons utilisables -- dans le titre pour le mode `titre`, dans le
    # contenu et absents du titre pour le mode `contenu`.
    for i in range(12):
        memory_db.observation_save(
            None, PROJET, "convention",
            f"Convention numero {i} regissant nommage horodatage condensat "
            f"artefacts publies",
            f"Chaque livrable du lot {i} embarque une empreinte tronquee plutot "
            f"qu'un compteur incremental, lequel ne survit jamais a deux "
            f"machines emettant simultanement vers le meme entrepot. Cas {i}.",
        )
    return cible


def test_les_deux_modes_sortent_du_meme_run(script, base, capsys) -> None:
    """Sans comparaison, un temoin biaise se lit comme un bon resultat."""
    code = script.main([
        "--projet", PROJET, "--db", str(base), "--mode", "les-deux", "--n", "5",
    ])
    assert code == 0
    sortie = capsys.readouterr().out
    assert "contenu" in sortie and "titre" in sortie, sortie
    assert "BIAISE" in sortie, "le temoin doit rester etiquete comme tel"


def test_les_deux_en_json_porte_les_deux_mesures(script, base, capsys) -> None:
    code = script.main([
        "--projet", PROJET, "--db", str(base), "--mode", "les-deux", "--n", "5",
        "--json",
    ])
    assert code == 0
    charge = json.loads(capsys.readouterr().out)
    assert set(charge["modes"]) == {"contenu", "titre"}, charge
    for mode, bloc in charge["modes"].items():
        assert bloc["mode"] == mode
        assert "mesure" in bloc


def test_un_mode_seul_garde_sa_forme_de_sortie(script, base, capsys) -> None:
    """Le contrat existant ne bouge pas : `modes` n'apparait qu'en les-deux."""
    code = script.main([
        "--projet", PROJET, "--db", str(base), "--mode", "contenu", "--n", "5",
        "--json",
    ])
    assert code == 0
    charge = json.loads(capsys.readouterr().out)
    assert charge["mode"] == "contenu"
    assert "modes" not in charge
