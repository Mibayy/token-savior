"""MCP tool schema definitions for Token Savior.

Each entry maps a tool name to its ``description`` and ``inputSchema``.
server.py builds ``mcp.types.Tool`` objects from this dict at import time.
"""

from __future__ import annotations

# Shared project parameter injected into multi-project tools
_PROJECT_PARAM = {
    "project": {
        "type": "string",
        "description": (
            "Optional project name or path to target a specific project. "
            "Omit to use the active project."
        ),
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
        "description": "Return a compact symbol-oriented summary of changes. Without ref: worktree vs HEAD. With ref: HEAD vs that ref (e.g. 'HEAD~3', branch name).",
        "inputSchema": {
            "type": "object",
            "properties": {
                "ref": {
                    "type": "string",
                    "description": "Git ref to compare against (e.g. 'HEAD~3', 'main'). Omit for current worktree changes.",
                },
                "max_files": {
                    "type": "integer",
                    "description": "Maximum changed files to report (default 20).",
                },
                "max_symbols_per_file": {
                    "type": "integer",
                    "description": "Maximum symbols to report per file (default 20).",
                },
                **_PROJECT_PARAM,
            },
        },
    },
    "get_changed_symbols_since_ref": {
        "description": "[DEPRECATED -- use get_changed_symbols(ref=...) instead] Return a compact symbol-oriented summary of git changes since a given ref.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "since_ref": {
                    "type": "string",
                    "description": "Git ref to compare against HEAD and current worktree.",
                },
                "max_files": {
                    "type": "integer",
                    "description": "Maximum changed files to report (default 20).",
                },
                "max_symbols_per_file": {
                    "type": "integer",
                    "description": "Maximum symbols to report per file (default 20).",
                },
                **_PROJECT_PARAM,
            },
            "required": ["since_ref"],
        },
        "deprecated": True,
    },
    "summarize_patch_by_symbol": {
        "description": "Summarize a set of changed files as symbol-level entries for compact review instead of textual diffs.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "changed_files": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Changed files to summarize. Omit to summarize indexed files currently passed in by caller logic.",
                },
                "max_files": {
                    "type": "integer",
                    "description": "Maximum files to report (default 20).",
                },
                "max_symbols_per_file": {
                    "type": "integer",
                    "description": "Maximum symbols to report per file (default 20).",
                },
                **_PROJECT_PARAM,
            },
        },
    },
    "build_commit_summary": {
        "description": "Build a compact commit/review summary from changed files using symbol-level structure instead of textual diffs.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "changed_files": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Changed files to summarize.",
                },
                "max_files": {
                    "type": "integer",
                    "description": "Maximum files to report (default 20).",
                },
                "max_symbols_per_file": {
                    "type": "integer",
                    "description": "Maximum symbols to report per file (default 20).",
                },
                **_PROJECT_PARAM,
            },
            "required": ["changed_files"],
        },
    },
    # ── Checkpoints ───────────────────────────────────────────────────────
    "create_checkpoint": {
        "description": "Create a compact checkpoint for a bounded set of files before a workflow mutation.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "file_paths": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Project files to save into the checkpoint.",
                },
                **_PROJECT_PARAM,
            },
            "required": ["file_paths"],
        },
    },
    "list_checkpoints": {
        "description": "List available checkpoints for the active project.",
        "inputSchema": {"type": "object", "properties": {**_PROJECT_PARAM}},
    },
    "delete_checkpoint": {
        "description": "Delete a specific checkpoint.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "checkpoint_id": {
                    "type": "string",
                    "description": "Checkpoint identifier to delete.",
                },
                **_PROJECT_PARAM,
            },
            "required": ["checkpoint_id"],
        },
    },
    "prune_checkpoints": {
        "description": "Keep only the newest N checkpoints and delete older ones.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "keep_last": {
                    "type": "integer",
                    "description": "How many recent checkpoints to keep (default 10).",
                },
                **_PROJECT_PARAM,
            },
        },
    },
    "restore_checkpoint": {
        "description": "Restore files from a previously created checkpoint.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "checkpoint_id": {
                    "type": "string",
                    "description": "Checkpoint identifier returned by create_checkpoint.",
                },
                **_PROJECT_PARAM,
            },
            "required": ["checkpoint_id"],
        },
    },
    "compare_checkpoint_by_symbol": {
        "description": "Compare a checkpoint against current files at symbol level, returning added/removed/changed symbols without a textual diff.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "checkpoint_id": {
                    "type": "string",
                    "description": "Checkpoint identifier returned by create_checkpoint.",
                },
                "max_files": {
                    "type": "integer",
                    "description": "Maximum files to compare (default 20).",
                },
                **_PROJECT_PARAM,
            },
            "required": ["checkpoint_id"],
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
        "description": "Insert content immediately before or after an indexed symbol, avoiding a file-wide edit payload.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "symbol_name": {
                    "type": "string",
                    "description": "Function, method, class, or section name near which to insert.",
                },
                "content": {
                    "type": "string",
                    "description": "Content to insert.",
                },
                "position": {
                    "type": "string",
                    "description": "Insertion position: 'before' or 'after' (default 'after').",
                },
                "file_path": {
                    "type": "string",
                    "description": "Optional file path to disambiguate symbols.",
                },
                **_PROJECT_PARAM,
            },
            "required": ["symbol_name", "content"],
        },
    },
    # ── Tests & validation ────────────────────────────────────────────────
    "find_impacted_test_files": {
        "description": "Infer a compact set of likely impacted pytest files from changed files or symbols.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "changed_files": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Changed project files to map to likely impacted tests.",
                },
                "symbol_names": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Changed symbols to map to likely impacted tests.",
                },
                "max_tests": {
                    "type": "integer",
                    "description": "Maximum impacted test files to return (default 20).",
                },
                **_PROJECT_PARAM,
            },
        },
    },
    "run_impacted_tests": {
        "description": "Run only the inferred impacted pytest files and return a compact summary instead of full logs.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "changed_files": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Changed project files to map to likely impacted tests.",
                },
                "symbol_names": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Changed symbols to map to likely impacted tests.",
                },
                "max_tests": {
                    "type": "integer",
                    "description": "Maximum impacted test files to run (default 20).",
                },
                "timeout_sec": {
                    "type": "integer",
                    "description": "Maximum runtime in seconds (default 120).",
                },
                "max_output_chars": {
                    "type": "integer",
                    "description": "Maximum stdout/stderr characters to keep when included (default 12000).",
                },
                "include_output": {
                    "type": "boolean",
                    "description": "Include bounded raw stdout/stderr in the response. Default false for token efficiency.",
                },
                "compact": {
                    "type": "boolean",
                    "description": "Return only the minimum useful fields for agent loops.",
                },
                **_PROJECT_PARAM,
            },
        },
    },
    "apply_symbol_change_and_validate": {
        "description": "Replace a symbol, reindex the file, and run only the inferred impacted tests. Set rollback_on_failure=true to auto-restore on test failure.",
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
                "rollback_on_failure": {
                    "type": "boolean",
                    "description": "Create a checkpoint and auto-restore if tests fail (default false).",
                },
                "max_tests": {
                    "type": "integer",
                    "description": "Maximum impacted test files to run (default 20).",
                },
                "timeout_sec": {
                    "type": "integer",
                    "description": "Maximum runtime in seconds (default 120).",
                },
                "max_output_chars": {
                    "type": "integer",
                    "description": "Maximum stdout/stderr characters to keep when included (default 12000).",
                },
                "include_output": {
                    "type": "boolean",
                    "description": "Include bounded raw stdout/stderr in the response. Default false for token efficiency.",
                },
                "compact": {
                    "type": "boolean",
                    "description": "Return only the minimum useful fields for agent loops.",
                },
                **_PROJECT_PARAM,
            },
            "required": ["symbol_name", "new_source"],
        },
    },
    "apply_symbol_change_validate_with_rollback": {
        "description": "[DEPRECATED -- use apply_symbol_change_and_validate(rollback_on_failure=true)] Replace, validate, rollback on failure.",
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
                "max_tests": {
                    "type": "integer",
                    "description": "Maximum impacted test files to run (default 20).",
                },
                "timeout_sec": {
                    "type": "integer",
                    "description": "Maximum runtime in seconds (default 120).",
                },
                "max_output_chars": {
                    "type": "integer",
                    "description": "Maximum stdout/stderr characters to keep when included (default 12000).",
                },
                "include_output": {
                    "type": "boolean",
                    "description": "Include bounded raw stdout/stderr in the response. Default false for token efficiency.",
                },
                "compact": {
                    "type": "boolean",
                    "description": "Return only the minimum useful fields for agent loops.",
                },
                **_PROJECT_PARAM,
            },
            "required": ["symbol_name", "new_source"],
        },
        "deprecated": True,
    },
    # ── Project actions ───────────────────────────────────────────────────
    "discover_project_actions": {
        "description": "Detect conventional project actions from build files (tests, lint, build, run) without executing them.",
        "inputSchema": {"type": "object", "properties": {**_PROJECT_PARAM}},
    },
    "run_project_action": {
        "description": "Run a previously discovered project action by id with bounded output and timeout.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "action_id": {
                    "type": "string",
                    "description": "Action id returned by discover_project_actions (e.g. 'python:test', 'npm:test').",
                },
                "timeout_sec": {
                    "type": "integer",
                    "description": "Maximum runtime in seconds (default 120).",
                },
                "max_output_chars": {
                    "type": "integer",
                    "description": "Maximum stdout/stderr characters to keep (default 12000).",
                },
                "include_output": {
                    "type": "boolean",
                    "description": "Include bounded raw stdout/stderr in the response. Default false for token efficiency.",
                },
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
        "description": "Get the full source code of a function or method by name. Uses the symbol table to locate the file automatically.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "name": {
                    "type": "string",
                    "description": "Function or method name (e.g. 'my_func' or 'MyClass.my_method').",
                },
                "file_path": {
                    "type": "string",
                    "description": "Optional file path to narrow the search.",
                },
                "max_lines": {
                    "type": "integer",
                    "description": "Maximum number of source lines to return (0 = unlimited, default 0).",
                },
                **_PROJECT_PARAM,
            },
            "required": ["name"],
        },
    },
    "get_class_source": {
        "description": "Get the full source code of a class by name. Uses the symbol table to locate the file automatically.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "name": {
                    "type": "string",
                    "description": "Class name.",
                },
                "file_path": {
                    "type": "string",
                    "description": "Optional file path to narrow the search.",
                },
                "max_lines": {
                    "type": "integer",
                    "description": "Maximum number of source lines to return (0 = unlimited, default 0).",
                },
                **_PROJECT_PARAM,
            },
            "required": ["name"],
        },
    },
    "get_functions": {
        "description": "List all functions (with name, lines, params, file). Filter to a specific file or get all project functions.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "file_path": {
                    "type": "string",
                    "description": "Relative path to filter to a single file. Omit for all project functions.",
                },
                "max_results": {
                    "type": "integer",
                    "description": "Maximum number of results to return (0 = unlimited, default 0).",
                },
                **_PROJECT_PARAM,
            },
        },
    },
    "get_classes": {
        "description": "List all classes (with name, lines, methods, bases, file). Filter to a specific file or get all project classes.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "file_path": {
                    "type": "string",
                    "description": "Relative path to filter to a single file. Omit for all project classes.",
                },
                "max_results": {
                    "type": "integer",
                    "description": "Maximum number of results to return (0 = unlimited, default 0).",
                },
                **_PROJECT_PARAM,
            },
        },
    },
    "get_imports": {
        "description": "List all imports (with module, names, line). Filter to a specific file or get all project imports.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "file_path": {
                    "type": "string",
                    "description": "Relative path to filter to a single file. Omit for all project imports.",
                },
                "max_results": {
                    "type": "integer",
                    "description": "Maximum number of results to return (0 = unlimited, default 0).",
                },
                **_PROJECT_PARAM,
            },
        },
    },
    "find_symbol": {
        "description": "Find where a symbol (function, method, class) is defined. Returns file path, line range, type, signature, and a source preview (~20 lines).",
        "inputSchema": {
            "type": "object",
            "properties": {
                "name": {
                    "type": "string",
                    "description": "Symbol name to find (e.g. 'ProjectIndexer', 'annotate', 'MyClass.run').",
                },
                **_PROJECT_PARAM,
            },
            "required": ["name"],
        },
    },
    "get_dependencies": {
        "description": "What does this symbol call/use? Returns list of symbols referenced by the named function or class.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "name": {
                    "type": "string",
                    "description": "Symbol name to query.",
                },
                "max_results": {
                    "type": "integer",
                    "description": "Maximum number of results to return (0 = unlimited, default 0).",
                },
                **_PROJECT_PARAM,
            },
            "required": ["name"],
        },
    },
    "get_dependents": {
        "description": "What calls/uses this symbol? Returns list of symbols that reference the named function or class.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "name": {
                    "type": "string",
                    "description": "Symbol name to query.",
                },
                "max_results": {
                    "type": "integer",
                    "description": "Maximum number of results to return (0 = unlimited, default 0).",
                },
                "max_total_chars": {
                    "type": "integer",
                    "description": "Maximum total characters in the response (default 50000, 0 = unlimited).",
                },
                **_PROJECT_PARAM,
            },
            "required": ["name"],
        },
    },
    "get_change_impact": {
        "description": "Analyze the impact of changing a symbol. Returns direct and transitive dependents, each scored with a confidence value (1.0 = direct caller, 0.6 = 2 hops, etc.) and depth.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "name": {
                    "type": "string",
                    "description": "Symbol name to analyze.",
                },
                "max_direct": {
                    "type": "integer",
                    "description": "Maximum number of direct dependents to return (0 = unlimited, default 0).",
                },
                "max_transitive": {
                    "type": "integer",
                    "description": "Maximum number of transitive dependents to return (0 = unlimited, default 0).",
                },
                "max_total_chars": {
                    "type": "integer",
                    "description": "Maximum total characters in the response (default 50000, 0 = unlimited).",
                },
                **_PROJECT_PARAM,
            },
            "required": ["name"],
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
                **_PROJECT_PARAM,
            },
            "required": ["from_name", "to_name"],
        },
    },
    "get_edit_context": {
        "description": (
            "All-in-one context for editing a symbol. Returns the symbol source, "
            "its direct dependencies (what it calls), and its callers (who uses it) "
            "in a single response. Saves 3 separate tool calls."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "name": {
                    "type": "string",
                    "description": "Symbol name to get full edit context for.",
                },
                "max_deps": {
                    "type": "integer",
                    "description": "Max dependencies to return (default 10).",
                },
                "max_callers": {
                    "type": "integer",
                    "description": "Max callers to return (default 10).",
                },
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
        "description": "Re-index the entire project. Use after making significant file changes to refresh the structural index.",
        "inputSchema": {"type": "object", "properties": {**_PROJECT_PARAM}},
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
        "description": (
            "Find all files related to a feature keyword, then trace imports to build the "
            "complete feature map. Example: get_feature_files('contrat') returns all routes, "
            "components, lib, types connected to contracts. Each file is classified by role."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "keyword": {
                    "type": "string",
                    "description": "Feature keyword (e.g. 'contrat', 'paiement', 'auth').",
                },
                "max_results": {
                    "type": "integer",
                    "description": "Max files to return (0 = all, default 0).",
                },
                **_PROJECT_PARAM,
            },
            "required": ["keyword"],
        },
    },
    # ── Usage stats ───────────────────────────────────────────────────────
    "get_usage_stats": {
        "description": "Session efficiency stats: tool calls, characters returned vs total source, estimated token savings.",
        "inputSchema": {"type": "object", "properties": {}},
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
        "description": (
            "Analyze config files for issues and insights: duplicate keys, hardcoded secrets, orphan entries, "
            "config file loaders (which code loads which config), and schema (what keys code expects with defaults). "
            "Checks can be filtered via the 'checks' parameter."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "checks": {
                    "type": "array",
                    "items": {
                        "type": "string",
                        "enum": ["duplicates", "secrets", "orphans", "loaders", "schema"],
                    },
                    "description": 'Checks to run (default: duplicates,secrets,orphans). Options: "duplicates", "secrets", "orphans", "loaders", "schema".',
                },
                "file_path": {
                    "type": "string",
                    "description": "Specific config file to analyze. Omit to analyze all config files.",
                },
                "severity": {
                    "type": "string",
                    "enum": ["all", "error", "warning"],
                    "description": 'Filter by severity (default: "all").',
                },
                **_PROJECT_PARAM,
            },
        },
    },
    "find_dead_code": {
        "description": (
            "Find unreferenced functions and classes in the codebase. "
            "Detects symbols with zero callers, excluding entry points (main, tests, route handlers, etc.)."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "max_results": {
                    "type": "integer",
                    "description": "Maximum number of dead symbols to report (default: 50).",
                },
                **_PROJECT_PARAM,
            },
        },
    },
    "find_hotspots": {
        "description": (
            "Rank functions by complexity score (line count, branching, nesting depth, parameter count). "
            "Helps identify code that needs refactoring."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "max_results": {
                    "type": "integer",
                    "description": "Maximum number of functions to report (default: 20).",
                },
                "min_score": {
                    "type": "number",
                    "description": "Minimum complexity score to include (default: 0).",
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
    "get_symbol_cluster": {
        "description": (
            "Get the functional cluster for a symbol -- all closely related symbols "
            "grouped by community detection on the dependency graph. Useful for "
            "understanding which symbols belong to the same functional area without "
            "chaining multiple dependency queries."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "name": {
                    "type": "string",
                    "description": "Symbol name to find the cluster for.",
                },
                "max_members": {
                    "type": "integer",
                    "description": "Maximum cluster members to return (default 30).",
                },
                **_PROJECT_PARAM,
            },
            "required": ["name"],
        },
    },
}

# Set of deprecated tool names for quick lookup
DEPRECATED_TOOLS: set[str] = {
    name for name, schema in TOOL_SCHEMAS.items() if schema.get("deprecated")
}
