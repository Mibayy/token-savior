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

from pathlib import Path
from typing import Literal

Scope = Literal["global", "local"]

SUPPORTED_AGENTS: tuple[str, ...] = ("claude", "cursor", "gemini", "codex")


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
        # OpenAI Codex / Copilot CLI uses ~/.codex/settings.json.
        return home / ".codex" / "settings.json"
    raise ValueError(f"unsupported agent: {agent}")


def _local_settings(agent: str, cwd: Path) -> Path:
    if agent == "claude":
        return cwd / ".claude" / "settings.json"
    if agent == "cursor":
        return cwd / ".cursor" / "settings.json"
    if agent == "gemini":
        return cwd / ".gemini" / "settings.json"
    if agent == "codex":
        return cwd / ".codex" / "settings.json"
    raise ValueError(f"unsupported agent: {agent}")


def settings_path(agent: str, scope: Scope = "global", *, home: Path | None = None,
                  cwd: Path | None = None) -> Path:
    """Return the settings.json path for the given agent and scope.

    `home` and `cwd` overrides are exposed for tests so we never touch the
    real user filesystem.
    """
    home = home or Path.home()
    cwd = cwd or Path.cwd()
    if scope == "global":
        return _global_settings(agent, home)
    return _local_settings(agent, cwd)


# --------------------------------------------------------------------------- #
# Bundled hook config locations                                               #
# --------------------------------------------------------------------------- #
_HOOK_CONFIG_FILES = {
    "claude": ("tool-capture-hooks-config.json", "bash-rewriter-config.json"),
    "cursor": ("tool-capture-cursor.json",),
    "gemini": ("tool-capture-gemini.json",),
    "codex": ("tool-capture-codex.json",),
}


def hook_config_paths(agent: str, ts_root: Path) -> list[Path]:
    """Return the bundled hook config JSON file(s) for an agent.

    Claude gets both tool_capture (PostToolUse) and bash_rewriter (PreToolUse).
    Other agents currently only ship the tool_capture half.
    """
    if agent not in _HOOK_CONFIG_FILES:
        raise ValueError(f"unsupported agent: {agent}")
    hooks_dir = ts_root / "hooks"
    return [hooks_dir / name for name in _HOOK_CONFIG_FILES[agent]]
