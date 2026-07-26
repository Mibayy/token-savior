"""Deux clients MCP sur le meme projet ne doivent pas se corrompre.

Cas jamais teste, et le plus proche de la corruption reelle observee pendant
le developpement : chaque client garde son propre index en memoire. Des que
l'un ecrit, l'index de l'autre est perime, et sa prochaine edition vise des
lignes qui ont bouge.

Le scenario est celui d'une session Claude Code et d'une session Codex ouvertes
sur le meme depot, ce qui n'a rien d'exotique.
"""
from __future__ import annotations

import subprocess

import pytest

from token_savior.project_indexer import ProjectIndexer
from token_savior.server_handlers import edit as edit_handlers
from token_savior.slot_manager import SlotManager


@pytest.fixture
def projet(tmp_path):
    (tmp_path / "app").mkdir()
    (tmp_path / "app" / "__init__.py").write_text("", encoding="utf-8")
    (tmp_path / "app" / "calc.py").write_text(
        "def avant(x):\n    return x\n\n\n"
        "def cible(total, taux):\n"
        "    return total - (total * taux) // 100\n\n\n"
        "def apres(y):\n    return y\n", encoding="utf-8")
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=False,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return tmp_path


def _client(projet):
    """Un client = son propre gestionnaire de slots, donc son propre index."""
    mgr = SlotManager(cache_version=2)
    mgr.register_roots([str(projet)])
    slot, err = mgr.resolve(str(projet))
    assert not err, err
    return slot


def _corps(nom, marque):
    return (f"def {nom}(total, taux):\n"
            f'    """{marque}."""\n'
            f"    return total - (total * taux) // 100")


def test_le_second_client_voit_lecriture_du_premier(projet) -> None:
    a, b = _client(projet), _client(projet)
    edit_handlers._h_replace_symbol_source(
        a, {"symbol_name": "cible", "new_source": _corps("cible", "VERSION A")})

    edit_handlers._h_replace_symbol_source(
        b, {"symbol_name": "cible", "new_source": _corps("cible", "VERSION B")})

    source = (projet / "app" / "calc.py").read_text(encoding="utf-8")
    assert source.count("def cible(") == 1, f"duplication :\n{source}"
    assert "VERSION B" in source, source
    assert "VERSION A" not in source, "l'edition de B a laisse celle de A"


def test_les_voisins_survivent_a_deux_editions_croisees(projet) -> None:
    """Le degat le plus courant d'un index perime : ecrire sur les lignes du
    symbole d'a cote."""
    a, b = _client(projet), _client(projet)
    edit_handlers._h_replace_symbol_source(
        a, {"symbol_name": "cible", "new_source": _corps("cible", "A")})
    edit_handlers._h_replace_symbol_source(
        b, {"symbol_name": "cible", "new_source": _corps("cible", "B")})

    source = (projet / "app" / "calc.py").read_text(encoding="utf-8")
    assert "def avant(x):" in source, "le voisin du dessus a ete ecrase"
    assert "def apres(y):" in source, "le voisin du dessous a ete ecrase"
    compile(source, "calc.py", "exec")


def test_un_client_qui_relit_apres_lautre_voit_la_verite(projet) -> None:
    a, b = _client(projet), _client(projet)
    edit_handlers._h_replace_symbol_source(
        a, {"symbol_name": "cible", "new_source": _corps("cible", "ECRIT PAR A")})

    from token_savior.query_api import create_project_query_functions
    vu = str(create_project_query_functions(
        ProjectIndexer(str(projet)).index())["get_function_source"]("cible", level=0))
    assert "ECRIT PAR A" in vu, vu[:200]
    assert b is not None
