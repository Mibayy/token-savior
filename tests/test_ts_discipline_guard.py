"""Contrat du garde-fou de discipline Token Savior.

Le test central est `test_le_contexte_debloque_le_symbole_et_lui_seul` : le
garde-fou ne se contente pas d'exiger un appel a `get_edit_context`, il exige
qu'il porte **sur le symbole edite**. Sans ca, un appel unique en debut de
session ouvrirait la porte a toutes les editions suivantes, et le garde-fou
serait un ceremonial plutot qu'une verification.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

HOOK = Path(__file__).resolve().parents[1] / "hooks" / "ts_discipline_guard.py"


def lancer(payload: dict, etat: Path, env_extra: dict | None = None) -> dict | None:
    env = dict(os.environ)
    env.pop("TS_GUARD_OFF", None)
    # Le garde-fou est opt-in : sans ce drapeau il laisse tout passer, ce qui
    # ferait passer la suite entiere pour de mauvaises raisons.
    env["TS_DISCIPLINE_GUARD"] = "1"
    env["XDG_STATE_HOME"] = str(etat)
    if env_extra:
        env.update(env_extra)
    out = subprocess.run(
        [sys.executable, str(HOOK)],
        input=json.dumps(payload), capture_output=True, text=True, env=env,
        timeout=15, check=False,
    ).stdout.strip()
    return json.loads(out) if out else None


def raison(verdict: dict | None) -> str:
    assert verdict is not None
    return verdict["hookSpecificOutput"]["permissionDecisionReason"]


@pytest.fixture
def projet(tmp_path: Path) -> Path:
    (tmp_path / ".token-savior-cache.json").write_text("{}", encoding="utf-8")
    return tmp_path


def edition(symbole: str) -> dict:
    return {
        "session_id": "s1",
        "tool_name": "mcp__token-savior__replace_symbol_source",
        "tool_input": {"symbol_name": symbole, "new_source": "x"},
    }


def contexte(nom: str | None = None, noms: list[str] | None = None) -> dict:
    entree: dict = {}
    if nom:
        entree["name"] = nom
    if noms:
        entree["names"] = noms
    return {
        "session_id": "s1",
        "tool_name": "mcp__token-savior__get_edit_context",
        "tool_input": entree,
    }


# --- La regle centrale ---------------------------------------------------- #

def test_le_contexte_debloque_le_symbole_et_lui_seul(tmp_path: Path) -> None:
    etat = tmp_path / "etat"

    assert lancer(edition("cible"), etat) is not None, "sans contexte : refus"
    assert lancer(contexte("cible"), etat) is None, "demander le contexte passe"
    assert lancer(edition("cible"), etat) is None, "apres contexte : autorise"
    assert lancer(edition("voisin"), etat) is not None, (
        "un contexte sur un symbole ne doit pas ouvrir les editions des autres"
    )


def test_le_mode_batch_de_get_full_context_compte(tmp_path: Path) -> None:
    """`get_full_context(names=[...])` couvre chacun des symboles demandes."""
    etat = tmp_path / "etat"
    payload = {
        "session_id": "s1",
        "tool_name": "mcp__token-savior__get_full_context",
        "tool_input": {"names": ["a", "b"]},
    }
    assert lancer(payload, etat) is None
    assert lancer(edition("a"), etat) is None
    assert lancer(edition("b"), etat) is None
    assert lancer(edition("c"), etat) is not None


def test_les_sessions_sont_cloisonnees(tmp_path: Path) -> None:
    etat = tmp_path / "etat"
    assert lancer(contexte("cible"), etat) is None
    autre = dict(edition("cible"), session_id="s2")
    assert lancer(autre, etat) is not None, (
        "le contexte d'une session ne doit pas valider l'edition d'une autre"
    )


@pytest.mark.parametrize("outil", [
    "mcp__token-savior__insert_near_symbol",
    "mcp__token-savior__add_field_to_model",
    "mcp__token-savior__move_symbol",
    "mcp__token-savior-recall__replace_symbol_source",
])
def test_toutes_les_primitives_d_edition_sont_couvertes(tmp_path: Path, outil: str) -> None:
    """Le nom du serveur MCP varie (`token-savior` ou `token-savior-recall`)."""
    payload = {
        "session_id": "s1", "tool_name": outil,
        "tool_input": {"name": "cible", "symbol_name": "cible"},
    }
    assert lancer(payload, tmp_path / "etat") is not None


# --- Lecture ------------------------------------------------------------- #

@pytest.mark.parametrize("ext", [".py", ".ts", ".tsx", ".js", ".jsx"])
def test_refuse_read_natif_sur_code_indexe(projet: Path, tmp_path: Path, ext: str) -> None:
    f = projet / f"module{ext}"
    f.write_text("x = 1\n", encoding="utf-8")
    payload = {"session_id": "s1", "tool_name": "Read", "tool_input": {"file_path": str(f)}}
    assert "get_function_source" in raison(lancer(payload, tmp_path / "etat"))


@pytest.mark.parametrize("nom", ["notes.md", "config.json", "data.sql", ".env"])
def test_laisse_lire_ce_qui_n_est_pas_du_code(projet: Path, tmp_path: Path, nom: str) -> None:
    f = projet / nom
    f.write_text("x\n", encoding="utf-8")
    payload = {"session_id": "s1", "tool_name": "Read", "tool_input": {"file_path": str(f)}}
    assert lancer(payload, tmp_path / "etat") is None


def test_laisse_lire_du_code_hors_projet_indexe(tmp_path: Path) -> None:
    f = tmp_path / "isole.py"
    f.write_text("x = 1\n", encoding="utf-8")
    payload = {"session_id": "s1", "tool_name": "Read", "tool_input": {"file_path": str(f)}}
    assert lancer(payload, tmp_path / "etat") is None


def test_ignore_les_dependances_vendorisees(projet: Path, tmp_path: Path) -> None:
    """node_modules n'est pas du code du projet : le lire nativement est normal."""
    f = projet / "node_modules" / "paquet" / "index.js"
    f.parent.mkdir(parents=True)
    f.write_text("x\n", encoding="utf-8")
    payload = {"session_id": "s1", "tool_name": "Read", "tool_input": {"file_path": str(f)}}
    assert lancer(payload, tmp_path / "etat") is None


