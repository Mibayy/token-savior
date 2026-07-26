"""Garde-fous sur les bundles de hooks Gemini CLI.

Gemini est le troisieme jeu de conventions rencontre, et il ne ressemble ni a
Claude Code ni a Codex :

- ses evenements sont `SessionStart`, `SessionEnd`, `Stop`, `BeforeTool`,
  `AfterTool`, `Notification` (extraits du paquet @google/gemini-cli 0.38.1).
  Il n'a **ni** `UserPromptSubmit` **ni** `PreCompact`, ni les noms
  `PreToolUse` / `PostToolUse` de Claude ;
- ses timeouts sont en **millisecondes** (`DEFAULT_HOOK_TIMEOUT = 6e4`), donc
  comme Claude Code et a l'inverse de Codex ;
- ses outils s'appellent `run_shell_command`, `read_file`, `web_fetch`... et
  jamais `Bash`.

Les trois erreurs correspondantes sont silencieuses : un hook mal nomme, mal
minute ou mal matche ne leve aucune erreur, il ne fait simplement rien.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
HOOKS_DIR = REPO_ROOT / "hooks"

GEMINI_EVENTS = frozenset({
    "SessionStart",
    "SessionEnd",
    "Stop",
    "BeforeTool",
    "AfterTool",
    "Notification",
})

GEMINI_BUNDLES = ("tool-capture-gemini.json", "memory-gemini.json")

# Noms d'outils Claude qui n'existent pas cote Gemini : les voir dans un
# matcher signale un copier-coller depuis un bundle Claude.
CLAUDE_ONLY_TOOL_NAMES = ("Bash", "Edit", "Write", "NotebookEdit", "Grep", "Glob")


def _load(name: str) -> dict:
    return json.loads((HOOKS_DIR / name).read_text(encoding="utf-8"))


@pytest.mark.parametrize("bundle", GEMINI_BUNDLES)
def test_gemini_bundle_declares_only_events_the_package_exposes(bundle: str) -> None:
    hooks = _load(bundle)["hooks"]
    inconnus = sorted(set(hooks) - GEMINI_EVENTS)
    assert not inconnus, (
        f"{bundle} declare des evenements absents de gemini-cli: {inconnus}. "
        "Gemini n'a ni PreToolUse/PostToolUse ni UserPromptSubmit/PreCompact."
    )


@pytest.mark.parametrize("bundle", GEMINI_BUNDLES)
def test_gemini_matchers_use_gemini_tool_names(bundle: str) -> None:
    hooks = _load(bundle)["hooks"]
    for event, entries in hooks.items():
        for entry in entries:
            matcher = entry.get("matcher")
            if not matcher:
                continue
            for nom in CLAUDE_ONLY_TOOL_NAMES:
                assert nom not in matcher.split("|"), (
                    f"{bundle}:{event} matche l'outil Claude {nom!r}. "
                    "Gemini expose run_shell_command, read_file, web_fetch..."
                )


def test_gemini_memory_bundle_uses_milliseconds() -> None:
    """Gemini compte en millisecondes; convertir en secondes rendrait les
    hooks de fin de session quasi instantanes et donc inutiles."""
    hooks = _load("memory-gemini.json")["hooks"]
    timeouts = [
        hook["timeout"]
        for entries in hooks.values()
        for entry in entries
        for hook in entry.get("hooks", [])
        if hook.get("timeout") is not None
    ]
    assert timeouts, "aucun timeout declare"
    assert max(timeouts) > 1000, (
        "tous les timeouts Gemini sont sous la seconde : la conversion Codex "
        "(secondes) a probablement ete appliquee au mauvais fichier"
    )


def test_gemini_memory_bundle_carries_the_recall() -> None:
    """SessionStart porte l'injection memoire. Sans lui le bundle est decoratif."""
    hooks = _load("memory-gemini.json")["hooks"]
    assert "SessionStart" in hooks
    cmd = hooks["SessionStart"][0]["hooks"][0]["command"]
    assert "memory-session-start.sh" in cmd


def test_gemini_bundles_reference_existing_scripts() -> None:
    manquants: list[str] = []
    for bundle in GEMINI_BUNDLES:
        raw = (HOOKS_DIR / bundle).read_text(encoding="utf-8")
        for token in raw.split():
            if token.startswith("{{TS_HOOKS_DIR}}/"):
                script = token.split("{{TS_HOOKS_DIR}}/", 1)[1].strip('",')
                if not (HOOKS_DIR / script).exists():
                    manquants.append(f"{bundle} -> {script}")
    assert not manquants, f"scripts introuvables: {manquants}"
