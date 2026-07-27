#!/bin/bash
# Pre-push checklist for Token Savior. Run BEFORE every `git push`.
# Exit non-zero if anything fails so a wrapping `&&` chain stops.
set -euo pipefail

VENV=/root/.local/token-savior-venv/bin
cd "$(dirname "$0")/.."

# `scripts/` ajoute le 27/07/2026. Il n'etait linte nulle part, ni ici ni dans
# la CI : `ruff check scripts/` y trouvait 29 erreurs, dont une variable morte
# portant une formule de calcul d'economies abandonnee au profit d'une autre.
# Un repertoire du depot echappait au controle sans que rien ne le dise, et
# c'est andrebrait qui l'a signale de biais, en notant que son propre EXE001
# passait avant comme apres sa PR.
echo "==> [1/3] ruff check src/ tests/ scripts/"
"$VENV/python3" -m ruff check src/ tests/ scripts/

echo "==> [2/3] pytest tests/ -q"
# Bare `pytest`, not `python -m pytest`: the module form silently prepends the
# CWD to sys.path, which made import errors invisible here and fatal in CI.
"$VENV/pytest" tests/ -q

echo "==> [3/3] git status (uncommitted check)"
if [ -n "$(git status --porcelain)" ]; then
    echo "WARN: uncommitted changes still present"
    git status --short
fi

echo
echo "Preflight OK. Safe to push."
