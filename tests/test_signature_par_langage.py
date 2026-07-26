"""Une signature Java ne doit pas commencer par `def`.

Trois endroits construisaient `def nom(params)` quel que soit le langage. Sur
du Java cela donnait `def totalPour(quantite)` : un mot-cle absent de ce
langage, et surtout **les types perdus** alors que `qualified_name` les porte
(`boutique.Tarif.totalPour(int)`). Un agent qui lit cette signature peut
ecrire un appel qui ne compile pas, et rien ne l'avertit.

Trouve en exercant Java et Ruby pour la premiere fois.
"""
from __future__ import annotations

import pytest

from token_savior.query_api import format_signature


class FauxSymbole:
    def __init__(self, name, parameters, qualified_name="", return_type=None):
        self.name = name
        self.parameters = parameters
        self.qualified_name = qualified_name
        self.return_type = return_type


JAVA = FauxSymbole("totalPour", ["quantite"], "boutique.Tarif.totalPour(int)", "int")
RUBY = FauxSymbole("total_pour", ["quantite"], "Boutique.Tarif.total_pour")
PY = FauxSymbole("appliquer_remise", ["total", "pourcentage"], "appliquer_remise")
TS = FauxSymbole("totalPanier", ["lignes", "prixUnitaire"], "totalPanier")


def test_java_ne_dit_pas_def() -> None:
    sig = format_signature(JAVA, "java/src/main/java/boutique/Tarif.java")
    assert not sig.startswith("def "), sig


def test_java_retrouve_les_types_perdus() -> None:
    """Les types sont dans qualified_name ; ne pas les rendre les jette."""
    sig = format_signature(JAVA, "java/Tarif.java")
    assert "int quantite" in sig, sig
    assert sig.startswith("int "), f"type de retour absent : {sig}"


@pytest.mark.parametrize("sym,chemin", [(RUBY, "ruby/lib/tarif.rb"),
                                        (PY, "app/tarifs.py")])
def test_python_et_ruby_gardent_def(sym, chemin) -> None:
    """`def` est correct dans ces deux langages : ne pas le retirer par exces."""
    assert format_signature(sym, chemin).startswith("def "), chemin


def test_typescript_sans_def() -> None:
    sig = format_signature(TS, "front/src/panier.ts")
    assert not sig.startswith("def "), sig
    assert "totalPanier(lignes, prixUnitaire)" in sig, sig


def test_types_absents_rend_les_noms_seuls() -> None:
    """Mieux vaut des noms seuls qu'un appariement invente."""
    sans = FauxSymbole("f", ["a", "b"], "f")
    assert format_signature(sans, "x.java") == "f(a, b)"


def test_desequilibre_types_noms_ne_fabrique_rien() -> None:
    """Un `(int,int)` pour un seul nom signale un parseur imprecis : on
    n'invente pas d'appariement, on rend ce dont on est sur."""
    bancal = FauxSymbole("g", ["a"], "C.g(int,int)")
    sig = format_signature(bancal, "x.java")
    assert sig == "g(a)", sig


def test_chemin_inconnu_retombe_sur_def() -> None:
    """Sans extension, on ne sait pas : le comportement historique est garde."""
    assert format_signature(PY, "").startswith("def ")
