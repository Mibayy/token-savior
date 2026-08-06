"""Erreurs du verificateur de types, rendues comme des symboles.

Manque comble le 06/08/2026, identifie en decortiquant `oraios/serena`
(`GetDiagnosticsForFileTool`, `GetDiagnosticsForSymbolTool`). Token Savior
n'avait aucun moyen de dire si une edition compilait : l'agent devait lancer
un build en Bash et lire un mur de texte.

Pourquoi pas LSP. La note de decision LSP a mesure 6 s de demarrage a froid
pour un resultat que tree-sitter couvrait deja sur les tests impactes. Ici on
appelle **le verificateur que le projet utilise deja** (`tsc`, `mypy`,
`pyright`) : aucune dependance nouvelle, aucun serveur a maintenir, et la
verite vient de l'outil auquel l'equipe fait deja confiance.

Ce que ca apporte face a un `npx tsc` lance en Bash :
- sortie bornee et structuree plutot qu'un flux ;
- le **symbole englobant** de chaque erreur, qui est l'unite dans laquelle un
  agent edite ;
- un compte exact quand la borne tronque, parce qu'une borne muette se lit
  comme une absence d'erreur.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
from dataclasses import dataclass, field


@dataclass
class Diagnostic:
    fichier: str
    ligne: int
    colonne: int
    gravite: str
    code: str
    message: str
    symbole: str | None = field(default=None)


# `chemin(ligne,colonne): gravite TSxxxx: message`
_RE_TSC = re.compile(
    r"^(?P<fichier>[^(\n]+)\((?P<ligne>\d+),(?P<colonne>\d+)\):\s+"
    r"(?P<gravite>error|warning)\s+(?P<code>TS\d+):\s+(?P<message>.*)$"
)

# `chemin:ligne: gravite: message  [code]`  (la colonne est optionnelle)
_RE_MYPY = re.compile(
    r"^(?P<fichier>[^:\n]+):(?P<ligne>\d+)(?::(?P<colonne>\d+))?:\s+"
    r"(?P<gravite>error|warning|note):\s+(?P<message>.*?)(?:\s+\[(?P<code>[\w-]+)\])?$"
)


def analyser_tsc(brut: str) -> list[Diagnostic]:
    """Lit la sortie de `tsc --noEmit --pretty false`.

    Les lignes de resume (`Found 3 errors in 2 files.`) et l'echo de commande
    ne matchent pas : un diagnostic exige un couple (ligne, colonne).
    """
    trouves: list[Diagnostic] = []
    for ligne in brut.splitlines():
        m = _RE_TSC.match(ligne.strip())
        if not m:
            continue
        trouves.append(
            Diagnostic(
                fichier=m.group("fichier").strip(),
                ligne=int(m.group("ligne")),
                colonne=int(m.group("colonne")),
                gravite=m.group("gravite"),
                code=m.group("code"),
                message=m.group("message").strip(),
            )
        )
    return trouves


def analyser_mypy(brut: str) -> list[Diagnostic]:
    """Lit la sortie de `mypy`. Les `note:` sont gardees, marquees comme telles.

    Une note explique souvent l'erreur qui la precede ; la jeter obligerait
    l'agent a relancer l'outil pour comprendre ce qu'il vient de lire.
    """
    trouves: list[Diagnostic] = []
    for ligne in brut.splitlines():
        m = _RE_MYPY.match(ligne.strip())
        if not m:
            continue
        trouves.append(
            Diagnostic(
                fichier=m.group("fichier").strip(),
                ligne=int(m.group("ligne")),
                colonne=int(m.group("colonne") or 0),
                gravite=m.group("gravite"),
                code=m.group("code") or "",
                message=m.group("message").strip(),
            )
        )
    return trouves


def analyser_pyright(brut: str) -> list[Diagnostic]:
    """Lit `pyright --outputjson`. Silencieux si la sortie n'est pas du JSON."""
    try:
        charge = json.loads(brut)
    except (ValueError, TypeError):
        return []
    trouves: list[Diagnostic] = []
    for d in charge.get("generalDiagnostics", []):
        debut = (d.get("range") or {}).get("start") or {}
        trouves.append(
            Diagnostic(
                fichier=d.get("file", ""),
                ligne=int(debut.get("line", 0)) + 1,   # pyright compte a partir de 0
                colonne=int(debut.get("character", 0)) + 1,
                gravite=d.get("severity", "error"),
                code=d.get("rule", ""),
                message=(d.get("message") or "").split("\n")[0].strip(),
            )
        )
    return trouves


