#!/usr/bin/env python3
"""Banc local : une chaine d'outils en appels separes contre un seul ts_execute.

Ce que ce banc mesure, et surtout ce qu'il ne mesure pas.

MESURE (deterministe, reproductible, sans appeler un modele) :
  - le temps serveur de chaque forme,
  - le nombre d'allers-retours MCP,
  - le volume de charge utile rendu au modele.

NE MESURE PAS : le cout LLM du round-trip. Chaque aller-retour MCP oblige le
modele a relire son contexte pour emettre l'appel suivant ; ce cout-la depend
de la taille du contexte au moment de l'appel, pas du serveur. Il est estime
plus bas a partir d'un ordre de grandeur explicite, et presente comme une
estimation, pas comme une mesure.

Pourquoi cette precaution : la telemetrie de ce projet a deja sous-compte de 47
a 81 %, et un chiffre de banc qui melange du mesure et de l'estime finit par
etre cite comme s'il etait entierement mesure.

    python3 bench_local_chaine.py [--repetitions 5]
"""

from __future__ import annotations

import argparse
import asyncio
import statistics
import sys
import time

# La chaine testee est celle qu'un agent ecrit vraiment pour comprendre un
# symbole avant de l'editer : localiser, lire, situer dans le graphe.
CHAINE = [
    ("find_symbol", {"name": "run_script_async"}),
    ("get_function_source", {"name": "run_script_async"}),
    ("get_git_status", {}),
    ("get_structure_summary", {}),
]

SCRIPT = """
const a = await tools.find_symbol({ name: "run_script_async" });
const b = await tools.get_function_source({ name: "run_script_async" });
const c = await tools.get_git_status({});
const d = await tools.get_structure_summary({});
return { a, b, c, d };
"""


def _texte(parties) -> str:
    return "".join(p.text for p in parties if hasattr(p, "text"))


def separes() -> tuple[float, int]:
    """N appels MCP distincts. Rend (secondes, octets rendus)."""
    from token_savior.server import _dispatch_tool, _track_call

    debut = time.perf_counter()
    octets = 0
    for nom, args in CHAINE:
        record = _track_call(nom, args)
        octets += len(_texte(_dispatch_tool(nom, args, record)))
    return time.perf_counter() - debut, octets


def groupes() -> tuple[float, int]:
    """Un seul ts_execute. Rend (secondes, octets rendus)."""
    from token_savior.server import _handle_ts_execute

    debut = time.perf_counter()
    texte = _handle_ts_execute({"script": SCRIPT})
    texte = asyncio.run(texte) if asyncio.iscoroutine(texte) else texte
    return time.perf_counter() - debut, len(_texte(texte))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--repetitions", type=int, default=5)
    ap.add_argument("--projet", default="/root/token-savior")
    args = ap.parse_args()

    from token_savior import server

    server._dispatch_tool("switch_project", {"project": args.projet}, None)

    # Un tour a blanc : le premier appel paie l'indexation, pas la forme testee.
    separes()
    groupes()

    ts_sep, ts_grp, o_sep, o_grp = [], [], 0, 0
    for _ in range(args.repetitions):
        t, o = separes()
        ts_sep.append(t); o_sep = o
        t, o = groupes()
        ts_grp.append(t); o_grp = o

    ms = lambda v: [x * 1000 for x in v]
    sep, grp = ms(ts_sep), ms(ts_grp)
    med_sep, med_grp = statistics.median(sep), statistics.median(grp)

    print(f"chaine de {len(CHAINE)} outils, {args.repetitions} repetitions\n")
    print(f"  {'':18} {'min':>9} {'mediane':>9} {'max':>9} {'A-R':>5} {'octets':>9}")
    print(f"  {'appels separes':18} {min(sep):8.1f}ms {med_sep:8.1f}ms {max(sep):8.1f}ms "
          f"{len(CHAINE):5} {o_sep:9,}")
    print(f"  {'un ts_execute':18} {min(grp):8.1f}ms {med_grp:8.1f}ms {max(grp):8.1f}ms "
          f"{1:5} {o_grp:9,}")
    # Un premier appel bien plus lent que les suivants trahit un demarrage
    # amorti ; le dire, sinon la mediane cache la vraie forme du cout.
    if max(grp) > 2 * med_grp:
        print(f"\n  ATTENTION : ecart min/max de {max(grp)/min(grp):.1f}x sur ts_execute,")
        print("             le cout de demarrage du worker n'est pas amorti.")

    print("\n  MESURE : le temps serveur et le nombre d'allers-retours ci-dessus.")
    if med_grp < med_sep:
        print(f"           ts_execute est {med_sep / med_grp:.2f}x plus rapide cote serveur.")
    else:
        print(f"           ts_execute est {med_grp / med_sep:.2f}x PLUS LENT cote serveur ;")
        print("           son interet ne serait alors que le nombre d'allers-retours.")

    # L'estimation est nommee comme telle et sa base est donnee, pour qu'on
    # puisse la contester sans relire le code.
    economie = len(CHAINE) - 1
    print(f"\n  ESTIME (non mesure ici) : {economie} allers-retours evites. Chacun oblige le")
    print("           modele a relire son contexte pour emettre l'appel suivant. Le cout")
    print("           depend de la taille du contexte a cet instant et ne peut pas etre")
    print("           mesure sans lancer un vrai modele.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
