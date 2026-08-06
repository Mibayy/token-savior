"""Un appelant qui n'appelle rien est pire qu'une absence de reponse.

Constate le 06/08/2026 sur /root/estalle, en confrontant Token Savior a serena
sur le meme symbole, avec une verite terrain etablie au grep.

`get_dependents("createSignatureRequest")` rendait
`app/api/admin/demande-prestations/route.ts::POST` — un fichier qui ne contient
**aucune occurrence** de ce nom. Le vrai appelant,
`app/api/contrats/envoyer/route.ts`, n'etait pas dans la reponse.

Cause : le graphe de dependances est indexe par **nom simple**. Il enregistre
« createSignatureRequest <- POST », sans dire quel POST. Or un projet Next.js
compte une trentaine de fonctions nommees POST. La resolution en choisissait
une, dans l'ordre alphabetique.

Sens de l'erreur a preferer : une reponse vide se voit, un mauvais appelant se
lit comme un fait et envoie editer le mauvais fichier. Quand l'appelant est
ambigu, on ne devine pas — on filtre sur l'import reel, et ce qu'on ne peut
pas trancher, on le dit.
"""

from __future__ import annotations

import pytest


@pytest.fixture
def projet(tmp_path):
    """Deux fichiers exportent `handler`. Un seul importe la cible."""
    (tmp_path / "lib").mkdir()
    (tmp_path / "lib" / "signature.py").write_text(
        "def creer_demande_signature(doc):\n"
        '    """Envoie un document en signature."""\n'
        "    return {'id': 1}\n",
        encoding="utf-8",
    )
    # Le VRAI appelant : il importe et il appelle.
    (tmp_path / "vrai_appelant.py").write_text(
        "from lib.signature import creer_demande_signature\n"
        "\n"
        "def handler(req):\n"
        "    return creer_demande_signature(req)\n",
        encoding="utf-8",
    )
    # Le FAUX : meme nom de fonction, aucun rapport avec la cible.
    (tmp_path / "aaa_faux_appelant.py").write_text(
        "def handler(req):\n"
        "    return {'ok': True}\n",
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


def _fichiers(entries) -> set[str]:
    return {
        e.get("file", "")
        for e in entries
        if isinstance(e, dict) and e.get("file")
    }


def test_le_faux_appelant_n_est_pas_rendu(qf) -> None:
    entries = qf["get_dependents"]("creer_demande_signature")
    fichiers = _fichiers(entries)

    assert "aaa_faux_appelant.py" not in fichiers, (
        "un fichier qui ne contient aucune occurrence du symbole a ete rendu "
        "comme appelant ; il se lit comme un fait et envoie editer le mauvais "
        f"fichier (rendu : {sorted(fichiers)})"
    )


def test_le_vrai_appelant_est_rendu(qf) -> None:
    """Le correctif ne doit pas vider la reponse pour eviter le faux."""
    entries = qf["get_dependents"]("creer_demande_signature")
    fichiers = _fichiers(entries)
    assert "vrai_appelant.py" in fichiers, (
        f"le vrai appelant a disparu (rendu : {sorted(fichiers)})"
    )
