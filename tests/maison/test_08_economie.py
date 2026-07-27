"""La promesse chiffree : un outil cense faire gagner ne doit pas faire depenser.

Tout le projet repose sur une affirmation mesurable -- passer par l'outil
coute moins cher que l'alternative naive. Elle n'etait verifiee nulle part.

Audit du 27/07/2026, sur un projet-cobaye construit pour ca :

    get_function_source(petite fonction)   131 car.  vs  76 car. (fichier entier)
    get_edit_context                       459 car.  vs 216 car. (la chaine de trois)

Deux contradictions directes. La premiere venait de l'indice
"-> get_full_context(...)", ~70 caracteres ajoutes a *toutes* les reponses :
sur un petit symbole, l'outil cense economiser des tokens en depensait plus
que `cat`. La seconde d'une source expediee deux fois, en entier dans
`source` et en extrait dans `location.source_preview`.

Ces tests figent les deux proprietes. Ce qu'ils ne pretendent pas mesurer :
le cout d'un outil composite compare a une chaine qui rend moins de choses.
`get_edit_context` rend aussi les voisins et les tests impactes ; comparer les
tailles seules serait malhonnete, et son gain se compte en allers-retours.
"""

from __future__ import annotations

import json
import subprocess
import textwrap
from pathlib import Path

import pytest


@pytest.fixture
def projet_mesure(tmp_path: Path, appeler) -> Path:
    """Un fichier a une seule petite fonction, un autre a une grosse."""
    racine = tmp_path / "mesure"
    (racine / "pkg").mkdir(parents=True)
    (racine / "pkg" / "petit.py").write_text(
        textwrap.dedent('''
            """Petit module."""

            def court(x):
                """Trois lignes."""
                return x + 1
        ''').lstrip(),
        encoding="utf-8",
    )
    (racine / "pkg" / "gros.py").write_text(
        '"""Gros module."""\n\n\ndef long(x):\n'
        + "".join(f"    v{i} = {i}\n" for i in range(60))
        + "    return v0\n",
        encoding="utf-8",
    )
    env = {
        "PATH": "/usr/bin:/bin", "HOME": str(racine),
        "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@x.invalid",
        "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@x.invalid",
    }
    for cmd in (["init", "-q", "-b", "main"], ["add", "-A"], ["commit", "-q", "-m", "i"]):
        subprocess.run(["git", *cmd], cwd=racine, check=True, capture_output=True, env=env)
    appeler.brut("set_project_root", path=str(racine))
    return racine


def test_lire_une_petite_fonction_coute_moins_que_le_fichier(
    appeler, projet_mesure: Path,
) -> None:
    """Sinon `cat` est un meilleur outil, et tout le projet perd son sens."""
    fichier = (projet_mesure / "pkg" / "petit.py").read_text(encoding="utf-8")
    sortie = appeler(
        "get_function_source", project=str(projet_mesure), name="court", level=0,
    )
    assert len(sortie) <= len(fichier), (
        f"l'outil rend {len(sortie)} caracteres la ou lire le fichier entier "
        f"en coute {len(fichier)} :\n{sortie}"
    )


def test_l_indice_ne_pese_pas_plus_du_quart_de_la_reponse(
    appeler, projet_mesure: Path,
) -> None:
    """Une suggestion qui coute le quart de la reponse n'est pas rentable.

    Sur un petit symbole l'agent a deja ce qu'il cherchait : il n'a aucune
    raison de suivre l'indice, donc l'indice est du pur cout.
    """
    sortie = appeler(
        "get_function_source", project=str(projet_mesure), name="court", level=0,
    )
    assert "get_full_context(" not in sortie, (
        "l'indice est ajoute a une reponse trop courte pour l'absorber :\n" + sortie
    )


def test_l_indice_reste_present_quand_la_reponse_l_absorbe(
    appeler, projet_mesure: Path,
) -> None:
    """Le pendant : on ne supprime pas l'indice, on le rend proportionne."""
    sortie = appeler(
        "get_function_source", project=str(projet_mesure), name="long", level=0,
    )
    assert "get_full_context(" in sortie, (
        "sur une reponse longue l'indice est abordable et doit rester :\n"
        + sortie[-300:]
    )


def test_le_contexte_d_edition_n_expedie_pas_la_source_deux_fois(
    appeler, projet_mesure: Path,
) -> None:
    sortie = appeler("get_edit_context", project=str(projet_mesure), name="long")
    try:
        charge = json.loads(sortie)
    except json.JSONDecodeError:
        pytest.skip(f"reponse non JSON : {sortie[:200]}")
        return
    localisation = charge.get("location") or {}
    if charge.get("source"):
        assert "source_preview" not in localisation, (
            "la source complete est deja dans `source`, son extrait dans "
            "`location` la facture une seconde fois"
        )


@pytest.mark.parametrize("symbole", ["court", "long"])
def test_le_niveau_abrege_coute_moins_que_le_niveau_complet(
    appeler, projet_mesure: Path, symbole: str,
) -> None:
    complet = appeler(
        "get_function_source", project=str(projet_mesure), name=symbole,
        level=0, force_full=True,
    )
    abrege = appeler(
        "get_function_source", project=str(projet_mesure), name=symbole,
        level=2, force_full=True,
    )
    assert len(abrege) <= len(complet), (
        f"le niveau 2 rend {len(abrege)} caracteres contre {len(complet)} "
        f"pour le niveau 0 sur {symbole}"
    )


def test_le_resume_de_structure_coute_moins_que_le_fichier(
    appeler, projet_mesure: Path,
) -> None:
    fichier = (projet_mesure / "pkg" / "gros.py").read_text(encoding="utf-8")
    sortie = appeler(
        "get_structure_summary", project=str(projet_mesure), file_path="pkg/gros.py",
    )
    assert len(sortie) < len(fichier), (
        f"resume {len(sortie)} car. contre fichier {len(fichier)} car."
    )


def test_le_cache_ne_rallonge_jamais_une_reponse(
    appeler, projet_mesure: Path,
) -> None:
    """Deja corrige, fige ici avec les autres proprietes d'economie."""
    for symbole in ("court", "long"):
        premier = appeler(
            "get_function_source", project=str(projet_mesure), name=symbole, level=0,
        )
        second = appeler(
            "get_function_source", project=str(projet_mesure), name=symbole, level=0,
        )
        assert len(second) <= len(premier), (
            f"sur {symbole}, le second appel coute {len(second)} caracteres "
            f"contre {len(premier)} pour le premier"
        )
