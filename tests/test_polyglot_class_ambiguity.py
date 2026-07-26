"""Une classe homonyme dans deux langages rendait une reponse sur deux.

Trouve en exercant Java et Ruby pour la premiere fois. Le produit embarque
`tree-sitter-java` et `tree-sitter-ruby` depuis longtemps, et aucun audit ne
les avait jamais fait tourner.

Les fonctions collectaient leurs candidats et signalaient l'ambiguite ; les
classes rendaient le **premier** match et sortaient. Sur un projet polyglotte,
demander `Tarif` renvoyait donc la classe Java, presentee comme la seule, sans
rien qui permette de s'en douter. C'est la pire categorie de defaut : une
reponse fausse rendue avec assurance, impossible a distinguer d'une bonne.

Le message nomme desormais les fichiers. Une ambiguite qui dit ou regarder se
resout en un appel ; une ambiguite muette en coute trois.
"""
from __future__ import annotations

import pytest

from token_savior.project_indexer import ProjectIndexer
from token_savior.query_api import create_project_query_functions


@pytest.fixture
def polyglotte(tmp_path):
    """Meme nom de classe en Python, Java et Ruby."""
    (tmp_path / "app").mkdir()
    (tmp_path / "app" / "tarif.py").write_text(
        "class Tarif:\n"
        "    def total(self, q):\n        return q\n", encoding="utf-8")
    (tmp_path / "java").mkdir()
    (tmp_path / "java" / "Tarif.java").write_text(
        "package boutique;\n\npublic class Tarif {\n"
        "    public int total(int q) { return q; }\n}\n", encoding="utf-8")
    (tmp_path / "ruby").mkdir()
    (tmp_path / "ruby" / "tarif.rb").write_text(
        "class Tarif\n  def total(q)\n    q\n  end\nend\n", encoding="utf-8")
    # une classe qui n'existe qu'une fois, temoin
    (tmp_path / "app" / "unique.py").write_text(
        "class ClasseUnique:\n    pass\n", encoding="utf-8")
    return tmp_path


def _qfns(racine):
    return create_project_query_functions(ProjectIndexer(str(racine)).index())


def test_une_classe_homonyme_est_signalee_ambigue(polyglotte) -> None:
    q = _qfns(polyglotte)
    res = q["find_symbol"]("Tarif")

    assert "error" in res, f"une seule reponse rendue : {res}"
    assert "ambiguous" in res["error"], res["error"]


def test_le_message_nomme_les_fichiers(polyglotte) -> None:
    """Une ambiguite qui ne dit pas ou regarder ne fait que deplacer le probleme."""
    q = _qfns(polyglotte)
    res = q["find_symbol"]("Tarif")

    message = res.get("error", "") + " " + " ".join(res.get("candidates") or [])
    for attendu in (".py", ".java", ".rb"):
        assert attendu in message, f"{attendu} absent du message : {message}"


def test_une_classe_unique_repond_toujours(polyglotte) -> None:
    """Le correctif ne doit pas rendre ambigu ce qui ne l'est pas."""
    q = _qfns(polyglotte)
    res = q["find_symbol"]("ClasseUnique")

    assert "error" not in res, res
    assert "unique.py" in res.get("file", "")


def test_file_path_tranche_l_ambiguite(polyglotte) -> None:
    q = _qfns(polyglotte)
    source = str(q["get_class_source"]("Tarif", "ruby/tarif.rb", level=0))

    assert "def total(q)" in source, source[:200]
    assert "public class" not in source, "la classe Java a ete rendue a la place"


@pytest.mark.parametrize("langage,fichier,attendu", [
    ("java", "java/Tarif.java", "public class Tarif"),
    ("ruby", "ruby/tarif.rb", "class Tarif"),
    ("python", "app/tarif.py", "class Tarif:"),
])
def test_les_trois_langages_sont_indexes(polyglotte, langage, fichier, attendu) -> None:
    """`tree-sitter-java` et `tree-sitter-ruby` sont livres depuis longtemps et
    n'avaient jamais ete exerces par un test."""
    q = _qfns(polyglotte)
    classes = str(q["get_classes"](fichier))

    assert "Tarif" in classes, f"{langage} : aucune classe indexee dans {fichier}"
