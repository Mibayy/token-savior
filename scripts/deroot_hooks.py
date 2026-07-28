#!/usr/bin/env python3
"""Retire les chemins machine-specifiques des scripts de hooks livres.

Ces scripts partent dans la roue PyPI. Depuis la v4.11.0 `ts init` les
installe pour tout le monde, or ils pointaient tous vers l'arborescence d'une
seule machine : l'interpreteur d'un venv precis, un checkout source precis, un
repertoire de donnees precis. Chez quiconque d'autre, ils ne font rien -- en
silence, ce qui est le pire des modes de panne.

Cinq motifs, tous remplaces par une resolution a l'execution :

  /root/.local/token-savior-venv/bin/python3  -> $TS_PY
  /root/token-savior/src                      -> $TS_SRC (vide si pip install)
  /root/token-savior/scripts/X.py             -> $TS_SCRIPTS/X.py
  /root/.local/share/token-savior             -> $TS_DATA
  /root/memory-backup                         -> $TS_BACKUP

Le preambule est idempotent : relancer ce script ne l'ajoute pas deux fois.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

HOOKS = Path(__file__).resolve().parents[1] / "hooks"

MARQUEUR = "# --- resolution des chemins (voir scripts/deroot_hooks.py) ---"

PREAMBULE = f'''{MARQUEUR}
# Aucun chemin en dur : ces scripts sont livres dans la roue PyPI et doivent
# fonctionner sur la machine de l'utilisateur, pas sur celle de l'auteur.
TS_DATA="${{TOKEN_SAVIOR_DATA_DIR:-${{XDG_DATA_HOME:-$HOME/.local/share}}/token-savior}}"
# shellcheck disable=SC2034  # preambule partage : chaque hook n en consomme qu un sous-ensemble
TS_BACKUP="${{TOKEN_SAVIOR_BACKUP_DIR:-$TS_DATA/memory-backup}}"
# Interpreteur : celui qui sait importer token_savior. Un venv dedie l'emporte
# s'il est declare, sinon on prend le python du PATH.
# La sonde `python3 -c "import token_savior"` demarre un interpreteur complet,
# mesure a ~127 ms sur ce VPS, et elle etait payee a CHAQUE appel de hook, donc
# a chaque outil utilise par l'agent. Le resultat est desormais memorise, et
# invalide des que le binaire python change (chemin + mtime + taille).
if [ -n "${{TOKEN_SAVIOR_PYTHON:-}}" ]; then
  TS_PY="$TOKEN_SAVIOR_PYTHON"
else
  _ts_py_bin="$(command -v python3 2>/dev/null)"
  if [ -z "$_ts_py_bin" ]; then
    TS_PY="${{TS_PY:-python3}}"
  else
    _ts_cache="${{XDG_CACHE_HOME:-$HOME/.cache}}/token-savior/interpreteur"
    _ts_sig="$_ts_py_bin:$(stat -c '%Y:%s' "$_ts_py_bin" 2>/dev/null || echo 0)"
    if [ -r "$_ts_cache" ] && IFS='|' read -r _c_sig _c_py < "$_ts_cache" 2>/dev/null \
       && [ "$_c_sig" = "$_ts_sig" ] && [ -n "$_c_py" ]; then
      TS_PY="$_c_py"
    else
      if "$_ts_py_bin" -c "import token_savior" 2>/dev/null; then
        TS_PY="$_ts_py_bin"
      else
        TS_PY="${{TS_PY:-python3}}"
      fi
      mkdir -p "$(dirname "$_ts_cache")" 2>/dev/null \
        && printf '%s|%s\n' "$_ts_sig" "$TS_PY" > "$_ts_cache" 2>/dev/null || true
    fi
    unset _ts_py_bin _ts_cache _ts_sig _c_sig _c_py
  fi
fi
# Checkout source : utile en developpement seulement. Apres `pip install`,
# token_savior est importable sans rien ajouter a sys.path.
# shellcheck disable=SC2034  # preambule partage : chaque hook n en consomme qu un sous-ensemble
TS_SRC="${{TOKEN_SAVIOR_SRC:-}}"
# shellcheck disable=SC2034  # preambule partage : chaque hook n en consomme qu un sous-ensemble
TS_SCRIPTS="${{TOKEN_SAVIOR_SCRIPTS:-$(cd "$(dirname "${{BASH_SOURCE[0]:-$0}}")/.." 2>/dev/null && pwd)/scripts}}"
{MARQUEUR.replace('---', '--- fin ---')}
'''

REMPLACEMENTS = [
    ("/root/.local/token-savior-venv/bin/python3", '$TS_PY'),
    ("/root/token-savior/scripts/", '$TS_SCRIPTS/'),
    ("/root/token-savior/src", '$TS_SRC'),
    ("/root/.local/share/token-savior", '$TS_DATA'),
    ("/root/memory-backup", '$TS_BACKUP'),
]


def insere_preambule(texte: str) -> str:
    if MARQUEUR in texte:
        return texte
    lignes = texte.split("\n")
    i = 1 if lignes and lignes[0].startswith("#!") else 0
    while i < len(lignes) and (lignes[i].startswith("#") or not lignes[i].strip()):
        i += 1
    return "\n".join(lignes[:i] + PREAMBULE.split("\n") + lignes[i:])


def main() -> int:
    total = 0
    for f in sorted(HOOKS.glob("*.sh")):
        src = f.read_text(encoding="utf-8")
        if "/root/" not in src:
            continue
        out = insere_preambule(src)
        for avant, apres in REMPLACEMENTS:
            n = out.count(avant)
            if n:
                out = out.replace(avant, apres)
                total += n
        f.write_text(out, encoding="utf-8")
        restants = len(re.findall(r"/root/", out))
        print(f"  {f.name:28} reste {restants} occurrence(s) de /root/")
    print(f"\n{total} chemins remplaces")
    reste = sum(len(re.findall(r"/root/", p.read_text(encoding='utf-8')))
                for p in HOOKS.glob("*.sh"))
    print(f"total restant dans hooks/*.sh : {reste}")
    return 1 if reste else 0


if __name__ == "__main__":
    sys.exit(main())
