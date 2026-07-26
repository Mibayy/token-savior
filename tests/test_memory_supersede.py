"""Peremption : la plus recente archive celle qu'elle rend fausse.

Avant ce comportement, deux observations sur le meme sujet (proximite entre
0.85 et 0.95) restaient TOUTES DEUX actives, l'une a jour et l'autre perimee,
avec un simple tag "near-duplicate" et rien qui dise laquelle gagne. C'est
l'ecart mesure par le benchmark STALE : les systemes memoire retrouvent
l'information fraiche dans 77% des cas mais echouent a la preferer 56% du temps.

L'arbitrage teste ici est deterministe : le LLM a produit le contenu, il ne juge
pas la fraicheur.
"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from token_savior import memory_db
from token_savior.memory import observations as obs_mod

PROJECT = "/tmp/test-supersede"


@pytest.fixture(autouse=True)
def _memory_tmpdb(tmp_path: Path):
    db_path = tmp_path / "memory.db"
    # notify_telegram est neutralise : un test ne doit pas envoyer de message.
    with patch.object(memory_db, "MEMORY_DB_PATH", db_path), \
         patch.object(obs_mod, "notify_telegram", lambda *a, **k: None):
        yield db_path


def _save(title: str, content: str, type: str = "note") -> int | None:
    return obs_mod.observation_save(None, PROJECT, type, title, content)


def _etat(obs_id: int) -> tuple:
    """(archived, superseded_by) en tuple simple : la connexion rend des Row."""
    conn = memory_db.get_db()
    row = conn.execute(
        "SELECT archived, superseded_by FROM observations WHERE id=?", (obs_id,)
    ).fetchone()
    return tuple(row) if row is not None else ()


def _proche(old_id: int, score: float):
    """Simule le verdict du detecteur de quasi-doublon."""
    return lambda *a, **k: {"id": old_id, "score": score}


def test_la_plus_recente_perime_la_precedente(monkeypatch):
    vieux = _save("port du service gw2", "le service ecoute sur 8940")
    assert vieux is not None
    monkeypatch.setattr(obs_mod, "semantic_dedup_check", _proche(vieux, 0.93))
    neuf = _save("port du service gw2", "le service ecoute sur 8941")
    assert neuf is not None

    archived, superseded_by = _etat(vieux)
    assert archived == 1
    assert superseded_by == neuf
    assert _etat(neuf) == (0, None)


def test_rien_n_est_supprime(monkeypatch):
    """Une observation perimee reste interrogeable : sans elle, impossible de
    repondre « qu'est-ce qui etait vrai en avril », ni de defaire une erreur."""
    vieux = _save("port du service gw2", "le service ecoute sur 8940")
    monkeypatch.setattr(obs_mod, "semantic_dedup_check", _proche(vieux, 0.93))
    _save("port du service gw2", "le service ecoute sur 8941")

    conn = memory_db.get_db()
    contenu = conn.execute(
        "SELECT content FROM observations WHERE id=?", (vieux,)
    ).fetchone()[0]
    assert "8940" in contenu


def test_un_guardrail_n_est_jamais_perime_en_silence(monkeypatch):
    """Une regle figee ne devient pas fausse parce qu'on a reparle du sujet.
    L'archiver en silence serait la pire panne possible de ce mecanisme."""
    regle = _save("jamais de DELETE en masse en prod", "cibler des lignes de test",
                  type="guardrail")
    monkeypatch.setattr(obs_mod, "semantic_dedup_check", _proche(regle, 0.94))
    _save("jamais de DELETE en masse en prod", "utiliser une base jetable",
          type="guardrail")
    assert _etat(regle) == (0, None)


def test_proximite_faible_ne_perime_pas(monkeypatch):
    """Sous le seuil, on ne tranche pas : deux observations proches ne sont pas
    forcement la meme, et archiver a tort coute plus cher que garder du bruit."""
    vieux = _save("port du service gw2", "le service ecoute sur 8940")
    monkeypatch.setattr(obs_mod, "semantic_dedup_check", _proche(vieux, 0.87))
    _save("configuration nginx de gw2", "proxy vers le backend")
    assert _etat(vieux) == (0, None)


def test_une_observation_ne_se_perime_pas_elle_meme(monkeypatch):
    """Garde-fou de boucle : si le detecteur renvoie l'observation en cours,
    l'archiver la rendrait invisible juste apres l'avoir ecrite."""
    seule = _save("un sujet", "un contenu")
    conn = memory_db.get_db()
    now = "2026-07-26T12:00:00"
    assert obs_mod._supersede_stale(conn, {"id": seule, "score": 0.99}, seule, now) is None
    assert _etat(seule) == (0, None)
