"""Appels reels a travers le protocole MCP, pas des appels de fonction.

Ce fichier existe a cause d'une erreur precise. La v4.15.0 a introduit des
alias d'arguments, passe 2253 tests, et **n'a rien change en production** : le
SDK MCP valide les arguments contre le schema annonce **avant** d'appeler le
handler, donc l'appel etait refuse avant d'atteindre la traduction. Les tests
appelaient `_normalize_arguments()` directement et voyaient donc une fonction
qui marche tres bien, dans un chemin que personne n'emprunte.

Une suite verte qui ne traverse pas le vrai chemin ne prouve rien. Ces tests
lancent le serveur, parlent le protocole, et n'ont aucune connaissance de
l'implementation.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys

import pytest


class ServeurMCP:
    """Client MCP minimal sur stdio, comme le ferait Claude Code ou Codex."""

    def __init__(self, racine: str):
        env = dict(os.environ, WORKSPACE_ROOTS=racine, TOKEN_SAVIOR_PROFILE="full",
                   TOKEN_SAVIOR_CLIENT="pytest-e2e")
        env.pop("TOKEN_SAVIOR_AUTODISCOVER", None)
        self.p = subprocess.Popen(
            [sys.executable, "-m", "token_savior.server"],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            text=True, env=env, bufsize=1,
        )
        self.i = 0
        self._rpc("initialize", {"protocolVersion": "2024-11-05", "capabilities": {},
                                 "clientInfo": {"name": "e2e", "version": "1"}})
        self._send({"jsonrpc": "2.0", "method": "notifications/initialized"})

    def _send(self, msg):
        self.p.stdin.write(json.dumps(msg) + "\n")
        self.p.stdin.flush()

    def _rpc(self, methode, params, limite=1500):
        self.i += 1
        self._send({"jsonrpc": "2.0", "id": self.i, "method": methode, "params": params})
        for _ in range(limite):
            ligne = self.p.stdout.readline()
            if not ligne:
                return None
            try:
                d = json.loads(ligne)
            except ValueError:
                continue
            if d.get("id") == self.i:
                return d
        return None

    def appel(self, nom, args) -> str:
        r = self._rpc("tools/call", {"name": nom, "arguments": args})
        if r is None:
            return "PAS DE REPONSE"
        if "error" in r:
            return f"REJET PROTOCOLE: {r['error']}"
        res = r.get("result") or {}
        return " ".join(c.get("text", "") for c in res.get("content", [])
                        if isinstance(c, dict))

    def outils(self) -> dict:
        r = self._rpc("tools/list", {})
        return {t["name"]: t for t in ((r or {}).get("result") or {}).get("tools", [])}

    def stop(self):
        try:
            self.p.terminate(); self.p.wait(timeout=15)
        except Exception:
            self.p.kill()


@pytest.fixture(scope="module")
def serveur(tmp_path_factory):
    racine = tmp_path_factory.mktemp("projet-e2e")
    (racine / "app").mkdir()
    (racine / "app" / "service.py").write_text(
        'def calculer_total(lignes):\n'
        '    """Somme les montants."""\n'
        '    return sum(l["montant"] for l in lignes)\n\n\n'
        'class Facture:\n'
        '    def total(self):\n'
        '        return calculer_total([])\n',
        encoding="utf-8")
    s = ServeurMCP(str(racine))
    yield s
    s.stop()


def _echoue(reponse: str) -> bool:
    r = reponse.strip().lower()
    return (r.startswith(("error", "rejet"))
            or "validation error" in r or r == "pas de reponse")


# --- Les alias doivent passer LA VALIDATION, pas seulement la traduction --- #

@pytest.mark.parametrize("outil,args", [
    ("search_codebase", {"query": "calculer_total"}),
    ("search_codebase", {"pattern": "calculer_total"}),
    ("get_function_source", {"symbol_name": "calculer_total"}),
    ("get_function_source", {"name": "calculer_total"}),
    ("get_full_context", {"symbol": "calculer_total"}),
    ("get_class_source", {"class_name": "Facture"}),
    ("find_symbol", {"symbol": "calculer_total"}),
])
def test_un_alias_traverse_le_protocole(serveur, outil, args) -> None:
    """Le test qui manquait. Il echoue sur la v4.15.0, ou les alias etaient
    traduits apres une validation qui les rejetait deja."""
    reponse = serveur.appel(outil, args)
    assert not _echoue(reponse), f"{outil}({args}) -> {reponse[:200]}"


def test_le_schema_annonce_les_alias(serveur) -> None:
    """Un alias accepte mais non declare reste invisible pour l'appelant."""
    schema = serveur.outils()["search_codebase"]["inputSchema"]
    assert "query" in schema.get("properties", {})
    # et le `required` doit accepter l'un OU l'autre
    if "required" in schema:
        assert "pattern" not in schema["required"] or "anyOf" in schema
    else:
        assert "anyOf" in schema


def test_le_canonique_gagne_si_les_deux_sont_fournis(serveur) -> None:
    reponse = serveur.appel("search_codebase",
                            {"pattern": "calculer_total", "query": "introuvable_xyz"})
    assert "calculer_total" in reponse


# --- Non-regression sur le chemin normal ---------------------------------- #

@pytest.mark.parametrize("outil,args,attendu", [
    ("find_symbol", {"name": "calculer_total"}, "service.py"),
    # La vue par defaut est compacte, et un second appel sur le meme symbole
    # rend « body unchanged since last view » : c'est l'economie recherchee,
    # pas une panne. On verifie donc la signature, toujours presente.
    ("get_function_source", {"name": "calculer_total"}, "calculer_total(lignes)"),
    ("search_codebase", {"pattern": "Facture"}, "service.py"),
    ("list_projects", {}, "projet-e2e"),
])
def test_les_appels_canoniques_repondent_toujours(serveur, outil, args, attendu) -> None:
    reponse = serveur.appel(outil, args)
    assert not _echoue(reponse), reponse[:200]
    assert attendu in reponse, f"{outil} -> {reponse[:200]}"


def test_un_argument_obligatoire_absent_donne_un_message_utile(serveur) -> None:
    """Ni une KeyError brute, ni un rejet muet."""
    reponse = serveur.appel("get_function_source", {})
    assert reponse.strip() != "Error: 'name'"
    assert "name" in reponse
