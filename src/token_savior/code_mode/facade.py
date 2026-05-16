"""Allowed-tool whitelist + TypeScript facade emitter for Code Mode scripts."""
from __future__ import annotations

ALLOWED_TOOLS: list[str] = [
    "find_symbol",
    "get_function_source",
    "get_class_source",
    "search_codebase",
    "list_files",
    "get_structure_summary",
    "get_project_summary",
    "get_functions",
    "get_classes",
    "get_imports",
    "get_full_context",
    "get_dependencies",
    "get_dependents",
    "get_change_impact",
    "get_call_chain",
    "get_file_dependencies",
    "get_file_dependents",
    "get_edit_context",
    "get_git_status",
    "get_changed_symbols",
    "build_commit_summary",
    "replace_symbol_source",
    "insert_near_symbol",
    "add_field_to_model",
    "find_dead_code",
    "find_hotspots",
    "find_semantic_duplicates",
    "find_import_cycles",
    "detect_breaking_changes",
    "analyze_config",
    "analyze_docker",
    "list_projects",
    "switch_project",
    "reindex",
]


def build_facade_dts() -> str:
    """Emit a minimal TypeScript declaration of the tools facade.

    Returned as a string so server can serve it to the model on first ts_search
    or via a dedicated meta tool.
    """
    lines = ["// Auto-generated facade for Token Savior Code Mode", "", "export interface Tools {"]
    for name in ALLOWED_TOOLS:
        lines.append(f"  {name}: (args?: Record<string, unknown>) => Promise<unknown>;")
    lines.append("}")
    return "\n".join(lines)
