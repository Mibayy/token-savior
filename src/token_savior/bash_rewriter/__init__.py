"""Token Savior bash command rewriter.

Public API: :func:`rewrite` takes a Bash command string and returns
``(new_command, reason)``. If the command is left untouched, ``reason``
is ``None``.

The rewriter is intentionally conservative:

* Never alters commands containing shell composition / substitution
  operators (``|``, ``>``, ``<``, ``&&``, ``||``, ``;``, ``$(...)``,
  backticks, or the ``--`` separator).
* Never alters commands that the user explicitly tagged as verbose
  (``-v``, ``-vv``, ``--verbose``).
* Pure pass-through for anything no rule recognises.

Used by the PreToolUse hook in ``hooks/bash_rewriter_hook.py``.
"""
from __future__ import annotations

from .rules import RULES, RewriteRule, is_unsafe_to_rewrite

__all__ = ["rewrite", "RULES", "RewriteRule", "is_unsafe_to_rewrite"]


def rewrite(command: str) -> tuple[str, str | None]:
    """Return ``(new_command, reason)`` for a Bash command.

    ``reason`` is ``None`` when the command is left untouched.
    """
    if not isinstance(command, str):
        return command, None
    stripped = command.strip()
    if not stripped:
        return command, None
    if is_unsafe_to_rewrite(stripped):
        return command, None
    for rule in RULES:
        if rule.matches(stripped):
            new_cmd = rule.apply(stripped)
            if new_cmd != stripped and new_cmd:
                return new_cmd, rule.reason
            return command, None
    return command, None
