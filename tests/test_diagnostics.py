"""Les erreurs du compilateur, rendues comme des symboles et non comme du texte.

Manque identifie le 06/08/2026 en decortiquant `oraios/serena`, qui expose
`GetDiagnosticsForFileTool` et `GetDiagnosticsForSymbolTool`. Token Savior
n'avait rien : pour savoir si son edition compilait, un agent devait lancer un
build en Bash et lire une sortie non structuree, souvent longue.

Ce que ce module apporte par rapport a un `npx tsc` en Bash :
- une sortie structuree et bornee, au lieu d'un mur de texte ;
- le **symbole englobant** de chaque erreur, qui est l'unite dans laquelle
  l'agent edite -- une ligne nue l'oblige a relire le fichier pour savoir quoi
  corriger ;
- le regroupement par fichier, et un compte exact quand la borne tronque.

Le choix de ne pas passer par LSP est mesure, pas dogmatique : voir la note de
decision LSP (6 s de demarrage a froid pour un superset pratique deja couvert
par tree-sitter sur les tests impactes). Ici on appelle le verificateur que le
projet utilise deja, donc zero dependance nouvelle.
"""

from __future__ import annotations

import pytest

from token_savior import diagnostics


class TestAnalyseDeSortie:
    """Le format de chaque verificateur, teste sur sa sortie reelle."""

    def test_tsc_ligne_standard(self) -> None:
        brut = (
            "app/api/contrats/envoyer/route.ts(42,7): error TS2345: "
            "Argument of type 'string' is not assignable to parameter of type 'number'.\n"
        )
        [d] = diagnostics.analyser_tsc(brut)
        assert d.fichier == "app/api/contrats/envoyer/route.ts"
        assert d.ligne == 42
        assert d.colonne == 7
        assert d.gravite == "error"
        assert d.code == "TS2345"
        assert "not assignable" in d.message

    def test_tsc_ignore_le_bruit(self) -> None:
        brut = (
            "> tsc --noEmit\n"
            "\n"
            "src/a.ts(1,1): error TS1005: ';' expected.\n"
            "Found 1 error in src/a.ts:1\n"
        )
        trouves = diagnostics.analyser_tsc(brut)
        assert len(trouves) == 1, "les lignes de resume ne sont pas des diagnostics"
        assert trouves[0].code == "TS1005"

    def test_mypy_ligne_standard(self) -> None:
        brut = 'src/token_savior/x.py:88: error: Argument 1 has incompatible type  [arg-type]\n'
        [d] = diagnostics.analyser_mypy(brut)
        assert d.fichier == "src/token_savior/x.py"
        assert d.ligne == 88
        assert d.gravite == "error"
        assert d.code == "arg-type"

    def test_mypy_distingue_note_et_erreur(self) -> None:
        brut = (
            "a.py:1: error: vrai probleme  [misc]\n"
            "a.py:2: note: ceci n'est qu'une precision\n"
        )
        trouves = diagnostics.analyser_mypy(brut)
        assert [d.gravite for d in trouves] == ["error", "note"]


class TestDetectionDuVerificateur:
    def test_un_projet_typescript_est_reconnu(self, tmp_path) -> None:
        (tmp_path / "tsconfig.json").write_text("{}", encoding="utf-8")
        assert diagnostics.detecter_verificateur(str(tmp_path)) == "tsc"

    def test_un_projet_python_avec_mypy_est_reconnu(self, tmp_path) -> None:
        (tmp_path / "pyproject.toml").write_text(
            "[tool.mypy]\nstrict = true\n", encoding="utf-8"
        )
        assert diagnostics.detecter_verificateur(str(tmp_path)) == "mypy"

    def test_un_projet_sans_verificateur_le_dit(self, tmp_path) -> None:
        assert diagnostics.detecter_verificateur(str(tmp_path)) is None


class TestRegroupement:
    """La forme rendue a l'agent : bornee, comptee, jamais tronquee en silence."""

    def _d(self, fichier: str, ligne: int, msg: str = "boum"):
        return diagnostics.Diagnostic(
            fichier=fichier, ligne=ligne, colonne=1, gravite="error",
            code="TS1", message=msg,
        )

    def test_regroupe_par_fichier(self) -> None:
        rendu = diagnostics.regrouper(
            [self._d("a.ts", 1), self._d("a.ts", 9), self._d("b.ts", 3)],
            max_par_fichier=10, max_fichiers=10,
        )
        assert rendu["total"] == 3
        assert {f["fichier"] for f in rendu["fichiers"]} == {"a.ts", "b.ts"}

    def test_une_troncature_est_annoncee(self) -> None:
        rendu = diagnostics.regrouper(
            [self._d("a.ts", i) for i in range(1, 26)],
            max_par_fichier=5, max_fichiers=10,
        )
        assert rendu["total"] == 25, "le total doit compter ce qui a ete coupe"
        fichier = rendu["fichiers"][0]
        assert len(fichier["diagnostics"]) == 5
        assert fichier["tronque"] == 20, (
            "une borne silencieuse se lit comme une absence d'erreur"
        )

    def test_sans_erreur_le_dit_explicitement(self) -> None:
        rendu = diagnostics.regrouper([], max_par_fichier=5, max_fichiers=5)
        assert rendu["total"] == 0
        assert rendu["fichiers"] == []


class TestSymboleEnglobant:
    """L'apport propre a Token Savior : un agent edite des symboles, pas des lignes."""

    class _IndexFactice:
        def get_functions(self, fichier):
            if fichier != "src/boutique.ts":
                return []
            return [
                {"name": "calculerTotal", "line": 1, "line_end": 4},
                {"name": "appliquerRemise", "line": 5, "line_end": 7},
            ]

    def test_chaque_erreur_recoit_sa_fonction(self) -> None:
        trouves = [
            diagnostics.Diagnostic("src/boutique.ts", 2, 9, "error", "TS2322", "boum"),
            diagnostics.Diagnostic("src/boutique.ts", 6, 18, "error", "TS2304", "boum"),
        ]
        diagnostics.attacher_symboles(trouves, self._IndexFactice())
        assert [d.symbole for d in trouves] == ["calculerTotal", "appliquerRemise"]

    def test_un_fichier_hors_index_ne_fait_pas_echouer(self) -> None:
        """Une erreur sans symbole reste utile ; une exception perdrait tout."""
        trouves = [diagnostics.Diagnostic("vendor/inconnu.ts", 3, 1, "error", "TS1", "boum")]
        diagnostics.attacher_symboles(trouves, self._IndexFactice())
        assert trouves[0].symbole is None

    def test_index_absent_est_tolere(self) -> None:
        trouves = [diagnostics.Diagnostic("a.ts", 1, 1, "error", "TS1", "boum")]
        diagnostics.attacher_symboles(trouves, None)
        assert trouves[0].symbole is None
