"""Tool annotations (readOnlyHint & co) for the MCP protocol boundary.

Why this module exists
----------------------
MCP lets a server declare, per tool, whether it only reads, whether it can
destroy state, whether it is idempotent, and whether it reaches outside the
machine. Clients use those hints to decide what an agent may call unattended.

Without them the protocol defaults are hostile: `readOnlyHint` defaults to
false and `destructiveHint` to true, so *every* Token Savior tool looks
potentially destructive to a client — including `get_function_source`. Any
consumer that wants a read-only subset then has to hard-code its own list and
keep it in sync by hand. That list drifts silently the day a tool is added.

The classification lives here, next to a test that fails if a tool in
TOOL_SCHEMAS is not classified. Adding a tool without saying what it does to
the world is a CI failure, not a silent mislabel.
"""

from __future__ import annotations

# Tools that change state: files on disk, the index, memory, or the active
# project. Everything not listed here is read-only.
MUTATING_TOOLS = frozenset({
    # structural code edits
    "add_field_to_model",
    "edit_lines_in_symbol",
    "insert_near_symbol",
    "move_symbol",
    "replace_symbol_source",
    # index / project state
    "checkpoint",
    "corpus_build",
    "reindex",
    "set_project_root",
    "switch_project",
    # persistent memory
    "memory_admin",
    "memory_delete",
    "memory_save",
    "reasoning_save",
    # captures
    "capture_purge",
    "capture_put",
    # execution — arbitrary side effects by construction
    "run_impacted_tests",
    "run_project_action",
    "ts_execute",
})

# Subset of MUTATING_TOOLS that can overwrite or remove existing state, as
# opposed to only adding to it. `insert_near_symbol` adds a symbol and is not
# listed; `replace_symbol_source` overwrites one and is.
#
# The three execution tools are listed conservatively: what a project action
# or a sandbox script does is not knowable from here, so we do not promise a
# client that it is safe.
DESTRUCTIVE_TOOLS = frozenset({
    "add_field_to_model",
    "capture_purge",
    "edit_lines_in_symbol",
    "memory_admin",
    "memory_delete",
    "move_symbol",
    "replace_symbol_source",
    "run_project_action",
    "ts_execute",
})

# Mutating tools where calling twice with the same arguments leaves the same
# state as calling once. Read-only tools are idempotent by definition and are
# not repeated here.
#
# `checkpoint` and `capture_put` are absent on purpose: each call creates a new
# snapshot or a new capture row.
IDEMPOTENT_MUTATORS = frozenset({
    "capture_purge",
    "corpus_build",
    "memory_delete",
    "reindex",
    "set_project_root",
    "switch_project",
})

# Nothing here talks to the network: Token Savior indexes and edits a local
# working copy. Kept as a named constant so a future tool that does reach out
# has an obvious place to break the assumption.
OPEN_WORLD_TOOLS: frozenset[str] = frozenset()


def annotations_for(name: str) -> dict[str, bool]:
    """Return the MCP annotation hints for one tool, by name.

    Unknown names are treated as read-only: `TOOL_SCHEMAS` is the source of
    truth for what exists, and `test_tool_annotations` guarantees every entry
    in it is classified here, so an unknown name at runtime is a filtered or
    legacy tool rather than an unclassified mutator.
    """
    read_only = name not in MUTATING_TOOLS
    return {
        "readOnlyHint": read_only,
        "destructiveHint": (not read_only) and name in DESTRUCTIVE_TOOLS,
        "idempotentHint": read_only or name in IDEMPOTENT_MUTATORS,
        "openWorldHint": name in OPEN_WORLD_TOOLS,
    }


def read_only_tool_names(all_names: object) -> frozenset[str]:
    """The read-only subset of `all_names`.

    Exposed so consumers that need a safe list — a judge loop, a review agent —
    can derive it from the server instead of maintaining their own copy.
    """
    return frozenset(n for n in all_names if n not in MUTATING_TOOLS)
