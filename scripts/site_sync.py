"""Injecte les chiffres vrais dans la page publiee, pour qu'elle ne puisse plus mentir.

Constat du 26/07 : le site annoncait v4.0 a v4.3 alors que le paquet etait en
4.10.0. Sept versions mineures d'ecart, parce que chaque nombre etait ecrit en
dur dans le HTML et qu'il fallait penser a le mettre a jour. Personne n'y pense.

Ici les chiffres sont lus depuis la seule source de verite (pyproject, le
manifeste d'outils, le CHANGELOG) et injectes entre des marqueurs. Le workflow
Pages appelle ce script avant de publier, et `--check` echoue si la page a
derive, ce qui transforme une derive silencieuse en build rouge.

Marqueur attendu dans le HTML :
    <!--ts:version-->4.10.0<!--/ts:version-->

Usage :
    python3 scripts/site_sync.py            # reecrit la page
    python3 scripts/site_sync.py --check    # echoue si elle a derive
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PAGE = ROOT / "docs" / "index.html"


def _version() -> str:
    m = re.search(r'^version\s*=\s*"([^"]+)"', (ROOT / "pyproject.toml").read_text(encoding="utf-8"), re.M)
    return m.group(1) if m else "?"


def _tool_count() -> str:
    """Nombre d'outils reellement exposes, pas un chiffre recopie."""
    sys.path.insert(0, str(ROOT / "src"))
    try:
        from token_savior.tool_schemas import TOOL_SCHEMAS
        return str(len(TOOL_SCHEMAS))
    except Exception:
        return "?"


def _latest_entry() -> str:
    """Titre de la derniere entree du CHANGELOG, sans le prefixe de version."""
    for line in (ROOT / "CHANGELOG.md").read_text(encoding="utf-8").splitlines():
        if line.startswith("## "):
            m = re.match(r"##\s+v?[\d.]+\s*[—-]\s*(.+?)\s*\(", line)
            return m.group(1) if m else line[3:].strip()
    return ""


# Le meme chiffre apparait a plusieurs endroits de la page. Le premier
# correctif n'en avait marque qu'un : le site continuait d'annoncer 59 outils
# ailleurs. Chaque occurrence a son marqueur, sinon la verification passe au
# vert sur une page encore fausse.
FIELDS = {
    "version": _version,
    "tools": _tool_count,
    "tools2": _tool_count,
    "latest": _latest_entry,
}


def sync(check: bool = False) -> int:
    html = PAGE.read_text(encoding="utf-8")
    drift, missing = [], []

    for name, getter in FIELDS.items():
        want = getter()
        pattern = re.compile(rf"(<!--ts:{name}-->)(.*?)(<!--/ts:{name}-->)", re.DOTALL)
        found = pattern.search(html)
        if not found:
            missing.append(name)
            continue
        if found.group(2) != want:
            drift.append(f"{name}: page={found.group(2)!r} source={want!r}")
        html = pattern.sub(lambda m, w=want: m.group(1) + w + m.group(3), html)

    if missing:
        print("marqueurs absents de la page : " + ", ".join(missing), file=sys.stderr)
        return 2
    if check:
        if drift:
            print("La page a derive par rapport au code :", file=sys.stderr)
            for d in drift:
                print("  " + d, file=sys.stderr)
            print("Lancer `python3 scripts/site_sync.py` et committer.", file=sys.stderr)
            return 1
        print("Page a jour.")
        return 0

    PAGE.write_text(html, encoding="utf-8")
    print("Page synchronisee :")
    for name, getter in FIELDS.items():
        print(f"  {name:8} = {getter()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(sync(check="--check" in sys.argv))
