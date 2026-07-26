"""Ne jamais refuser ce qu'on peut resoudre.

Deux familles d'echecs mesurees en rejouant 100 appels reels de sessions
enregistrees. Les deux etaient classees « pas des defauts » a la premiere
lecture, et les deux en sont.

**Noms d'arguments.** 9 appels sur 295 utilisaient un nom inexistant, et chacun
etait le nom employe par un outil VOISIN pour la meme chose : `query` vient de
`ts_search`, `source` de `replace_symbol_source`. Le meme concept portait trois
noms selon l'outil (`name` / `project` / `symbol_name`), donc l'appelant
devinait. Une devinette ratee coute un aller-retour complet.

**Resolution de projet.** `scribe-transcription` ne trouvait jamais le projet
`scribe` : le flou ne cherchait que le hint DANS le nom, jamais l'inverse. Et
un chemin reel non enregistre etait refuse alors qu'on savait quoi faire.
"""
from __future__ import annotations

import pytest

from token_savior.server import _normalize_arguments
from token_savior.slot_manager import SlotManager

# --- Alias d'arguments ----------------------------------------------------- #

@pytest.mark.parametrize("outil,donne,attendu", [
    ("search_codebase", {"query": "def foo"}, {"pattern": "def foo"}),
    ("search_codebase", {"q": "x"}, {"pattern": "x"}),
    ("insert_near_symbol", {"source": "code"}, {"content": "code"}),
    ("insert_near_symbol", {"new_source": "code"}, {"content": "code"}),
    ("replace_symbol_source", {"content": "code"}, {"new_source": "code"}),
    ("switch_project", {"project": "p"}, {"name": "p"}),
    ("set_project_root", {"project": "/x"}, {"path": "/x"}),
    ("get_function_source", {"symbol_name": "f"}, {"name": "f"}),
    ("get_full_context", {"symbol": "f"}, {"name": "f"}),
    ("ts_search", {"pattern": "x"}, {"query": "x"}),
])
def test_traduit_les_alias_reellement_observes(outil, donne, attendu) -> None:
    assert _normalize_arguments(outil, donne) == attendu


def test_le_nom_canonique_l_emporte_sur_l_alias() -> None:
    """Si l'appelant fournit les deux, on ne doit pas ecraser le bon."""
    out = _normalize_arguments("search_codebase", {"pattern": "bon", "query": "alias"})
    assert out["pattern"] == "bon"


def test_n_invente_rien_pour_un_outil_sans_table() -> None:
    args = {"foo": 1}
    assert _normalize_arguments("get_git_status", args) == args


def test_ne_mute_pas_l_appelant() -> None:
    args = {"query": "x"}
    _normalize_arguments("search_codebase", args)
    assert args == {"query": "x"}, "l'argument d'origine doit rester intact"


@pytest.mark.parametrize("args", [None, "pas un dict", 42])
def test_tolere_une_entree_malformee(args) -> None:
    assert _normalize_arguments("search_codebase", args) == args


# --- Resolution de projet -------------------------------------------------- #

def _mgr(tmp_path, *noms):
    m = SlotManager(cache_version=1)
    roots = []
    for n in noms:
        d = tmp_path / n
        d.mkdir(parents=True, exist_ok=True)
        (d / "a.py").write_text("x = 1\n", encoding="utf-8")
        roots.append(str(d))
    m.register_roots(roots)
    return m


def test_un_hint_plus_long_que_le_nom_du_projet(tmp_path) -> None:
    """Le cas mesure : `scribe-transcription` doit trouver `scribe`."""
    m = _mgr(tmp_path, "scribe", "intel")
    slot, err = m.resolve("scribe-transcription")
    assert err == "", err
    assert slot.root.endswith("scribe")


def test_le_nom_le_plus_long_gagne(tmp_path) -> None:
    """`api` ne doit pas rafler ce qui appartient a `api-client`."""
    m = _mgr(tmp_path, "api-client", "apix")
    slot, err = m.resolve("mon-api-client-v2")
    assert err == ""
    assert slot.root.endswith("api-client")


def test_les_noms_trop_courts_ne_matchent_pas_a_l_envers(tmp_path) -> None:
    """Un projet nomme `ui` matcherait la moitie des phrases."""
    m = _mgr(tmp_path, "ui", "core")
    _, err = m.resolve("construire-ui-et-autre-chose")
    assert err != "", "un nom de 2 lettres ne doit pas capturer par inclusion"


def test_un_chemin_reel_non_enregistre_est_rattache(tmp_path) -> None:
    """Refuser ici envoyait vers set_project_root sans raison : on connait le
    chemin, il existe, et l'enregistrer est exactement ce qui etait voulu."""
    m = _mgr(tmp_path, "connu")
    neuf = tmp_path / "jamais-vu"
    neuf.mkdir()
    (neuf / "b.py").write_text("y = 2\n", encoding="utf-8")
    slot, err = m.resolve(str(neuf))
    assert err == "", err
    assert slot.root == str(neuf)


def test_un_chemin_inexistant_reste_une_erreur_utile(tmp_path) -> None:
    m = _mgr(tmp_path, "connu")
    _, err = m.resolve("/chemin/qui/n/existe/pas")
    assert "not found" in err
    assert "connu" in err, "l'erreur doit lister ce qui existe"


def test_l_ambiguite_reste_refusee(tmp_path) -> None:
    """Basculer en silence vers le mauvais projet coute plus cher qu'un refus."""
    m = _mgr(tmp_path, "app-front", "app-back")
    _, err = m.resolve("app")
    assert err != ""
    assert "Multiple" in err or "Did you mean" in err
