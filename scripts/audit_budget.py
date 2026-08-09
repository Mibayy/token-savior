#!/usr/bin/env python3
"""Ou part le budget de jetons d'une session, par categorie de source.

Le tableau de bord d'economies mesure ce que Token Savior fait gagner. Il ne
dit pas ce qu'il ne touche pas, et c'est justement la question quand on se
demande quoi construire ensuite. Cet audit lit les transcrits d'un client
(Claude Code par defaut) et repartit les jetons REMONTES AU MODELE entre les
sources qui les produisent.

Ce qu'il repond : « si je veux economiser, ou dois-je regarder ». Ce qu'il ne
repond pas : « combien j'ai deja economise » (c'est `ts stats`).

Mesure du 09/08/2026 sur 400 transcrits de ce VPS, ~5,75 M de jetons rendus :

    Bash (sorties de commande)   30,0 %
    Read de code                 26,5 %
    Read d'images / PDF          21,5 %
    Read de doc / config          9,6 %
    Token Savior                  4,7 %
    Web (search + fetch)          5,2 %

Deux approximations assumees, ecrites ici pour qu'on ne les oublie pas en
lisant le tableau :

* Le texte est compte en caracteres / 4. C'est la regle de pouce usuelle, a
  +/- 15 % selon la langue et la densite de ponctuation.
* Une image est comptee `--jetons-image` (1 500 par defaut), une borne HAUTE
  pour une capture d'ecran de taille courante. Un chiffre exact demanderait
  les dimensions de chaque image, que le transcrit ne porte pas. Si la part
  des images decide d'un chantier, la mesurer pour de vrai avant.

Usage::

    python scripts/audit_budget.py                    # 200 transcrits recents
    python scripts/audit_budget.py --transcrits 400
    python scripts/audit_budget.py --json             # pour un suivi dans le temps
"""
from __future__ import annotations

import argparse
import collections
import glob
import json
import os

# Extensions traitees comme du code source : ce sont celles qu'un index
# structurel peut servir a la place d'une lecture brute.
EXT_CODE = {
    ".py", ".ts", ".tsx", ".js", ".jsx", ".swift", ".go", ".rs", ".java",
    ".rb", ".php", ".cs", ".c", ".h", ".cpp", ".kt", ".scala",
}
EXT_IMAGE = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp", ".pdf", ".heic"}

# Un nom d'outil shell par client. Une egalite stricte sur "Bash" rendait
# l'audit aveugle partout ailleurs -- meme piege que dans le hook de capture.
OUTILS_SHELL = {
    "Bash", "run_shell_command", "shell", "local_shell", "execute_command",
    "terminal", "RunCommand",
}
OUTILS_LECTURE = {"Read", "read_file", "ReadFile"}
OUTILS_WEB = {"WebSearch", "WebFetch", "web_fetch", "web_search"}


def _categorie(nom_outil: str, chemin: str) -> str:
    """Categorie de budget d'un appel, d'apres l'outil et sa cible."""
    if nom_outil.startswith("mcp__token-savior"):
        return "Token Savior"
    if nom_outil in OUTILS_SHELL:
        return "Bash (sorties de commande)"
    if nom_outil in OUTILS_LECTURE:
        ext = os.path.splitext(chemin or "")[1].lower()
        if ext in EXT_IMAGE:
            return "Read d'images / PDF"
        if ext in EXT_CODE:
            return "Read de code"
        return "Read de doc / config"
    if nom_outil in OUTILS_WEB:
        return "Web (search + fetch)"
    if nom_outil in {"Grep", "Glob", "search_file_content"}:
        return "Grep / Glob"
    if nom_outil.startswith("mcp__"):
        return "autres MCP"
    return f"{nom_outil}"


def _taille(contenu: object) -> int:
    if isinstance(contenu, str):
        return len(contenu)
    try:
        return len(json.dumps(contenu, default=str))
    except Exception:
        return len(str(contenu))


