"""Les modules ES `.mjs` et CommonJS `.cjs` sont du JavaScript.

Releve sur 30 jours de transcriptions (06/10/2026) : toutes les erreurs
« file not found in index » de `get_functions` et la plupart de celles de
`read_lines` portaient sur des `.mjs` (`launchpad/pump.mjs`,
`runtime/cycle-reconciler.mjs`, `src/decodage.mjs`). Le fichier existait, il
n'etait simplement jamais indexe : les motifs d'inclusion s'arretaient a
`.js` et `.jsx`.
"""
from __future__ import annotations

from pathlib import Path

from token_savior.project_indexer import ProjectIndexer


def test_mjs_et_cjs_sont_indexes_avec_leurs_symboles(tmp_path: Path) -> None:
    (tmp_path / "lib").mkdir()
    (tmp_path / "lib" / "pump.mjs").write_text(
        "import { cle } from './outils.mjs';\n"
        "export async function acheter(mint) {\n  return cle(mint);\n}\n",
        encoding="utf-8")
    (tmp_path / "lib" / "outils.mjs").write_text(
        "export function cle(x) {\n  return x;\n}\n", encoding="utf-8")
    (tmp_path / "lib" / "ancien.cjs").write_text(
        "function vieux() {\n  return 1;\n}\nmodule.exports = { vieux };\n",
        encoding="utf-8")
    index = ProjectIndexer(str(tmp_path)).index()
    fichiers = set(index.files)
    assert "lib/pump.mjs" in fichiers
    assert "lib/ancien.cjs" in fichiers
    noms = {f.name for f in index.files["lib/pump.mjs"].functions}
    assert "acheter" in noms
    assert "vieux" in {f.name for f in index.files["lib/ancien.cjs"].functions}
