"""Le harnais lui-meme doit tenir avant qu'on empile 250 tests dessus."""

from __future__ import annotations


def test_le_projet_cobaye_est_indexe(appeler) -> None:
    sortie = appeler("get_project_summary")
    assert "boutique" in sortie.lower(), sortie[:400]


def test_le_dispatch_rend_du_texte(appeler) -> None:
    sortie = appeler("find_symbol", name="calculer_total")
    assert "panier.py" in sortie, sortie[:400]


def test_l_historique_git_existe(appeler) -> None:
    sortie = appeler("get_git_status")
    assert "main" in sortie, sortie[:400]
