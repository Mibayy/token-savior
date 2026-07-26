"""Garde-fous sur les bundles de hooks Codex.

Deux pieges ont ete rencontres en portant le moteur memoire de Claude Code
vers OpenAI Codex CLI, et ce sont les deux seuls que ce fichier surveille :

1. **Evenements inventes.** Codex n'expose pas les memes evenements de cycle
   de vie que Claude Code. Declarer `Stop` ou `StopFailure` dans
   `~/.codex/hooks.json` ne provoque aucune erreur : le hook ne se declenche
   simplement jamais. Panne silencieuse, exactement le mode de defaillance
   qu'on traque. La liste de reference ci-dessous a ete extraite du binaire
   codex 0.145.0 (`strings`), pas d'une documentation.

2. **Unite de timeout.** Claude Code compte en millisecondes, Codex en
   secondes. Recopier `timeout: 120000` d'un bundle Claude vers un bundle
   Codex ne plante pas non plus : ca demande juste 33 heures d'attente.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
HOOKS_DIR = REPO_ROOT / "hooks"

# Extrait de: strings <codex-0.145.0-binary> | grep -x '<EventName>'
CODEX_EVENTS = frozenset({
    "SessionStart",
    "SessionEnd",
    "PreToolUse",
    "PostToolUse",
    "UserPromptSubmit",
    "PreCompact",
    "Notification",
    "SubagentStop",
})

CODEX_BUNDLES = ("tool-capture-codex.json", "memory-codex.json")

# Un hook Codex plus long que ca signale presque surement un timeout Claude
# (millisecondes) recopie tel quel.
MAX_CODEX_TIMEOUT_SEC = 300


def _load(name: str) -> dict:
    return json.loads((HOOKS_DIR / name).read_text(encoding="utf-8"))


@pytest.mark.parametrize("bundle", CODEX_BUNDLES)
def test_codex_bundle_declares_only_events_the_binary_exposes(bundle: str) -> None:
    hooks = _load(bundle)["hooks"]
    inconnus = sorted(set(hooks) - CODEX_EVENTS)
    assert not inconnus, (
        f"{bundle} declare des evenements absents de codex 0.145.0: {inconnus}. "
        "Un hook sur un evenement inconnu ne leve pas d'erreur, il ne se "
        "declenche jamais."
    )


@pytest.mark.parametrize("bundle", CODEX_BUNDLES)
def test_codex_timeouts_are_seconds_not_milliseconds(bundle: str) -> None:
    hooks = _load(bundle)["hooks"]
    for event, entries in hooks.items():
        for entry in entries:
            for hook in entry.get("hooks", []):
                t = hook.get("timeout")
                if t is None:
                    continue
                assert t <= MAX_CODEX_TIMEOUT_SEC, (
                    f"{bundle}:{event} a timeout={t}. Codex compte en secondes; "
                    "cette valeur ressemble a un timeout Claude en millisecondes."
                )


def test_claude_memory_bundle_still_uses_milliseconds() -> None:
    """Le pendant du test precedent : on ne veut pas 'corriger' Claude par erreur.

    Claude Code compte en millisecondes, donc son bundle memoire doit contenir
    au moins une valeur qui serait absurde en secondes.
    """
    hooks = _load("memory-hooks-config.json")["hooks"]
    timeouts = [
        hook.get("timeout")
        for entries in hooks.values()
        for entry in entries
        for hook in entry.get("hooks", [])
        if hook.get("timeout") is not None
    ]
    assert timeouts, "le bundle memoire Claude ne declare aucun timeout"
    assert max(timeouts) > MAX_CODEX_TIMEOUT_SEC, (
        "les timeouts du bundle Claude sont passes sous le seuil des secondes : "
        "quelqu'un a probablement applique la conversion Codex au mauvais fichier"
    )


def test_codex_memory_bundle_covers_the_session_lifecycle() -> None:
    """SessionStart porte le recall (144/153 observations). Sans lui, rien."""
    hooks = _load("memory-codex.json")["hooks"]
    for requis in ("SessionStart", "UserPromptSubmit", "SessionEnd"):
        assert requis in hooks, f"memory-codex.json ne cable pas {requis}"


def test_codex_bundles_reference_existing_hook_scripts() -> None:
    """Un chemin de script faux est une autre panne silencieuse."""
    manquants: list[str] = []
    for bundle in CODEX_BUNDLES:
        raw = (HOOKS_DIR / bundle).read_text(encoding="utf-8")
        for token in raw.split():
            if token.startswith("{{TS_HOOKS_DIR}}/"):
                script = token.split("{{TS_HOOKS_DIR}}/", 1)[1].strip('",')
                if not (HOOKS_DIR / script).exists():
                    manquants.append(f"{bundle} -> {script}")
    assert not manquants, f"scripts de hooks introuvables: {manquants}"
