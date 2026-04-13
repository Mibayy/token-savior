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
        "description": "Run tests impacted by current changes.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "changed_files": {"type": "array", "items": {"type": "string"}, "description": "Changed files"},
                "symbol_names": {"type": "array", "items": {"type": "string"}, "description": "Changed symbols"},
                "max_tests": {"type": "integer", "description": "Max test files"},
                "timeout_sec": {"type": "integer", "description": "Timeout seconds"},
                "max_output_chars": {"type": "integer", "description": "Max output chars"},
                "include_output": {"type": "boolean", "description": "Include raw output"},
                "compact": {"type": "boolean", "description": "Compact result"},
                **_PROJECT_PARAM,
            },
        },
    },
    "apply_symbol_change_and_validate": {
        "description": "Replace symbol source, reindex, run tests.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "symbol_name": {"type": "string", "description": "Symbol to replace"},
                "new_source": {"type": "string", "description": "Replacement source"},
                "file_path": {"type": "string", "description": "Optional file path"},
                "rollback_on_failure": {"type": "boolean", "description": "Auto-restore on fail"},
                "max_tests": {"type": "integer", "description": "Max test files"},
                "timeout_sec": {"type": "integer", "description": "Timeout seconds"},
                "max_output_chars": {"type": "integer", "description": "Max output chars"},
                "include_output": {"type": "boolean", "description": "Include raw output"},
                "compact": {"type": "boolean", "description": "Compact result"},
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
        "description": "Get the source of a function or method by name. Use `level` to trade detail for tokens (L0 full, L1 signature+doc, L2 semantic summary, L3 one-liner).",
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
                    "description": "Maximum number of source lines to return (0 = unlimited, default 0). Only applies at level=0.",
                },
                "level": {
                    "type": "integer",
                    "description": "Abstraction level: 0=full source, 1=signature+doc, 2=semantic summary, 3=one-liner. Default 0.",
                    "minimum": 0,
                    "maximum": 3,
                },
                "force_full": {
                    "type": "boolean",
                    "description": "Bypass the session symbol cache and always return the full body at the requested level. Default false.",
                },
                **_PROJECT_PARAM,
            },
            "required": ["name"],
        },
    },
    "get_class_source": {
        "description": "Get the source of a class by name. Use `level` to trade detail for tokens (L0 full, L1 signature+doc, L2 semantic summary, L3 one-liner).",
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
                    "description": "Maximum number of source lines to return (0 = unlimited, default 0). Only applies at level=0.",
                },
                "level": {
                    "type": "integer",
                    "description": "Abstraction level: 0=full source, 1=signature+doc, 2=semantic summary, 3=one-liner. Default 0.",
                    "minimum": 0,
                    "maximum": 3,
                },
                "force_full": {
                    "type": "boolean",
                    "description": "Bypass the session symbol cache and always return the full body at the requested level. Default false.",
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
                    ],
                    "description": "Observation type",
                },
                "title": {"type": "string", "description": "Short title"},
                "content": {"type": "string", "description": "Full content"},
                "why": {"type": "string", "description": "Why it matters"},
                "how_to_apply": {"type": "string", "description": "How to apply"},
                "symbol": {"type": "string", "description": "Related code symbol"},
                "file_path": {"type": "string", "description": "Related file path"},
                "context": {"type": "string", "description": "Free context: URL, command, config"},
                "tags": {"type": "array", "items": {"type": "string"}, "description": "Tags list"},
                "importance": {"type": "integer", "description": "Importance 1-10"},
                "session_id": {"type": "integer", "description": "Session id"},
                "is_global": {"type": "boolean", "description": "Visible in all projects"},
                "ttl_days": {"type": "integer", "description": "TTL in days"},
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
        "description": "Classement des observations par score LRU, access_count ou âge.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "limit": {"type": "integer", "description": "Nombre d'obs (défaut 20)."},
                "sort_by": {
                    "type": "string",
                    "enum": ["score", "access_count", "age"],
                    "description": "Critère de tri (défaut score).",
                },
            },
        },
    },
    "memory_why": {
        "description": "Explique pourquoi une observation apparaît dans les résultats (trace recency, type, symbol, links, FTS match).",
        "inputSchema": {
            "type": "object",
            "properties": {
                "id": {"type": "integer", "description": "ID de l'observation."},
                "query": {"type": "string", "description": "Optionnel — query FTS5 pour vérifier le match."},
            },
            "required": ["id"],
        },
    },
    "memory_doctor": {
        "description": "Rapport de santé de la mémoire : orphans, near-duplicates, obs incomplètes.",
        "inputSchema": {"type": "object", "properties": {}},
    },
    "memory_from_bash": {
        "description": "Sauvegarde une commande bash comme observation mémoire (type=command par défaut).",
        "inputSchema": {
            "type": "object",
            "properties": {
                "command": {"type": "string", "description": "Commande bash complète à mémoriser."},
                "type": {
                    "type": "string",
                    "description": "Type d'observation (command par défaut).",
                    "enum": ["command", "infra", "config"],
                },
                "context": {"type": "string", "description": "Contexte libre (service, composant, ...)."},
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
            "Full-text search across all observations. Returns compact results. "
            "Use memory_get with IDs to fetch full details."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "FTS5 search query (supports AND, OR, NOT, phrases).",
                },
                "type_filter": {
                    "type": "string",
                    "description": "Filter by observation type (optional).",
                },
                "limit": {
                    "type": "integer",
                    "description": "Max results to return (default 20).",
                },
                **_PROJECT_PARAM,
            },
            "required": ["query"],
        },
    },
    "memory_get": {
        "description": "Get full details of observations by IDs (Layer 3 — full detail).",
        "inputSchema": {
            "type": "object",
            "properties": {
                "ids": {
                    "type": "array",
                    "items": {"type": "integer"},
                    "description": "List of observation IDs to fetch.",
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
            "Compact index of recent observations (Layer 1 — progressive disclosure). "
            "Returns a markdown table with ID, type, title, importance, date."
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
        "description": (
            "Build a named thematic corpus from observations filtered by type/tags/symbol. "
            "Returns a summary (count, type breakdown, preview) and stores IDs for later query."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "name": {
                    "type": "string",
                    "description": "Corpus name (unique per project).",
                },
                "filter_type": {
                    "type": "string",
                    "description": "Filter by observation type (optional).",
                },
                "filter_tags": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Require at least one matching tag (optional).",
                },
                "filter_symbol": {
                    "type": "string",
                    "description": "Filter by linked symbol (optional).",
                },
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
                    "description": "Skip functions shorter than this (default 4).",
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
    "get_relevance_cluster": {
        "description": (
            "RWR-ranked relevant symbols. Mathematically optimal context for editing `name`. "
            "Catches symbols BFS misses through multi-hop reinforcement."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "name": {
                    "type": "string",
                    "description": "Seed symbol (function/method/class) to centre the random walk on.",
                },
                "budget": {
                    "type": "integer",
                    "description": "Top-K symbols to return (default 10).",
                },
                "include_reverse": {
                    "type": "boolean",
                    "description": "Include reverse-dependency edges in the walk graph (default true).",
                },
                **_PROJECT_PARAM,
            },
            "required": ["name"],
        },
    },
    "pack_context": {
        "description": (
            "Optimal context bundle for a query within a token budget. "
            "Knapsack-greedy ranking by value/cost on the indexed symbols."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Natural-language query / topic to pack context for.",
                },
                "budget_tokens": {
                    "type": "integer",
                    "description": "Maximum total estimated tokens (default 4000).",
                },
                "max_symbols": {
                    "type": "integer",
                    "description": "Maximum number of candidate symbols to consider (default 20).",
                },
                **_PROJECT_PARAM,
            },
            "required": ["query"],
        },
    },
    "get_backward_slice": {
        "description": (
            "Minimal set of lines affecting `variable` at `line` inside symbol `name`. "
            "Debug in -70% tokens."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "name": {
                    "type": "string",
                    "description": "Function/method name to slice.",
                },
                "variable": {
                    "type": "string",
                    "description": "Variable whose dependencies you want to trace.",
                },
                "line": {
                    "type": "integer",
                    "description": "Absolute (1-based) line number where the criterion holds.",
                },
                "file_path": {
                    "type": "string",
                    "description": "Optional file path to disambiguate the symbol.",
                },
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
}

# Set of deprecated tool names for quick lookup
DEPRECATED_TOOLS: set[str] = {
    name for name, schema in TOOL_SCHEMAS.items() if schema.get("deprecated")
}
