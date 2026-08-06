"""L'instrument de budget, et la propriete qui le rend utilisable.

Un diagnostic qui decale la sortie d'un seul octet invalide toute mesure A/B
prise avec lui. Le premier test de ce fichier est donc le plus important : la
sortie de `_count_and_wrap_result` doit etre identique au caractere pres, que
le diagnostic soit allume ou eteint.
"""

from __future__ import annotations

import json

import pytest

from token_savior import budget_diag


class TestAllumage:
    def test_eteint_par_defaut(self) -> None:
        assert budget_diag.demarrer({}) is None

    @pytest.mark.parametrize("valeur", ["", "0", "off", "false", "no", "  ", "OFF"])
    def test_les_valeurs_eteintes(self, valeur: str) -> None:
        assert budget_diag.demarrer({"TS_BUDGET_DIAG": valeur}) is None

    @pytest.mark.parametrize("valeur", ["1", "true", "on", "yes", "stderr", "json"])
    def test_les_valeurs_allumees(self, valeur: str) -> None:
        assert budget_diag.demarrer({"TS_BUDGET_DIAG": valeur}) is not None

    def test_une_valeur_inconnue_est_un_chemin(self, tmp_path) -> None:
        cible = str(tmp_path / "j.jsonl")
        d = budget_diag.demarrer({"TS_BUDGET_DIAG": cible})
        assert d is not None
        d.rapporter(budget_diag.RapportAppel(outil="find_symbol", projet="p"))
        assert budget_diag.lire_journal(cible)


class TestSortieIdentiqueQuandEteint:
    """La garantie sans laquelle l'instrument ne sert a rien."""

    def test_le_chemin_commun_rend_les_memes_octets(self, monkeypatch, tmp_path) -> None:
        from token_savior import server_runtime

        class _Slot:
            root = str(tmp_path)
            stats_file = None

        monkeypatch.setattr(server_runtime, "_estimate_naive_chars_for_call",
                            lambda *a, **k: 4242)

        resultat = {"symbol": "calculer_total", "file": "boutique.py", "line": 4}

        monkeypatch.delenv("TS_BUDGET_DIAG", raising=False)
        eteint = server_runtime._count_and_wrap_result(_Slot(), "find_symbol", {}, resultat)

        journal = str(tmp_path / "budget.jsonl")
        monkeypatch.setenv("TS_BUDGET_DIAG", journal)
        allume = server_runtime._count_and_wrap_result(_Slot(), "find_symbol", {}, resultat)

        assert [c.text for c in eteint] == [c.text for c in allume], (
            "le diagnostic a change la reponse ; toute mesure A/B prise avec "
            "lui serait invalide"
        )
        assert budget_diag.lire_journal(journal), "et il doit quand meme avoir mesure"


class TestRapport:
    def test_economie_et_part(self) -> None:
        r = budget_diag.RapportAppel(outil="get_full_context", projet="p",
                                     octets_rendus=2_000, octets_naifs=10_000)
        assert r.economie == 8_000
        assert r.part_economisee == 0.8

    def test_une_economie_negative_est_possible_et_visible(self) -> None:
        """Un outil peut couter plus cher qu'une lecture naive. Il faut le voir."""
        r = budget_diag.RapportAppel(outil="get_function_source", projet="p",
                                     octets_rendus=317, octets_naifs=232)
        assert r.economie == -85
        assert r.part_economisee < 0

    def test_naif_a_zero_ne_divise_pas_par_zero(self) -> None:
        r = budget_diag.RapportAppel(outil="x", projet="p", octets_rendus=10, octets_naifs=0)
        assert r.part_economisee == 0.0


class TestResume:
    def _a(self, outil: str, rendus: int, naifs: int, tronque: bool = False) -> dict:
        return {"outil": outil, "octets_rendus": rendus, "octets_naifs": naifs,
                "economie": naifs - rendus, "tronque": tronque}

    def test_agrege_par_outil(self) -> None:
        r = budget_diag.resumer([
            self._a("find_symbol", 100, 1000),
            self._a("find_symbol", 200, 2000),
            self._a("get_full_context", 500, 800),
        ])
        assert r["appels"] == 3
        assert r["octets_rendus"] == 800
        assert {e["outil"] for e in r["par_outil"]} == {"find_symbol", "get_full_context"}

    def test_sort_les_appels_a_economie_negative(self) -> None:
        """Noyes dans une moyenne ils sont invisibles, et ce sont eux qui comptent."""
        r = budget_diag.resumer([
            self._a("find_symbol", 100, 5000),
            self._a("get_function_source", 317, 232),
        ])
        assert len(r["economie_negative"]) == 1
        assert r["economie_negative"][0]["outil"] == "get_function_source"

    def test_journal_vide(self) -> None:
        r = budget_diag.resumer([])
        assert r["appels"] == 0 and r["par_outil"] == []

    def test_une_ligne_illisible_ne_fait_pas_tomber_la_lecture(self, tmp_path) -> None:
        f = tmp_path / "j.jsonl"
        f.write_text('{"outil":"a","octets_rendus":1,"octets_naifs":2}\n{ pas du json\n',
                     encoding="utf-8")
        assert len(budget_diag.lire_journal(str(f))) == 1
