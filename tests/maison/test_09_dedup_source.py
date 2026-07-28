"""get_full_context omet la source deja servie cette session.

Tue le gaspillage `read -> get_full_context` (mesure 295x/3j) : l'agent lit une
source via get_function_source, puis get_full_context sur le meme symbole la
re-renvoyait. Teste sur le vrai chemin de dispatch, avec le cache CSC reel.
"""
from __future__ import annotations


def test_get_full_context_dedupe_source_deja_servie(appeler):
    # 1) l'agent lit la source (niveau 0 -> peuple le cache CSC de session)
    appeler("get_function_source", name="calculer_frais", level=0)
    # 2) get_full_context sur le meme symbole -> source OMISE, pointeur a la place
    out = appeler("get_full_context", name="calculer_frais")
    assert "already served" in out
    assert "def calculer_frais" not in out          # la source complete n'y est plus
    # 3) LOSSLESS : mode='full' rend toujours la source complete
    plein = appeler("get_full_context", name="calculer_frais", mode="full")
    assert "def calculer_frais" in plein
    # 4) symbole JAMAIS lu -> pas de dedup (pas de faux positif)
    autre = appeler("get_full_context", name="calculer_total")
    assert "already served" not in autre


def test_dedup_ne_touche_pas_les_deps(appeler):
    """La dedup omet la source mais garde deps/dependents."""
    appeler("get_function_source", name="calculer_frais", level=0)
    out = appeler("get_full_context", name="calculer_frais", depth=1)
    assert "already served" in out
    # depth=1 -> le bundle contient toujours les cles deps/dependents
    assert "dependencies" in out or "dependents" in out
