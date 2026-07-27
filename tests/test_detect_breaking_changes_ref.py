"""Le handler doit honorer la ref demandee, pas la jeter en silence.

Regression du 27/07/2026. Le schema public de `detect_breaking_changes`
expose `ref` : c'est ce que documente CLAUDE.md (`ref="v1"`), ce qu'envoie la
CLI (`{"ref": args.ref}`) et ce que declare tool_schemas. Le handler, lui, ne
lisait que `since_ref`, le nom interne du moteur, que personne n'envoie
jamais. La valeur demandee etait donc silencieusement jetee et l'analyse
repartait toujours de HEAD~1.

Verifie en conditions reelles avant correctif : `HEAD~1`, `HEAD~3`, `HEAD~6`
et le tag `v4.18.0` rendaient tous les quatre
"Breaking Change Analysis (HEAD~1..working tree)".

C'est le pire endroit ou perdre un argument. La regle du depot est "avant un
commit/PR, detect_breaking_changes pour verifier qu'on ne casse pas l'API" :
toute verification contre un tag de release ne comparait en fait que le
dernier commit, et repondait rassurant.

Pourquoi les 11 tests de test_breaking_changes.py ne l'ont pas vu : ils
appellent tous le moteur en direct avec `since_ref=`, jamais le handler. La
traduction d'arguments n'etait couverte nulle part. Ce test attaque donc
exactement cette couche.
"""

from __future__ import annotations

import pytest


class _IndexeurFactice:
    _project_index = None


class _SlotFactice:
    root = "/projet/factice"
    indexer = _IndexeurFactice()


@pytest.fixture
def refs_vues(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Intercepte le moteur et enregistre la ref qu'il recoit reellement."""
    from token_savior.server_handlers import analysis

    vues: list[str] = []

    def faux_moteur(index, since_ref="HEAD~1"):
        vues.append(since_ref)
        return "no breaking changes"

    monkeypatch.setattr(analysis, "run_breaking_changes", faux_moteur)
    monkeypatch.setattr(analysis, "_prep", lambda slot: None)
    return vues


@pytest.mark.parametrize(
    ("arguments", "attendu"),
    [
        ({"ref": "v4.18.0"}, "v4.18.0"),
        ({"ref": "HEAD~6"}, "HEAD~6"),
        ({"ref": "abc1234"}, "abc1234"),
    ],
)
def test_la_ref_demandee_arrive_au_moteur(
    refs_vues: list[str], arguments: dict, attendu: str,
) -> None:
    from token_savior.server_handlers import analysis

    analysis._h_detect_breaking_changes(_SlotFactice(), arguments)
    assert refs_vues == [attendu], (
        f"ref demandee {arguments['ref']!r}, ref reellement analysee "
        f"{refs_vues!r} -- l'argument a ete jete"
    )


def test_le_nom_interne_reste_accepte(refs_vues: list[str]) -> None:
    """`since_ref` continue de fonctionner pour qui l'utilisait deja."""
    from token_savior.server_handlers import analysis

    analysis._h_detect_breaking_changes(_SlotFactice(), {"since_ref": "HEAD~2"})
    assert refs_vues == ["HEAD~2"]


def test_ref_l_emporte_sur_since_ref(refs_vues: list[str]) -> None:
    """En cas de doublon, le nom public tranche."""
    from token_savior.server_handlers import analysis

    analysis._h_detect_breaking_changes(
        _SlotFactice(), {"ref": "v2", "since_ref": "v1"},
    )
    assert refs_vues == ["v2"]


def test_sans_argument_le_defaut_est_conserve(refs_vues: list[str]) -> None:
    from token_savior.server_handlers import analysis

    analysis._h_detect_breaking_changes(_SlotFactice(), {})
    assert refs_vues == ["HEAD~1"]
