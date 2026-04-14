"""Capture-mode configuration (filesystem, no SQLite).

Lifted from memory_db.py during the memory/ subpackage split.
Manages ~/.config/token-savior/mode.json + activity tracker + session override.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

MODE_CONFIG_PATH = Path.home() / ".config" / "token-savior" / "mode.json"
SESSION_OVERRIDE_PATH = Path.home() / ".config" / "token-savior" / "session_mode_override"
ACTIVITY_TRACKER_PATH = Path.home() / ".config" / "token-savior" / "activity_tracker.json"

DEFAULT_MODES = {
    "current_mode": "code",
    "project_defaults": {},
    "modes": {
        "code": {
            "description": "Dev général — capture tout",
            "auto_capture_types": ["bugfix", "error_pattern", "guardrail", "convention", "command", "config"],
            "notify_telegram_types": ["guardrail", "error_pattern", "warning"],
            "session_summary": True,
            "prompt_archive": True,
        },
        "review": {
            "description": "Code review — focus décisions",
            "auto_capture_types": ["decision", "convention", "warning", "research", "idea"],
            "notify_telegram_types": ["warning"],
            "session_summary": True,
            "prompt_archive": False,
        },
        "debug": {
            "description": "Debug intensif — focus erreurs",
            "auto_capture_types": ["error_pattern", "bugfix", "guardrail", "command", "infra"],
            "notify_telegram_types": ["error_pattern", "guardrail"],
            "session_summary": True,
            "prompt_archive": True,
        },
        "infra": {
            "description": "Maintenance VPS — focus services et config",
            "auto_capture_types": ["command", "infra", "config", "warning", "guardrail"],
            "notify_telegram_types": ["warning", "guardrail", "infra"],
            "session_summary": True,
            "prompt_archive": True,
        },
        "silent": {
            "description": "Pas de capture automatique",
            "auto_capture_types": [],
            "notify_telegram_types": [],
            "session_summary": False,
            "prompt_archive": False,
        },
    },
}


def _load_mode_file() -> dict[str, Any]:
    """Load (or bootstrap) the mode config file."""
    if not MODE_CONFIG_PATH.exists():
        MODE_CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
        MODE_CONFIG_PATH.write_text(json.dumps(DEFAULT_MODES, indent=2, ensure_ascii=False))
        return json.loads(json.dumps(DEFAULT_MODES))
    try:
        data = json.loads(MODE_CONFIG_PATH.read_text(encoding="utf-8"))
        if "modes" not in data or "current_mode" not in data:
            raise ValueError("invalid mode file")
        return data
    except Exception:
        return json.loads(json.dumps(DEFAULT_MODES))


def _read_session_override() -> str | None:
    """Return the active session mode override, or None."""
    try:
        if SESSION_OVERRIDE_PATH.exists():
            name = SESSION_OVERRIDE_PATH.read_text(encoding="utf-8").strip()
            return name or None
    except Exception:
        pass
    return None


def set_session_override(mode_name: str) -> bool:
    """Write a session-scoped mode override. Cleared at session end."""
    data = _load_mode_file()
    if mode_name not in data.get("modes", {}):
        return False
    SESSION_OVERRIDE_PATH.parent.mkdir(parents=True, exist_ok=True)
    SESSION_OVERRIDE_PATH.write_text(mode_name)
    return True


def clear_session_override() -> None:
    try:
        if SESSION_OVERRIDE_PATH.exists():
            SESSION_OVERRIDE_PATH.unlink()
    except Exception:
        pass


def get_current_mode(project_root: str | None = None) -> dict[str, Any]:
    """Resolve the active mode config with origin tracking.

    Priority: session override → project default → global current_mode → 'code'.
    Returned dict includes 'name' and 'origin' keys.
    """
    data = _load_mode_file()
    modes = data.get("modes", {})

    name: str | None = None
    origin: str = "global"

    override = _read_session_override()
    if override and override in modes:
        name = override
        origin = "session override"

    if name is None and project_root:
        pd = data.get("project_defaults") or {}
        candidate = pd.get(project_root)
        if candidate and candidate in modes:
            name = candidate
            origin = "project default"

    if name is None:
        name = data.get("current_mode", "code")
        origin = "global"

    cfg = modes.get(name) or DEFAULT_MODES["modes"]["code"]
    return {"name": name, "origin": origin, **cfg}


def list_modes() -> list[dict]:
    data = _load_mode_file()
    return [
        {"name": n, **cfg, "active": n == data.get("current_mode")}
        for n, cfg in data.get("modes", {}).items()
    ]


def _read_activity_tracker() -> dict[str, Any]:
    try:
        if ACTIVITY_TRACKER_PATH.exists():
            return json.loads(ACTIVITY_TRACKER_PATH.read_text(encoding="utf-8"))
    except Exception:
        pass
    return {
        "recent_tools": [],
        "last_updated": 0,
        "suggested_mode": "code",
        "current_mode_source": "auto",
    }


def _write_activity_tracker(data: dict[str, Any]) -> None:
    try:
        ACTIVITY_TRACKER_PATH.parent.mkdir(parents=True, exist_ok=True)
        ACTIVITY_TRACKER_PATH.write_text(json.dumps(data, indent=2), encoding="utf-8")
    except Exception:
        pass


def set_mode(mode_name: str, source: str = "manual") -> bool:
    """Switch the global current mode.

    source: 'manual' | 'auto' | 'project' | 'global'. When 'manual', auto-switch
    from activity tracker is disabled until the next SessionEnd resets it.
    """
    from token_savior import memory_db  # lazy: invalidate_memory_cache still lives in memory_db

    data = _load_mode_file()
    if mode_name not in data.get("modes", {}):
        return False
    data["current_mode"] = mode_name
    MODE_CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    MODE_CONFIG_PATH.write_text(json.dumps(data, indent=2, ensure_ascii=False))
    tracker = _read_activity_tracker()
    tracker["suggested_mode"] = mode_name
    tracker["current_mode_source"] = source
    tracker["last_updated"] = int(time.time())
    _write_activity_tracker(tracker)
    try:
        memory_db.invalidate_memory_cache()
    except Exception:
        pass
    return True


def set_project_mode(project_root: str, mode_name: str) -> bool:
    """Set a default mode for a specific project_root."""
    data = _load_mode_file()
    if mode_name not in data.get("modes", {}):
        return False
    pd = data.setdefault("project_defaults", {})
    pd[project_root] = mode_name
    MODE_CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    MODE_CONFIG_PATH.write_text(json.dumps(data, indent=2, ensure_ascii=False))
    return True
