"""ts_execute ne rend jamais plus que ce que Claude Code accepte (07/10/2026).

Un script qui relisait quatre fichiers entiers rendait 57 545 caractères :
Claude Code refusait le résultat et le déposait dans un fichier, le script
avait tourné pour rien.
"""
from __future__ import annotations

import json

from token_savior.server import _TS_EXECUTE_BUDGET, _borner_sortie_script


def test_une_petite_sortie_reste_intacte() -> None:
    o = {"value": {"a": 1}, "logs": [], "error": None, "tool_calls": 2}
    assert json.loads(_borner_sortie_script(o)) == o


def test_une_grosse_valeur_est_coupee_et_le_dit() -> None:
    o = {"value": {"f": "x" * (_TS_EXECUTE_BUDGET * 2)}, "logs": ["ok"], "error": None, "tool_calls": 4}
    out = _borner_sortie_script(o)
    assert len(out) <= _TS_EXECUTE_BUDGET
    d = json.loads(out)
    assert d["tool_calls"] == 4 and d["logs"] == ["ok"]
    assert "_tronque" in d and str(_TS_EXECUTE_BUDGET) in d["_tronque"]
