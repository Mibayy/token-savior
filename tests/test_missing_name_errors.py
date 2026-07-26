"""Un argument manquant doit dire quoi fournir, pas fuiter une KeyError.

Trouve par audit de 69 tools appeles un par un : trois d'entre eux rendaient
`Error: 'name'` — la representation d'une `KeyError` Python, telle quelle. Pour
un client LLM c'est le pire message possible : il ne nomme ni l'argument
manquant ni la facon de l'obtenir, donc l'appelant reessaie a l'aveugle et paie
l'aller-retour deux fois. C'est exactement le gaspillage que ce projet combat.

Les trois concernes sont parmi les plus utilises : `get_function_source`,
`get_class_source`, `get_full_context`.
"""
from __future__ import annotations

import pytest

from token_savior.server_handlers.code_nav import _require_name


@pytest.mark.parametrize("tool", ["get_function_source", "get_class_source",
                                   "get_full_context"])
@pytest.mark.parametrize("args", [{}, {"name": ""}, {"name": None},
                                  {"name": 42}, {"nom": "x"}])
def test_refuse_un_nom_absent_ou_invalide(tool: str, args: dict) -> None:
    msg = _require_name(args, tool)
    assert msg is not None, (tool, args)
    assert msg.startswith(tool), "le message doit nommer l outil appele"
    assert "name=" in msg, "il doit nommer l'argument manquant"


@pytest.mark.parametrize("tool", ["get_function_source", "get_class_source"])
def test_laisse_passer_un_nom_valide(tool: str) -> None:
    assert _require_name({"name": "ma_fonction"}, tool) is None


def test_le_mode_batch_est_accepte_la_ou_il_existe() -> None:
    """`get_full_context` accepte `names=[...]`. Exiger `name` le casserait."""
    assert _require_name({"names": ["a", "b"]}, "get_full_context", batch=True) is None
    # ... mais pas la ou le mode batch n'existe pas.
    assert _require_name({"names": ["a"]}, "get_function_source") is not None


@pytest.mark.parametrize("vide", [[], None, "pas une liste"])
def test_un_batch_vide_ne_compte_pas(vide) -> None:
    assert _require_name({"names": vide}, "get_full_context", batch=True) is not None


def test_le_message_oriente_vers_la_sortie() -> None:
    """Un refus qui ne dit pas quoi faire ensuite se paie en reessais."""
    msg = _require_name({}, "get_full_context", batch=True)
    assert "search_codebase" in msg
    assert "ts_search" in msg
    assert "names=" in msg


def test_ne_fuit_jamais_une_keyerror() -> None:
    """La forme exacte du bug d'origine : `Error: 'name'` et rien d'autre."""
    msg = _require_name({}, "get_class_source")
    assert msg.strip() != "Error: 'name'"
    assert len(msg.splitlines()) >= 3, "un message utile tient en plusieurs lignes"
