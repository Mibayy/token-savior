#!/usr/bin/env python3
"""Mesure la qualite du rappel memoire sur un corpus reel, de facon rejouable.

Pourquoi ce fichier existe. Le 27/07/2026, trois bancs de mesure ont ete
construits dans la journee pour diagnostiquer le rappel, et les trois ont ete
jetes dans /tmp. Les chiffres ont servi une fois, personne n'a pu les rejouer,
et le prochain audit serait reparti de zero. Une mesure qu'on ne peut pas
refaire n'est pas une mesure, c'est une anecdote.

Ce que le script mesure, et pourquoi ces trois-la :

  trouve@N    l'observation cible est-elle dans les N premiers resultats
  rang moyen  a quelle place, quand elle y est
  registre    la MEME cible, interrogee de deux facons differentes

Le registre est le point important, et c'est celui qu'on rate. Une requete
formulee avec les mots du document trouve toujours : elle demande au moteur de
retrouver un mot qu'on vient de lui donner. Une requete formulee autrement est
la seule qui dise quelque chose. Le script construit donc systematiquement les
deux, et affiche l'ecart.

    --mode titre     requetes baties sur le TITRE de la cible. BIAISE, garde
                     comme temoin : c'est la mesure flatteuse.
    --mode contenu   requetes baties sur des mots du CONTENU absents du titre.
                     C'est celle qui compte.

Mesure de reference du 27/07 sur 45 observations reelles, mode contenu :

    avant correction   39/45 trouvees, rang moyen 2,08
    apres              45/45 trouvees, rang moyen 1,00

Usage :

    python scripts/eval_rappel.py --projet /root/token-savior
    python scripts/eval_rappel.py --projet X --mode titre --n 60
    python scripts/eval_rappel.py --projet X --json > mesure.json

Voir aussi `sweep_floor.py` d'andrebrait (issue #92), qui mesure l'autre
moitie du probleme : le SEUIL de distance vectorielle, sur un corpus etiquete
qu'on fournit. Les deux se completent -- celui-ci dit si la bonne reponse
sort, celui-la dit ou couper la bande de bruit.
"""
from __future__ import annotations

import argparse
import json
import random
import re
import sqlite3
import sys
from pathlib import Path

# Mots de liaison, pour fabriquer une question humaine plutot qu'une liste de
# mots-cles. C'est justement ce que la recherche encaissait mal.
LIAISON = [
    "comment on gere",
    "qu'est-ce qu'on avait dit sur",
    "rappelle-moi",
    "je cherche ce qu'on sait de",
    "on fait quoi quand",
]

STOP = {
    "que", "qui", "les", "des", "une", "aux", "pour", "avec", "dans", "sur",
    "par", "est", "sont", "the", "and", "for", "with", "this", "that", "you",
    "are", "how", "what", "can", "will", "from", "plutot", "avant", "apres",
    "pas", "ne", "en", "le", "la", "de", "du", "au", "un", "il", "elle",
    "ce", "cette", "ces", "son", "sa", "ses", "leur", "nos", "vos",
}


def jetons(texte: str, longueur_min: int = 4) -> list[str]:
    return [t for t in re.findall(rf"[A-Za-zÀ-ÿ0-9_]{{{longueur_min},}}", texte or "")
            if t.lower() not in STOP]


def construire_jeu(db: Path, projet: str, n: int, mode: str, graine: int) -> list[dict]:
    """Tire n observations et fabrique deux requetes par cible.

    En mode ``contenu``, les mots de la requete viennent du contenu et sont
    absents du titre : aucune jambe ne part avec un avantage offert. En mode
    ``titre``, ils viennent du titre -- c'est le temoin biaise, utile pour
    montrer l'ecart, jamais pour conclure.
    """
    conn = sqlite3.connect(str(db))
    conn.row_factory = sqlite3.Row
    lignes = conn.execute(
        "SELECT id, title, content FROM observations "
        "WHERE archived = 0 AND project_root = ? AND LENGTH(title) > 20 "
        "  AND LENGTH(content) > 80 ORDER BY id",
        (projet,),
    ).fetchall()
    conn.close()
    if not lignes:
        return []

    alea = random.Random(graine)
    cas: list[dict] = []
    for r in alea.sample(list(lignes), min(len(lignes), n * 3)):
        if mode == "titre":
            source = jetons(r["title"])
        else:
            mots_titre = {t.lower() for t in jetons(r["title"])}
            source = [t for t in jetons(r["content"]) if t.lower() not in mots_titre]
            # Les plus longs d'abord : plus distinctifs, moins de hasard.
            source = sorted(set(source), key=len, reverse=True)
        if len(source) < 5:
            continue
        cas.append({
            "id": r["id"],
            "titre": r["title"],
            "courte": " ".join(source[:4]),
            "longue": f"{alea.choice(LIAISON)} {' '.join(source[:8])} sur ce projet",
        })
        if len(cas) >= n:
            break
    return cas


