#!/usr/bin/env python3
"""Hook PreToolUse — borne une lecture de fichier qui n'a pas demande de borne.

Pourquoi ici et pas en PostToolUse. Un hook PostToolUse ne peut qu'AJOUTER du
contexte : la sortie de l'outil est deja partie vers le modele quand il
s'execute. C'est pour ca que la capture (`tool_capture_hook.py`) preserve du
contenu a travers une compaction mais n'economise pas un seul jeton dans le
tour courant. Seul PreToolUse peut changer ce qui sera renvoye, en reecrivant
l'appel avant qu'il parte. Toute economie reelle passe par la.

Ce que fait ce hook. Si `Read` vise un fichier texte long et que l'appelant
n'a fixe ni `limit` ni `offset`, il injecte `limit` et explique dans quel
outil aller chercher la suite. Il ne refuse jamais rien : un hook bloquant a
ete retire de ce depot le 27/07/2026 parce qu'il cassait plus qu'il ne
sauvait. Reecrire n'est pas bloquer.

Mesure qui justifie le hook (audit du 09/08/2026, 400 transcrits,
`scripts/audit_budget.py`) : la lecture de code represente 26,6 % des jetons
rendus au modele, deuxieme poste apres les sorties de commande. La moyenne est
de 9,2 Ko par lecture de `.ts` et 4,7 Ko par `.py`, soit bien plus que ce
qu'une question precise demande.

Ce qu'il ne fait PAS, et pourquoi. Il ne touche pas aux images, qui pesent
pourtant 21,6 % du budget. Une image est facturee a la surface, apres le
redimensionnement que l'API applique deja au-dela d'environ 1 150 kilopixels :
reduire un PNG de 4 Mo a sa version 1 568 px ne change donc PAS son cout en
jetons, seulement son poids sur le reseau. La seule vraie economie serait de
rogner la zone utile ou de rendre l'arbre d'accessibilite au lieu du pixel --
deux decisions que ce hook n'a pas les moyens de prendre a la place de
l'appelant.

Variables d'environnement :

  TS_READ_GUARD          "0" pour desactiver (ACTIF par defaut, voir plus bas)
  TS_READ_MAX_LINES      plafond injecte, en lignes (defaut 400)
  TS_READ_MIN_LINES      en dessous, on ne touche a rien (defaut 600)
  TS_READ_GUARD_LOG      chemin optionnel ; une ligne JSON par reecriture

Actif par defaut, contrairement a la convention du depot. Le contre-exemple
est dans le meme dossier : `bash_rewriter_hook.py` est livre desactive depuis
des mois, il est installe sur cette machine, et il n'a jamais reecrit une
seule commande. Un economiseur eteint economise zero.
"""
from __future__ import annotations

import json
import os
import sys

# Extensions dont une lecture integrale se remplace par une navigation
# structurelle. Volontairement limite au code : un .md ou un .json de config
# se lit souvent en entier pour de bonnes raisons.
EXT_CODE = {
    ".py", ".ts", ".tsx", ".js", ".jsx", ".swift", ".go", ".rs", ".java",
    ".rb", ".php", ".cs", ".c", ".h", ".cpp", ".kt", ".scala", ".m", ".mm",
}

PLAFOND_DEFAUT = 400
PLANCHER_DEFAUT = 600


def _passer() -> None:
    """Payload neutre : Claude Code garde l'appel tel quel."""
    print(json.dumps({"continue": True}), flush=True)


def _journaliser(entree: dict) -> None:
    chemin = os.environ.get("TS_READ_GUARD_LOG")
    if not chemin:
        return
    try:
        with open(chemin, "a", encoding="utf-8") as f:
            f.write(json.dumps(entree, default=str) + "\n")
    except OSError:
        pass


def _compter_lignes(chemin: str, plafond_utile: int) -> int | None:
    """Nombre de lignes, ou None si le fichier n'est pas lisible en texte.

    S'arrete des qu'on a depasse `plafond_utile` : on n'a pas besoin du
    compte exact d'un fichier de 40 000 lignes pour decider de le borner.
    """
    try:
        n = 0
        with open(chemin, "rb") as f:
            for _ in f:
                n += 1
                if n > plafond_utile:
                    return n
        return n
    except (OSError, ValueError):
        return None


def main() -> None:
    if os.environ.get("TS_READ_GUARD", "1") == "0":
        _passer()
        return
    try:
        evenement = json.load(sys.stdin)
    except (ValueError, OSError):
        _passer()
        return

    if evenement.get("tool_name") not in {"Read", "read_file", "ReadFile"}:
        _passer()
        return

    entree = evenement.get("tool_input") or {}
    if not isinstance(entree, dict):
        _passer()
        return

    chemin = entree.get("file_path") or entree.get("path") or ""
    if not chemin or not isinstance(chemin, str):
        _passer()
        return

    # Une borne explicite est une decision de l'appelant : on ne la remplace
    # jamais. Un `offset` seul compte aussi -- il signale une lecture ciblee.
    if entree.get("limit") or entree.get("offset"):
        _passer()
        return

    extension = os.path.splitext(chemin)[1].lower()
    if extension not in EXT_CODE:
        _passer()
        return

    try:
        plafond = int(os.environ.get("TS_READ_MAX_LINES", PLAFOND_DEFAUT))
        plancher = int(os.environ.get("TS_READ_MIN_LINES", PLANCHER_DEFAUT))
    except ValueError:
        plafond, plancher = PLAFOND_DEFAUT, PLANCHER_DEFAUT

    lignes = _compter_lignes(chemin, plancher)
    if lignes is None or lignes <= plancher:
        _passer()
        return

    nom = os.path.basename(chemin)
    raison = (
        f"[token-savior:read-guard] {nom} fait {lignes}+ lignes ; lecture bornee "
        f"aux {plafond} premieres. Pour la suite, viser au lieu de tout lire : "
        f"read_lines(file_path='{chemin}', start=N, end=M) si vous tenez un "
        f"numero de ligne, get_function_source(name=...) si vous tenez un nom, "
        f"get_full_context(name=...) si vous voulez aussi les appelants. "
        f"Read(offset=..., limit=...) reste accepte tel quel."
    )
    _journaliser({"fichier": chemin, "lignes": lignes, "plafond": plafond})
    print(json.dumps({
        "continue": True,
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "allow",
            "permissionDecisionReason": raison,
            "updatedInput": {**entree, "limit": plafond},
        },
    }), flush=True)


if __name__ == "__main__":
    main()
