"""
Pure functions for merging Token Savior hook configurations into an
existing agent settings JSON document.

Design goals:
    * Idempotent : running merge twice produces the same final document.
    * Non-destructive : never drops keys the user already had.
    * Stdlib only.

The hook entries are compared by a stable fingerprint (command path +
matcher) so re-running `ts init` does not duplicate them.
"""
from __future__ import annotations

import copy
import json
from typing import Any


# --------------------------------------------------------------------------- #
# Fingerprint                                                                 #
# --------------------------------------------------------------------------- #
def _hook_command(entry: dict) -> str | None:
    """Return the command string of a hook entry, regardless of its shape.

    Claude / Cursor / Gemini all wrap commands under `hooks: [{type, command}]`.
    Codex stores `command` directly on the matcher entry.  We support both.
    """
    if "command" in entry and isinstance(entry["command"], str):
        return entry["command"]
    inner = entry.get("hooks")
    if isinstance(inner, list):
        for h in inner:
            if isinstance(h, dict) and isinstance(h.get("command"), str):
                return h["command"]
    return None


def _entry_fingerprint(entry: dict) -> tuple[str, str]:
    """A stable identity for a hook entry: (matcher, command)."""
    matcher = str(entry.get("matcher", ""))
    cmd = _hook_command(entry) or ""
    return (matcher, cmd)


# --------------------------------------------------------------------------- #
# Merge logic                                                                 #
# --------------------------------------------------------------------------- #
def merge_hook_arrays(existing: list, incoming: list) -> list:
    """Concatenate two hook-entry arrays, skipping incoming entries whose
    (matcher, command) fingerprint already appears in `existing`.
    """
    out = list(existing)
    seen = {_entry_fingerprint(e) for e in existing if isinstance(e, dict)}
    for entry in incoming:
        if not isinstance(entry, dict):
            continue
        fp = _entry_fingerprint(entry)
        if fp in seen:
            continue
        seen.add(fp)
        out.append(copy.deepcopy(entry))
    return out


def merge_hook_config(existing: dict, new_hooks: dict) -> dict:
    """Deep-merge a Token Savior hook config bundle into an existing settings
    document.

    `existing`   : full agent settings dict (may be empty).
    `new_hooks`  : dict shaped like the bundled hook configs — either the
                   whole file (with a top-level "hooks" key) or just the
                   inner "hooks" dict.  Both forms accepted.

    Returns a NEW dict; `existing` is not mutated.
    """
    result = copy.deepcopy(existing)

    # Accept either wrapper-style ({"hooks": {...}, "_comment": ...}) or
    # bare ({"PostToolUse": [...]}). Normalize to the inner dict.
    if "hooks" in new_hooks and isinstance(new_hooks["hooks"], dict):
        incoming = new_hooks["hooks"]
    else:
        incoming = new_hooks

    if not isinstance(incoming, dict):
        return result

    existing_hooks = result.get("hooks")
    if not isinstance(existing_hooks, dict):
        existing_hooks = {}

    for event_name, entries in incoming.items():
        if not isinstance(entries, list):
            # Unknown shape — pass through wholesale.
            existing_hooks[event_name] = entries
            continue
        current = existing_hooks.get(event_name)
        if not isinstance(current, list):
            current = []
        existing_hooks[event_name] = merge_hook_arrays(current, entries)

    result["hooks"] = existing_hooks
    return result


# --------------------------------------------------------------------------- #
# Diff                                                                        #
# --------------------------------------------------------------------------- #
def format_diff(before: dict, after: dict) -> str:
    """Return a unified-style textual diff of two JSON docs."""
    import difflib

    a = json.dumps(before, indent=2, sort_keys=True).splitlines(keepends=True)
    b = json.dumps(after, indent=2, sort_keys=True).splitlines(keepends=True)
    diff = difflib.unified_diff(a, b, fromfile="before", tofile="after", n=3)
    return "".join(diff) or "(no changes)\n"


def added_entries(before: dict, after: dict) -> list[tuple[str, tuple[str, str]]]:
    """List of (event_name, (matcher, command)) tuples present in `after.hooks`
    but not in `before.hooks`.  Used for short summary lines.
    """
    out: list[tuple[str, tuple[str, str]]] = []
    bh: dict[str, Any] = before.get("hooks") or {}
    ah: dict[str, Any] = after.get("hooks") or {}
    for event, entries in ah.items():
        if not isinstance(entries, list):
            continue
        prior = {_entry_fingerprint(e) for e in (bh.get(event) or []) if isinstance(e, dict)}
        for e in entries:
            if not isinstance(e, dict):
                continue
            fp = _entry_fingerprint(e)
            if fp not in prior:
                out.append((event, fp))
    return out