def collecter(fichiers: list[str], jetons_image: int) -> dict:
    """Parcourt des transcrits JSONL et repartit les jetons rendus."""
    jetons: collections.Counter = collections.Counter()
    appels: collections.Counter = collections.Counter()
    # tool_use et tool_result vivent dans deux messages differents : on garde
    # le nom et la cible de l'appel en attendant son resultat.
    en_attente: dict[str, tuple[str, str]] = {}

    for chemin_fichier in fichiers:
        try:
            with open(chemin_fichier, encoding="utf-8", errors="ignore") as fh:
                for ligne in fh:
                    try:
                        evt = json.loads(ligne)
                    except ValueError:
                        continue
                    for bloc in (evt.get("message") or {}).get("content") or []:
                        if not isinstance(bloc, dict):
                            continue
                        if bloc.get("type") == "tool_use":
                            entree = bloc.get("input") or {}
                            cible = ""
                            if isinstance(entree, dict):
                                cible = (
                                    entree.get("file_path")
                                    or entree.get("notebook_path")
                                    or entree.get("path")
                                    or ""
                                )
                            en_attente[bloc.get("id")] = (bloc.get("name", "?"), cible)
                        elif bloc.get("type") == "tool_result":
                            attendu = en_attente.pop(bloc.get("tool_use_id"), None)
                            if attendu is None:
                                continue
                            nom, cible = attendu
                            cat = _categorie(nom, cible)
                            if cat == "Read d'images / PDF":
                                jetons[cat] += jetons_image
                            else:
                                jetons[cat] += _taille(bloc.get("content")) / 4
                            appels[cat] += 1
        except OSError:
            continue

    return {"jetons": jetons, "appels": appels}


def rendre_texte(res: dict, nb_fichiers: int, jetons_image: int) -> str:
    jetons, appels = res["jetons"], res["appels"]
    total = sum(jetons.values())
    if total <= 0:
        return "Aucun appel d'outil trouve dans ces transcrits."
    lignes = [
        (
            f"Jetons rendus au modele — {nb_fichiers} transcrits, "
            f"{sum(appels.values())} appels, ~{total / 1e6:.2f} M jetons"
        ),
        f"(texte = caracteres / 4 ; image = {jetons_image}, borne haute)",
        "",
        f"{'source':30} {'appels':>7} {'kilo-jetons':>12} {'part':>7}",
    ]
    for cat, val in jetons.most_common():
        lignes.append(
            f"{cat:30} {appels[cat]:7} {val / 1000:12.0f} {100 * val / total:6.1f}%"
        )
    return "\n".join(lignes)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument(
        "--dossier",
        default=os.path.expanduser("~/.claude/projects"),
        help="racine des transcrits (defaut : ~/.claude/projects)",
    )
    p.add_argument("--transcrits", type=int, default=200,
                   help="nombre de transcrits les plus recents (defaut 200)")
    p.add_argument("--jetons-image", type=int, default=1500,
                   help="cout suppose d'une image, borne haute (defaut 1500)")
    p.add_argument("--json", action="store_true", help="sortie JSON")
    args = p.parse_args(argv)

    motif = os.path.join(args.dossier, "**", "*.jsonl")
    fichiers = sorted(glob.glob(motif, recursive=True),
                      key=os.path.getmtime, reverse=True)[: args.transcrits]
    if not fichiers:
        print(f"aucun transcrit sous {args.dossier}")
        return 1

    res = collecter(fichiers, args.jetons_image)
    if args.json:
        total = sum(res["jetons"].values()) or 1
        print(json.dumps({
            "transcrits": len(fichiers),
            "jetons_image_supposes": args.jetons_image,
            "total_jetons_estimes": round(total),
            "categories": {
                cat: {
                    "appels": res["appels"][cat],
                    "jetons": round(val),
                    "part": round(100 * val / total, 2),
                }
                for cat, val in res["jetons"].most_common()
            },
        }, indent=2, ensure_ascii=False))
    else:
        print(rendre_texte(res, len(fichiers), args.jetons_image))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
