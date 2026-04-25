"""MCP tool schema definitions for Token Savior.

Each entry maps a tool name to its ``description`` and ``inputSchema``.
server.py builds ``mcp.types.Tool`` objects from this dict at import time.
"""

from __future__ import annotations

# Shared project parameter injected into multi-project tools
_PROJECT_PARAM = {
    "project": {"type": "string", "description": "Project name/path (default: active)."}
}

# TCS — compressed output toggle for structural listing tools
_COMPRESS_PARAM = {
    "compress": {"type": "boolean", "description": "Compact rows (default true)."}
}

# Batch mode: pass multiple names in one call instead of N sequential calls.
_NAMES_PARAM = {
    "names": {
        "type": "array",
        "items": {"type": "string"},
        "maxItems": 10,
        "description": "Batch mode: list of names (max 10). Returns {name: result} dict. Mutually exclusive with 'name'.",
    }
}

TOOL_SCHEMAS: dict[str, dict] = {
    # ── Meta tools ────────────────────────────────────────────────────────
    "list_projects": {
        "description": (
        "List all registered workspace projects with index status.\n"
        "USE WHEN: you need to pick a project name before switch_project.\n"
        "NOT WHEN: you want the active project's overview — use get_project_summary."
    ),
        "inputSchema": {"type": "object", "properties": {}},
    },
    "switch_project": {
        "description": (
        "Switch the active project.\n"
        "USE WHEN: the user pivots to a different indexed codebase.\n"
        "NOT WHEN: the project isn't registered yet — use set_project_root.\n"
        "Subsequent tool calls without project= target the new active root."
    ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "name": {
                    "type": "string",
                    "description": "Project name (basename of path) or full path.",
                },
            },
            "required": ["name"],
        },
    },
    # ── Git & diff ────────────────────────────────────────────────────────
    "get_git_status": {
        "description": (
        "Structured git status: branch, ahead/behind, staged, unstaged, untracked.\n"
        "USE WHEN: pre-commit sanity check or diagnosing local repo state.\n"
        "NOT WHEN: you need symbol-level changes, not file-level — use get_changed_symbols."
    ),
        "inputSchema": {"type": "object", "properties": {**_PROJECT_PARAM}},
    },
    "get_changed_symbols": {
        "description": (
        "Symbol-level summary of worktree changes (or HEAD vs ref).\n"
        "USE WHEN: reviewing which functions/classes moved in a diff.\n"
        "NOT WHEN: commit-message tasks — use build_commit_summary; scoped to specific file paths — use summarize_patch_by_symbol."
    ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "ref": {"type": "string", "description": "Compare base (omit=worktree)."},
                "max_files": {"type": "integer", "description": "Default 20."},
                "max_symbols_per_file": {"type": "integer", "description": "Default 20."},
                **_PROJECT_PARAM,
            },
        },
    },
    "summarize_patch_by_symbol": {
        "description": (
        "Symbol-level summary of a specific set of changed files.\n"
        "USE WHEN: reviewing a patch scoped to known file paths.\n"
        "NOT WHEN: whole-worktree changes — use get_changed_symbols; commit message draft — use build_commit_summary."
    ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "changed_files": {"type": "array", "items": {"type": "string"}},
                "max_files": {"type": "integer", "description": "Default 20."},
                "max_symbols_per_file": {"type": "integer", "description": "Default 20."},
                **_PROJECT_PARAM,
            },
        },
    },
    "build_commit_summary": {
        "description": (
        "Compact commit/review narrative with stats, hotspots, suggested type.\n"
        "USE WHEN: drafting a commit message or PR description.\n"
        "NOT WHEN: you just need the diff symbol-level — use get_changed_symbols."
    ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "changed_files": {"type": "array", "items": {"type": "string"}},
                "max_files": {"type": "integer", "description": "Default 20."},
                "max_symbols_per_file": {"type": "integer", "description": "Default 20."},
                **_PROJECT_PARAM,
            },
            "required": ["changed_files"],
        },
    },
    # ── Checkpoints (unified) ─────────────────────────────────────────────
    "checkpoint": {
        "description": (
        "Unified checkpoint CRUD. op = create | list (default) | restore | delete | prune | compare.\n"
        "USE WHEN: snapshotting a bounded file set before risky edits, or diffing worktree vs a prior state.\n"
        "NOT WHEN: you want git-based history — use git directly (checkpoints are separate from git)."
    ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "op": {
                    "type": "string",
                    "enum": ["create", "list", "restore", "delete", "prune", "compare"],
                    "description": "Operation to perform (default 'list').",
                },
                "file_paths": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "For op=create: project files to snapshot.",
                },
                "checkpoint_id": {
                    "type": "string",
                    "description": "For op=restore/delete/compare: checkpoint identifier.",
                },
                "keep_last": {
                    "type": "integer",
                    "description": "For op=prune: how many recent checkpoints to keep (default 10).",
                },
                "max_files": {
                    "type": "integer",
                    "description": "For op=compare: max files compared (default 20).",
                },
                **_PROJECT_PARAM,
            },
        },
    },
    # ── Structural edits ──────────────────────────────────────────────────
    "replace_symbol_source": {
        "description": (
        "Replace an indexed symbol's full source block directly.\n"
        "USE WHEN: rewriting a function/method/class body in place.\n"
        "NOT WHEN: adding code near an existing symbol — use insert_near_symbol.\n"
        "Triggers inline reindex, no manual reindex needed."
    ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "symbol_name": {
                    "type": "string",
                    "description": "Function, method, class, or section name to replace.",
                },
                "new_source": {
                    "type": "string",
                    "description": "Replacement source for the symbol.",
                },
                "file_path": {
                    "type": "string",
                    "description": "Optional file path to disambiguate symbols.",
                },
                **_PROJECT_PARAM,
            },
            "required": ["symbol_name", "new_source"],
        },
    },
    "insert_near_symbol": {
        "description": (
        "Insert content before or after an indexed symbol.\n"
        "USE WHEN: adding a new function/import/decorator adjacent to an existing one.\n"
        "NOT WHEN: replacing a symbol's body — use replace_symbol_source.\n"
        "Triggers inline reindex."
    ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "symbol_name": {"type": "string"},
                "content": {"type": "string"},
                "position": {"type": "string", "description": "'before' or 'after' (default after)."},
                "file_path": {"type": "string"},
                **_PROJECT_PARAM,
            },
            "required": ["symbol_name", "content"],
        },
    },
    "move_symbol": {
        "description": (
        "Move a symbol to a different file, updating imports in all call sites.\n"
        "USE WHEN: relocating a function/class and want the import graph fixed automatically.\n"
        "NOT WHEN: in-place body rewrite — use replace_symbol_source; adding a field to a class — use add_field_to_model."
    ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "symbol": {"type": "string", "description": "Symbol name to move."},
                "target_file": {"type": "string", "description": "Relative path to the target file."},
                "create_if_missing": {"type": "boolean", "description": "Create target file if it doesn't exist (default true)."},
                **_PROJECT_PARAM,
            },
            "required": ["symbol", "target_file"],
        },
    },
    "add_field_to_model": {
        "description": (
        "Add a field to a model/class/interface. Supports .prisma, .py (dataclass, SQLAlchemy), .ts/.tsx.\n"
        "USE WHEN: extending a data model with a new typed field.\n"
        "NOT WHEN: adding arbitrary code near a symbol — use insert_near_symbol."
    ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "model": {"type": "string", "description": "Model/class/interface name."},
                "field_name": {"type": "string", "description": "Name of the new field."},
                "field_type": {"type": "string", "description": "Type of the field (e.g. 'String', 'DateTime?', 'number')."},
                "file_path": {"type": "string", "description": "Optional file path to disambiguate."},
                "after": {"type": "string", "description": "Insert after the line containing this string."},
                **_PROJECT_PARAM,
            },
            "required": ["model", "field_name", "field_type"],
        },
    },
    "apply_refactoring": {
        "description": (
        "Polymorphic refactoring: rename, move, add_field, extract.\n"
        "USE WHEN: cross-file rename with import fixup; moving a symbol; adding a model field; extracting a block into a new function.\n"
        "NOT WHEN: rewriting one function body in place — use replace_symbol_source."
    ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "type": {
                    "type": "string",
                    "enum": ["rename", "move", "add_field", "extract"],
                    "description": "Refactoring operation type.",
                },
                "symbol": {"type": "string", "description": "Symbol name (rename/move)."},
                "new_name": {"type": "string", "description": "New name (rename/extract)."},
                "target_file": {"type": "string", "description": "Target file (move)."},
                "create_if_missing": {"type": "boolean", "description": "Create target if missing (move, default true)."},
                "model": {"type": "string", "description": "Model name (add_field)."},
                "field_name": {"type": "string", "description": "Field name (add_field)."},
                "field_type": {"type": "string", "description": "Field type (add_field)."},
                "file_path": {"type": "string", "description": "File path (extract/add_field)."},
                "after": {"type": "string", "description": "Insert after (add_field)."},
                "start_line": {"type": "integer", "description": "Start line (extract)."},
                "end_line": {"type": "integer", "description": "End line (extract)."},
                **_PROJECT_PARAM,
            },
            "required": ["type"],
        },
    },
    # ── Tests & validation ────────────────────────────────────────────────
    "find_impacted_test_files": {
        "description": (
        "Infer pytest files likely impacted by changed files or symbols.\n"
        "USE WHEN: narrowing which tests to run after an edit.\n"
        "NOT WHEN: you want to actually run them — use run_impacted_tests (calls this internally)."
    ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "changed_files": {"type": "array", "items": {"type": "string"}},
                "symbol_names": {"type": "array", "items": {"type": "string"}},
                "max_tests": {"type": "integer", "description": "Default 20."},
                **_PROJECT_PARAM,
            },
        },
    },
    "run_impacted_tests": {
        "description": (
        "Run pytest on files impacted by the current worktree changes.\n"
        "USE WHEN: quick regression check after an edit, scoped to impacted tests only.\n"
        "NOT WHEN: you want the list only (no execution) — use find_impacted_test_files."
    ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "changed_files": {"type": "array", "items": {"type": "string"}},
                "symbol_names": {"type": "array", "items": {"type": "string"}},
                "max_tests": {"type": "integer"},
                "timeout_sec": {"type": "integer"},
                "max_output_chars": {"type": "integer"},
                "include_output": {"type": "boolean"},
                "compact": {"type": "boolean"},
                **_PROJECT_PARAM,
            },
        },
    },
    "apply_symbol_change_and_validate": {
        "description": (
        "Replace symbol source, reindex, run impacted tests in one call.\n"
        "USE WHEN: committing a symbol rewrite and wanting automatic test-gated safety.\n"
        "NOT WHEN: pre-change static safety check only — use verify_edit (no execution)."
    ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "symbol_name": {"type": "string"},
                "new_source": {"type": "string"},
                "file_path": {"type": "string"},
                "rollback_on_failure": {"type": "boolean"},
                "max_tests": {"type": "integer"},
                "timeout_sec": {"type": "integer"},
                "max_output_chars": {"type": "integer"},
                "include_output": {"type": "boolean"},
                "compact": {"type": "boolean"},
                **_PROJECT_PARAM,
            },
            "required": ["symbol_name", "new_source"],
        },
    },
    # ── Project actions ───────────────────────────────────────────────────
    "discover_project_actions": {
        "description": (
        "Detect conventional project actions from build files (tests, lint, build, run) without executing.\n"
        "USE WHEN: discovering what commands are available in an unfamiliar project.\n"
        "NOT WHEN: you already know the action id and want to run it — use run_project_action."
    ),
        "inputSchema": {"type": "object", "properties": {**_PROJECT_PARAM}},
    },
    "run_project_action": {
        "description": (
        "Run a discovered project action by id (bounded output, bounded timeout).\n"
        "USE WHEN: executing a lint/test/build action returned by discover_project_actions.\n"
        "NOT WHEN: you don't know the action id yet — use discover_project_actions first."
    ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "action_id": {"type": "string", "description": "e.g. 'python:test', 'npm:test'."},
                "timeout_sec": {"type": "integer", "description": "Default 120."},
                "max_output_chars": {"type": "integer", "description": "Default 12000."},
                "include_output": {"type": "boolean"},
                **_PROJECT_PARAM,
            },
            "required": ["action_id"],
        },
    },
    # ── Query tools ───────────────────────────────────────────────────────
    "get_project_summary": {
        "description": (
        "Project overview: file count, packages, top classes/functions, infra dirs.\n"
        "USE WHEN: getting your bearings on an unfamiliar project at session start.\n"
        "NOT WHEN: you need per-file structure — use get_structure_summary."
    ),
        "inputSchema": {"type": "object", "properties": {**_PROJECT_PARAM}},
    },
    "list_files": {
        "description": (
        "List indexed files, optionally filtered by glob.\n"
        "USE WHEN: confirming which files are in scope, or hunting a file by name pattern.\n"
        "NOT WHEN: you know the file and want its structure — use get_structure_summary."
    ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "pattern": {
                    "type": "string",
                    "description": "Glob pattern to filter files (uses fnmatch).",
                },
                "max_results": {
                    "type": "integer",
                    "description": "Maximum number of results to return (0 = unlimited, default 0).",
                },
                **_PROJECT_PARAM,
            },
        },
    },
    "get_structure_summary": {
        "description": (
        "Structure of one file (functions, classes, imports, line counts), or project-wide if file omitted.\n"
        "USE WHEN: quick table-of-contents of a file before drilling in.\n"
        "NOT WHEN: you want a cross-project overview — use get_project_summary."
    ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "file_path": {
                    "type": "string",
                    "description": "Relative path to a file in the project. Omit for project-level summary.",
                },
                **_PROJECT_PARAM,
            },
        },
    },
    "get_function_source": {
        "description": (
        "Fetch a function/method source body.\n"
        "USE WHEN: you need to read the code of a specific named function.\n"
        "NOT WHEN: you want function + callers + deps in one shot — use get_full_context."
    ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "Function or method (e.g. 'MyClass.method')."},
                **_NAMES_PARAM,
                "file_path": {"type": "string"},
                "max_lines": {"type": "integer", "description": "Cap lines (0=all, level=0 only)."},
                "level": {"type": "integer", "minimum": 0, "maximum": 3},
                "force_full": {"type": "boolean", "description": "Bypass symbol cache."},
                "hints": {"type": "boolean", "description": "Append a one-line get_full_context hint (default true)."},
                **_PROJECT_PARAM,
            },
        },
    },
    "get_class_source": {
        "description": (
        "Fetch a class source body (including methods).\n"
        "USE WHEN: you need to read a specific named class's definition.\n"
        "NOT WHEN: you want class + callers + deps together — use get_full_context.\n"
        "Auto-downgrades to level 2 past 300 lines."
    ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "name": {"type": "string"},
                **_NAMES_PARAM,
                "file_path": {"type": "string"},
                "max_lines": {"type": "integer", "description": "Cap lines (0=all, level=0 only)."},
                "level": {"type": "integer", "minimum": 0, "maximum": 3},
                "force_full": {"type": "boolean", "description": "Bypass symbol cache."},
                "hints": {"type": "boolean", "description": "Append a one-line get_full_context hint (default true)."},
                **_PROJECT_PARAM,
            },
        },
    },
    "get_functions": {
        "description": (
        "List functions in a file (file_path=...) or across the project.\n"
        "USE WHEN: enumerating what's where — file-wide or project-wide inventory.\n"
        "NOT WHEN: you know the function name — use find_symbol (cheaper, exact)."
    ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "file_path": {"type": "string", "description": "Filter to file (omit=all)."},
                "max_results": {"type": "integer", "description": "Default 100. 0=unlimited. Truncated results carry a trailing `_truncated` marker with total count."},
                "hints": {"type": "boolean", "description": "Append a `_hints` entry with next-step tool calls (default true)."},
                **_COMPRESS_PARAM,
                **_PROJECT_PARAM,
            },
        },
    },
    "get_classes": {
        "description": (
        "List classes (name, lines, methods, bases, file).\n"
        "USE WHEN: file-wide or project-wide class inventory.\n"
        "NOT WHEN: you know the class name — use find_symbol (cheaper, exact)."
    ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "file_path": {"type": "string", "description": "Filter to file (omit=all)."},
                "max_results": {"type": "integer", "description": "Default 100. 0=unlimited. Truncated results carry a trailing `_truncated` marker with total count."},
                "hints": {"type": "boolean", "description": "Append a `_hints` entry with next-step tool calls (default true)."},
                **_COMPRESS_PARAM,
                **_PROJECT_PARAM,
            },
        },
    },
    "get_imports": {
        "description": (
        "List imports (module, names, line).\n"
        "USE WHEN: auditing what a file/project pulls in.\n"
        "NOT WHEN: you want the reverse direction (who imports X) — use get_file_dependents."
    ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "file_path": {"type": "string", "description": "Filter to file (omit=all)."},
                "max_results": {
                    "type": "integer",
                    "description": "Default 100. 0=unlimited. Truncated results carry a trailing `_truncated` marker with total count.",
                },
                **_COMPRESS_PARAM,
                **_PROJECT_PARAM,
            },
        },
    },
    "find_symbol": {
        "description": (
        "Locate a symbol: file, line, signature, minimal preview.\n"
        "USE WHEN: you know the name and need its location.\n"
        "NOT WHEN: you also need source + deps — use get_full_context instead."
    ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "name": {"type": "string"},
                **_NAMES_PARAM,
                "level": {
                    "type": "integer",
                    "minimum": 0,
                    "maximum": 2,
                    "description": "0 full, 1 no preview, 2 minimal.",
                },
                "hints": {"type": "boolean", "description": "Add a `_hints` key with next-step tool calls (default true)."},
                **_COMPRESS_PARAM,
                **_PROJECT_PARAM,
            },
        },
    },
    "get_dependencies": {
        "description": (
        "Outgoing deps of a symbol: what X calls/uses (downstream).\n"
        "USE WHEN: tracing a call chain X → Y → Z (depth > 1 walks it in one call).\n"
        "NOT WHEN: incoming (who-calls-X) — use get_dependents; transitive blast radius — use get_change_impact."
    ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "name": {"type": "string"},
                "max_results": {"type": "integer", "description": "Default 100. 0=unlimited. Truncated results carry a trailing `_truncated` marker with total count."},
                "depth": {"type": "integer", "description": "Transitive BFS depth (default 1)."},
                **_COMPRESS_PARAM,
                **_PROJECT_PARAM,
            },
            "required": ["name"],
        },
    },
    "get_dependents": {
        "description": (
        "Incoming deps: who calls/uses X, direct references only.\n"
        "USE WHEN: finding direct callers of a function/class (depth 1).\n"
        "NOT WHEN: outgoing (what X calls) — use get_dependencies; transitive impact — use get_change_impact."
    ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "name": {"type": "string"},
                "max_results": {"type": "integer", "description": "0=all."},
                "max_total_chars": {"type": "integer", "description": "Default 50000."},
                **_COMPRESS_PARAM,
                **_PROJECT_PARAM,
            },
            "required": ["name"],
        },
    },
    "get_change_impact": {
        "description": (
        "Impact analysis: direct + transitive dependents of a symbol.\n"
        "USE WHEN: estimating risk before a rename, delete, or refactor (depth ≥ 2).\n"
        "NOT WHEN: depth 1 who-calls-X — use get_dependents; outgoing (what X calls) — use get_dependencies."
    ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "name": {"type": "string"},
                "max_direct": {"type": "integer", "description": "0=all."},
                "max_transitive": {"type": "integer", "description": "0=all."},
                "max_total_chars": {"type": "integer", "description": "Default 50000."},
                **_PROJECT_PARAM,
            },
            "required": ["name"],
        },
    },
    "get_full_context": {
        "description": (
        "Symbol bundle: location + source + deps/dependents (depth=1) or + change_impact (depth=2).\n"
        "USE WHEN: first-read of an unfamiliar symbol, holistic understanding in one call.\n"
        "NOT WHEN: just the body — use get_function_source / get_class_source; pre-edit safety with siblings + tests — use get_edit_context."
    ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "Symbol name (function, method, class)."},
                **_NAMES_PARAM,
                "depth": {
                    "type": "integer",
                    "minimum": 0,
                    "maximum": 2,
                    "description": "0=symbol+source, 1=+deps/dependents (default), 2=+change_impact.",
                },
                "max_lines": {"type": "integer", "description": "Cap source lines (default 200)."},
                "mode": {
                    "type": "string",
                    "enum": ["compact", "full"],
                    "description": "compact (default): source head 80 lines + deps/dependents as names only. full: raw payload.",
                },
                **_PROJECT_PARAM,
            },
        },
    },
    "get_call_chain": {
        "description": (
        "Shortest dependency path between two symbols (BFS through the dep graph).\n"
        "USE WHEN: proving connectivity A → ... → B, or finding the chain that links them.\n"
        "NOT WHEN: direct neighbors only — use get_dependencies / get_dependents."
    ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "from_name": {
                    "type": "string",
                    "description": "Starting symbol name.",
                },
                "to_name": {
                    "type": "string",
                    "description": "Target symbol name.",
                },
                "level": {
                    "type": "integer",
                    "minimum": 0,
                    "maximum": 2,
                    "description": "Per-hop verbosity: 0=full (source_preview), 1=sig+file, 2=minimal name+file+line. Default 2.",
                },
                **_PROJECT_PARAM,
            },
            "required": ["from_name", "to_name"],
        },
    },
    "get_edit_context": {
        "description": (
        "Pre-edit bundle: source + direct deps + callers + same-file siblings + impacted tests.\n"
        "USE WHEN: about to modify a symbol and want adjacent context upfront.\n"
        "NOT WHEN: just understanding (no edit planned) — use get_full_context; file-wide audit — use audit_file."
    ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "name": {"type": "string"},
                "max_deps": {"type": "integer", "description": "Default 10."},
                "max_callers": {"type": "integer", "description": "Default 10."},
                **_PROJECT_PARAM,
            },
            "required": ["name"],
        },
    },
    "get_file_dependencies": {
        "description": (
        "Files imported by this file (outgoing file-level import edges).\n"
        "USE WHEN: \"what does module X depend on at file level\".\n"
        "NOT WHEN: reverse direction (who imports X) — use get_file_dependents."
    ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "file_path": {
                    "type": "string",
                    "description": "Relative path to the file.",
                },
                "max_results": {
                    "type": "integer",
                    "description": "Maximum number of results to return (0 = unlimited, default 0).",
                },
                **_PROJECT_PARAM,
            },
            "required": ["file_path"],
        },
    },
    "get_file_dependents": {
        "description": (
        "Files that import this file (incoming file-level import edges).\n"
        "USE WHEN: \"who depends on module X\" — cheaper than search_codebase('import X').\n"
        "NOT WHEN: outgoing (what X imports) — use get_file_dependencies."
    ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "file_path": {
                    "type": "string",
                    "description": "Relative path to the file.",
                },
                "max_results": {
                    "type": "integer",
                    "description": "Maximum number of results to return (0 = unlimited, default 0).",
                },
                **_PROJECT_PARAM,
            },
            "required": ["file_path"],
        },
    },
    "search_codebase": {
        "description": (
        "Regex (default) or semantic (semantic=true) search across indexed files.\n"
        "USE WHEN: regex for exact pattern/literal strings. Semantic for NL descriptions when you don't know the name.\n"
        "NOT WHEN: you already know the symbol name — use find_symbol (cheaper).\n"
        "SAFETY: semantic hits are leads, not answers; re-resolve via find_symbol before acting. First semantic call triggers ~2min reindex."
    ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "pattern": {
                    "type": "string",
                    "description": (
                        "Regex pattern (regex mode) or natural-language "
                        "description (semantic mode)."
                    ),
                },
                "max_results": {
                    "type": "integer",
                    "description": "Maximum number of results to return (default 100, 0 = unlimited).",
                },
                "ignore_generated": {
                    "type": "boolean",
                    "description": "Skip generated/minified files (default true). Regex mode only.",
                },
                "semantic": {
                    "type": "boolean",
                    "description": (
                        "If true, interpret `pattern` as a description and "
                        "rank symbols by embedding cosine similarity. "
                        "Returns enriched hits with signature/docstring/"
                        "score. Default false (regex)."
                    ),
                },
                **_PROJECT_PARAM,
            },
            "required": ["pattern"],
        },
    },
    "search_in_symbols": {
        "description": (
        "Regex search that returns the enclosing function/class for each match, in addition to file:line.\n"
        "USE WHEN: the next step is \"read the containing symbol\" — the `symbol` field plugs into get_function_source / get_class_source.\n"
        "NOT WHEN: plain file-line hits are enough — use search_codebase (cheaper)."
    ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "pattern": {
                    "type": "string",
                    "description": "Regular expression pattern to search for.",
                },
                "max_results": {
                    "type": "integer",
                    "description": "Maximum number of results to return (default 100, 0 = unlimited).",
                },
                **_PROJECT_PARAM,
            },
            "required": ["pattern"],
        },
    },
    # ── Index management ──────────────────────────────────────────────────
    "reindex": {
        "description": (
        "Rebuild the project index.\n"
        "USE WHEN: a file was edited outside TS tools (via your editor, sed, git pull) and you want the new state indexed.\n"
        "NOT WHEN: you just used replace_symbol_source / insert_near_symbol — those reindex inline."
    ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "force": {
                    "type": "boolean",
                    "description": "Rebuild even if no mtime changes detected.",
                },
                **_PROJECT_PARAM,
            },
        },
    },
    "set_project_root": {
        "description": (
        "Register a new project root and switch to it.\n"
        "USE WHEN: the user opens a project that isn't in WORKSPACE_ROOTS yet.\n"
        "NOT WHEN: the project is already registered — use switch_project.\n"
        "Triggers a full reindex of the new root."
    ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Absolute path to the project root directory.",
                },
            },
            "required": ["path"],
        },
    },
    # ── Feature discovery ─────────────────────────────────────────────────
    "get_feature_files": {
        "description": (
        "Files matching a feature keyword + traced imports, classified by role (core, test, config).\n"
        "USE WHEN: locating all files touching a feature before diving in.\n"
        "NOT WHEN: plain glob filtering — use list_files (cheaper)."
    ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "keyword": {"type": "string"},
                "max_results": {"type": "integer", "description": "0=all."},
                **_PROJECT_PARAM,
            },
            "required": ["keyword"],
        },
    },
    # ── Stats (unified) ───────────────────────────────────────────────────
    "get_stats": {
        "description": (
        "Unified stats dispatcher. category = usage (default) | session_budget | tca | dcp | linucb | warmstart | leiden | speculation | lattice.\n"
        "USE WHEN: inspecting a specific subsystem's internal state.\n"
        "NOT WHEN: overall project health check — use get_project_summary (code) or memory_doctor (memory, full profile).\n"
        "usage = session efficiency & chars saved; others = per-subsystem diagnostics."
    ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "category": {
                    "type": "string",
                    "enum": [
                        "usage", "session_budget", "tca", "dcp", "linucb",
                        "warmstart", "leiden", "speculation", "lattice",
                    ],
                    "description": "Which stats subsystem to report (default 'usage').",
                },
                "context_type": {
                    "type": "string",
                    "description": "For category=lattice: filter to one context (navigation/edit/review/unknown).",
                },
                "budget_tokens": {
                    "type": "integer",
                    "description": "For category=session_budget: soft budget cap (default 200000).",
                },
                **_PROJECT_PARAM,
            },
        },
    },
    # ── Routes, Env, Components ───────────────────────────────────────────
    "get_routes": {
        "description": (
        "Detect API routes and pages in a Next.js App Router project: path, file, HTTP methods, type.\n"
        "USE WHEN: mapping the HTTP surface of a Next.js project.\n"
        "NOT WHEN: CLI/main entry points — use get_entry_points; non-Next frameworks aren't covered here."
    ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "max_results": {
                    "type": "integer",
                    "description": "Max routes to return (0 = all, default 0).",
                },
                **_PROJECT_PARAM,
            },
        },
    },
    "get_env_usage": {
        "description": (
        "Cross-reference an env var across code, .env files, and workflow configs. Shows where it's defined, read, written.\n"
        "USE WHEN: auditing an env var's lifecycle before renaming or removing it.\n"
        "NOT WHEN: bulk orphan detection — use analyze_config(checks=['orphans'])."
    ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "var_name": {
                    "type": "string",
                    "description": "Environment variable name (e.g. HELLOASSO_CLIENT_ID).",
                },
                "max_results": {
                    "type": "integer",
                    "description": "Max results (0 = all, default 0).",
                },
                **_PROJECT_PARAM,
            },
            "required": ["var_name"],
        },
    },
    "get_components": {
        "description": (
        "Detect React components in .tsx/.jsx: pages, layouts, named (uppercase) and default exports.\n"
        "USE WHEN: inventorying UI surface of a React/Next project.\n"
        "NOT WHEN: generic class or function listing — use get_classes / get_functions."
    ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "file_path": {
                    "type": "string",
                    "description": "Optional file to scan (default: all .tsx/.jsx).",
                },
                "max_results": {
                    "type": "integer",
                    "description": "Max results (0 = all, default 0).",
                },
                **_PROJECT_PARAM,
            },
        },
    },
    # ── Analysis tools ────────────────────────────────────────────────────
    "analyze_config": {
        "description": (
        "Audit config files (.env/.yaml/.toml/.json): duplicates, secrets, orphans.\n"
        "USE WHEN: pre-deploy sanity check, or after touching .env to catch orphaned vars.\n"
        "NOT WHEN: you need raw file content — use your client's file-read tool.\n"
        "checks=['orphans'] = cheapest single-check mode."
    ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "checks": {
                    "type": "array",
                    "items": {"type": "string", "enum": ["duplicates", "secrets", "orphans", "loaders", "schema"]},
                    "description": "Checks to run",
                },
                "file_path": {"type": "string", "description": "Specific config file"},
                "severity": {"type": "string", "enum": ["all", "error", "warning"], "description": "Severity filter"},
                "max_issues": {"type": "integer", "description": "Cap total issues shown (default 10, 0 = unlimited). Raise for full audit."},
                **_PROJECT_PARAM,
            },
        },
    },
    "find_dead_code": {
        "description": (
        "Project-wide audit of unreferenced functions/classes (zero callers, excludes entry points, tests, route handlers).\n"
        "USE WHEN: pre-release cleanup or refactor scoping.\n"
        "NOT WHEN: file-scoped audit — use audit_file (batches dead_code + hotspots + duplicates); just list a file's functions — use get_functions."
    ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "max_results": {
                    "type": "integer",
                    "description": "Maximum number of dead symbols to report (default: 20). Header always shows true total; raise for full audit.",
                },
                **_PROJECT_PARAM,
            },
        },
    },
    "find_hotspots": {
        "description": (
        "Rank functions by hotspot kind. complexity (all langs) | allocation (Java) | performance (Java).\n"
        "USE WHEN: picking refactor targets across a project.\n"
        "NOT WHEN: scoped to one file — use audit_file (batches dead_code + hotspots + duplicates).\n"
        "T0-T3 tiers rank actionability. min_score default 0 for complexity, 1 for Java kinds."
    ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "kind": {
                    "type": "string",
                    "enum": ["complexity", "allocation", "performance"],
                    "description": "Hotspot category (default 'complexity').",
                },
                "max_results": {
                    "type": "integer",
                    "description": "Maximum number of functions to report (default: 20).",
                },
                "min_score": {
                    "type": "number",
                    "description": "Minimum score to include (default: 0 for complexity, 1 for Java kinds).",
                },
                **_PROJECT_PARAM,
            },
        },
    },
    "detect_breaking_changes": {
        "description": (
        "Breaking API changes vs a git ref: removed funcs/params, added required params, signature changes.\n"
        "USE WHEN: pre-merge / pre-release gate to catch caller-breaking edits.\n"
        "NOT WHEN: you want a plain symbol-level diff — use get_changed_symbols.\n"
        "TERMINAL — output is complete; do not re-explore each item."
    ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "since_ref": {
                    "type": "string",
                    "description": 'Git ref to compare against (default: "HEAD~1"). Can be a commit SHA, branch, or tag.',
                },
                **_PROJECT_PARAM,
            },
        },
    },
    "find_cross_project_deps": {
        "description": (
        "Dependencies between indexed projects: which project imports packages from other indexed projects.\n"
        "USE WHEN: understanding reach across a multi-repo workspace.\n"
        "NOT WHEN: within one project — use get_file_dependents."
    ),
        "inputSchema": {
            "type": "object",
            "properties": {},
        },
    },
    "analyze_docker": {
        "description": (
        "Audit Dockerfiles: base images, stages, exposed ports, ENV/ARG, cross-ref with config files.\n"
        "USE WHEN: pre-deploy Docker review. Flags 'latest' tags and missing env vars.\n"
        "NOT WHEN: non-Dockerfile config audit — use analyze_config."
    ),
        "inputSchema": {
            "type": "object",
            "properties": {
                **_PROJECT_PARAM,
            },
        },
    },
    "get_db_schema": {
        "description": (
        "Condensed SQL-migration snapshot: tables (cols, types, nullability, defaults), PKs, FKs, indexes, RLS policies.\n"
        "USE WHEN: writing queries against a Supabase/Postgres-like schema — avoids re-reading raw .sql each time.\n"
        "NOT WHEN: you need raw migration history — use your client's file-read tool.\n"
        "Auto-detects supabase/migrations, migrations/, db/migrations/, prisma/migrations/."
    ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "migrations_dir": {
                    "type": "string",
                    "description": "Relative or absolute path to the migrations directory (default: auto-detect).",
                },
                "dialect": {
                    "type": "string",
                    "description": "SQL dialect -- currently only 'postgres' is implemented.",
                },
                "tables": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Optional filter: only return these table names.",
                },
                **_PROJECT_PARAM,
            },
        },
    },
    "get_library_symbol": {
        "description": (
        "Resolve a library symbol (npm .d.ts or Python module): signature, JSDoc/docstring, source location.\n"
        "USE WHEN: checking the installed version's exact signature — beats Context7/doc fetches, ~100 tokens.\n"
        "NOT WHEN: you don't know the exact name — use find_library_symbol_by_description (NL) or list_library_symbols (regex).\n"
        "For dotted paths, pass the full chain (e.g. 'SupabaseAuthClient.signInWithOtp')."
    ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "package": {
                    "type": "string",
                    "description": "npm package name (e.g. '@supabase/supabase-js') or Python module (e.g. 'pandas').",
                },
                "symbol_path": {
                    "type": "string",
                    "description": "Dotted symbol path inside the package (e.g. 'createClient', 'SupabaseAuthClient.signInWithOtp').",
                },
                "max_files": {
                    "type": "integer",
                    "description": "Cap on .d.ts files scanned (default 200).",
                },
                **_PROJECT_PARAM,
            },
            "required": ["package"],
        },
    },
    "list_library_symbols": {
        "description": (
        "List top-level exports of an installed library (.d.ts or Python module), optionally regex-filtered.\n"
        "USE WHEN: you know roughly what you want but not the exact name, and regex is enough.\n"
        "NOT WHEN: NL description without regex — use find_library_symbol_by_description; exact name known — use get_library_symbol."
    ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "package": {
                    "type": "string",
                    "description": "npm package name or Python module.",
                },
                "pattern": {
                    "type": "string",
                    "description": "Case-insensitive regex filter on symbol names (optional).",
                },
                "max_files": {
                    "type": "integer",
                    "description": "Cap on .d.ts files scanned (default 100).",
                },
                "limit": {
                    "type": "integer",
                    "description": "Cap on results (default 100).",
                },
                **_PROJECT_PARAM,
            },
            "required": ["package"],
        },
    },
    "find_library_symbol_by_description": {
        "description": (
        "Rank a package's exports by Nomic-embedding similarity to a NL description. On-the-fly, no persistent index.\n"
        "USE WHEN: you don't know the exact name of a library export you need.\n"
        "NOT WHEN: you know the name — use get_library_symbol (exact); regex-filtered enum is enough — use list_library_symbols.\n"
        "SAFETY: re-resolve via get_library_symbol before acting. No low-confidence warning (scores don't discriminate on short docs)."
    ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "package": {
                    "type": "string",
                    "description": "npm package name or Python module.",
                },
                "description": {
                    "type": "string",
                    "description": "Natural-language description of what the symbol does.",
                },
                "limit": {
                    "type": "integer",
                    "description": "Top-K hits to return (default 10).",
                },
                "max_files": {
                    "type": "integer",
                    "description": "Cap on .d.ts files scanned (default 100).",
                },
                "candidate_pool": {
                    "type": "integer",
                    "description": "Max exports considered before ranking (default 200).",
                },
                **_PROJECT_PARAM,
            },
            "required": ["package", "description"],
        },
    },
    "audit_file": {
        "description": (
        "Mega-batch audit of a single file: dead_code + hotspots + semantic duplicates in one call.\n"
        "USE WHEN: triaging or reviewing a specific file before touching it.\n"
        "NOT WHEN: project-wide hotspot ranking — use find_hotspots."
    ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "file_path": {"type": "string", "description": "Relative path of the file to audit."},
                "max_dead": {"type": "integer", "description": "Cap on dead-code scan (default 50)."},
                "max_hotspots": {"type": "integer", "description": "Cap on hotspot scan (default 50)."},
                "min_score": {"type": "number", "description": "Minimum complexity score (default 0)."},
                "min_lines": {"type": "integer", "description": "Semantic-dup min length (default 6)."},
                "max_dup_groups": {"type": "integer", "description": "Semantic-dup group cap (default 20)."},
                **_PROJECT_PARAM,
            },
            "required": ["file_path"],
        },
    },
    "get_entry_points": {
        "description": (
        "Score functions by likelihood of being execution entry points: routes, handlers, main, exported APIs.\n"
        "USE WHEN: understanding a project's entry surface during onboarding.\n"
        "NOT WHEN: HTTP routes specifically in Next.js — use get_routes; React UI — use get_components."
    ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "max_results": {
                    "type": "integer",
                    "description": "Maximum number of entry points to return (default 20).",
                },
                **_PROJECT_PARAM,
            },
        },
    },
    "get_related_symbols": {
        "description": (
        "Related-symbols query. method = community | rwr | cluster | coactive.\n"
        "USE WHEN: finding symbols adjacent to X via graph or access patterns (beyond direct deps).\n"
        "NOT WHEN: direct deps only — use get_dependencies / get_dependents; call path A→B — use get_call_chain.\n"
        "community=Leiden; rwr=random-walk-with-restart; cluster=greedy modularity; coactive=TCA co-access."
    ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "method": {
                    "type": "string",
                    "enum": ["community", "rwr", "cluster", "coactive"],
                    "description": "Algorithm (default 'community').",
                },
                "name": {
                    "type": "string",
                    "description": "Seed symbol. Required for rwr/cluster/coactive; optional for community when list_all=true.",
                },
                "max_members": {
                    "type": "integer",
                    "description": "cluster: max members (default 30).",
                },
                "budget": {
                    "type": "integer",
                    "description": "rwr: top-K symbols (default 10).",
                },
                "include_reverse": {
                    "type": "boolean",
                    "description": "rwr: include reverse-dependency edges (default true).",
                },
                "top_k": {
                    "type": "integer",
                    "description": "coactive: max results (default 5).",
                },
                "community_name": {
                    "type": "string",
                    "description": "community: look up by community name instead of seed symbol.",
                },
                "list_all": {
                    "type": "boolean",
                    "description": "community: enumerate all communities (default false).",
                },
                "min_size": {
                    "type": "integer",
                    "description": "community+list_all: min members (default 2).",
                },
                "max_results": {
                    "type": "integer",
                    "description": "community+list_all: max communities (default 30).",
                },
                **_PROJECT_PARAM,
            },
        },
    },
    "get_duplicate_classes": {
        "description": (
        "Find Java classes duplicated across files (by FQN or simple name).\n"
        "USE WHEN: Java-specific class-level dedup scan.\n"
        "NOT WHEN: generic function/class dedup across any language — use find_semantic_duplicates (AST or embedding)."
    ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "Filter class."},
                "simple_name_mode": {"type": "boolean", "description": "Group by simple name."},
                "max_results": {"type": "integer", "description": "0=all."},
                **_PROJECT_PARAM,
            },
        },
    },
    # ── Memory Engine tools ───────────────────────────────────────────────
    "memory_save": {
        "description": (
        "Persist a fact, guardrail, or note across sessions.\n"
        "USE WHEN: the user says \"remember this\" or a finding is non-obvious from code.\n"
        "NOT WHEN: auto-capture via PostToolUse hooks already covers it (bash, WebFetch).\n"
        "Types: note, command, infra, guardrail, warning. Scope project_root by default."
    ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "type": {
                    "type": "string",
                    "enum": [
                        "user", "feedback", "project", "reference",
                        "guardrail", "error_pattern", "decision", "convention",
                        "bugfix", "warning", "note",
                        "command", "research", "infra", "config", "idea",
                        "ruled_out",
                    ],
                },
                "title": {"type": "string"},
                "content": {"type": "string"},
                "why": {"type": "string"},
                "how_to_apply": {"type": "string"},
                "symbol": {"type": "string"},
                "file_path": {"type": "string"},
                "context": {"type": "string"},
                "tags": {"type": "array", "items": {"type": "string"}},
                "importance": {"type": "integer", "description": "1-10"},
                "session_id": {"type": "integer"},
                "is_global": {"type": "boolean"},
                "ttl_days": {"type": "integer"},
                "narrative": {
                    "type": "string",
                    "description": "Optional free-form narrative explaining the obs in prose.",
                },
                "facts": {
                    "type": "string",
                    "description": "Optional atomic facts (JSON array or bullet list).",
                },
                "concepts": {
                    "type": "string",
                    "description": "Optional conceptual tags (JSON array or comma list).",
                },
                **_PROJECT_PARAM,
            },
            "required": ["type", "title", "content"],
        },
    },
    "memory_maintain": {
        "description": (
        "Maintenance rollup: promote, relink, export, extract patterns.\n"
        "USE WHEN: periodic memory housekeeping (weekly or pre-distillation)."
    ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "action": {"type": "string", "enum": ["promote", "relink", "export", "patterns"], "description": "Action"},
                "dry_run": {"type": "boolean", "description": "Preview only"},
                "output_dir": {"type": "string", "description": "Export dir"},
                "window_days": {"type": "integer", "description": "Patterns window"},
                "min_occurrences": {"type": "integer", "description": "Patterns threshold"},
                **_PROJECT_PARAM,
            },
            "required": ["action"],
        },
    },
    "memory_top": {
        "description": (
        "Rank observations by score, access_count, or age.\n"
        "USE WHEN: auditing what's most-touched or oldest in the store."
    ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "limit": {"type": "integer", "description": "Default 20."},
                "sort_by": {"type": "string", "enum": ["score", "access_count", "age"]},
            },
        },
    },
    "memory_why": {
        "description": (
        "Explain why a specific observation matched the last injection (recency, type, symbol, FTS).\n"
        "USE WHEN: debugging why a memory did or didn't surface."
    ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "id": {"type": "integer"},
                "query": {"type": "string", "description": "Optional FTS query."},
            },
            "required": ["id"],
        },
    },
    "memory_doctor": {
        "description": (
        "Memory health report: orphans, near-dupes, incomplete obs, vector coverage, hook wiring.\n"
        "USE WHEN: diagnosing why auto-injection / recall is flaky or silent.\n"
        "NOT WHEN: generic tool-usage stats — use get_stats."
    ),
        "inputSchema": {"type": "object", "properties": {}},
    },
    "memory_vector_reindex": {
        "description": (
        "Backfill obs_vectors for observations missing an embedding. No-op if sqlite-vec/fastembed unavailable.\n"
        "USE WHEN: after enabling the vector extra, or post-corruption recovery of the vec table."
    ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "limit": {"type": "integer", "description": "Max obs to index this run (default 500)."},
                **_PROJECT_PARAM,
            },
        },
    },
    "memory_distill": {
        "description": (
        "MDL-based distillation: cluster similar obs into an abstraction + deltas.\n"
        "USE WHEN: compressing N near-equivalent obs into one canonical + diffs."
    ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "dry_run": {"type": "boolean", "description": "Preview (default true)."},
                "min_cluster_size": {"type": "integer", "description": "Default 3."},
                "compression_required": {"type": "number", "description": "Default 0.2."},
                **_PROJECT_PARAM,
            },
        },
    },
    "memory_dedup_sweep": {
        "description": (
        "Backfill observations.content_hash (SHA256 of normalized content). Default: only NULL hashes.\n"
        "USE WHEN: after a hash-formula change (recompute=true) or one-off backfill of old obs."
    ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "recompute": {"type": "boolean", "description": "Rehash every row, not just NULL (default false)."},
                "batch_size": {"type": "integer", "description": "Commit cadence (default 500)."},
                **_PROJECT_PARAM,
            },
        },
    },
    "memory_roi_gc": {
        "description": (
        "Archive observations whose ROI score falls below a threshold.\n"
        "USE WHEN: periodic cleanup pass to prune low-utility memories."
    ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "dry_run": {"type": "boolean", "description": "Preview (default true)."},
                "threshold": {"type": "number", "description": "Default 0.0."},
                **_PROJECT_PARAM,
            },
        },
    },
    "memory_roi_stats": {
        "description": (
        "Token Economy ROI stats — net value by observation type.\n"
        "USE WHEN: measuring which observation types pay for their token cost."
    ),
        "inputSchema": {
            "type": "object",
            "properties": {**_PROJECT_PARAM},
        },
    },
    "memory_from_bash": {
        "description": (
        "Save a bash command as an observation (type=command, auto-extracted).\n"
        "USE WHEN: manual capture of a shell command worth remembering.\n"
        "NOT WHEN: generic fact/note capture — use memory_save (more flexible schema)."
    ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "command": {"type": "string"},
                "type": {"type": "string", "enum": ["command", "infra", "config"]},
                "context": {"type": "string"},
                **_PROJECT_PARAM,
            },
            "required": ["command"],
        },
    },
    "memory_set_global": {
        "description": (
        "Set an observation's global visibility flag (is_global=True crosses all projects).\n"
        "USE WHEN: promoting a project-scoped obs to cross-project (e.g. a VPS-wide convention)."
    ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "id": {"type": "integer", "description": "Observation ID"},
                "is_global": {"type": "boolean", "description": "True=global, False=local"},
            },
            "required": ["id", "is_global"],
        },
    },
    "memory_search": {
        "description": (
        "Layer 2 FTS5 search over memory observations, compact rows with snippets (~60 tokens/result).\n"
        "USE WHEN: finding prior memories relevant to the current task.\n"
        "NOT WHEN: the user wants to persist a new fact — use memory_save (write path, lean default)."
    ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "FTS5 (AND/OR/NOT/phrase)."},
                "type_filter": {"type": "string"},
                "limit": {"type": "integer", "description": "Default 20."},
                **_PROJECT_PARAM,
            },
            "required": ["query"],
        },
    },
    "memory_session_history": {
        "description": (
        "Last N structured session-end rollups (request, investigated, learned, completed, next_steps, notes).\n"
        "USE WHEN: catching up on what happened in recent sessions on this project."
    ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "limit": {"type": "integer", "description": "Default 10."},
                **_PROJECT_PARAM,
            },
        },
    },
    "memory_get": {
        "description": (
        "Layer 3: full observation content by IDs (~200 tokens/result). Final progressive-disclosure layer.\n"
        "USE WHEN: reading the full body of an observation surfaced by memory_search or memory_index."
    ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "ids": {
                    "type": "array",
                    "items": {"type": ["integer", "string"]},
                    "description": (
                        "Observation IDs. Each item may be an integer (42), a "
                        "digit string (\"42\"), or a citation URI (\"ts://obs/42\")."
                    ),
                },
                "full": {
                    "type": "boolean",
                    "description": "If false (default), content trimmed to 80 chars. If true, full content.",
                },
                **_PROJECT_PARAM,
            },
            "required": ["ids"],
        },
    },
    "memory_delete": {
        "description": (
        "Soft-delete an observation by ID (sets archived=1).\n"
        "USE WHEN: removing a specific obs confirmed wrong or obsolete."
    ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "id": {
                    "type": "integer",
                    "description": "Observation ID to archive.",
                },
                **_PROJECT_PARAM,
            },
            "required": ["id"],
        },
    },
    "memory_index": {
        "description": (
        "Layer 1: compact index of recent observations — ID, type, title, importance, age, citation URI.\n"
        "USE WHEN: cheapest memory exploration — always start here before drilling deeper."
    ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "limit": {
                    "type": "integer",
                    "description": "Max entries to return (default 30).",
                },
                "type_filter": {
                    "type": "string",
                    "description": "Filter by observation type (optional).",
                },
                **_PROJECT_PARAM,
            },
            "required": [],
        },
    },
    "memory_timeline": {
        "description": (
        "Chronological context around an observation (before/after in time).\n"
        "USE WHEN: reconstructing the temporal sequence that produced an obs."
    ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "observation_id": {
                    "type": "integer",
                    "description": "Center observation ID.",
                },
                "window": {
                    "type": "integer",
                    "description": "Window in hours around the observation (default 24).",
                },
                **_PROJECT_PARAM,
            },
            "required": ["observation_id"],
        },
    },
    "memory_prompts": {
        "description": (
        "Save or search prompt history (archival of notable user prompts).\n"
        "USE WHEN: pinning the current prompt or retrieving a past one."
    ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "action": {"type": "string", "enum": ["save", "search"], "description": "save or search"},
                "prompt_text": {"type": "string", "description": "Prompt to save"},
                "prompt_number": {"type": "integer", "description": "Prompt ordinal"},
                "query": {"type": "string", "description": "Search query"},
                "limit": {"type": "integer", "description": "Max results"},
                **_PROJECT_PARAM,
            },
            "required": ["action"],
        },
    },
    "memory_mode": {
        "description": (
        "Get or set the memory capture mode (code | review | debug | infra | silent).\n"
        "USE WHEN: switching the session's capture aggressiveness."
    ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "action": {"type": "string", "enum": ["get", "set", "set_project"], "description": "Action"},
                "mode": {"type": "string", "enum": ["code", "review", "debug", "silent"], "description": "Mode name"},
                "project": {"type": "string", "description": "Project path"},
            },
            "required": ["action"],
        },
    },
    "corpus_build": {
        "description": (
        "Build a thematic corpus from observations filtered by type / tags / symbol.\n"
        "USE WHEN: assembling a focused knowledge slice for downstream Q&A."
    ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "Unique per project."},
                "filter_type": {"type": "string"},
                "filter_tags": {"type": "array", "items": {"type": "string"}},
                "filter_symbol": {"type": "string"},
                **_PROJECT_PARAM,
            },
            "required": ["name"],
        },
    },
    "memory_archive": {
        "description": (
        "Manage archived observations (list, undelete, purge).\n"
        "USE WHEN: recovering a soft-deleted obs or cleaning dead archives."
    ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "action": {"type": "string", "enum": ["run", "list", "restore"], "description": "run=decay, list, restore"},
                "id": {"type": "integer", "description": "ID for restore"},
                "dry_run": {"type": "boolean", "description": "Preview only"},
                "limit": {"type": "integer", "description": "List max entries"},
                **_PROJECT_PARAM,
            },
            "required": ["action"],
        },
    },
    "memory_status": {
        "description": (
        "Memory Engine snapshot: active/archived counts, mode, last session, summaries.\n"
        "USE WHEN: quick state check of the memory store at session start."
    ),
        "inputSchema": {
            "type": "object",
            "properties": {},
        },
    },
    # ── Program slicing & context packing (Phase 2) ───────────────────────
    "verify_edit": {
        "description": (
        "EditSafety certificate — static analysis before a symbol replacement: signature, exceptions, side-effects, test impact.\n"
        "USE WHEN: gating a destructive edit with cheap static checks.\n"
        "NOT WHEN: you want execution-level validation — use apply_symbol_change_and_validate."
    ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "symbol_name": {
                    "type": "string",
                    "description": "Symbol that would be replaced.",
                },
                "new_source": {
                    "type": "string",
                    "description": "Proposed replacement source.",
                },
                "file_path": {
                    "type": "string",
                    "description": "Optional file path to disambiguate the symbol.",
                },
                **_PROJECT_PARAM,
            },
            "required": ["symbol_name", "new_source"],
        },
    },
    "find_semantic_duplicates": {
        "description": (
        "Find duplicate functions. method='ast' (fast, hash-based, catches copy-paste) or 'embedding' (Nomic cosine, catches conceptual clones, tagged sim=min..mean per cluster).\n"
        "USE WHEN: scoping a dedup or consolidation pass across the project.\n"
        "NOT WHEN: file-scoped audit — use audit_file (batches dead_code + hotspots + duplicates).\n"
        "SAFETY: always verify via get_function_source before merging; embedding matches can be conceptual not functional."
    ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "min_lines": {
                    "type": "integer",
                    "description": "Skip functions shorter than this (default 2). Applies to method='ast'.",
                },
                "max_groups": {
                    "type": "integer",
                    "description": "Max duplicate groups to return (default 10). Raise for full audit.",
                },
                "method": {
                    "type": "string",
                    "enum": ["ast", "embedding"],
                    "description": (
                        "ast (default, fast, exact) or embedding (slower, "
                        "catches conceptual clones). Embedding reuses the "
                        "symbol_vectors index from search_codebase(semantic=True) "
                        "— first call triggers a ~2min reindex."
                    ),
                },
                "min_similarity": {
                    "type": "number",
                    "description": (
                        "Cosine threshold for method='embedding' (default 0.90). "
                        "Lower = more recall + more noise."
                    ),
                },
                **_PROJECT_PARAM,
            },
        },
    },
    "find_import_cycles": {
        "description": (
        "Detect import cycles (strongly-connected components) in the file-level import graph (Tarjan's).\n"
        "USE WHEN: debugging a circular-import error or preparing a module reorg.\n"
        "NOT WHEN: you want neighbors of one file — use get_file_dependencies / get_file_dependents."
    ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "max_cycles": {
                    "type": "integer",
                    "description": "Maximum number of cycles to return (default 20, 0 = unlimited).",
                },
                **_PROJECT_PARAM,
            },
        },
    },
    "get_call_predictions": {
        "description": (
        "Predict next-likely tool calls from a first-order Markov model trained on prior sessions.\n"
        "USE WHEN: inspecting what the prefetcher thinks you'll do next."
    ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "tool_name": {
                    "type": "string",
                    "description": "Current tool name (e.g. 'get_function_source').",
                },
                "symbol_name": {
                    "type": "string",
                    "description": "Optional current symbol focus (e.g. 'observation_save').",
                },
                "top_k": {
                    "type": "integer",
                    "description": "Maximum number of predictions to return (default 5).",
                },
            },
            "required": ["tool_name"],
        },
    },
    "pack_context": {
        "description": (
        "Knapsack-packed context bundle for a query within a token budget.\n"
        "USE WHEN: manual token-budget assembly for a complex handoff prompt.\n"
        "NOT WHEN: standard symbol lookup with deps — use get_full_context."
    ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "budget_tokens": {"type": "integer", "description": "Default 4000."},
                "max_symbols": {"type": "integer", "description": "Default 20."},
                **_PROJECT_PARAM,
            },
            "required": ["query"],
        },
    },
    "get_backward_slice": {
        "description": (
        "Minimal lines affecting a variable at a given line inside a symbol.\n"
        "USE WHEN: extracting the causal slice behind one value for debugging.\n"
        "NOT WHEN: you want callers of the enclosing symbol — use get_dependents."
    ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "name": {"type": "string"},
                "variable": {"type": "string"},
                "line": {"type": "integer", "description": "1-based."},
                "file_path": {"type": "string"},
                **_PROJECT_PARAM,
            },
            "required": ["name", "variable", "line"],
        },
    },
    "corpus_query": {
        "description": (
        "Format all observations of a named corpus as markdown context + a question, ready for answering.\n"
        "USE WHEN: asking a question over a pre-built corpus (after corpus_build)."
    ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "name": {
                    "type": "string",
                    "description": "Corpus name previously built via corpus_build.",
                },
                "question": {
                    "type": "string",
                    "description": "Question to answer with the corpus context.",
                },
                **_PROJECT_PARAM,
            },
            "required": ["name", "question"],
        },
    },
    "memory_bus_push": {
        "description": (
        "Push a volatile observation to the inter-agent memory bus (tagged by agent_id).\n"
        "USE WHEN: publishing a live note to peer agents without persisting to the main store."
    ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "agent_id": {"type": "string"},
                "title": {"type": "string"},
                "content": {"type": "string"},
                "type": {"type": "string", "description": "Default 'note'."},
                "symbol": {"type": "string"},
                "file_path": {"type": "string"},
                "tags": {"type": "array", "items": {"type": "string"}},
                "ttl_days": {"type": "integer", "description": "Default 1."},
                **_PROJECT_PARAM,
            },
            "required": ["agent_id", "title", "content"],
        },
    },
    "memory_bus_list": {
        "description": (
        "List recent live messages on the inter-agent memory bus, optionally filtered by agent_id.\n"
        "USE WHEN: reading peer-agent signals during a multi-agent task."
    ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "agent_id": {"type": "string", "description": "Filter by subagent id (optional)."},
                "limit": {"type": "integer", "description": "Max rows (default 20)."},
                "include_expired": {"type": "boolean", "description": "Show expired bus rows too."},
                **_PROJECT_PARAM,
            },
        },
    },
    "reasoning_save": {
        "description": (
        "Persist a reasoning trace (goal + steps + conclusion) for later reuse.\n"
        "USE WHEN: capturing a multi-step analysis you expect to repeat."
    ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "goal": {"type": "string"},
                "steps": {"type": "array", "items": {"type": "object"}, "description": "[{tool,args,observation},...]"},
                "conclusion": {"type": "string"},
                "confidence": {"type": "number", "description": "0.0-1.0 (default 0.8)."},
                "evidence_obs_ids": {"type": "array", "items": {"type": "integer"}},
                "ttl_days": {"type": "integer"},
                **_PROJECT_PARAM,
            },
            "required": ["goal", "steps", "conclusion"],
        },
    },
    "reasoning_search": {
        "description": (
        "Search stored reasoning chains by goal similarity (FTS5 + Jaccard).\n"
        "USE WHEN: checking if you've already reasoned about a similar goal in a prior session."
    ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Goal-like query text."},
                "threshold": {
                    "type": "number",
                    "description": "Minimum Jaccard similarity (default 0.3).",
                },
                "limit": {"type": "integer", "description": "Max rows (default 5)."},
                **_PROJECT_PARAM,
            },
            "required": ["query"],
        },
    },
    "reasoning_list": {
        "description": (
        "List stored reasoning chains by access_count then recency.\n"
        "USE WHEN: enumerating all past reasoning — pick one before reasoning_search for targeted lookup."
    ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "limit": {"type": "integer", "description": "Max rows (default 50)."},
                **_PROJECT_PARAM,
            },
        },
    },
    "memory_consistency": {
        "description": (
        "Run Bayesian self-consistency check on symbol-linked obs (updates α/β; flags stale + quarantine).\n"
        "USE WHEN: periodic sweep to flag obs that reference now-invalid symbols."
    ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "project_root": {
                    "type": "string",
                    "description": "Project filter; omit to run across all projects.",
                },
                "limit": {
                    "type": "integer",
                    "description": "Max observations to check this pass (default 100).",
                },
                "dry_run": {
                    "type": "boolean",
                    "description": "Report what would change without persisting.",
                },
            },
        },
    },
    "memory_quarantine_list": {
        "description": (
        "List observations quarantined by the consistency check (Bayesian validity < 40 %).\n"
        "USE WHEN: reviewing obs the consistency sweep flagged as suspect."
    ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "project_root": {
                    "type": "string",
                    "description": "Filter by project; omit for all projects.",
                },
                "limit": {
                    "type": "integer",
                    "description": "Max rows to return (default 50).",
                },
            },
        },
    },
    # ── Tool Capture (sandbox of verbose tool outputs) ───────────────────
    "capture_put": {
        "description": (
            "Persist a tool output (Bash, WebFetch, Playwright, gh api, MCP) outside context.\n"
            "USE WHEN: a hook or an explicit caller wants to stash a >8KB output and only return a preview to the model.\n"
            "NOT WHEN: the output is small enough (<2KB) that holding it in context costs nothing."
        ),
        "inputSchema": {
            "type": "object",
            "required": ["tool_name", "output"],
            "properties": {
                "tool_name": {"type": "string", "description": "Logical tool name (e.g. 'Bash', 'WebFetch', 'mcp__playwright__snapshot')."},
                "output": {"type": "string", "description": "Full raw output to capture."},
                "args_summary": {"type": "string", "description": "Short human description of the call (URL, command, query)."},
                "session_id": {"type": "string", "description": "Optional session id to scope retrieval."},
                "project_root": {"type": "string", "description": "Optional active project root."},
                "meta": {"type": "object", "description": "Free-form metadata stored alongside the capture."},
            },
        },
    },
    "capture_search": {
        "description": (
            "BM25 search across captured tool outputs. Returns rows with id, snippet, bytes, age.\n"
            "USE WHEN: the agent needs to find which capture holds a specific token (URL, error, identifier).\n"
            "NOT WHEN: it already knows the capture id — use capture_get directly."
        ),
        "inputSchema": {
            "type": "object",
            "required": ["query"],
            "properties": {
                "query": {"type": "string", "description": "FTS5 query (terms ANDed by default)."},
                "limit": {"type": "integer", "description": "Max rows (default 20)."},
                "session_id": {"type": "string"},
                "project_root": {"type": "string"},
                "tool_name": {"type": "string", "description": "Restrict to a single source tool."},
            },
        },
    },
    "capture_get": {
        "description": (
            "Fetch a captured output, optionally sliced (head/tail/all/preview/line:start-end).\n"
            "USE WHEN: you need the actual content of a known capture id.\n"
            "NOT WHEN: stats or counts are enough — use capture_aggregate (cheaper)."
        ),
        "inputSchema": {
            "type": "object",
            "required": ["id"],
            "properties": {
                "id": {"type": "integer", "description": "Capture id (from search/list)."},
                "range": {"type": "string", "description": "head | tail | all | preview | line:start-end (default preview)."},
                "max_bytes": {"type": "integer", "description": "Cap returned content size."},
            },
        },
    },
    "capture_aggregate": {
        "description": (
            "Aggregate over a capture without dumping it: stats, count_lines, unique_lines, extract:<re>, count:<re>.\n"
            "USE WHEN: you only need a count, distinct values, or summary stats — saves loading the full output."
        ),
        "inputSchema": {
            "type": "object",
            "required": ["id"],
            "properties": {
                "id": {"type": "integer"},
                "transform": {"type": "string", "description": "stats (default) | count_lines | unique_lines | extract | count | extract:<regex> | count:<regex>"},
                "pattern": {"type": "string", "description": "Regex when transform is 'extract' or 'count' without inline regex."},
            },
        },
    },
    "capture_list": {
        "description": (
            "List recent captures, newest first.\n"
            "USE WHEN: you want to enumerate what's been sandboxed in this session."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "session_id": {"type": "string"},
                "project_root": {"type": "string"},
                "tool_name": {"type": "string"},
                "limit": {"type": "integer", "description": "Max rows (default 50)."},
            },
        },
    },
    "capture_purge": {
        "description": (
            "Delete captures older than N seconds, or for a session/project.\n"
            "USE WHEN: cleaning up after a session or trimming the capture store."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "older_than_sec": {"type": "integer", "description": "Delete captures older than this many seconds."},
                "session_id": {"type": "string"},
                "project_root": {"type": "string"},
            },
        },
    },
}
