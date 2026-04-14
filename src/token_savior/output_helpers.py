"""Shared helpers for bounding command output in MCP responses.

Subprocess wrappers across the codebase need to clamp stdout/stderr so
responses stay within MCP size budgets. Consolidated here to avoid drift.
"""

from __future__ import annotations


def truncate_output(value: str, max_output_chars: int) -> str:
    """Clamp output to ``max_output_chars``, appending an omitted-count marker.

    If ``value`` fits, returns it unchanged. Otherwise returns the first
    ``max_output_chars`` characters followed by ``"\\n... [truncated N chars]"``
    where ``N`` is the number of characters omitted.

    >>> truncate_output("hello", 10)
    'hello'
    >>> truncate_output("abcdefghij", 5)
    'abcde\\n... [truncated 5 chars]'
    """
    if len(value) <= max_output_chars:
        return value
    omitted = len(value) - max_output_chars
    return value[:max_output_chars] + f"\n... [truncated {omitted} chars]"
