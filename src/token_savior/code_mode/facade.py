"""Allowed-tool whitelist + TypeScript facade emitter for Code Mode scripts.

The facade is generated from each tool's MCP `inputSchema` so the model writing
ts_execute scripts can rely on real argument types instead of opaque `unknown`.
Generated lazily and cached at module level for fast access.
"""
from __future__ import annotations

from typing import Any

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


_PRIMITIVE_MAP = {
    "string": "string",
    "integer": "number",
    "number": "number",
    "boolean": "boolean",
    "null": "null",
}


def _json_schema_to_ts(schema: Any) -> str:
    """Convert a JSON Schema fragment to a TypeScript type expression.

    Handles the subset we actually use: primitives, arrays, enums, object
    properties, and unions via `type: [a, b]`. Anything else falls back to
    `unknown` rather than fail loudly — the goal is a useful hint, not full
    JSON Schema fidelity.
    """
    if not isinstance(schema, dict):
        return "unknown"

    enum_vals = schema.get("enum")
    if enum_vals:
        parts = []
        for v in enum_vals:
            if isinstance(v, str):
                parts.append(repr(v).replace("'", '"').replace('\\"', "'"))
            elif isinstance(v, bool):
                parts.append("true" if v else "false")
            elif v is None:
                parts.append("null")
            else:
                parts.append(str(v))
        return " | ".join(parts) if parts else "unknown"

    t = schema.get("type")
    if isinstance(t, list):
        return " | ".join(_json_schema_to_ts({**schema, "type": one}) for one in t)

    if t == "array":
        items = schema.get("items")
        if isinstance(items, dict):
            return f"Array<{_json_schema_to_ts(items)}>"
        return "unknown[]"

    if t == "object":
        props = schema.get("properties") or {}
        if not props:
            return "Record<string, unknown>"
        required = set(schema.get("required") or [])
        fields = []
        for key, sub in props.items():
            optional = "" if key in required else "?"
            fields.append(f"{key}{optional}: {_json_schema_to_ts(sub)}")
        return "{ " + "; ".join(fields) + " }"

    return _PRIMITIVE_MAP.get(t, "unknown")


def build_tool_signature(name: str, schema: Any) -> str:
    """Return a single TS method signature line for one tool."""
    input_schema = (schema or {}).get("inputSchema") or {}
    props = input_schema.get("properties") or {}
    if not props:
        return f"  {name}: (args?: Record<string, unknown>) => Promise<unknown>;"
    required = set(input_schema.get("required") or [])
    fields = []
    for key, sub in props.items():
        optional = "" if key in required else "?"
        fields.append(f"{key}{optional}: {_json_schema_to_ts(sub)}")
    arg_type = "{ " + "; ".join(fields) + " }"
    arg_decl = "args" if required else "args?"
    return f"  {name}: ({arg_decl}: {arg_type}) => Promise<unknown>;"


def build_facade_dts(schemas: dict | None = None, tools: list[str] | None = None) -> str:
    """Emit a TypeScript declaration of the tools facade.

    Args:
        schemas: dict[name -> {inputSchema: ...}]. Defaults to TOOL_SCHEMAS.
        tools: subset of tool names to include. Defaults to ALLOWED_TOOLS.
    """
    if schemas is None:
        from token_savior.tool_schemas import TOOL_SCHEMAS
        schemas = TOOL_SCHEMAS
    tools = tools or ALLOWED_TOOLS

    lines = [
        "// Auto-generated facade for Token Savior Code Mode.",
        "// Each method maps to one MCP tool. Args reflect the tool's inputSchema.",
        "// Returns are typed `Promise<unknown>` — inspect at runtime via console.log.",
        "",
        "export interface Tools {",
    ]
    for name in tools:
        if name in schemas:
            lines.append(build_tool_signature(name, schemas[name]))
        else:
            lines.append(f"  {name}: (args?: Record<string, unknown>) => Promise<unknown>;")
    lines.append("}")
    return "\n".join(lines)


_FACADE_CACHE: str | None = None


def get_cached_facade() -> str:
    """Return the auto-generated facade for ALLOWED_TOOLS, cached at module level."""
    global _FACADE_CACHE
    if _FACADE_CACHE is None:
        _FACADE_CACHE = build_facade_dts()
    return _FACADE_CACHE
