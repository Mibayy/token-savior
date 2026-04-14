"""Per-domain MCP-tool handlers for the Token Savior server.

Each submodule owns a slice of the dispatch table and exports a ``HANDLERS``
dict mapping tool name → handler(slot, args). Step 13 will aggregate them
into a single ``ALL_HANDLERS`` mapping with collision detection.
"""