_COMMANDES: dict[str, list[str]] = {
    "tsc": ["npx", "--no-install", "tsc", "--noEmit", "--pretty", "false"],
    "mypy": ["mypy", "--no-color-output", "--no-error-summary", "."],
    "pyright": ["pyright", "--outputjson"],
}

_ANALYSEURS = {"tsc": analyser_tsc, "mypy": analyser_mypy, "pyright": analyser_pyright}


def detecter_verificateur(racine: str) -> str | None:
    """Quel verificateur ce projet utilise-t-il deja ?

    On ne devine pas : on lit ce que le depot declare. Un projet sans
    verificateur n'en recevra pas un de notre part -- imposer `mypy` a une base
    qui ne l'utilise pas produirait des centaines de faux positifs que personne
    n'a demandes.
    """
    if os.path.exists(os.path.join(racine, "tsconfig.json")):
        return "tsc"
    pyproject = os.path.join(racine, "pyproject.toml")
    if os.path.exists(pyproject):
        try:
            contenu = open(pyproject, encoding="utf-8", errors="replace").read()
        except OSError:
            contenu = ""
        if "[tool.mypy]" in contenu:
            return "mypy"
        if "[tool.pyright]" in contenu or "[tool.basedpyright]" in contenu:
            return "pyright"
    if os.path.exists(os.path.join(racine, "mypy.ini")):
        return "mypy"
    if os.path.exists(os.path.join(racine, "pyrightconfig.json")):
        return "pyright"
    return None


def regrouper(
    trouves: list[Diagnostic], max_par_fichier: int, max_fichiers: int
) -> dict:
    """Regroupe par fichier, borne, et **annonce ce qui a ete coupe**.

    Une borne muette se lit comme une absence d'erreur : c'est la facon la plus
    economique de faire croire a un agent que son edition compile.
    """
    par_fichier: dict[str, list[Diagnostic]] = {}
    for d in trouves:
        par_fichier.setdefault(d.fichier, []).append(d)

    # Les fichiers les plus atteints d'abord : c'est la ou l'edition a casse.
    ordonnes = sorted(par_fichier.items(), key=lambda kv: -len(kv[1]))
    fichiers = []
    for chemin, liste in ordonnes[:max_fichiers]:
        gardes = liste[:max_par_fichier]
        fichiers.append(
            {
                "fichier": chemin,
                "total": len(liste),
                "tronque": len(liste) - len(gardes),
                "diagnostics": [
                    {
                        "ligne": d.ligne,
                        "colonne": d.colonne,
                        "gravite": d.gravite,
                        "code": d.code,
                        "message": d.message,
                        **({"symbole": d.symbole} if d.symbole else {}),
                    }
                    for d in gardes
                ],
            }
        )
    return {
        "total": len(trouves),
        "fichiers_atteints": len(par_fichier),
        "fichiers_omis": max(0, len(ordonnes) - max_fichiers),
        "fichiers": fichiers,
    }


def executer(racine: str, verificateur: str, timeout: int = 180) -> tuple[str, str | None]:
    """Lance le verificateur. Rend (sortie, erreur lisible ou None).

    Un verificateur absent n'est pas une panne de Token Savior : on le dit en
    clair plutot que de rendre zero diagnostic, qui se lirait comme « tout va
    bien ».
    """
    cmd = _COMMANDES.get(verificateur)
    if not cmd:
        return "", f"verificateur inconnu : {verificateur}"
    try:
        proc = subprocess.run(
            cmd, cwd=racine, capture_output=True, text=True, timeout=timeout
        )
    except FileNotFoundError:
        return "", f"{cmd[0]} introuvable sur cette machine"
    except subprocess.TimeoutExpired:
        return "", f"{verificateur} n'a pas fini en {timeout}s"
    return (proc.stdout or "") + (proc.stderr or ""), None


def attacher_symboles(trouves: list[Diagnostic], index) -> None:
    """Renseigne `symbole` avec le symbole englobant, quand l'index le sait.

    C'est l'apport propre a Token Savior : un agent edite des symboles, pas des
    lignes. Sans ca il doit relire le fichier pour savoir quoi corriger.
    Silencieux si l'index ne connait pas le fichier -- une erreur reste utile
    sans son symbole, elle ne l'est plus si l'appel echoue.
    """
    if index is None:
        return
    for d in trouves:
        try:
            fns = index.get_functions(d.fichier) or []
        except Exception:
            continue
        for fn in fns:
            debut = fn.get("line") or fn.get("line_start") or 0
            fin = fn.get("line_end") or fn.get("end_line") or 0
            if debut and fin and debut <= d.ligne <= fin:
                d.symbole = fn.get("name")
                break
