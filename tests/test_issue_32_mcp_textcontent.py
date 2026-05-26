"""Regression test for issue #32.

Bug: every successful tool call returned `isError=True` with a pydantic
`CallToolResult` validation error on `mcp` >= 3.5.0 because handlers
returned `_compat.TextContent` shim instances instead of `mcp.types.TextContent`.
The SDK validates `CallToolResult` with pydantic v2, which rejects the shim
(same class name, different class object).

This test exercises the protocol boundary: it builds a real `CallToolResult`
from the value `call_tool` returns. If any handler leaks shim instances, the
construction raises a `ValidationError`.

Existing integration tests never caught this because they only inspect the
returned list directly -- they never go through the SDK validation step.
"""

from __future__ import annotations

import asyncio

import pytest

from mcp.types import CallToolResult, TextContent as McpTextContent

from token_savior.server import call_tool


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


def _assert_mcp_compatible(result):
    assert isinstance(result, list) and result, "call_tool must return non-empty list"
    for item in result:
        assert isinstance(item, McpTextContent), (
            f"shim TextContent leaked to protocol boundary: {type(item).__module__}."
            f"{type(item).__name__}"
        )
    # Final check: the SDK builds this object after every call_tool. pydantic v2
    # rejects shim instances even if duck-type compatible.
    CallToolResult(content=result, isError=False)


class TestIssue32:
    def test_success_path_returns_real_textcontent(self, tmp_path):
        # Use a tool that doesn't require pre-registered state.
        result = _run(call_tool("get_usage_stats", {}))
        _assert_mcp_compatible(result)

    def test_error_path_returns_real_textcontent(self):
        # Unknown tool -> dispatcher returns an error TextContent.
        result = _run(call_tool("nonexistent_tool_xyz", {}))
        _assert_mcp_compatible(result)
        assert "unknown tool" in result[0].text.lower() or "error" in result[0].text.lower()

    @pytest.mark.parametrize("tool_name", ["ts_search", "ts_extended"])
    def test_ts_meta_tools_return_real_textcontent(self, tool_name):
        # Meta tools (ts_search, ts_extended) have their own handler paths;
        # cover them too since they were affected by the same shim leak.
        args = {"query": "find symbol"} if tool_name == "ts_search" else {"mode": "list"}
        result = _run(call_tool(tool_name, args))
        _assert_mcp_compatible(result)
