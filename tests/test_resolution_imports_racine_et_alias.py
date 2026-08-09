"""Le graphe d'imports doit se remplir ailleurs que dans `src/` et `lib/`.

Deux pannes trouvees le 09/08/2026 **par le banc**, pas par la relecture. Elles
partagent la meme forme : une convention codee en dur, un echec silencieux, et
une reponse vide qui se lit comme une information.

1. **Racine au-dessus du paquet.** Les prefixes cherches etaient
   `["", "src/", "lib/"]`. Sur un depot dont le code vit dans `depot/`,
   `app/`, `packages/`..., aucun import ne se resolvait, donc `import_graph`
   restait vide, donc `find_import_cycles` rendait `[]`. Trois modeles sur
   quatre ont repris ce `[]` a leur compte et affirme « aucun cycle » sur un
   depot qui en contenait un evident.

2. **Alias TypeScript.** `@/` etait resolu en dur vers `src/`, alors que la
   convention Next.js App Router **sans** dossier `src` declare
   `"@/*": ["./*"]`. Mesure avant/apres sur les projets reels de ce VPS :
   estalle 105 -> 461 aretes, improvence 66 -> 594.

Le cout de ces pannes n'est pas l'absence de reponse, c'est sa forme : `[]`
et « aucune dependance » sont indiscernables d'un fait etabli.
"""

from __future__ import annotations

import json

import pytest

from token_savior.project_indexer import ProjectIndexer, _json_sans_commentaires
from token_savior.query_api import create_project_query_functions


def _indexer(racine):
    idx = ProjectIndexer(str(racine))
    idx.index()
    return idx


def _cycle_python(base, prefixe: str) -> None:
    """Ecrit un cycle a -> b -> a sous `prefixe`.

    Le dossier prefixe ne recoit **pas** d'`__init__.py` : c'est un dossier de
    rangement (`src/`, `depot/`, `app/`), pas un paquet. S'il en avait un,
    `from paquet.b import ...` serait un import invalide en Python et le test
    mesurerait autre chose que ce qu'il pretend.
    """
    dossier = base / prefixe if prefixe else base
    (dossier / "paquet").mkdir(parents=True, exist_ok=True)
    (dossier / "paquet" / "__init__.py").write_text("", encoding="utf-8")
    (dossier / "paquet" / "a.py").write_text(
        "from paquet.b import beta\n\n\ndef alpha():\n    return beta()\n", encoding="utf-8")
    (dossier / "paquet" / "b.py").write_text(
        "import paquet.a\n\n\ndef beta():\n    return paquet.a\n", encoding="utf-8")


class TestRacineAuDessusDuPaquet:
    @pytest.mark.parametrize("prefixe", ["", "src", "depot", "app", "packages/coeur"])
    def test_le_cycle_est_vu_quel_que_soit_le_dossier(self, tmp_path, prefixe) -> None:
        """`src/` marchait deja ; les autres non, et rien ne le disait."""
        _cycle_python(tmp_path, prefixe)
        qf = create_project_query_functions(_indexer(tmp_path)._project_index)
        cycles = qf["find_import_cycles"]()
        assert cycles, (
            f"aucun cycle trouve avec le code sous « {prefixe or '(racine)'} » ; "
            "un graphe vide rend [] , ce qui se lit « pas de cycle » au lieu de "
            "« je n'ai pas su resoudre les imports »"
        )

    def test_les_aretes_existent_vraiment(self, tmp_path) -> None:
        _cycle_python(tmp_path, "depot")
        graphe = _indexer(tmp_path)._project_index.import_graph
        aretes = {f: v for f, v in graphe.items() if "paquet" in f and v}
        assert aretes, "le graphe est vide alors que deux modules s'importent"


