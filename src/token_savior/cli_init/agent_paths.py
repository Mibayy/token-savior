"""
Agent settings path resolution for `ts init`.

Each supported AI agent stores its hook/tool configuration in a known JSON
settings file. We expose two helpers:

    SUPPORTED_AGENTS                  : tuple of agent names
    settings_path(agent, scope)       : pathlib.Path for the settings file
    hook_config_path(agent, ts_root)  : pathlib.Path to the bundled hook JSON

The bundled hook configs live under <ts_repo>/hooks/ and ship with the
package.  They are merged into the agent settings file by merger.py.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Literal

Scope = Literal["global", "local"]

SUPPORTED_AGENTS: tuple[str, ...] = ("claude", "cursor", "gemini", "codex", "openclaw")

# Agents dont la compatibilite se limite au serveur MCP : ils n'exposent aucun
# systeme de hooks connu, donc `ts init` n'a rien a y ecrire. MCP etant un
# protocole ouvert, Token Savior y fonctionne sans adaptation. Le chemin sert
# a la documentation et aux diagnostics, pas au merge de hooks.
MCP_ONLY_AGENTS: dict[str, str] = {
    # Hermes (Nous Research) : config YAML, serveurs sous la cle `mcp_servers`.
    # Ses outils sont prefixes `mcp_<serveur>_<outil>` (simple underscore), la
    # ou Claude Code et Codex utilisent `mcp__<serveur>__<outil>`.
    "hermes": ".hermes/config.yaml",
}


# --------------------------------------------------------------------------- #
# Agent settings file locations                                               #
# --------------------------------------------------------------------------- #
def _global_settings(agent: str, home: Path) -> Path:
    if agent == "claude":
        return home / ".claude" / "settings.json"
    if agent == "cursor":
        # Cursor uses ~/.cursor/settings.json on macOS/Linux for the user scope.
        # Some installs use XDG (~/.config/cursor/settings.json) — we check the
        # former first; merger.py only writes the path returned here.
        return home / ".cursor" / "settings.json"
    if agent == "gemini":
        return home / ".gemini" / "settings.json"
    if agent == "codex":
        # OpenAI Codex CLI reads hook config from ~/.codex/hooks.json;
        # config.toml holds its non-hook settings.
        return home / ".codex" / "hooks.json"
    if agent == "openclaw":
        # OpenClaw garde hooks et serveurs MCP dans un unique fichier JSON5.
        return home / ".openclaw" / "openclaw.json"
    raise ValueError(f"unsupported agent: {agent}")


def _local_settings(agent: str, cwd: Path) -> Path:
    if agent == "claude":
        return cwd / ".claude" / "settings.json"
    if agent == "cursor":
        return cwd / ".cursor" / "settings.json"
    if agent == "gemini":
        return cwd / ".gemini" / "settings.json"
    if agent == "codex":
        return cwd / ".codex" / "hooks.json"
    if agent == "openclaw":
        return cwd / ".openclaw" / "openclaw.json"
    raise ValueError(f"unsupported agent: {agent}")


def detection_path(agent: str, home: Path) -> Path:
    """Return the file whose existence marks an installed agent.

    Usually the global settings file itself — except Codex, whose hooks.json
    only exists after `ts init` has run; its install marker is config.toml.
    """
    if agent == "codex":
        return home / ".codex" / "config.toml"
    return _global_settings(agent, home)


def settings_path(agent: str, scope: Scope = "global", *, home: Path | None = None,
                  cwd: Path | None = None) -> Path:
    """Return the settings.json path for the given agent and scope.

    `home` and `cwd` overrides are exposed for tests so we never touch the
    real user filesystem. Claude Code honors the CLAUDE_CONFIG_DIR environment
    variable for its global config root; an explicit `home` override wins.
    """
    if scope == "global" and agent == "claude" and home is None:
        config_dir = os.environ.get("CLAUDE_CONFIG_DIR")
        if config_dir:
            return Path(config_dir) / "settings.json"
    home = home or Path.home()
    cwd = cwd or Path.cwd()
    if scope == "global":
        return _global_settings(agent, home)
    return _local_settings(agent, cwd)


# --------------------------------------------------------------------------- #
# Bundled hook config locations                                               #
# --------------------------------------------------------------------------- #
_HOOK_CONFIG_FILES = {
    "claude": (
        "tool-capture-hooks-config.json",
        "bash-rewriter-config.json",
        "memory-hooks-config.json",
    ),
    "cursor": ("tool-capture-cursor.json",),
    "gemini": ("tool-capture-gemini.json",),
    "codex": ("tool-capture-codex.json", "memory-codex.json"),
    # OpenClaw charge des hook packs (dossier HOOK.md + handler.js) au lieu de
    # commandes par evenement : le bundle declare le dossier, pas des commandes.
    "openclaw": ("openclaw-config.json",),
}


def hook_config_paths(agent: str, ts_root: Path) -> list[Path]:
    """Return the bundled hook config JSON file(s) for an agent.

    Claude gets tool_capture (PostToolUse), bash_rewriter (PreToolUse) and the
    memory engine. Codex gets tool_capture plus a Codex-specific memory bundle
    whose timeouts are expressed in seconds rather than milliseconds.
    Cursor and Gemini still ship the tool_capture half only.
    """
    if agent not in _HOOK_CONFIG_FILES:
        raise ValueError(f"unsupported agent: {agent}")
    hooks_dir = ts_root / "hooks"
    return [hooks_dir / name for name in _HOOK_CONFIG_FILES[agent]]
