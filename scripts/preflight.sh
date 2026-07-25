#!/bin/bash
# Pre-push checklist for Token Savior. Run BEFORE every `git push`.
# Exit non-zero if anything fails so a wrapping `&&` chain stops.
set -euo pipefail

VENV=/root/.local/token-savior-venv/bin
cd "$(dirname "$0")/.."

echo "==> [1/3] ruff check src/ tests/"
"$VENV/python3" -m ruff check src/ tests/

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
