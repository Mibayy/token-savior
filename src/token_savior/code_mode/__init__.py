"""Code Mode: 2-tool MCP facade running user scripts in a Node sandbox.

Collapses multi-tool chains (find_symbol -> get_function_source -> get_dependents)
into a single ts_execute call with a typed tool facade.
"""
from .facade import (
    ALLOWED_TOOLS,
    build_facade_dts,
    build_tool_signature,
    get_cached_facade,
)
from .sandbox import run_script_async

__all__ = [
    "ALLOWED_TOOLS",
    "build_facade_dts",
    "build_tool_signature",
    "get_cached_facade",
    "run_script_async",
]