class TestAliasTypeScript:
    def _projet_next(self, base, cible_alias: str) -> None:
        (base / "tsconfig.json").write_text(
            json.dumps({"compilerOptions": {"paths": {"@/*": [cible_alias]}}}),
            encoding="utf-8")
        (base / "lib").mkdir(parents=True, exist_ok=True)
        (base / "lib" / "utils.ts").write_text("export const f = () => 1;\n", encoding="utf-8")
        (base / "app").mkdir(parents=True, exist_ok=True)
        (base / "app" / "page.tsx").write_text(
            "import { f } from '@/lib/utils';\nexport default function P() { return f(); }\n",
            encoding="utf-8")

    def test_alias_vers_la_racine(self, tmp_path) -> None:
        """`"@/*": ["./*"]` : la convention Next.js sans dossier src."""
        self._projet_next(tmp_path, "./*")
        graphe = _indexer(tmp_path)._project_index.import_graph
        assert "lib/utils.ts" in graphe.get("app/page.tsx", set()), (
            "l'alias @/ n'a pas ete lu dans le tsconfig ; c'est 232 fichiers "
            "sans aucune arete sur un projet reel"
        )

    def test_alias_vers_src_reste_supporte(self, tmp_path) -> None:
        """Ne pas casser les projets qui, eux, ont bien un dossier src."""
        (tmp_path / "tsconfig.json").write_text(
            json.dumps({"compilerOptions": {"paths": {"@/*": ["./src/*"]}}}), encoding="utf-8")
        (tmp_path / "src" / "lib").mkdir(parents=True, exist_ok=True)
        (tmp_path / "src" / "lib" / "utils.ts").write_text("export const f = 1;\n", encoding="utf-8")
        (tmp_path / "src" / "app").mkdir(parents=True, exist_ok=True)
        (tmp_path / "src" / "app" / "page.tsx").write_text(
            "import { f } from '@/lib/utils';\n", encoding="utf-8")
        graphe = _indexer(tmp_path)._project_index.import_graph
        assert "src/lib/utils.ts" in graphe.get("src/app/page.tsx", set())

    def test_sans_tsconfig_on_replie_sur_la_convention(self, tmp_path) -> None:
        (tmp_path / "src" / "lib").mkdir(parents=True, exist_ok=True)
        (tmp_path / "src" / "lib" / "utils.ts").write_text("export const f = 1;\n", encoding="utf-8")
        (tmp_path / "app").mkdir(parents=True, exist_ok=True)
        (tmp_path / "app" / "page.tsx").write_text("import { f } from '@/lib/utils';\n", encoding="utf-8")
        graphe = _indexer(tmp_path)._project_index.import_graph
        assert "src/lib/utils.ts" in graphe.get("app/page.tsx", set())

    def test_un_paquet_externe_ne_cree_pas_d_arete(self, tmp_path) -> None:
        """`react` n'est pas un fichier du projet : aucune arete inventee."""
        self._projet_next(tmp_path, "./*")
        (tmp_path / "app" / "page.tsx").write_text(
            "import { useState } from 'react';\nimport { f } from '@/lib/utils';\n",
            encoding="utf-8")
        cibles = _indexer(tmp_path)._project_index.import_graph.get("app/page.tsx", set())
        assert cibles == {"lib/utils.ts"}, f"arete parasite : {cibles}"


class TestNettoyageJsonAvecCommentaires:
    """Le motif d'un alias ressemble a un commentaire, et c'est un piege reel."""

    def test_un_motif_alias_n_est_pas_pris_pour_un_commentaire(self) -> None:
        """`"@/*"` contient `/*`. Une regex naive avale le reste du fichier."""
        brut = '{"compilerOptions": {"paths": {"@/*": ["./*"]}}}'
        assert json.loads(_json_sans_commentaires(brut))["compilerOptions"]["paths"] == {
            "@/*": ["./*"]}

    def test_les_vrais_commentaires_partent(self) -> None:
        brut = """{
          // une ligne de commentaire
          "compilerOptions": {
            /* un bloc */
            "paths": {"@/*": ["./*"]},
          }
        }"""
        conf = json.loads(_json_sans_commentaires(brut))
        assert conf["compilerOptions"]["paths"] == {"@/*": ["./*"]}

    def test_une_barre_dans_une_chaine_est_preservee(self) -> None:
        brut = '{"url": "https://exemple.fr/a", "x": 1}'
        assert json.loads(_json_sans_commentaires(brut))["url"] == "https://exemple.fr/a"
