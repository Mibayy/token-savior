"""Handler de `get_diagnostics` : les erreurs du verificateur de types.

Voir `token_savior.diagnostics` pour le pourquoi et le choix de ne pas passer
par LSP.
"""

from __future__ import annotations

from token_savior import diagnostics as diag
from token_savior.server_runtime import _prep
from token_savior.slot_manager import _ProjectSlot


def _h_get_diagnostics(slot: _ProjectSlot, args: dict) -> object:
    racine = slot.root
    verificateur = args.get("checker") or diag.detecter_verificateur(racine)
    if not verificateur:
        # Dire qu'on n'a pas cherche, pas qu'il n'y a rien : « 0 erreur » et
        # « aucun verificateur » se lisent pareil pour un agent presse.
        return {
            "verificateur": None,
            "note": (
                "Aucun verificateur de types declare par ce projet "
                "(ni tsconfig.json, ni [tool.mypy]/[tool.pyright], ni mypy.ini). "
                "Preciser checker='tsc'|'mypy'|'pyright' pour en forcer un."
            ),
        }

    sortie, erreur = diag.executer(
        racine, verificateur, timeout=int(args.get("timeout") or 180)
    )
    if erreur:
        return {"verificateur": verificateur, "erreur": erreur}

    trouves = diag._ANALYSEURS[verificateur](sortie)

    fichier_demande = args.get("file_path")
    if fichier_demande:
        trouves = [d for d in trouves if d.fichier.endswith(str(fichier_demande))]

    if trouves:
        try:
            _prep(slot)
            diag.attacher_symboles(trouves, slot.indexer._project_index)
        except Exception:
            # Le symbole englobant est un confort. Une erreur sans symbole
            # reste exploitable ; une exception ici perdrait tout le rapport.
            pass

    rendu = diag.regrouper(
        trouves,
        max_par_fichier=int(args.get("max_per_file") or 10),
        max_fichiers=int(args.get("max_files") or 15),
    )
    rendu["verificateur"] = verificateur
    return rendu


HANDLERS: dict[str, object] = {
    "get_diagnostics": _h_get_diagnostics,
}
