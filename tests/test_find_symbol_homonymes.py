"""Deux fonctions du meme nom : le silence est le pire des choix.

Constate le 06/08/2026 sur /root/estalle. `createSignatureRequest` est defini
DEUX fois — `lib/yousign.ts:56` (ancien fournisseur) et `lib/oodrive.ts:263`
(celui reellement utilise). `find_symbol` rendait `lib/yousign.ts`, sans un mot
sur l'existence de l'autre.

Un agent qui demande un symbole et en recoit un seul le croit unique. Ici il
serait parti editer le code d'un fournisseur abandonne, en toute confiance.

L'ambiguite est deja dite pour les classes homonymes entre langages
(`test_polyglot_class_ambiguity`). Elle ne l'etait pas pour les fonctions.

Sens de l'erreur a preferer : nommer les candidats coute quelques dizaines
d'octets. Choisir en silence coute une edition dans le mauvais fichier.
"""

from __future__ import annotations

import pytest


@pytest.fixture
def projet(tmp_path):
    """Deux definitions homonymes, dans deux fichiers, avec des corps differents."""
    (tmp_path / "ancien.py").write_text(
        "def creer_demande_signature(doc):\n"
        '    """Ancien fournisseur, abandonne."""\n'
        "    return {'fournisseur': 'ancien'}\n",
        encoding="utf-8",
    )
    (tmp_path / "actuel.py").write_text(
        "def creer_demande_signature(doc):\n"
        '    """Fournisseur reellement utilise."""\n'
        "    return {'fournisseur': 'actuel'}\n",
        encoding="utf-8",
    )
    return tmp_path


@pytest.fixture
def qf(projet):
    from token_savior.project_indexer import ProjectIndexer
    from token_savior.query_api import create_project_query_functions

    idx = ProjectIndexer(str(projet))
    idx.index()
    return create_project_query_functions(idx._project_index)


def test_l_ambiguite_est_nommee(qf) -> None:
    out = qf["find_symbol"](name="creer_demande_signature")
    texte = str(out)

    assert "ancien.py" in texte and "actuel.py" in texte, (
        "un seul des deux fichiers a ete nomme ; l'agent croira le symbole "
        f"unique et editera peut-etre le mauvais (rendu : {texte[:300]})"
    )


def test_un_symbole_unique_ne_declenche_aucun_bruit(qf, projet) -> None:
    """Signaler une ambiguite inexistante serait paye a chaque appel."""
    (projet / "seul.py").write_text("def fonction_unique():\n    return 1\n", encoding="utf-8")
    from token_savior.project_indexer import ProjectIndexer
    from token_savior.query_api import create_project_query_functions

    idx = ProjectIndexer(str(projet))
    idx.index()
    qf2 = create_project_query_functions(idx._project_index)

    texte = str(qf2["find_symbol"](name="fonction_unique"))
    assert "ambig" not in texte.lower(), f"bruit sur un symbole unique : {texte[:200]}"
    assert "seul.py" in texte
