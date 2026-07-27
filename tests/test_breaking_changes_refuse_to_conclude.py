"""`detect_breaking_changes` ne doit jamais rendre un feu vert qu'il n'a pas mesure.

Cet outil est explicitement un controle d'avant-commit : le CLAUDE.md du projet
dit de le lancer avant un commit ou une PR pour verifier qu'on ne casse pas
l'API. Un controle qui echoue en disant que tout va bien est pire que pas de
controle du tout, parce qu'il retire la vigilance qu'on aurait eue sans lui.

Mesure du 27/07/2026, sur un depot ou un parametre venait reellement d'etre
supprime, donc avec un changement cassant bien present :

    reference valide      -> 1 issue, parameter 'taux' was removed
    reference inexistante -> "no breaking changes detected"
    repertoire sans git   -> "no breaking changes detected"

Les deux derniers cas ne rataient pas le changement : ils ne regardaient rien,
et rendaient la phrase qu'on lit comme un feu vert.

Aucun test existant ne pouvait l'attraper : ils partent tous d'une fixture
`git_repo` correctement initialisee avec une reference valide, c'est-a-dire du
seul chemin ou l'outil fonctionne.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from token_savior.breaking_changes import detect_breaking_changes
from token_savior.project_indexer import ProjectIndexer

AVANT = "def total(q, taux):\n    return q * taux\n"
APRES = "def total(q):\n    return q\n"  # parametre supprime : cassant


def _depot_avec_changement_cassant(racine: Path) -> None:
    (racine / "m.py").write_text(AVANT, encoding="utf-8")
    for commande in (
        ["git", "init", "-q"],
        ["git", "add", "-A"],
        ["git", "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q", "-m", "v1"],
        ["git", "tag", "v1"],
    ):
        subprocess.run(commande, cwd=racine, check=True)
    (racine / "m.py").write_text(APRES, encoding="utf-8")


def _rapport(racine: Path, ref: str) -> str:
    return detect_breaking_changes(ProjectIndexer(str(racine)).index(), since_ref=ref)


def test_temoin_la_reference_valide_voit_bien_le_changement(tmp_path: Path) -> None:
    """Sans ce temoin, un echec des tests suivants ne dirait pas ou est le probleme.

    S'il tombe, ce n'est pas la gestion d'erreur qui est en cause mais la
    detection elle-meme, et il faut chercher ailleurs.
    """
    _depot_avec_changement_cassant(tmp_path)
    rapport = _rapport(tmp_path, "v1")
    assert "taux" in rapport, rapport
    assert "no breaking changes" not in rapport.lower(), rapport


@pytest.mark.parametrize(
    "ref,situation",
    [
        ("reference-qui-n-existe-pas", "une reference introuvable"),
        ("HEAD~99999", "une reference hors de l'historique"),
    ],
)
def test_une_reference_irresolvable_ne_rend_pas_un_feu_vert(
    tmp_path: Path, ref: str, situation: str
) -> None:
    _depot_avec_changement_cassant(tmp_path)
    rapport = _rapport(tmp_path, ref)
    assert "no breaking changes" not in rapport.lower(), (
        f"Avec {situation}, le rapport se lit comme un feu vert alors que rien "
        f"n'a ete compare :\n{rapport}"
    )
    assert "DID NOT RUN" in rapport, rapport


def test_un_repertoire_sans_git_ne_rend_pas_un_feu_vert(tmp_path: Path) -> None:
    """Le cas le plus courant en vrai : lancer l'outil hors d'un depot.

    Un repertoire sans git ne peut rien avoir a comparer. Repondre « aucun
    changement cassant » y est litteralement vrai et pratiquement mensonger.
    """
    (tmp_path / "m.py").write_text(APRES, encoding="utf-8")
    rapport = _rapport(tmp_path, "HEAD~1")
    assert "no breaking changes" not in rapport.lower(), rapport
    assert "not a git repository" in rapport, rapport


def test_le_rapport_dit_explicitement_qu_il_ne_conclut_pas(tmp_path: Path) -> None:
    """La formulation compte autant que le fait de ne pas mentir.

    Un rapport qui signale l'echec en petits caracteres mais reste lisible
    comme un succes ne corrige rien. On exige la phrase qui interdit la
    mauvaise lecture.
    """
    _depot_avec_changement_cassant(tmp_path)
    rapport = _rapport(tmp_path, "ref-absente")
    assert "NOT a clean bill of health" in rapport, rapport
