"""Le garde-fou est opt-in, et la regle d'edition native.

Deux sujets que la suite principale ne couvre pas, pour deux raisons
differentes :

- l'opt-in est justement ce que cette suite neutralise (elle force le drapeau),
  donc il faut le tester ici, sans lui ;
- la regle d'edition native est arrivee par fusion depuis un second hook, elle
  n'avait pas de tests dans ce depot.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

HOOK = Path(__file__).resolve().parents[1] / "hooks" / "ts_discipline_guard.py"


def run_hook(payload: dict, env_extra: dict | None = None) -> dict | None:
    env = dict(os.environ)
    for cle in ("TS_GUARD_OFF", "TS_DISCIPLINE_GUARD"):
        env.pop(cle, None)
    if env_extra:
        env.update(env_extra)
    out = subprocess.run(
        [sys.executable, str(HOOK)],
        input=json.dumps(payload), capture_output=True, text=True, env=env,
        timeout=15, check=False,
    ).stdout.strip()
    return json.loads(out) if out else None


@pytest.fixture
def projet(tmp_path: Path) -> Path:
    (tmp_path / ".token-savior-cache.json").write_text("{}", encoding="utf-8")
    return tmp_path


def _edit_natif(chemin: Path) -> dict:
    return {
        "session_id": "s1",
        "tool_name": "Edit",
        "tool_input": {"file_path": str(chemin), "old_string": "a", "new_string": "b"},
    }


# --- L'opt-in ------------------------------------------------------------- #

def test_sans_le_drapeau_le_garde_fou_ne_refuse_rien(projet: Path) -> None:
    """Ce garde-fou *refuse* des appels. L'activer par defaut casserait toute
    installation existante a la mise a jour. Meme contrat que TS_BASH_COMPACT.
    """
    f = projet / "module.py"
    f.write_text("x = 1\n", encoding="utf-8")
    assert run_hook(_edit_natif(f)) is None


def test_avec_le_drapeau_il_refuse(projet: Path, tmp_path: Path) -> None:
    f = projet / "module.py"
    f.write_text("x = 1\n", encoding="utf-8")
    verdict = run_hook(_edit_natif(f), {"TS_DISCIPLINE_GUARD": "1",
                                        "XDG_STATE_HOME": str(tmp_path / "etat")})
    assert verdict is not None
    assert verdict["hookSpecificOutput"]["permissionDecision"] == "deny"


def test_le_debrayage_gagne_sur_l_activation(projet: Path, tmp_path: Path) -> None:
    """Les deux drapeaux ensemble : TS_GUARD_OFF doit l'emporter, sinon il n'y
    a plus de porte de sortie une fois le garde-fou active en config."""
    f = projet / "module.py"
    f.write_text("x = 1\n", encoding="utf-8")
    assert run_hook(_edit_natif(f), {"TS_DISCIPLINE_GUARD": "1",
                                     "TS_GUARD_OFF": "1",
                                     "XDG_STATE_HOME": str(tmp_path / "etat")}) is None


# --- La regle d'edition native ------------------------------------------- #

def _actif(tmp_path: Path) -> dict:
    return {"TS_DISCIPLINE_GUARD": "1", "XDG_STATE_HOME": str(tmp_path / "etat")}


@pytest.mark.parametrize("ext", [".py", ".ts", ".tsx", ".js", ".jsx"])
def test_refuse_l_edition_native_de_code_indexe(projet: Path, tmp_path: Path,
                                                ext: str) -> None:
    f = projet / f"module{ext}"
    f.write_text("x = 1\n", encoding="utf-8")
    verdict = run_hook(_edit_natif(f), _actif(tmp_path))
    assert verdict is not None
    motif = verdict["hookSpecificOutput"]["permissionDecisionReason"]
    # Un garde-fou qui bloque sans nommer le remplacant se contourne.
    assert "get_edit_context" in motif
    assert "replace_symbol_source" in motif


def test_la_creation_d_un_fichier_reste_permise(projet: Path, tmp_path: Path) -> None:
    """`replace_symbol_source` ne peut rien remplacer dans un fichier absent."""
    payload = {"session_id": "s1", "tool_name": "Write",
               "tool_input": {"file_path": str(projet / "neuf.py"), "content": "x = 1\n"}}
    assert run_hook(payload, _actif(tmp_path)) is None


@pytest.mark.parametrize("nom", ["notes.md", "config.json", "compose.yml", "schema.sql"])
def test_les_fichiers_non_code_restent_en_edition_native(projet: Path, tmp_path: Path,
                                                         nom: str) -> None:
    f = projet / nom
    f.write_text("contenu\n", encoding="utf-8")
    assert run_hook(_edit_natif(f), _actif(tmp_path)) is None


def test_le_code_hors_projet_indexe_reste_editable(tmp_path: Path) -> None:
    f = tmp_path / "isole.py"
    f.write_text("x = 1\n", encoding="utf-8")
    assert run_hook(_edit_natif(f), _actif(tmp_path)) is None
