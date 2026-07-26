"""Synchronisation des roots MCP : le client dit ce que l'utilisateur a ouvert.

C'est la reponse du protocole lui-meme au probleme du rattachement automatique.
L'auto-decouverte devine depuis le systeme de fichiers ; `roots` est **declare
par le client**, mis a jour quand l'utilisateur ouvre ou ferme un espace de
travail, et ne demande aucune action a l'agent.

Deux exigences opposees, testees toutes les deux :

- ne rien casser chez les clients qui n'implementent pas la capacite (elle est
  optionnelle dans la specification) ;
- ne jamais laisser une erreur reseau ou une reponse malformee tuer le serveur.
"""
from __future__ import annotations

import asyncio
import os
from types import SimpleNamespace

import pytest

import token_savior.server_runtime as rt


class FakeRoot:
    def __init__(self, uri: str) -> None:
        self.uri = uri


class FakeSession:
    """Session MCP minimale : capacite declaree + reponse a roots/list."""

    def __init__(self, *, supports: bool = True, roots=None, raises: bool = False):
        self._supports = supports
        self._roots = roots or []
        self._raises = raises
        self.asked = False

    def check_client_capability(self, _cap) -> bool:
        return self._supports

    async def list_roots(self):
        self.asked = True
        if self._raises:
            raise RuntimeError("transport ferme")
        return SimpleNamespace(roots=self._roots)


@pytest.fixture(autouse=True)
def _etat_propre(monkeypatch):
    """Chaque test repart d'un registre vide et d'un drapeau non consomme."""
    monkeypatch.setattr(rt, "_client_roots_synced", False)
    recus: list[list[str]] = []
    monkeypatch.setattr(rt, "_register_roots", lambda r: recus.append(list(r)))
    monkeypatch.setattr(rt.s._slot_mgr, "projects", {}, raising=False)
    return recus


def projet(tmp_path, nom: str):
    d = tmp_path / nom
    d.mkdir(parents=True)
    (d / ".git").mkdir()
    return d


def lance(session):
    return asyncio.run(rt.sync_client_roots(session))


# --- Le cas nominal ------------------------------------------------------- #

def test_enregistre_les_roots_annonces_par_le_client(tmp_path, _etat_propre) -> None:
    a, b = projet(tmp_path, "alpha"), projet(tmp_path, "beta")
    s = FakeSession(roots=[FakeRoot(f"file://{a}"), FakeRoot(f"file://{b}")])
    fresh = lance(s)
    assert s.asked
    assert {os.path.basename(p) for p in fresh} == {"alpha", "beta"}
    assert _etat_propre, "les roots doivent etre enregistres"


def test_remonte_au_projet_quand_le_client_donne_un_sous_dossier(tmp_path, _etat_propre) -> None:
    """Un client peut pointer `projet/src`. On indexe le projet, pas le sous-dossier."""
    p = projet(tmp_path, "alpha")
    (p / "src").mkdir()
    fresh = lance(FakeSession(roots=[FakeRoot(f"file://{p / 'src'}")]))
    assert fresh == [str(p)]


def test_decode_les_uri_echappees(tmp_path, _etat_propre) -> None:
    p = projet(tmp_path, "mon projet")
    fresh = lance(FakeSession(roots=[FakeRoot(f"file://{str(p).replace(' ', '%20')}")]))
    assert fresh == [str(p)]


def test_ne_demande_qu_une_fois_par_session(tmp_path, _etat_propre) -> None:
    """Interroger le client a chaque appel d'outil serait un aller-retour de trop."""
    p = projet(tmp_path, "alpha")
    s = FakeSession(roots=[FakeRoot(f"file://{p}")])
    lance(s)
    s2 = FakeSession(roots=[FakeRoot(f"file://{p}")])
    assert lance(s2) == []
    assert not s2.asked


# --- Tout ce qui doit rester silencieux ----------------------------------- #

def test_client_sans_la_capacite(tmp_path, _etat_propre) -> None:
    """`roots` est optionnel dans la specification. Ne pas l'avoir n'est pas
    une erreur, et ne doit rien casser."""
    s = FakeSession(supports=False, roots=[FakeRoot(f"file://{projet(tmp_path, 'a')}")])
    assert lance(s) == []
    assert not s.asked


def test_erreur_de_transport(tmp_path, _etat_propre) -> None:
    """Un serveur qui meurt parce que le client a raccroche est pire qu'un
    serveur qui devine."""
    assert lance(FakeSession(raises=True)) == []


def test_session_absente(_etat_propre) -> None:
    assert lance(None) == []


@pytest.mark.parametrize("uri", ["", "http://exemple.test/x", "ftp://x/y",
                                 "file:///chemin/qui/n/existe/pas"])
def test_uri_inutilisables_ignorees(uri: str, _etat_propre) -> None:
    assert lance(FakeSession(roots=[FakeRoot(uri)])) == []


def test_reponse_sans_attribut_roots(_etat_propre) -> None:
    class Muette(FakeSession):
        async def list_roots(self):
            self.asked = True
            return SimpleNamespace()

    assert lance(Muette()) == []


def test_ignore_ce_qui_est_deja_connu(tmp_path, monkeypatch, _etat_propre) -> None:
    p = projet(tmp_path, "deja")
    monkeypatch.setattr(rt.s._slot_mgr, "projects", {str(p): object()}, raising=False)
    assert lance(FakeSession(roots=[FakeRoot(f"file://{p}")])) == []
