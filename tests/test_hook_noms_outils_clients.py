"""Le hook memoire doit reconnaitre l'outil shell de CHAQUE client, pas de Claude seul.

Defaut reel livre en v4.11.0. `hooks/memory-gemini.json` cable l'evenement
`BeforeTool` sur `memory-pretooluse.sh`, mais le script testait
`[[ "$TOOL" == "Bash" ]]`, une egalite stricte sur le nom d'outil de Claude
Code. Sous Gemini l'outil shell s'appelle `run_shell_command` : le rappel
memoire par commande ne se declenchait donc **jamais**. Mesure avant correctif :

    tool_name=Bash               -> 185 octets rendus
    tool_name=run_shell_command  ->   0 octet
    tool_name=shell              ->   0 octet

Pourquoi ce test n'observe pas la sortie du hook : une sortie vide est
ambigue, elle peut signifier « branche non prise » ou « branche prise, aucune
observation ne correspond ». Le test remplace donc l'interpreteur Python par
un mouchard qui imprime le MODE retenu. On observe la decision, pas son
resultat, et le test ne depend d'aucune base memoire.
"""
from __future__ import annotations

import os
import pathlib
import subprocess

import pytest

HOOK = pathlib.Path(__file__).resolve().parents[1] / "hooks" / "memory-pretooluse.sh"

# Noms d'outil shell des clients qui embarquent un bundle de hooks livre.
OUTILS_SHELL = ["Bash", "run_shell_command", "shell", "local_shell",
                "execute_command", "terminal", "RunCommand"]
OUTILS_EDIT = ["Edit", "Write", "MultiEdit", "replace", "write_file",
               "edit_file", "apply_patch"]


@pytest.fixture(scope="module")
def mouchard(tmp_path_factory):
    """Faux interpreteur : imprime le MODE que le script a retenu.

    Le script appelle `$TS_PY -c "<programme>"`. Le programme contient la
    ligne `mode = '<MODE>'`, substituee par bash avant l'appel. Le mouchard
    la relit dans son argument et l'imprime.
    """
    p = tmp_path_factory.mktemp("mouchard") / "faux-python"
    p.write_text(
        '#!/bin/bash\n'
        '# $1 vaut -c, $2 le programme\n'
        'cat >/dev/null\n'
        'echo "$2" | grep -oE "^mode = .[a-z]+" | head -1\n',
        encoding="utf-8")
    p.chmod(0o755)
    return str(p)


def _mode_retenu(mouchard: str, tool_name: str, entree: dict | None = None) -> str:
    charge = {
        "hook_event_name": "PreToolUse",
        "session_id": "test",
        "cwd": "/tmp",
        "tool_name": tool_name,
        "tool_input": entree if entree is not None else {
            "command": "systemctl restart nginx",
            "file_path": "/tmp/x/service.py",
        },
    }
    import json
    env = dict(os.environ)
    env["TOKEN_SAVIOR_PYTHON"] = mouchard
    env.pop("TS_MEMORY_DISABLE", None)
    # check=False explicite : le test inspecte lui-meme la sortie du hook, une
    # exception levee sur code non nul masquerait ce qu'on cherche a mesurer.
    res = subprocess.run(["bash", str(HOOK)], input=json.dumps(charge),
                         capture_output=True, text=True, env=env, timeout=60,
                         check=False)
    sortie = res.stdout.strip()
    if not sortie:
        return ""  # sortie 0 avant tout appel : aucun mode retenu
    return sortie.split("'")[-1] if "'" in sortie else sortie


@pytest.mark.parametrize("outil", OUTILS_SHELL)
def test_chaque_nom_d_outil_shell_declenche_le_rappel(mouchard, outil) -> None:
    """C'est le defaut corrige : seul `Bash` passait."""
    assert _mode_retenu(mouchard, outil) == "bash", (
        f"{outil} n'atteint pas la branche shell : le rappel memoire est muet "
        f"sur le client qui utilise ce nom")


@pytest.mark.parametrize("outil", OUTILS_EDIT)
def test_chaque_nom_d_outil_d_edition_declenche_la_memoire_negative(mouchard, outil) -> None:
    """Meme piege sur l'edition : `write_file` (Gemini) et `apply_patch` (Codex)
    doivent faire remonter les observations `ruled_out` avant la mutation."""
    assert _mode_retenu(mouchard, outil) == "edit", (
        f"{outil} n'atteint pas la branche edition")


def test_les_outils_token_savior_restent_reconnus(mouchard) -> None:
    """Le correctif ne doit pas casser le prefixe mcp__<serveur>__."""
    assert _mode_retenu(
        mouchard, "mcp__token-savior__get_function_source",
        {"name": "encaisser"}) == "code"


def test_un_outil_hors_perimetre_ne_declenche_rien(mouchard) -> None:
    """Elargir la reconnaissance ne doit pas la rendre universelle : un outil
    sans rapport doit toujours sortir sans appeler Python."""
    assert _mode_retenu(mouchard, "WebSearch", {"query": "x"}) == ""


def test_le_court_circuit_reste_respecte(mouchard) -> None:
    """TS_MEMORY_DISABLE=1 doit continuer de tout couper."""
    import json
    env = dict(os.environ)
    env["TOKEN_SAVIOR_PYTHON"] = mouchard
    env["TS_MEMORY_DISABLE"] = "1"
    # check=False explicite : c'est l'absence de sortie qu'on verifie, pas le
    # code de retour du hook.
    res = subprocess.run(
        ["bash", str(HOOK)],
        input=json.dumps({"tool_name": "Bash", "tool_input": {"command": "systemctl restart nginx"}}),
        capture_output=True, text=True, env=env, timeout=60,
        check=False)
    assert res.stdout.strip() == ""


def test_le_bundle_gemini_cable_bien_ce_script(mouchard) -> None:
    """Le lien qui a rendu le defaut visible : c'est parce que le bundle Gemini
    pointe sur ce script que son nom d'outil devait y etre reconnu."""
    import json
    bundle = HOOK.parent / "memory-gemini.json"
    if not bundle.exists():
        pytest.skip("bundle Gemini absent")
    contenu = json.loads(bundle.read_text(encoding="utf-8"))
    brut = json.dumps(contenu)
    assert "memory-pretooluse.sh" in brut, "le bundle ne pointe plus ce script"
    assert "run_shell_command" in brut, (
        "le bundle Gemini ne filtre plus l'outil shell de Gemini")
