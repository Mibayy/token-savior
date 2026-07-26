"""Un garde-fou impossible a satisfaire legitimement se fait contourner.

Deux defauts trouves en essayant de m'y conformer, pas en le relisant.

**La sauvegarde n'etait pas reconnue.** Le motif exigeait que la SOURCE du
`cp` contienne litteralement `.db`. La forme la plus naturelle,
`DB=...; cp "$DB" "$DB.bak-$(date ...)"`, ne matchait donc pas : la sauvegarde
etait faite, le garde-fou la niait, et il ne restait qu'a le contourner.

**La porte de sortie ne s'ouvrait pas.** Le message propose
`TS_RULES_DISABLE=1`, mais le hook lisait la variable dans SON processus. Un
prefixe de commande la pose pour l'enfant, jamais pour lui. Le debrayage
documente etait inutilisable exactement quand on en avait besoin, ce qui est la
meilleure facon de faire retirer un garde-fou de la configuration pour de bon.
"""
from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path

import pytest

from token_savior.memory import rules

HOOK = Path(__file__).resolve().parents[1] / "src/token_savior/memory/rules_hook.py"


def _hook(commande: str) -> bool:
    """True si le hook refuse la commande."""
    p = subprocess.run(
        [sys.executable, str(HOOK)],
        input=json.dumps({"tool_name": "Bash",
                          "tool_input": {"command": commande},
                          "session_id": "test"}),
        capture_output=True, text=True, timeout=20, check=False)
    return bool(p.stdout.strip())


# --- La porte de sortie doit s'ouvrir -------------------------------------- #

def test_une_operation_destructrice_est_refusee() -> None:
    assert _hook("sqlite3 memoire.db 'DELETE FROM observations'")


@pytest.mark.parametrize("prefixe", ["TS_RULES_DISABLE=1", 'TS_RULES_DISABLE="1"',
                                     "TS_RULES_DISABLE=1 "])
def test_le_debrayage_en_prefixe_fonctionne(prefixe: str) -> None:
    """Ce que le message du garde-fou promet doit marcher."""
    assert not _hook(f"{prefixe} sqlite3 memoire.db 'DELETE FROM observations'")


def test_le_debrayage_ne_sactive_pas_par_hasard() -> None:
    """Mentionner la variable n'est pas la poser."""
    assert _hook("echo 'utiliser TS_RULES_DISABLE' && sqlite3 x.db 'DELETE FROM t'")


# --- La sauvegarde doit etre reconnue -------------------------------------- #

@pytest.mark.parametrize("commande", [
    'cp "$DB" "$DB.bak-20260726-212117"',
    "cp memory.db memory.db.bak",
    "cp /root/.local/share/token-savior/memory.db /tmp/memory.db.backup",
    "rsync base.sqlite base.sqlite.backup",
    "sqlite3 memoire.db '.backup /tmp/copie.db'",
    "pg_dump maprod > dump.sql",
])
def test_les_formes_de_sauvegarde_sont_reconnues(commande: str) -> None:
    motif = rules.PRECONDITION_COMMANDS["db-backup"]
    assert re.search(motif, commande), commande


@pytest.mark.parametrize("commande", [
    "cp a.txt b.txt",
    "ls memory.db",
    "cat backup.md",
])
def test_ce_qui_nest_pas_une_sauvegarde_ne_compte_pas(commande: str) -> None:
    """Trop accepter viderait la regle de son sens."""
    motif = rules.PRECONDITION_COMMANDS["db-backup"]
    assert not re.search(motif, commande), commande
