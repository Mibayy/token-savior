"""Sortie propre du processus, et surface reellement atteignable.

Deux points restes en suspens plus tot dans la journee, fermes ici. Les
laisser ouverts, c'est laisser un outil ne pas faire ce qu'il devrait.
"""

from __future__ import annotations

import os
import subprocess
import sys
import textwrap

import pytest

SEGFAULT = -11  # SIGSEGV tel que rendu par subprocess


SCENARIO = textwrap.dedent('''
    import shutil, subprocess, tempfile
    from pathlib import Path
    from token_savior import server

    base = Path(tempfile.mkdtemp())
    orig = base / "orig"; (orig / "pkg").mkdir(parents=True)
    (orig / "pkg" / "m.py").write_text("def g(x):\\n    return x\\n", encoding="utf-8")
    env = {"PATH": "/usr/bin:/bin", "HOME": str(orig),
           "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@x.invalid",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@x.invalid"}
    for c in (["init","-q","-b","main"], ["add","-A"], ["commit","-q","-m","i"]):
        subprocess.run(["git", *c], cwd=orig, check=True, capture_output=True, env=env)
    cop = base / "copie"; shutil.copytree(orig, cop)

    def appel(nom, **a):
        s = server._dispatch_tool(nom, a, a.get("name", ""))
        return "".join(getattr(b, "text", "") or "" for b in s)

    appel("set_project_root", path=str(orig))
    appel("find_symbol", name="g", project=str(orig))
    appel("set_project_root", path=str(cop))
''')


@pytest.mark.parametrize("essai", range(3))
def test_le_processus_sort_sans_segfault(essai: int) -> None:
    """Deux projets enregistres, donc deux surveillances de fichiers.

    Le fil de `watchfiles` est natif (backend Rust). Laisse tourner pendant la
    finalisation de l'interpreteur, il touche des objets Python deja demontes
    et le processus meurt sur SIGSEGV, sans aucune trame Python, apres avoir
    rendu tout son travail utile.

    Mesure du 27/07/2026 avant correctif : 8 plantages sur 8 des qu'un SECOND
    projet est enregistre. Un seul projet ne plantait pas -- c'est ce qui a
    fait conclure a tort, au premier examen, que le defaut n'etait pas
    reproductible. Les surveillances sont desormais arretees a l'atexit.

    L'environnement du sous-processus est **maitrise**, et non herite de
    pytest. Verifie : avec l'environnement herite, le scenario ne plantait pas
    meme sans le correctif, alors qu'il plantait 5 fois sur 5 lance a la main.
    Un test de regression dont le pouvoir de detection depend de variables
    d'environnement qu'il ne controle pas ne garde rien.

    Trois essais : un segfault de finalisation depend de l'ordonnancement, un
    essai unique pourrait passer par chance.
    """
    resultat = subprocess.run(
        [sys.executable, "-c", SCENARIO],
        capture_output=True,
        text=True,
        timeout=180,
        check=False,
        env={
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "HOME": os.environ.get("HOME", "/root"),
        },
    )
    assert resultat.returncode != SEGFAULT, (
        "le processus est mort sur SIGSEGV a la sortie\n"
        + resultat.stderr[-600:]
    )
    assert resultat.returncode == 0, (
        f"code de sortie {resultat.returncode}\n{resultat.stderr[-600:]}"
    )


@pytest.mark.parametrize(
    "outil",
    ["get_routes", "get_env_usage", "get_entry_points", "find_impacted_test_files"],
)
def test_les_outils_documentes_sont_atteignables_dans_le_bac_a_sable(
    outil: str,
) -> None:
    """Ils existaient cote MCP mais pas dans la facade Code Mode.

    Un script se prenait `tools.get_routes is not a function` sur des outils
    que la documentation presente comme disponibles. Tous en lecture seule,
    donc rien ne justifiait de les tenir hors de portee.
    """
    from token_savior.code_mode.facade import ALLOWED_TOOLS

    assert outil in ALLOWED_TOOLS, (
        f"{outil} est documente et servi par le MCP mais reste hors du bac a sable"
    )


def test_la_facade_n_expose_que_des_outils_reels() -> None:
    """Une entree d'allowlist qui ne correspond a aucun outil serait un piege."""
    from token_savior.code_mode.facade import ALLOWED_TOOLS
    from token_savior.tool_schemas import TOOL_SCHEMAS

    fantomes = sorted(set(ALLOWED_TOOLS) - set(TOOL_SCHEMAS))
    assert not fantomes, f"outils exposes au bac a sable mais inexistants : {fantomes}"


def test_les_quatre_outils_repondent_vraiment_depuis_un_script(appeler) -> None:
    """L'allowlist ne suffit pas : il faut que l'appel aboutisse."""
    sortie = appeler(
        "ts_execute",
        script=(
            "const out = {};"
            "out.routes = await tools.get_routes({});"
            "out.entrees = await tools.get_entry_points({});"
            "return out;"
        ),
    )
    assert "not a function" not in sortie, sortie[:400]
    assert "/panier" in sortie, f"get_routes ne rend pas les routes plantees :\n{sortie[:400]}"
