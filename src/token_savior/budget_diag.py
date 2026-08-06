"""Ou part le budget d'une reponse. Eteint par defaut, muet quand il l'est.

Manque identifie le 06/08/2026 en lisant `explore-budget-allocation.md` de
`colbymchenry/codegraph`. On pouvait lire une reponse de Token Savior et
deviner ; on ne pouvait pas dire « cet appel a rendu 12 Ko dont 3 Ko de
troncature annoncee, contre 47 Ko qu'une lecture naive aurait coute ».

C'est l'instrument qui manquait pour deux choses ecrites dans nos propres
notes : la telemetrie a sous-compte de 47 a 81 % sans que rien ne le signale,
et le bench ne se compare qu'a « sans Token Savior ». On ne repare pas ce
qu'on ne mesure pas.

**La propriete qui compte : eteint, il ne change pas un octet.** Un diagnostic
qui decale la sortie d'un seul caractere invalide toute mesure A/B prise avec
lui. `demarrer()` rend None si la variable n'est pas posee, et tous les points
d'appel sont alors des `diag?.`-equivalents (`if diag is not None`). Pinne par
`tests/test_budget_diag.py`.

Activation :

    TS_BUDGET_DIAG=1                 -> une ligne lisible par appel sur stderr
    TS_BUDGET_DIAG=json              -> un objet JSON par appel sur stderr
    TS_BUDGET_DIAG=/chemin/f.jsonl   -> un objet JSON par ligne, ajoute au fichier

Non pose, ou `0`/`off`/`false`/`no`/vide : eteint.
"""

from __future__ import annotations

import json
import os
import sys
import time
from dataclasses import dataclass, field
from typing import Any

_ETEINT = {"", "0", "off", "false", "no"}


@dataclass
class RapportAppel:
    """Ce qu'un appel a coute, et ce qu'il aurait coute sans Token Savior."""

    outil: str
    projet: str
    octets_rendus: int = 0
    octets_naifs: int = 0
    tronque: bool = False
    raison_troncature: str = ""
    bornes: dict[str, Any] = field(default_factory=dict)
    ms: float = 0.0

    @property
    def economie(self) -> int:
        """Octets non envoyes. Negatif si l'outil a coute plus cher que la lecture."""
        return self.octets_naifs - self.octets_rendus

    @property
    def part_economisee(self) -> float:
        if self.octets_naifs <= 0:
            return 0.0
        return self.economie / self.octets_naifs

    def en_dict(self) -> dict[str, Any]:
        return {
            "outil": self.outil,
            "projet": self.projet,
            "octets_rendus": self.octets_rendus,
            "octets_naifs": self.octets_naifs,
            "economie": self.economie,
            "part_economisee": round(self.part_economisee, 4),
            "tronque": self.tronque,
            "raison_troncature": self.raison_troncature,
            "bornes": self.bornes,
            "ms": round(self.ms, 2),
        }

    def en_ligne(self) -> str:
        part = f"{self.part_economisee * 100:5.1f}%"
        coupe = f" TRONQUE({self.raison_troncature})" if self.tronque else ""
        return (
            f"[budget] {self.outil:<26} rendu={self.octets_rendus:>7}o "
            f"naif={self.octets_naifs:>8}o economie={part} "
            f"{self.ms:>6.1f}ms{coupe}"
        )


class Diagnostic:
    """Un enregistreur par appel. N'existe que si la variable est posee."""

    def __init__(self, sortie: str) -> None:
        self._sortie = sortie
        self._t0 = time.perf_counter()

    def rapporter(self, rapport: RapportAppel) -> None:
        rapport.ms = (time.perf_counter() - self._t0) * 1000.0
        try:
            if self._sortie == "stderr":
                sys.stderr.write(rapport.en_ligne() + "\n")
            elif self._sortie == "json":
                sys.stderr.write(json.dumps(rapport.en_dict(), ensure_ascii=False) + "\n")
            else:
                with open(self._sortie, "a", encoding="utf-8") as f:
                    f.write(json.dumps(rapport.en_dict(), ensure_ascii=False) + "\n")
        except Exception:
            # Un instrument de mesure qui fait tomber la mesure ne sert a rien.
            pass


def demarrer(env: dict[str, str] | None = None) -> Diagnostic | None:
    """Rend un enregistreur, ou **None** si le diagnostic est eteint.

    None est le cas normal. Tous les appelants doivent tester `is not None`
    avant de construire quoi que ce soit : construire un rapport pour le jeter
    couterait du temps sur le chemin chaud, et ce chemin est celui de chaque
    appel d'outil.
    """
    valeur = (env if env is not None else os.environ).get("TS_BUDGET_DIAG", "")
    v = valeur.strip()
    if v.lower() in _ETEINT:
        return None
    if v.lower() in {"1", "true", "on", "yes", "stderr"}:
        return Diagnostic("stderr")
    if v.lower() == "json":
        return Diagnostic("json")
    return Diagnostic(v)  # tout le reste est traite comme un chemin de fichier


def lire_journal(chemin: str) -> list[dict[str, Any]]:
    """Relit un journal JSONL. Une ligne illisible est sautee, pas fatale."""
    out: list[dict[str, Any]] = []
    try:
        with open(chemin, encoding="utf-8") as f:
            for ligne in f:
                ligne = ligne.strip()
                if not ligne:
                    continue
                try:
                    out.append(json.loads(ligne))
                except ValueError:
                    continue
    except OSError:
        return []
    return out


def resumer(appels: list[dict[str, Any]]) -> dict[str, Any]:
    """Agrege un journal : total, par outil, et les appels a economie negative.

    Les appels a economie negative sont sortis a part exprès. Ce sont ceux ou
    Token Savior a coute plus cher qu'une lecture naive ; noyes dans une
    moyenne, ils sont invisibles, et ce sont eux qui disent quel outil regler.
    """
    if not appels:
        return {"appels": 0, "octets_rendus": 0, "octets_naifs": 0,
                "part_economisee": 0.0, "par_outil": [], "economie_negative": []}

    rendus = sum(a.get("octets_rendus", 0) for a in appels)
    naifs = sum(a.get("octets_naifs", 0) for a in appels)

    par_outil: dict[str, dict[str, Any]] = {}
    for a in appels:
        e = par_outil.setdefault(
            a.get("outil", "?"),
            {"outil": a.get("outil", "?"), "appels": 0, "rendus": 0, "naifs": 0, "tronques": 0},
        )
        e["appels"] += 1
        e["rendus"] += a.get("octets_rendus", 0)
        e["naifs"] += a.get("octets_naifs", 0)
        e["tronques"] += 1 if a.get("tronque") else 0
    for e in par_outil.values():
        e["part_economisee"] = round((e["naifs"] - e["rendus"]) / e["naifs"], 4) if e["naifs"] else 0.0

    negatifs = [a for a in appels if a.get("economie", 0) < 0]

    return {
        "appels": len(appels),
        "octets_rendus": rendus,
        "octets_naifs": naifs,
        "part_economisee": round((naifs - rendus) / naifs, 4) if naifs else 0.0,
        "par_outil": sorted(par_outil.values(), key=lambda e: -e["rendus"]),
        "economie_negative": sorted(negatifs, key=lambda a: a.get("economie", 0))[:20],
    }
