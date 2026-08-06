"""L'index semantique ne voyait que Python, en silence.

`collect_project_symbols` marchait au module `ast` : elle faisait
`root.rglob("*.py")`. Sur un projet TypeScript, l'index semantique se
remplissait donc avec les rares scripts Python du depot, et
`search_codebase(semantic=True)` rendait des resultats plausibles issus du
mauvais langage sans jamais dire que le langage principal n'etait pas couvert.

Mesure du 06/08/2026 sur /root/estalle (318 fichiers indexes, dont 315 .ts et
.tsx pour 21 symboles Python) : l'ancien collecteur voyait **21 symboles**, le
nouveau en voit **786**. Sur trois questions a verite terrain, la recherche
passait de 1 cible sur 6 a 3 sur 6.

Trois questions ne font pas un banc : ce chiffre dit qu'un defaut de couverture
a ete corrige, il ne mesure pas la qualite du classement.
"""

from __future__ import annotations

from token_savior.memory import symbol_embeddings as se


class _Portee:
    def __init__(self, start: int) -> None:
        self.start = start


class _Fn:
    def __init__(self, name: str, ligne: int, params=None, doc=None) -> None:
        self.name = name
        self.qualified_name = name
        self.line_range = _Portee(ligne)
        self.parameters = params if params is not None else ["req"]
        self.docstring = doc


class _Meta:
    def __init__(self, fonctions, lignes) -> None:
        self.functions = fonctions
        self.classes = []
        self.lines = lignes


class _Index:
    def __init__(self, fichiers) -> None:
        self.files = fichiers


class TestCollecteMultiLangage:
    def test_un_fichier_typescript_produit_des_symboles(self, tmp_path) -> None:
        index = _Index({
            "app/api/oodrive/webhook/route.ts": _Meta(
                [_Fn("POST", 1)],
                ["export async function POST(req) {", "  const svc = createServiceSupabase();", "}"],
            )
        })
        symboles = se.collect_symbols_from_index(str(tmp_path), index)
        assert len(symboles) == 1
        assert symboles[0]["file_path"] == "app/api/oodrive/webhook/route.ts"
        assert symboles[0]["kind"] == "function"

    def test_le_chemin_est_dans_le_document_vectorise(self, tmp_path) -> None:
        """Le seul porteur de sens quand le symbole s'appelle POST."""
        index = _Index({
            "app/api/oodrive/webhook/route.ts": _Meta([_Fn("POST", 1)], ["  const x = 1;"])
        })
        [sym] = se.collect_symbols_from_index(str(tmp_path), index)
        doc = sym["embed_doc"]
        assert "app/api/oodrive/webhook/route.ts" in doc
        assert "oodrive" in doc and "webhook" in doc, (
            "sans les mots du chemin, deux routes homonymes sont indiscernables"
        )

    def test_un_index_vide_ne_casse_pas(self, tmp_path) -> None:
        assert se.collect_symbols_from_index(str(tmp_path), _Index({})) == []


class TestJetons:
    def test_coupe_les_chemins_et_le_camel_case(self) -> None:
        jetons = se._jetons("components/admin/PlanningContractuelEditor.tsx")
        assert {"components", "admin", "planning", "contractuel", "editor"} <= jetons

    def test_ecarte_les_mots_vides_et_les_trop_courts(self) -> None:
        jetons = se._jetons("How is the a of ts")
        assert jetons == set(), f"restait : {jetons}"


class TestRecouvrement:
    def test_l_egalite_stricte_compte(self) -> None:
        assert se._recouvrement({"status"}, {"status", "route"}) == 1.0

    def test_un_prefixe_commun_rattrape_le_francais(self) -> None:
        """Le code est en francais, les questions arrivent en anglais."""
        assert se._recouvrement({"contract"}, {"contrats"}) == 1.0
        assert se._recouvrement({"planning"}, {"plannings"}) == 1.0

    def test_un_prefixe_trop_court_ne_compte_pas(self) -> None:
        """Trois lettres rapprocheraient n'importe quoi."""
        assert se._recouvrement({"api"}, {"application"}) == 0.0

    def test_aucun_mot_dans_la_question_rend_zero(self) -> None:
        assert se._recouvrement(set(), {"status"}) == 0.0


class TestReclassementHybride:
    def _h(self, fichier: str, cos: float) -> dict:
        return {"file": fichier, "symbol": "GET", "score": cos, "_cos": cos}

    def test_le_lexical_remonte_un_symbole_au_nom_vide_de_sens(self) -> None:
        hits = [
            self._h("components/ui/Bouton.tsx", 0.60),
            self._h("app/api/contrats/status/route.ts", 0.55),
        ]
        classe = se._reclasser_hybride("contract status route", hits)
        assert classe[0]["file"] == "app/api/contrats/status/route.ts", (
            "une correspondance lexicale forte ne doit pas etre diluee par les vecteurs"
        )

    def test_le_lexical_reste_minoritaire(self) -> None:
        """Un poids majoritaire ferait de la semantique un grep deguise."""
        hits = [
            self._h("lib/paiement.ts", 0.95),   # aucun mot commun, tres proche
            self._h("app/status/route.ts", 0.10),  # un mot commun, tres loin
        ]
        classe = se._reclasser_hybride("status", hits)
        assert classe[0]["file"] == "lib/paiement.ts"

    def test_une_question_sans_mot_utile_laisse_l_ordre_intact(self) -> None:
        hits = [self._h("a.ts", 0.9), self._h("b.ts", 0.1)]
        assert se._reclasser_hybride("the a of", hits) == hits
