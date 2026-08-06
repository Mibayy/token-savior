"""Deux symboles homonymes dans deux fichiers ne sont pas le meme symbole.

Constate en production le 06/08/2026, sur un projet Next.js. La cle du cache
de session etait `kind:project_root:name`, **sans le chemin du fichier**. Or
dans un projet Next.js chaque fichier de route exporte une fonction `POST` :
toutes les routes partageaient donc une seule entree de cache.

Consequence vecue : `get_function_source` sur `app/api/contrats/envoyer/route.ts`
a renvoye un accuse `[MODIFIED]` accompagne d'un diff calcule contre le `POST`
d'un *autre* fichier, avec la mention "Changed body, prior views: 2". Le corps
demande n'est jamais arrive. Il a fallu `force_full=true` pour l'obtenir.

Deux degats distincts :
- le corps demande n'est pas servi, alors qu'il n'a jamais ete vu ;
- le diff affiche compare deux fonctions sans rapport, ce qui est pire qu'une
  absence de reponse : il se lit comme une modification reelle du fichier
  demande.

Sens de l'erreur a preferer : en cas de doute sur l'identite d'un symbole, on
sert la source. Un octet de trop coute un octet ; un corps retenu a tort coute
un aller-retour, et peut faire inventer le corps manquant.
"""

from __future__ import annotations

import pytest


class _SlotFactice:
    root = "/projet/factice"


@pytest.fixture
def code_nav(monkeypatch: pytest.MonkeyPatch):
    from token_savior.server_handlers import code_nav as module

    # Chaque fichier a son propre corps, donc sa propre empreinte : c'est
    # exactement la situation de deux routes Next.js exportant `POST`.
    empreintes = {
        "app/api/a/route.ts": ("function", "empreinte-A", "export async function POST(req)"),
        "app/api/b/route.ts": ("function", "empreinte-B", "export async function POST(req)"),
    }

    def _meta(slot, args):
        return empreintes.get(args.get("file_path", ""))

    monkeypatch.setattr(module, "_lookup_symbol_meta", _meta)
    module.state._session_symbol_cache.clear()
    yield module
    module.state._session_symbol_cache.clear()


CORPS_A = "export async function POST(req) {\n  return handleA(req);\n}\n" + "// A\n" * 200
CORPS_B = "export async function POST(req) {\n  return handleB(req);\n}\n" + "// B\n" * 200


def test_deux_fichiers_homonymes_ne_partagent_pas_une_entree(code_nav) -> None:
    slot = _SlotFactice()

    premier = code_nav._csc_maybe_serve(
        slot, "function", {"name": "POST", "file_path": "app/api/a/route.ts"},
        lambda: CORPS_A, effective_level=0,
    )
    assert premier == CORPS_A, "le premier appel doit servir la source"

    second = code_nav._csc_maybe_serve(
        slot, "function", {"name": "POST", "file_path": "app/api/b/route.ts"},
        lambda: CORPS_B, effective_level=0,
    )

    assert second == CORPS_B, (
        "un POST d'un autre fichier a ete servi comme un rappel du premier : "
        "la cle du cache ignore le chemin, donc toutes les routes d'un projet "
        "Next.js se telescopent"
    )
    assert "[MODIFIED]" not in second, (
        "un diff a ete calcule entre deux fonctions sans rapport ; il se lit "
        "comme une modification reelle du fichier demande"
    )


def test_le_meme_fichier_beneficie_toujours_du_cache(code_nav) -> None:
    """Le correctif ne doit pas desactiver le cache la ou il est legitime."""
    slot = _SlotFactice()
    args = {"name": "POST", "file_path": "app/api/a/route.ts"}

    premier = code_nav._csc_maybe_serve(slot, "function", args, lambda: CORPS_A, effective_level=0)
    second = code_nav._csc_maybe_serve(slot, "function", args, lambda: CORPS_A, effective_level=0)

    assert premier == CORPS_A
    assert second != CORPS_A, "le rappel du meme symbole doit rester abrege"
    assert "@sym:" in second


def test_l_accuse_dit_comment_recuperer_le_corps(code_nav) -> None:
    """Un agent parallele n'a pas le corps que l'accuse lui dit de reutiliser.

    Le cache est une globale du processus, partagee par une session et tous
    les sous-agents qu'elle lance. L'agent B peut donc recevoir « tu l'as deja
    vu » pour un corps que seul l'agent A a recu. L'accuse doit nommer cette
    issue, sinon l'agent qui ne l'a pas est invite a reutiliser ce qu'il n'a
    jamais eu -- et un modele a qui l'on affirme qu'il detient un corps est en
    position de l'inventer.
    """
    slot = _SlotFactice()
    args = {"name": "POST", "file_path": "app/api/a/route.ts"}
    code_nav._csc_maybe_serve(slot, "function", args, lambda: CORPS_A, effective_level=0)
    accuse = code_nav._csc_maybe_serve(slot, "function", args, lambda: CORPS_A, effective_level=0)

    assert "force_full" in accuse, "l'accuse doit nommer l'issue de secours"
    assert "sub-agent" in accuse, (
        "l'accuse doit dire qu'un agent qui n'a pas le corps peut le redemander"
    )
    assert "reconstruct" in accuse, (
        "l'accuse doit interdire explicitement de reconstruire le corps de memoire"
    )