# --- Shell ---------------------------------------------------------------- #

@pytest.mark.parametrize("cmd_tpl", ["cat {}", "grep -n motif {}", "sed -n '1,5p' {}",
                                     "head -20 {}", "awk '{{print}}' {}"])
def test_refuse_la_lecture_de_code_au_shell(projet: Path, tmp_path: Path, cmd_tpl: str) -> None:
    f = projet / "module.py"
    f.write_text("x = 1\n", encoding="utf-8")
    payload = {"session_id": "s1", "tool_name": "Bash",
               "tool_input": {"command": cmd_tpl.format(f)}}
    assert "search_codebase" in raison(lancer(payload, tmp_path / "etat"))


@pytest.mark.parametrize("cmd", [
    "python3 -m pytest tests/ -q",
    "git status --porcelain",
    "npm install",
    "systemctl restart intel-api",
    "curl -s https://example.com",
])
def test_laisse_passer_le_vrai_usage_de_bash(tmp_path: Path, cmd: str) -> None:
    payload = {"session_id": "s1", "tool_name": "Bash", "tool_input": {"command": cmd}}
    assert lancer(payload, tmp_path / "etat") is None


def test_laisse_grep_sur_un_fichier_non_code(projet: Path, tmp_path: Path) -> None:
    f = projet / "journal.log"
    f.write_text("erreur\n", encoding="utf-8")
    payload = {"session_id": "s1", "tool_name": "Bash",
               "tool_input": {"command": f"grep erreur {f}"}}
    assert lancer(payload, tmp_path / "etat") is None


def test_laisse_grep_sur_du_code_hors_projet_indexe(tmp_path: Path) -> None:
    """Trouve par mutation : `endswith(CODE) and est_code_indexe(...)`.

    Le mutant qui remplace ce `and` par un `or` bloquait tout fichier `.py`,
    y compris hors projet indexe, ou Token Savior n'a rien a proposer. La
    suite ne le voyait pas : son seul cas passant utilisait un `.log`, donc
    la premiere condition suffisait a expliquer le passage.
    """
    f = tmp_path / "hors_index.py"
    f.write_text("x = 1\n", encoding="utf-8")
    payload = {"session_id": "s1", "tool_name": "Bash",
               "tool_input": {"command": f"grep motif {f}"}}
    assert lancer(payload, tmp_path / "etat") is None


@pytest.mark.parametrize("entree,attendu_refuse", [
    ({"symbol_name": "cible", "new_source": "x"}, True),
    ({"name": "cible"}, True),
])
def test_le_symbole_est_lu_sous_ses_deux_cles(tmp_path: Path, entree: dict,
                                              attendu_refuse: bool) -> None:
    """Trouve par mutation : `entree.get("symbol_name") or entree.get("name")`.

    Les primitives d'edition ne nomment pas le symbole de la meme facon :
    `replace_symbol_source` utilise `symbol_name`, d'autres `name`. Le mutant
    remplacant ce `or` par un `and` cassait le cas ou une seule des deux cles
    est presente, et la suite ne le voyait pas parce que son unique cas
    fournissait les deux.
    """
    payload = {"session_id": "s1",
               "tool_name": "mcp__token-savior__replace_symbol_source",
               "tool_input": entree}
    verdict = lancer(payload, tmp_path / "etat")
    assert (verdict is not None) is attendu_refuse


# --- Robustesse ----------------------------------------------------------- #

def test_debrayage_explicite(projet: Path, tmp_path: Path) -> None:
    f = projet / "module.py"
    f.write_text("x = 1\n", encoding="utf-8")
    payload = {"session_id": "s1", "tool_name": "Read", "tool_input": {"file_path": str(f)}}
    assert lancer(payload, tmp_path / "etat", {"TS_GUARD_OFF": "1"}) is None


@pytest.mark.parametrize("brut", ["", "   ", "pas du json", '{"tool_name": 42}',
                                  '{"tool_name":"Read","tool_input":"pas un dict"}'])
def test_fail_open_sur_entree_malformee(brut: str, tmp_path: Path) -> None:
    """Un garde-fou ne doit jamais etre la raison d'un arret de session.

    Le drapeau est mis explicitement : sans lui le hook rend la main avant
    meme de lire stdin, et le test passerait pour la mauvaise raison.
    """
    env = dict(os.environ, XDG_STATE_HOME=str(tmp_path), TS_DISCIPLINE_GUARD="1")
    env.pop("TS_GUARD_OFF", None)
    r = subprocess.run([sys.executable, str(HOOK)], input=brut,
                       capture_output=True, text=True, env=env, timeout=15,
                       check=False)
    assert r.returncode == 0
    assert not r.stdout.strip()
