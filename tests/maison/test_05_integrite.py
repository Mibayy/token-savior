"""Integrite : ce qui ne doit jamais arriver, meme en repondant "ok".

Les deux defauts figes ici ont ceci de commun qu'ils rendaient tous les deux
un succes. Un outil qui echoue bruyamment se corrige ; un outil qui reussit
en ecrivant au mauvais endroit se decouvre bien plus tard, quand le travail
est deja perdu.
"""

from __future__ import annotations

import shutil
import subprocess
import textwrap
from pathlib import Path

import pytest


def _depot(racine: Path, contenu: str) -> Path:
    (racine / "pkg").mkdir(parents=True, exist_ok=True)
    (racine / "pkg" / "m.py").write_text(textwrap.dedent(contenu).lstrip("\n"), encoding="utf-8")
    env = {
        "PATH": "/usr/bin:/bin", "HOME": str(racine),
        "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@x.invalid",
        "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@x.invalid",
    }
    for cmd in (["init", "-q", "-b", "main"], ["add", "-A"], ["commit", "-q", "-m", "i"]):
        subprocess.run(["git", *cmd], cwd=racine, check=True, capture_output=True, env=env)
    return racine


def test_editer_une_copie_n_ecrit_pas_dans_l_original(appeler, tmp_path: Path) -> None:
    """Le defaut le plus grave trouve le 27/07/2026.

    Un projet copie apres indexation emporte son \\.token-savior-cache.json,
    qui contient les chemins absolus de l'ORIGINAL. Charge depuis la copie, ce
    cache "matchait" sur la ref git : l'edition demandee sur la copie etait
    ecrite dans l'original, avec un ok: true et un chemin relatif en retour.
    Rien ne paraissait anormal.

    Scenario reel : `cp -r projet projet-essai`, on travaille sur la copie, et
    le travail part dans l'original.
    """
    original = _depot(tmp_path / "orig", '''
        def g(x):
            """ORIGINAL."""
            return x
    ''')
    appeler.brut("set_project_root", path=str(original))
    appeler("find_symbol", project=str(original), name="g")  # peuple le cache

    copie = tmp_path / "copie"
    shutil.copytree(original, copie)
    assert (copie / ".token-savior-cache.json").exists(), (
        "le scenario suppose que le cache est bien emporte par la copie"
    )

    appeler.brut("set_project_root", path=str(copie))
    appeler(
        "replace_symbol_source",
        project=str(copie),
        symbol_name="g",
        file_path="pkg/m.py",
        new_source='def g(x):\n    """MODIFIE."""\n    return x + 1\n',
    )

    texte_original = (original / "pkg" / "m.py").read_text(encoding="utf-8")
    texte_copie = (copie / "pkg" / "m.py").read_text(encoding="utf-8")
    assert "MODIFIE" not in texte_original, (
        "l'edition demandee sur la copie a ete ecrite dans l'original :\n"
        + texte_original
    )
    assert "MODIFIE" in texte_copie, (
        "l'edition n'a pas atteint la copie :\n" + texte_copie
    )


@pytest.mark.parametrize(
    "decorateur",
    [
        "@functools.cache",
        "@property",
        "@staticmethod",
        "@pytest.fixture",
        '@pytest.mark.parametrize("x", [1, 2])',
    ],
)
def test_un_remplacement_preserve_le_decorateur(
    appeler, tmp_path: Path, decorateur: str,
) -> None:
    """Le decorateur est au-dessus du `def` : sa perte ne se voit pas.

    La plage indexee d'un symbole commence a la premiere ligne de son bloc,
    decorateurs compris, alors que get_function_source ne les montre jamais.
    Un appelant qui relit puis remplace ne peut donc pas les restituer.

    Ce defaut a mange un @pytest.fixture et un @pytest.mark.parametrize
    pendant l'ecriture de cette serie, cassant 73 tests d'un coup.
    """
    racine = _depot(tmp_path / f"dec{abs(hash(decorateur)) % 9999}", f'''
        import functools

        import pytest


        {decorateur}
        def valeur(x):
            """Avant."""
            return x * 2
    ''')
    appeler.brut("set_project_root", path=str(racine))
    appeler(
        "replace_symbol_source",
        project=str(racine),
        symbol_name="valeur",
        file_path="pkg/m.py",
        new_source='def valeur(x):\n    """Apres."""\n    return x * 3\n',
    )
    apres = (racine / "pkg" / "m.py").read_text(encoding="utf-8")
    assert "Apres" in apres, f"l'edition n'a pas eu lieu :\n{apres}"
    assert decorateur in apres, f"decorateur {decorateur} mange :\n{apres}"


def test_une_nouvelle_source_avec_decorateur_ne_le_double_pas(
    appeler, tmp_path: Path,
) -> None:
    """Si l'appelant fournit lui-meme le decorateur, on ne l'ajoute pas deux fois."""
    racine = _depot(tmp_path / "dec_fourni", '''
        import functools


        @functools.cache
        def valeur(x):
            """Avant."""
            return x * 2
    ''')
    appeler.brut("set_project_root", path=str(racine))
    appeler(
        "replace_symbol_source",
        project=str(racine),
        symbol_name="valeur",
        file_path="pkg/m.py",
        new_source=(
            "@functools.cache\ndef valeur(x):\n"
            '    """Apres."""\n    return x * 3\n'
        ),
    )
    apres = (racine / "pkg" / "m.py").read_text(encoding="utf-8")
    assert apres.count("@functools.cache") == 1, f"decorateur duplique :\n{apres}"
    assert "Apres" in apres


def test_un_symbole_sans_decorateur_est_remplace_entierement(
    appeler, tmp_path: Path,
) -> None:
    """Le cas ordinaire ne doit pas avoir change de comportement."""
    racine = _depot(tmp_path / "sans_dec", '''
        def simple(x):
            """Avant."""
            return x
    ''')
    appeler.brut("set_project_root", path=str(racine))
    appeler(
        "replace_symbol_source",
        project=str(racine),
        symbol_name="simple",
        file_path="pkg/m.py",
        new_source='def simple(x):\n    """Apres."""\n    return x + 1\n',
    )
    apres = (racine / "pkg" / "m.py").read_text(encoding="utf-8")
    assert "Apres" in apres
    assert "Avant" not in apres, f"l'ancien corps subsiste :\n{apres}"


@pytest.mark.parametrize(
    "chemin_hostile",
    ["../evasion.py", "/etc/passwd", "pkg/../../evasion.py"],
)
def test_un_chemin_hors_projet_est_refuse(
    appeler, tmp_path: Path, chemin_hostile: str,
) -> None:
    """Aucune edition ne doit pouvoir sortir de la racine du projet."""
    racine = _depot(tmp_path / f"garde{abs(hash(chemin_hostile)) % 9999}", '''
        def g(x):
            """Doc."""
            return x
    ''')
    temoin = tmp_path / "evasion.py"
    temoin.write_text("intact\n", encoding="utf-8")

    appeler.brut("set_project_root", path=str(racine))
    sortie = appeler(
        "replace_symbol_source",
        project=str(racine),
        symbol_name="g",
        file_path=chemin_hostile,
        new_source="def g(x):\n    return 0\n",
    )
    assert "Traceback" not in sortie
    assert temoin.read_text(encoding="utf-8") == "intact\n", (
        f"un fichier hors projet a ete ecrit via {chemin_hostile!r}"
    )
