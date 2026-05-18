"""Local lightweight TextContent / Tool shims to avoid eager mcp.types import.

Pourquoi : `from mcp.types import TextContent` declenche `import mcp` qui
charge tout le SDK (uvicorn, sse_starlette, fastmcp, ...) ~800ms cold start.
Inacceptable pour la CLI fork-mode et tout consommateur qui ne fait pas
tourner le serveur MCP.

Ces classes sont duck-type compatibles avec leurs equivalents `mcp.types` :
  - meme constructeur (type/text pour TextContent ; name/description/inputSchema pour Tool)
  - memes attributs publics
  - le serveur MCP convertit a `mcp.types.*` au moment de servir la response
    JSON-RPC (boundary unique dans server.py::run).
"""

from dataclasses import dataclass, field


@dataclass
class TextContent:
    """Drop-in replacement for mcp.types.TextContent."""

    type: str = "text"
    text: str = ""


@dataclass
class ToolDef:
    """Drop-in replacement for mcp.types.Tool at module level.

    Le serveur MCP convertit chaque ToolDef en mcp.types.Tool au moment
    de servir le manifest (handler list_tools). Voir server.py::list_tools.
    """

    name: str
    description: str = ""
    inputSchema: dict = field(default_factory=dict)


# Compat alias : a lot of code uses `Tool` interchangeably with `ToolDef`.
Tool = ToolDef


# Module-style shim pour `import mcp.types as types`.
class _TypesShim:
    TextContent = TextContent
    Tool = ToolDef


types = _TypesShim()
