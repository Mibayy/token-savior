"""`move_symbol` echouait systematiquement, et personne ne le testait.

Trouve par un test adverse sur un projet fabrique, pas par relecture : le
handler appelait `slot.indexer.reindex()`, methode qui n'existe pas sur
`ProjectIndexer`. Chaque deplacement reussi levait donc une `AttributeError`
**apres** avoir modifie les deux fichiers : le travail etait fait, l'outil
rendait une erreur, et l'appelant croyait a un echec.

Deuxieme defaut, decouvert en verifiant le premier : les cles du resultat sont
`from_file` et `to_file`. Reindexer d'autres noms laissait l'index annoncer le
symbole a son ancienne place, donc `find_symbol` mentait juste apres un
deplacement reussi.

Aucun test n'exercait cet outil. Ces deux-la couvrent le contrat complet :
le fichier source, le fichier cible, et l'index.
"""
from __future__ import annotations

import pytest

from token_savior.server_handlers import edit as edit_handlers


@pytest.fixture
def projet(tmp_path):
    """Deux modules, un symbole a deplacer de l'un vers l'autre."""
    (tmp_path / "app").mkdir()
    (tmp_path / "app" / "__init__.py").write_text("", encoding="utf-8")
    (tmp_path / "app" / "source.py").write_text(
        "def garde_moi():\n    return 1\n\n\n"
        "def deplace_moi(x: int) -> int:\n    return x * 2\n", encoding="utf-8")
    (tmp_path / "app" / "cible.py").write_text(
        "def deja_la():\n    return 0\n", encoding="utf-8")
    return tmp_path


def _slot(projet):
    from token_savior.slot_manager import SlotManager

    mgr = SlotManager(cache_version=2)
    mgr.register_roots([str(projet)])
    slot, err = mgr.resolve(str(projet))
    assert not err, err
    return slot


def test_move_symbol_ne_leve_plus_et_deplace_vraiment(projet) -> None:
    slot = _slot(projet)
    resultat = edit_handlers._h_move_symbol(
        slot, {"symbol": "deplace_moi", "target_file": "app/cible.py"})

    assert resultat.get("ok"), resultat
    source = (projet / "app" / "source.py").read_text(encoding="utf-8")
    cible = (projet / "app" / "cible.py").read_text(encoding="utf-8")
    assert "def deplace_moi" not in source, "le symbole est reste dans la source"
    assert "def deplace_moi" in cible, "le symbole n'est pas arrive dans la cible"
    assert "def garde_moi" in source, "un voisin a ete emporte"
    # Les deux fichiers doivent rester compilables : un deplacement qui casse
    # la syntaxe est pire qu'un deplacement refuse.
    compile(source, "source.py", "exec")
    compile(cible, "cible.py", "exec")


def test_l_index_suit_le_deplacement(projet) -> None:
    """Le bug le plus sournois : l'outil reussit et l'index ment juste apres.

    On interroge par l'API de requete, pas par les entrailles de l'index :
    c'est ce que voit l'appelant, et ca ne depend pas de la structure interne.
    """
    from token_savior.server_handlers import code_nav

    slot = _slot(projet)
    edit_handlers._h_move_symbol(
        slot, {"symbol": "deplace_moi", "target_file": "app/cible.py"})

    from token_savior.server_runtime import _prep
    _prep(slot)
    reponse = str(code_nav._q_find_symbol(slot.query_fns, {"name": "deplace_moi"}))
    assert "cible.py" in reponse, f"l'index pointe encore ailleurs : {reponse[:200]}"
    assert "source.py" not in reponse, f"l'ancienne place subsiste : {reponse[:200]}"


def test_un_symbole_absent_ne_modifie_rien(projet) -> None:
    slot = _slot(projet)
    avant = (projet / "app" / "source.py").read_text(encoding="utf-8")
    resultat = edit_handlers._h_move_symbol(
        slot, {"symbol": "jamais_defini_xyz", "target_file": "app/cible.py"})
    assert not resultat.get("ok")
    assert (projet / "app" / "source.py").read_text(encoding="utf-8") == avant
