"""Per-domain MCP-tool handlers for the Token Savior server.

Each submodule owns a slice of the dispatch table and exports a ``HANDLERS``
dict mapping tool name -> handler. The handlers fall into four call-shape
categories that the main ``call_tool`` dispatcher iterates in order:

* ``META_HANDLERS``   -- ``handler(arguments) -> list[TextContent]``
                         (stats, project lifecycle, memory admin -- no slot)
* ``MEMORY_HANDLERS`` -- ``handler(arguments) -> str``
                         (memory engine; text response wrapped by caller)
* ``SLOT_HANDLERS``   -- ``handler(slot, arguments) -> raw result``
                         (everything that needs a project slot)
* ``QFN_HANDLERS``    -- ``handler(query_fns, arguments) -> raw result``
                         (structural code-navigation queries)

This module aggregates the per-domain dicts and asserts the union is
disjoint, so a tool name added to two domains by mistake fails at import
time rather than silently shadowing.
"""

from __future__ import annotations

from typing import Any

from token_savior.server_handlers.analysis import HANDLERS as _ANALYSIS_HANDLERS
from token_savior.server_handlers.checkpoints import HANDLERS as _CHECKPOINT_HANDLERS
from token_savior.server_handlers.code_nav import QFN_HANDLERS as _QFN_HANDLERS
from token_savior.server_handlers.edit import HANDLERS as _EDIT_HANDLERS
from token_savior.server_handlers.git import HANDLERS as _GIT_HANDLERS
from token_savior.server_handlers.memory import (
    ADMIN_HANDLERS as _MEMORY_ADMIN_HANDLERS,
    HANDLERS as _MEMORY_HANDLERS_RAW,
)
from token_savior.server_handlers.project import HANDLERS as _PROJECT_HANDLERS
from token_savior.server_handlers.project_actions import (
    HANDLERS as _PROJECT_ACTION_HANDLERS,
)
from token_savior.server_handlers.stats import HANDLERS as _STATS_HANDLERS
from token_savior.server_handlers.tests import HANDLERS as _TESTS_HANDLERS


def _merge_disjoint(label: str, *parts: dict[str, Any]) -> dict[str, Any]:
    """Merge handler dicts; raise on duplicate tool names within a category."""
    merged: dict[str, Any] = {}
    for part in parts:
        overlap = merged.keys() & part.keys()
        if overlap:
            raise RuntimeError(
                f"{label}: duplicate tool name(s) across handler modules: "
                f"{sorted(overlap)}"
            )
        merged.update(part)
    return merged


META_HANDLERS: dict[str, Any] = _merge_disjoint(
    "META_HANDLERS",
    _STATS_HANDLERS,
    _MEMORY_ADMIN_HANDLERS,
    _PROJECT_HANDLERS,
)

MEMORY_HANDLERS: dict[str, Any] = dict(_MEMORY_HANDLERS_RAW)

SLOT_HANDLERS: dict[str, Any] = _merge_disjoint(
    "SLOT_HANDLERS",
    _GIT_HANDLERS,
    _CHECKPOINT_HANDLERS,
    _EDIT_HANDLERS,
    _TESTS_HANDLERS,
    _PROJECT_ACTION_HANDLERS,
    _ANALYSIS_HANDLERS,
)

QFN_HANDLERS: dict[str, Any] = dict(_QFN_HANDLERS)


# Cross-category collision check: no tool may appear in more than one of the
# four dispatch tables. If it did, the call_tool dispatcher would silently
# pick whichever lookup runs first and the other handler would be dead code.
ALL_HANDLERS: dict[str, Any] = _merge_disjoint(
    "ALL_HANDLERS",
    META_HANDLERS,
    MEMORY_HANDLERS,
    SLOT_HANDLERS,
    QFN_HANDLERS,
)


__all__ = [
    "ALL_HANDLERS",
    "META_HANDLERS",
    "MEMORY_HANDLERS",
    "SLOT_HANDLERS",
    "QFN_HANDLERS",
]
