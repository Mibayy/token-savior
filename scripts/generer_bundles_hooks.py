#!/usr/bin/env python3
"""Genere les bundles de hooks du moteur memoire depuis une source unique.

Les trois bundles etaient edites a la main et avaient diverge : voir l'en-tete
de `token_savior/cli_init/vocabulaire_clients.py` pour ce qui manquait dans
chacun. Ils sont desormais derives.

    python3 scripts/generer_bundles_hooks.py            # ecrit les bundles
    python3 scripts/generer_bundles_hooks.py --verifier # code 1 si desynchro

`--verifier` est ce que lance le test de parite et la preflight : il echoue si
un bundle livre ne correspond plus a ce que declare le vocabulaire, ce qui
arrive des qu'on edite un JSON a la main au lieu de la source.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

RACINE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(RACINE / "src"))

from token_savior.cli_init.vocabulaire_clients import (
    CLIENTS,
    SCRIPTS,
    Client,
    delai,
    motif,
    outils_post_tool,
    outils_pre_tool,
)

# Nom du fichier livre pour chaque client.
FICHIERS = {
    "claude": "memory-hooks-config.json",
    "codex": "memory-codex.json",
    "gemini": "memory-gemini.json",
}

RAPPEL_LECTURE = (
    "Le mode lecture du hook pre-outil n'est pas cable par defaut : un appel "
    "coute ~197 ms et une session lit des fichiers en continu. Pour l'activer, "
    "ajouter les outils de lecture de ce client au matcher pre-outil ({outils})."
)


def construire(client: Client) -> dict:
    hooks: dict[str, list[dict]] = {}
    for canonique, nom_evenement in client.evenements.items():
        entree: dict = {}
        if canonique == "pre_tool":
            entree["matcher"] = motif(client, outils_pre_tool(client))
        elif canonique == "post_tool":
            entree["matcher"] = motif(client, outils_post_tool(client))
        entree["hooks"] = [{
            "type": "command",
            "command": "bash {{TS_HOOKS_DIR}}/" + SCRIPTS[canonique],
            "timeout": delai(client, canonique),
        }]
        hooks[nom_evenement] = [entree]

    return {
        "_comment": client.note,
        "_genere_par": ("scripts/generer_bundles_hooks.py depuis "
                        "token_savior/cli_init/vocabulaire_clients.py -- "
                        "ne pas editer a la main, la preflight le detecte"),
        "_mode_lecture": RAPPEL_LECTURE.format(outils="|".join(client.lecture)),
        "hooks": hooks,
    }


def rendu(donnees: dict) -> str:
    return json.dumps(donnees, indent=2, ensure_ascii=False) + "\n"


def main() -> int:
    parseur = argparse.ArgumentParser(description=__doc__)
    parseur.add_argument("--verifier", action="store_true",
                         help="ne rien ecrire ; code 1 si un bundle a derive")
    args = parseur.parse_args()

    dossier = RACINE / "hooks"
    derives: list[str] = []
    for nom_client, fichier in FICHIERS.items():
        attendu = rendu(construire(CLIENTS[nom_client]))
        cible = dossier / fichier
        actuel = cible.read_text(encoding="utf-8") if cible.exists() else ""
        if args.verifier:
            if actuel != attendu:
                derives.append(fichier)
            continue
        if actuel != attendu:
            cible.write_text(attendu, encoding="utf-8")
            print(f"  ecrit   {fichier}")
        else:
            print(f"  inchange {fichier}")

    if args.verifier:
        if derives:
            print("bundles desynchronises de la source : " + ", ".join(derives))
            print("corriger avec : python3 scripts/generer_bundles_hooks.py")
            return 1
        print("  bundles conformes a la source")
    return 0


if __name__ == "__main__":
    sys.exit(main())
