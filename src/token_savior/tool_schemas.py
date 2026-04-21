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
        "description": "List all registered workspace projects with their index status.",
        "inputSchema": {"type": "object", "properties": {}},
    },
    "switch_project": {
        "description": "Switch the active project. Subsequent tool calls without explicit project target this project.",
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
        "description": "Return a structured git status summary for the active project: branch, ahead/behind, staged, unstaged, and untracked files.",
        "inputSchema": {"type": "object", "properties": {**_PROJECT_PARAM}},
    },
    "get_changed_symbols": {
        "description": (
            "Symbol-level summary of changes (worktree, or HEAD vs ref). "
            "For PR/commit-message tasks prefer build_commit_summary which is tuned "
            "to produce a compact narrative with stats, hotspots, and suggested type."
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
        "description": "Symbol-level summary of changed files (compact review vs diff).",
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
        "description": "Symbol-level commit/review summary (vs textual diffs).",
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
            "Unified checkpoint CRUD. Pass op to select:\n"
            "  - create: snapshot a bounded set of files (requires file_paths)\n"
            "  - list (default): list available checkpoints for active project\n"
            "  - restore: revert files from a checkpoint (requires checkpoint_id)\n"
            "  - delete: remove one checkpoint (requires checkpoint_id)\n"
            "  - prune: keep only the newest N checkpoints (keep_last, default 10)\n"
            "  - compare: symbol-level diff of a checkpoint vs current files "
            "(requires checkpoint_id)"
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
        "description": "Replace an indexed symbol's full source block directly, without sending a file-wide patch.",
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
        "description": "Insert content before/after an indexed symbol.",
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
        "description": "Move a symbol (function/class) to a different file. Updates imports in all call sites. Returns {from_file, to_file, symbol, updated_imports}.",
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
        "description": "Add a field to a model/class/interface. Supports .prisma, .py (dataclass/SQLAlchemy), .ts/.tsx (interface/type).",
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
        "description": "Unified refactoring: rename, move, add_field, or extract. "
                       "Type 'rename': {symbol, new_name}. "
                       "Type 'move': {symbol, target_file}. "
                       "Type 'add_field': {model, field_name, field_type}. "
                       "Type 'extract': {file_path, start_line, end_line, new_name}.",
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
        "description": "Infer a compact set of likely impacted pytest files from changed files or symbols.",
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
        "description": "Run tests impacted by current changes.",
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
        "description": "Replace symbol source, reindex, run tests.",
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
        "description": "Detect conventional project actions from build files (tests, lint, build, run) without executing them.",
        "inputSchema": {"type": "object", "properties": {**_PROJECT_PARAM}},
    },
    "run_project_action": {
        "description": "Run a discovered project action by id (bounded output/timeout).",
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
        "description": "High-level overview of the project: file count, packages, top classes/functions.",
        "inputSchema": {"type": "object", "properties": {**_PROJECT_PARAM}},
    },
    "list_files": {
        "description": "List indexed files. Optional glob pattern to filter (e.g. '*.py', 'src/**/*.ts').",
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
        "description": "Structure summary for a file (functions, classes, imports, line counts) or the whole project if no file specified.",
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
        "description": "Get a function/method source. `level`: 0 full, 1 sig+doc, 2 summary, 3 one-liner. Batch: pass `names` (max 10) instead of `name` to get multiple in one call.",
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
        "description": "Get a class source. `level`: 0 full, 1 sig+doc, 2 summary, 3 one-liner. Batch: pass `names` (max 10) instead of `name` to get multiple in one call.",
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
            "List functions (name, lines, params, file). Preferred tool for "
            "\"list all functions of file X\" (pass `file_path=...`) or a project-wide enumeration. "
            "Trailing `_hints` entry suggests next tool calls (disable with hints=false)."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "file_path": {"type": "string", "description": "Filter to file (omit=all)."},
                "max_results": {"type": "integer", "description": "0=unlimited."},
                "hints": {"type": "boolean", "description": "Append a `_hints` entry with next-step tool calls (default true)."},
                **_COMPRESS_PARAM,
                **_PROJECT_PARAM,
            },
        },
    },
    "get_classes": {
        "description": "List classes (name, lines, methods, bases, file). Trailing `_hints` entry suggests next tool calls (disable with hints=false).",
        "inputSchema": {
            "type": "object",
            "properties": {
                "file_path": {"type": "string", "description": "Filter to file (omit=all)."},
                "max_results": {"type": "integer", "description": "0=unlimited."},
                "hints": {"type": "boolean", "description": "Append a `_hints` entry with next-step tool calls (default true)."},
                **_COMPRESS_PARAM,
                **_PROJECT_PARAM,
            },
        },
    },
    "get_imports": {
        "description": "List imports (module, names, line).",
        "inputSchema": {
            "type": "object",
            "properties": {
                "file_path": {"type": "string", "description": "Filter to file (omit=all)."},
                "max_results": {
                    "type": "integer",
                    "description": "0=unlimited.",
                },
                **_COMPRESS_PARAM,
                **_PROJECT_PARAM,
            },
        },
    },
    "find_symbol": {
        "description": "Locate a symbol (file, line, signature, preview). `level`: 0 full, 1 no source_preview, 2 minimal {name, file, line, type}. Batch: pass `names` (max 10) instead of `name`.",
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
            "Symbols called/used by this symbol. Pass depth>1 to walk the call "
            "chain transitively (BFS) — saves N round trips when tracing a chain "
            "X→Y→Z→... in one shot. Each hop is tagged with `depth` and `from`."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "name": {"type": "string"},
                "max_results": {"type": "integer", "description": "0=unlimited."},
                "depth": {"type": "integer", "description": "Transitive BFS depth (default 1)."},
                **_COMPRESS_PARAM,
                **_PROJECT_PARAM,
            },
            "required": ["name"],
        },
    },
    "get_dependents": {
        "description": "Symbols that reference this function/class.",
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
        "description": "Impact of changing a symbol: direct + transitive dependents with confidence/depth.",
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
        "description": "Full symbol context in one call: location + source + dependencies/dependents (depth=1) or + change_impact (depth=2). Batch: pass `names` (max 10) instead of `name`.",
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
        "description": "Find the shortest dependency path between two symbols (BFS through the dependency graph).",
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
        "description": "Symbol source + direct deps + callers + siblings + impacted tests in one call. Returns {symbol, source, location, dependencies, callers, siblings, impacted_tests}.",
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
        "description": "List files that this file imports from (file-level import graph).",
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
        "description": "List files that import from this file (reverse import graph).",
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
        "description": "Regex search across all indexed files. Returns up to 100 matches with file, line number, and content.",
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
        "description": "Re-index the entire project. No-op if no tracked file mtimes changed (pass force=true to rebuild anyway). Edit tools (replace_symbol_source, insert_near_symbol, etc.) already reindex inline — only needed after external edits (Edit/Write/sed).",
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
            "Add a new project root to the workspace and switch to it. "
            "Triggers a full reindex of the new root. "
            "After calling this, all other tools operate on the new project by default."
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
        "description": "Files matching a feature keyword + traced imports, classified by role.",
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
            "Unified stats dispatcher. Pass category to select which subsystem:\n"
            "  - usage (default): session efficiency, tool calls, chars saved\n"
            "  - session_budget: current session token budget consumption\n"
            "  - tca: co-activation matrix (symbols tracked, top pairs)\n"
            "  - dcp: differential context protocol chunk registry\n"
            "  - linucb: injection model feature weights θ\n"
            "  - warmstart: cross-session signature store\n"
            "  - leiden: community detector (modularity Q, sizes)\n"
            "  - speculation: speculative tool-tree execution stats\n"
            "  - lattice: adaptive Beta-Binomial posteriors per (context, level)"
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
        "description": "Detect all API routes and pages in a Next.js App Router project. Returns route path, file, HTTP methods, and type (api/page/layout).",
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
        "description": "Cross-reference an environment variable across all code, .env files, and workflow configs. Shows where it's defined, read, and written.",
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
        "description": "Detect React components in .tsx/.jsx files. Identifies pages, layouts, and named components by convention (uppercase name or default export).",
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
        "description": "Audit config files for issues.",
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
            "Project-wide audit of unreferenced functions/classes (zero callers, "
            "excludes entry points: main, tests, route handlers). "
            "NOT a listing tool — to list functions of a specific file use "
            "`get_functions(file_path=...)`; to outline a file use `get_structure_summary`."
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
            "Rank functions by a chosen *kind* of hotspot.\n"
            "  • complexity  — all languages: line count, branching, nesting, params (default min_score 0).\n"
            "  • allocation  — Java only: object/collection/stream allocation, boxing, formatting (default min_score 1).\n"
            "  • performance — Java only: blocking calls, locks, synchronized sections, mutable state (default min_score 1).\n"
            "Replaces find_allocation_hotspots / find_performance_hotspots."
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
            "Detect breaking API changes between the current code and a git ref. "
            "Finds removed functions, removed parameters, added required parameters, and signature changes."
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
            "Detect dependencies between indexed projects. "
            "Shows which projects import packages from other indexed projects and shared external dependencies."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {},
        },
    },
    "analyze_docker": {
        "description": (
            "Analyze Dockerfiles in the project: base images, stages, exposed ports, ENV/ARG vars, "
            "and cross-reference with config files. Flags issues like 'latest' tags and missing env vars."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                **_PROJECT_PARAM,
            },
        },
    },
    "get_entry_points": {
        "description": "Score functions by likelihood of being execution entry points (routes, handlers, main functions, exported APIs). Returns functions with score and reasons, sorted by likelihood desc.",
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
            "Unified related-symbols query. Choose *method*:\n"
            "  • community  — Leiden community for `name` (or `list_all=true` to enumerate all).\n"
            "  • rwr        — Random-walk-with-restart ranking centred on `name` (catches multi-hop).\n"
            "  • cluster    — Greedy-modularity cluster for `name` (dependency graph).\n"
            "  • coactive   — TCA co-activation (symbols often accessed together with `name`).\n"
            "Replaces get_community / get_relevance_cluster / get_symbol_cluster / "
            "get_coactive_symbols."
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
        "description": "Find Java classes duplicated across files (by FQN, or simple name).",
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
        "description": "Save an observation to memory.",
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
        "description": "Maintenance: promote, relink, export, patterns.",
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
        "description": "Rank obs by score, access_count, or age.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "limit": {"type": "integer", "description": "Default 20."},
                "sort_by": {"type": "string", "enum": ["score", "access_count", "age"]},
            },
        },
    },
    "memory_why": {
        "description": "Explain why an obs matched (recency, type, symbol, FTS).",
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
        "description": "Memory health report (orphans, near-dupes, incomplete, vector coverage).",
        "inputSchema": {"type": "object", "properties": {}},
    },
    "memory_vector_reindex": {
        "description": "Backfill obs_vectors for observations missing an embedding. No-op when sqlite-vec / sentence-transformers are unavailable.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "limit": {"type": "integer", "description": "Max obs to index this run (default 500)."},
                **_PROJECT_PARAM,
            },
        },
    },
    "memory_distill": {
        "description": "MDL distillation: cluster similar obs into abstraction + deltas.",
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
        "description": "Backfill observations.content_hash (SHA256 of normalized content). Default backfills NULL hashes only; set recompute=true after a hash-formula change.",
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
        "description": "Archive obs with ROI below threshold.",
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
        "description": "Token Economy ROI stats (net, by type).",
        "inputSchema": {
            "type": "object",
            "properties": {**_PROJECT_PARAM},
        },
    },
    "memory_from_bash": {
        "description": "Save a bash command as observation (type=command).",
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
        "description": "Set observation global visibility.",
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
            "[Layer 2/3 — Progressive Disclosure] "
            "Estimated tokens: ~60 per result. Use before memory_get. "
            "FTS5 search over observations (compact rows with snippets). "
            "Also surfaces matching session_summaries rollups as a separate section."
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
            "Last N structured session-end rollups (request / investigated / "
            "learned / completed / next_steps / notes) for the current project."
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
            "[Layer 3/3 — Progressive Disclosure] "
            "Estimated tokens: ~200 per result. Final layer — full content. "
            "Get full details of observations by IDs. Accepts integers, digit strings, "
            "or `ts://obs/{id}` citation URIs (as emitted by `memory_index`)."
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
        "description": "Soft-delete an observation by ID (sets archived=1).",
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
            "[Layer 1/3 — Progressive Disclosure] "
            "Estimated tokens: ~15 per result. Use before memory_search. "
            "Compact index of recent observations: ID, type, title, importance, date, "
            "citation URI. Always start here when exploring memory."
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
            "Chronological context around an observation (Layer 2). "
            "Shows observations before and after for temporal context."
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
        "description": "Save or search prompt history.",
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
        "description": "Get or set memory capture mode.",
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
        "description": "Build a thematic corpus from obs (filter type/tags/symbol).",
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
        "description": "Manage archived observations.",
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
            "Quick overview of the Memory Engine for the active project: active/archived "
            "obs count, current mode, last session + end_type, last summary date, prompts archived."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {},
        },
    },
    # ── Program slicing & context packing (Phase 2) ───────────────────────
    "verify_edit": {
        "description": (
            "EditSafety certificate before applying a symbol replacement. "
            "Static analysis only: signature, exceptions, side-effects, tests."
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
            "Find semantically identical functions across the codebase via "
            "AST-normalised hashing (alpha-renaming, docstrings stripped)."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "min_lines": {
                    "type": "integer",
                    "description": "Skip functions shorter than this (default 2).",
                },
                "max_groups": {
                    "type": "integer",
                    "description": "Max duplicate groups to return (default 10). Raise for full audit.",
                },
                **_PROJECT_PARAM,
            },
        },
    },
    "find_import_cycles": {
        "description": (
            "Detect import cycles (strongly-connected components) in the "
            "file-level import graph using Tarjan's algorithm. Returns sorted "
            "list of cycles."
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
            "Predict the next likely tool calls based on the persistent first-order "
            "Markov model trained on this session and prior sessions."
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
        "description": "Knapsack-packed context bundle for a query within token budget.",
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
        "description": "Minimal lines affecting `variable` at `line` in symbol `name`.",
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
            "Format all observations of a corpus as markdown context + a question, "
            "ready for Claude to answer with full context injected."
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
        "description": "Push a volatile obs to the inter-agent bus (tagged agent_id).",
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
        "description": "List recent live messages on the inter-agent memory bus, optionally filtered by agent_id.",
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
        "description": "Persist a reasoning trace (goal+steps+conclusion) for reuse.",
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
            "Search stored reasoning chains by goal similarity (FTS5 + Jaccard). "
            "Returns previous chains whose goal overlaps the query."
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
        "description": "List stored reasoning chains sorted by access_count then recency.",
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
            "Run Bayesian self-consistency check on symbol-linked observations "
            "(updates validity α/β, flags stale_suspected and quarantine)."
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
            "List observations currently quarantined by the consistency check "
            "(Bayesian validity below 40%)."
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
}
