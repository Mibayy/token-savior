"""Un abrege ne doit jamais etre enregistre comme s'il etait le corps.

Regression du 26/07/2026. Quand l'appelant ne precise pas `level`, les
handlers get_function_source / get_class_source tirent le niveau
d'abstraction au sort via le bandit (`thompson_sample_level`), et ce tirage
peut rendre 2, donc un abrege sans corps.

Le cache de session, lui, lisait `args["level"]` -- absent, donc 0. Il
enregistrait alors l'abrege comme etant la source complete. Le rappel
suivant, meme avec `level=0` explicite, recevait
"body unchanged since last view - reuse the body you already have" alors
que l'appelant n'avait jamais recu de corps, et n'avait d'autre issue que
`force_full=true`.
"""

from __future__ import annotations

import pytest


class _SlotFactice:
    root = "/projet/factice"


@pytest.fixture
def code_nav(monkeypatch: pytest.MonkeyPatch):
    from token_savior.server_handlers import code_nav as module

    # Le symbole resout toujours, avec une empreinte de corps stable : on
    # veut isoler la decision de mise en cache, pas la resolution.
    monkeypatch.setattr(
        module,
        "_lookup_symbol_meta",
        lambda slot, args: ("function", "empreinte-stable", "def f()"),
    )
    module.state._session_symbol_cache.clear()
    yield module
    module.state._session_symbol_cache.clear()


def test_un_abrege_echantillonne_ne_remplit_pas_le_cache(code_nav) -> None:
    args = {"name": "f"}  # aucun `level` : c'est le bandit qui tranche
    abrege = "[L2] f()\n  side-effects: yes"
    corps = "def f():\n    return 1"

    # Premier appel : le bandit a tire 2, on sert un abrege.
    sortie = code_nav._csc_maybe_serve(
        _SlotFactice(), "function", args, lambda: abrege, effective_level=2,
    )
    assert sortie == abrege
    assert not code_nav.state._session_symbol_cache, (
        "un abrege a ete enregistre comme s'il etait la source complete"
    )

    # Deuxieme appel, corps explicitement demande : il doit arriver.
    sortie = code_nav._csc_maybe_serve(
        _SlotFactice(), "function", args, lambda: corps, effective_level=0,
    )
    assert sortie == corps, (
        "le corps a ete remplace par un renvoi au cache alors qu'il n'avait "
        "jamais ete servi"
    )


def test_le_cache_fonctionne_toujours_quand_le_corps_a_bien_ete_servi(
    code_nav,
) -> None:
    """Le correctif ne doit pas desactiver l'economie recherchee."""
    args = {"name": "f"}
    # Corps volontairement long : depuis le 27/07/2026 l'accuse n'est servi
    # que s'il est reellement plus court que la source. Sur une fonction de
    # trois lignes, la source est la reponse la moins chere et c'est elle qui
    # revient -- ce que verifie le test voisin.
    corps = "def f():\n" + "".join(f"    x{i} = {i}\n" for i in range(40)) + "    return x0"

    premier = code_nav._csc_maybe_serve(
        _SlotFactice(), "function", args, lambda: corps, effective_level=0,
    )
    assert premier == corps

    second = code_nav._csc_maybe_serve(
        _SlotFactice(), "function", args, lambda: corps, effective_level=0,
    )
    assert second != corps, "le deuxieme appel aurait du etre servi en compact"
    assert "unchanged" in second
    assert len(second) < len(premier)
