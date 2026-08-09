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


# Les classes TestJetons / TestRecouvrement / TestReclassementHybride ont ete
# retirees le 09/08/2026 avec le reclassement lexical qu'elles couvraient.
# Elles testaient des helpers corrects au service d'un melange qui, lui,
# comptait deux fois le chemin du fichier : deja present dans le document
# vectorise, puis repondere a 0,35 apres coup. Le banc code_retrieval a
# chiffre le cout (MRR@10 0,690 -> 0,635, R@3 0,733 -> 0,633).