def mesurer(cas: list[dict], projet: str, limite: int) -> dict:
    from token_savior import memory_db

    resultat = {}
    for forme in ("courte", "longue"):
        rangs: list[int] = []
        trouves = 0
        for c in cas:
            ids = [o["id"] for o in memory_db.observation_search(projet, c[forme], limit=limite)]
            if c["id"] in ids:
                trouves += 1
                rangs.append(ids.index(c["id"]) + 1)
        resultat[forme] = {
            "trouve": trouves,
            "sur": len(cas),
            "taux": round(100.0 * trouves / len(cas), 1) if cas else 0.0,
            "rang_moyen": round(sum(rangs) / len(rangs), 2) if rangs else None,
        }
    return resultat


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--projet", required=True, help="project_root a mesurer")
    p.add_argument("--db", default=str(Path.home() / ".local/share/token-savior/memory.db"))
    p.add_argument("--mode", choices=("contenu", "titre", "les-deux"), default="contenu",
                   help="contenu = non biaise (defaut) ; titre = temoin flatteur ; "
                        "les-deux = les deux dans le meme run")
    p.add_argument("--n", type=int, default=45)
    p.add_argument("--limite", type=int, default=10)
    p.add_argument("--graine", type=int, default=11)
    p.add_argument("--json", action="store_true")
    a = p.parse_args(argv)

    db = Path(a.db)
    if not db.exists():
        print(f"base introuvable : {db}", file=sys.stderr)
        return 2

    modes = ("contenu", "titre") if a.mode == "les-deux" else (a.mode,)
    resultats: dict[str, dict] = {}
    for mode in modes:
        cas = construire_jeu(db, a.projet, a.n, mode, a.graine)
        if not cas:
            print(f"aucune observation exploitable pour {a.projet!r} dans {db} "
                  f"(mode {mode})", file=sys.stderr)
            return 2
        resultats[mode] = {
            "projet": a.projet, "mode": mode, "cas": len(cas),
            "limite": a.limite, "graine": a.graine,
            "mesure": mesurer(cas, a.projet, a.limite),
        }

    if a.json:
        # Un mode seul garde sa forme : `modes` n'apparait qu'en les-deux, pour
        # ne pas casser ce qui lit deja la sortie a plat.
        charge = ({"projet": a.projet, "limite": a.limite, "graine": a.graine,
                   "modes": resultats}
                  if a.mode == "les-deux" else resultats[a.mode])
        print(json.dumps(charge, ensure_ascii=False, indent=2))
        return 0

    print(f"  projet : {a.projet}")
    for mode, bloc in resultats.items():
        mesure = bloc["mesure"]
        print(f"  mode   : {mode}"
              + ("  (BIAISE, temoin seulement)" if mode == "titre" else ""))
        print(f"  cas    : {bloc['cas']} observations, fenetre {a.limite}\n")
        for forme in ("courte", "longue"):
            d = mesure[forme]
            rang = f"{d['rang_moyen']:.2f}" if d["rang_moyen"] else "-"
            print(f"    requete {forme:7} : {d['trouve']:3}/{d['sur']} ({d['taux']:5.1f} %)"
                  f"   rang moyen {rang}")
        ecart = (mesure["courte"]["taux"] - mesure["longue"]["taux"])
        if ecart > 5:
            print(f"\n  ECART de {ecart:.1f} points entre mots-cles et question humaine.")
            print("  C'est le symptome a surveiller : le moteur repond aux mots-cles")
            print("  et pas aux questions.")
        print()

    if a.mode == "les-deux":
        honnete = resultats["contenu"]["mesure"]["courte"]["taux"]
        temoin = resultats["titre"]["mesure"]["courte"]["taux"]
        print(f"  Ecart temoin - honnete : {temoin - honnete:+.1f} points.")
        print("  C'est ce que vaudrait la mesure si les requetes etaient baties")
        print("  sur le titre de leur propre cible. A lire, jamais a citer.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
