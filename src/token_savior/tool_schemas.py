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
        'List all registered workspace projects with index status.'   ),
        "inputSchema": {"type": "object", "properties": {}},
    },
    "switch_project": {
        "description": (
        'Switch the active project.'
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
        'Structured git status: branch, ahead/behind, staged, unstaged, untracked.'   ),
        "inputSchema": {"type": "object", "properties": {**_PROJECT_PARAM}},
    },
    "get_changed_symbols": {
        "description": (
        'Symbol-level summary of worktree changes (or HEAD vs ref).' ),
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
        'Symbol-level summary of a specific set of changed files.' ),
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
        'Compact commit/review narrative with stats, hotspots, suggested type.'   ),
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
        'Unified checkpoint CRUD. op = create | list (default) | restore | delete | prune | compare.'   ),
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
        "Replace an indexed symbol's full source block directly."
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
    "edit_lines_in_symbol": {
        "description": "Exact string-replace inside an indexed symbol's body (like Edit but symbol-scoped, no Read first needed).",
        "inputSchema": {
            "type": "object",
            "properties": {
                "symbol_name": {"type": "string", "description": "Function/method/class name to edit inside."},
                "old_string": {"type": "string", "description": "Exact text to find inside the symbol body (must be unique within the symbol unless replace_all=true)."},
                "new_string": {"type": "string", "description": "Replacement text."},
                "file_path": {"type": "string", "description": "Optional file path to disambiguate symbols."},
                "replace_all": {"type": "boolean", "description": "If true, replace every occurrence in the symbol body (default false)."},
                **_PROJECT_PARAM,
            },
            "required": ["symbol_name", "old_string", "new_string"],
        },
    },
    "insert_near_symbol": {
        "description": (
        'Insert content before or after an indexed symbol.'
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
        'Move a symbol to a different file, updating imports in all call sites.' ),
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
        'Add a field to a model/class/interface. Supports .prisma, .py (dataclass, SQLAlchemy), .ts/.tsx.'   ),
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
        'Polymorphic refactoring: rename, move, add_field, extract.'   ),
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
        'Infer pytest files likely impacted by changed files or symbols.'   ),
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
        'Run pytest on files impacted by the current worktree changes.'   ),
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
        'Replace symbol source, reindex, run impacted tests in one call.'   ),
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
        'Detect conventional project actions from build files (tests, lint, build, run) without executing.'   ),
        "inputSchema": {"type": "object", "properties": {**_PROJECT_PARAM}},
    },
    "run_project_action": {
        "description": (
        'Run a discovered project action by id (bounded output, bounded timeout).'   ),
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
        'Project overview: file count, packages, top classes/functions, infra dirs.'   ),
        "inputSchema": {"type": "object", "properties": {**_PROJECT_PARAM}},
    },
    "list_files": {
        "description": (
        'List indexed files, optionally filtered by glob.'   ),
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
        'Structure of one file (functions, classes, imports, line counts), or project-wide if file omitted.'   ),
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
        'Fetch a function/method source body.'   ),
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
        'Fetch a class source body (including methods).'
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
        'List functions in a file (file_path=...) or across the project.'   ),
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
        'List classes (name, lines, methods, bases, file).'   ),
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
        'List imports (module, names, line).'   ),
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
        'Locate a symbol: file, line, signature, minimal preview.'   ),
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
        'Outgoing deps of a symbol: what X calls/uses (downstream).' ),
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
        'Incoming deps: who calls/uses X, direct references only.' ),
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
        'Impact analysis: direct + transitive dependents of a symbol.' ),
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
        'Symbol bundle: location + source + deps/dependents (depth=1) or + change_impact (depth=2).' ),
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
        'Shortest dependency path between two symbols (BFS through the dep graph).'   ),
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
        'Pre-edit bundle: source + direct deps + callers + same-file siblings + impacted tests.' ),
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
        'Files imported by this file (outgoing file-level import edges).'   ),
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
        'Files that import this file (incoming file-level import edges).'   ),
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
        'Regex (default) or semantic (semantic=true) search across indexed files.'
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
        'Regex search that returns the enclosing function/class for each match, in addition to file:line.'   ),
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
        'Rebuild the project index.'   ),
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
        'Register a new project root and switch to it.'
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
        'Files matching a feature keyword + traced imports, classified by role (core, test, config).'   ),
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
        'Unified stats dispatcher. category = usage (default) | session_budget | tca | dcp | linucb | warmstart | leiden | speculation | lattice.'
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
        'Detect API routes and pages in a Next.js App Router project: path, file, HTTP methods, type.'   ),
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
        "Cross-reference an env var across code, .env files, and workflow configs. Shows where it's defined, read, written."   ),
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
        'Detect React components in .tsx/.jsx: pages, layouts, named (uppercase) and default exports.'   ),
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
        'Audit config files (.env/.yaml/.toml/.json): duplicates, secrets, orphans.'
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
        'Project-wide audit of unreferenced functions/classes (zero callers, excludes entry points, tests, route handlers).' ),
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
        'Rank functions by hotspot kind. complexity (all langs) | allocation (Java) | performance (Java).'
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
        'Breaking API changes vs a git ref: removed funcs/params, added required params, signature changes.'   ),
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
        'Dependencies between indexed projects: which project imports packages from other indexed projects.'   ),
        "inputSchema": {
            "type": "object",
            "properties": {},
        },
    },
    "analyze_docker": {
        "description": (
        'Audit Dockerfiles: base images, stages, exposed ports, ENV/ARG, cross-ref with config files.'   ),
        "inputSchema": {
            "type": "object",
            "properties": {
                **_PROJECT_PARAM,
            },
        },
    },
    "get_db_schema": {
        "description": (
        'Condensed SQL-migration snapshot: tables (cols, types, nullability, defaults), PKs, FKs, indexes, RLS policies.'
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
        'Resolve a library symbol (npm .d.ts or Python module): signature, JSDoc/docstring, source location.'
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
        'List top-level exports of an installed library (.d.ts or Python module), optionally regex-filtered.' ),
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
        "Rank a package's exports by Nomic-embedding similarity to a NL description. On-the-fly, no persistent index."
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
        'Mega-batch audit of a single file: dead_code + hotspots + semantic duplicates in one call.'   ),
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
        'Score functions by likelihood of being execution entry points: routes, handlers, main, exported APIs.' ),
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
        'Related-symbols query. method = community | rwr | cluster | coactive.'
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
        'Find Java classes duplicated across files (by FQN or simple name).'   ),
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
        'Persist a fact, guardrail, or note across sessions.'
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
        'Maintenance rollup: promote, relink, export, extract patterns.'
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
        'Rank observations by score, access_count, or age.'
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
        'Explain why a specific observation matched the last injection (recency, type, symbol, FTS).'
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
        'Memory health report: orphans, near-dupes, incomplete obs, vector coverage, hook wiring.'   ),
        "inputSchema": {"type": "object", "properties": {}},
    },
    "memory_vector_reindex": {
        "description": (
        'Backfill obs_vectors for observations missing an embedding. No-op if sqlite-vec/fastembed unavailable.'
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
        'MDL-based distillation: cluster similar obs into an abstraction + deltas.'
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
        'Backfill observations.content_hash (SHA256 of normalized content). Default: only NULL hashes.'
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
        'Archive observations whose ROI score falls below a threshold.'
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
        'Token Economy ROI stats — net value by observation type.'
    ),
        "inputSchema": {
            "type": "object",
            "properties": {**_PROJECT_PARAM},
        },
    },
    "memory_from_bash": {
        "description": (
        'Save a bash command as an observation (type=command, auto-extracted).'   ),
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
        "Set an observation's global visibility flag (is_global=True crosses all projects)."
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
        'Layer 2 FTS5 search over memory observations, compact rows with snippets (~60 tokens/result).'   ),
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
        'Last N structured session-end rollups (request, investigated, learned, completed, next_steps, notes).'
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
        'Layer 3: full observation content by IDs (~200 tokens/result). Final progressive-disclosure layer.'
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
        'Soft-delete an observation by ID (sets archived=1).'
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
        'Layer 1: compact index of recent observations — ID, type, title, importance, age, citation URI.'   ),
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
        'Chronological context around an observation (before/after in time).'
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
        'Save or search prompt history (archival of notable user prompts).'
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
        'Get or set the memory capture mode (code | review | debug | infra | silent).'
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
        'Build a thematic corpus from observations filtered by type / tags / symbol.'
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
        'Manage archived observations (list, undelete, purge).'
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
        'Memory Engine snapshot: active/archived counts, mode, last session, summaries.'
    ),
        "inputSchema": {
            "type": "object",
            "properties": {},
        },
    },
    # ── Program slicing & context packing (Phase 2) ───────────────────────
    "verify_edit": {
        "description": (
        'EditSafety certificate — static analysis before a symbol replacement: signature, exceptions, side-effects, test impact.'   ),
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
        "Find duplicate functions. method='ast' (fast, hash-based, catches copy-paste) or 'embedding' (Nomic cosine, catches conceptual clones, tagged sim=min..mean per cluster)."
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
        "Detect import cycles (strongly-connected components) in the file-level import graph (Tarjan's)."   ),
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
        'Predict next-likely tool calls from a first-order Markov model trained on prior sessions.'
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
        'Knapsack-packed context bundle for a query within a token budget.'   ),
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
        'Minimal lines affecting a variable at a given line inside a symbol.'   ),
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
        'Format all observations of a named corpus as markdown context + a question, ready for answering.'
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
        'Push a volatile observation to the inter-agent memory bus (tagged by agent_id).'
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
        'List recent live messages on the inter-agent memory bus, optionally filtered by agent_id.'
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
        'Persist a reasoning trace (goal + steps + conclusion) for later reuse.'
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
        'Search stored reasoning chains by goal similarity (FTS5 + Jaccard).'
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
        'List stored reasoning chains by access_count then recency.'   ),
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
        'Run Bayesian self-consistency check on symbol-linked obs (updates α/β; flags stale + quarantine).'
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
        'List observations quarantined by the consistency check (Bayesian validity < 40 %).'
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
        "description": 'Sandbox a verbose tool output to FTS5 store; returns id + preview.',
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
        "description": 'BM25 search across sandboxed tool outputs. Returns id, snippet, bytes.',
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
        "description": 'Read a capture (range: head/tail/all/preview/line:N-M).',
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
        "description": 'Aggregate over a capture: stats|count_lines|unique_lines|extract:<re>|count:<re>.',
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
        "description": 'List recent captures (newest first).',
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
        "description": 'Delete captures by age/session/project (filter required).',
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
