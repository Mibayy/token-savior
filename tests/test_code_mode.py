"""Tests for Code Mode (ts_execute) end-to-end via the real MCP handler."""
from __future__ import annotations

import asyncio
import json
import os
import textwrap

import pytest


def test_facade_dts_is_valid_typescript():
    """The generated facade should at minimum compile to a parseable .d.ts shape."""
    from token_savior.code_mode import build_facade_dts
    dts = build_facade_dts()
    assert "export interface Tools" in dts
    assert "find_symbol" in dts
    assert "get_function_source" in dts
    assert "Promise<unknown>" in dts


def test_facade_signature_types_from_inputschema():
    """Generated TS signatures must reflect the actual inputSchema (types, required, enums)."""
    from token_savior.code_mode.facade import build_tool_signature
    from token_savior.tool_schemas import TOOL_SCHEMAS

    sig = build_tool_signature("find_symbol", TOOL_SCHEMAS["find_symbol"])
    assert "find_symbol:" in sig
    assert "name?: string" in sig
    assert "names?: Array<string>" in sig
    assert "level?: number" in sig

    sig = build_tool_signature("get_dependencies", TOOL_SCHEMAS["get_dependencies"])
    assert "name: string" in sig  # required
    assert "args:" in sig and "args?:" not in sig  # has required field

    sig = build_tool_signature("get_full_context", TOOL_SCHEMAS["get_full_context"])
    assert '"compact"' in sig and '"full"' in sig  # enum as union


def test_facade_is_cached():
    """get_cached_facade() must memoize."""
    from token_savior.code_mode import get_cached_facade
    a = get_cached_facade()
    b = get_cached_facade()
    assert a is b


def test_code_mode_profile_exposes_minimal_manifest(monkeypatch):
    """profile=code_mode exposes exactly {ts_execute, ts_search, switch_project, list_projects}."""
    import sys
    monkeypatch.setenv("TOKEN_SAVIOR_PROFILE", "code_mode")
    if "token_savior.server" in sys.modules:
        del sys.modules["token_savior.server"]
    import token_savior.server as srv
    names = {t.name for t in srv.TOOLS}
    assert names == {"ts_execute", "ts_search", "switch_project", "list_projects"}, names


def test_ts_search_returns_ts_signatures_when_format_ts():
    """ts_search(format='ts') returns 'signature' entries, not 'inputSchema'."""
    from token_savior.server_handlers.tool_search import ts_search

    payload = ts_search("find a symbol", top_k=3, format="ts")
    assert payload["matched_tools"], "expected non-empty matched_tools"
    for entry in payload["matched_tools"]:
        assert "signature" in entry
        assert "inputSchema" not in entry
        assert "Promise<unknown>" in entry["signature"]


def test_ts_search_default_format_remains_schema():
    """Default format must keep the legacy JSONSchema behaviour."""
    from token_savior.server_handlers.tool_search import ts_search

    payload = ts_search("find a symbol", top_k=3)
    for entry in payload["matched_tools"]:
        assert "inputSchema" in entry
        assert "signature" not in entry


def test_ts_execute_arithmetic():
    """Bare script returning a value, no tool calls."""
    from token_savior.server import _handle_ts_execute

    result = asyncio.run(_handle_ts_execute({"script": "return 21 * 2;"}))
    payload = json.loads(result[0].text)
    assert payload["value"] == 42
    assert payload["error"] is None
    assert payload["tool_calls"] == 0


def test_ts_execute_real_chain(monkeypatch):
    """Two-call chain through the actual sandbox <-> Python bridge.

    Stubs `_dispatch_tool` to avoid polluting prod memory.db with test
    project rows; the goal is to verify the bridge marshals args + return
    values across the Node boundary correctly, not to re-test the
    individual TS tools (covered elsewhere).
    """
    import token_savior.server as srv
    from mcp.types import TextContent

    calls: list[tuple[str, dict]] = []

    def fake_dispatch(name, args, rec):
        calls.append((name, dict(args)))
        if name == "find_symbol":
            payload = {"file": "src/service.py", "line": 1, "symbol": args.get("name")}
        elif name == "get_function_source":
            payload = {"name": args.get("name"), "source": "def process_payment(): ..."}
        else:
            payload = {"unknown": name}
        return [TextContent(type="text", text=json.dumps(payload))]

    monkeypatch.setattr(srv, "_dispatch_tool", fake_dispatch)

    script = textwrap.dedent("""
        const sym = await tools.find_symbol({ name: "process_payment" });
        const src = await tools.get_function_source({ name: sym.symbol });
        return { sym, src };
    """).strip()
    result = asyncio.run(srv._handle_ts_execute({"script": script}))
    payload = json.loads(result[0].text)
    assert payload["error"] is None, payload
    assert payload["tool_calls"] == 2
    assert [c[0] for c in calls] == ["find_symbol", "get_function_source"]
    assert payload["value"]["sym"]["symbol"] == "process_payment"
    assert "process_payment" in payload["value"]["src"]["name"]


def test_ts_execute_rejects_unknown_tool():
    """A tool name not in ALLOWED_TOOLS must come back as a JS-side error."""
    from token_savior.server import _handle_ts_execute

    script = "return await tools.memory_admin({});"  # memory_admin not in facade
    result = asyncio.run(_handle_ts_execute({"script": script}))
    payload = json.loads(result[0].text)
    assert payload["error"] is not None
    assert (
        "not a function" in payload["error"]["message"].lower()
        or "not in" in payload["error"]["message"].lower()
    )


def test_ts_execute_timeout():
    """Long-running script killed at the configured timeout."""
    from token_savior.server import _handle_ts_execute

    script = "await new Promise(r => setTimeout(r, 5000)); return 'never';"
    result = asyncio.run(_handle_ts_execute({"script": script, "timeout_ms": 200}))
    payload = json.loads(result[0].text)
    assert payload["error"] is not None
    assert "timeout" in payload["error"]["message"].lower()


def test_ts_execute_empty_script():
    """Empty script is rejected with a clear error message."""
    from token_savior.server import _handle_ts_execute

    result = asyncio.run(_handle_ts_execute({"script": "  \n  "}))
    assert "required" in result[0].text.lower()


def test_ts_execute_throws_inside_script():
    """A user-thrown JS error is captured cleanly."""
    from token_savior.server import _handle_ts_execute

    script = "throw new Error('intentional boom');"
    result = asyncio.run(_handle_ts_execute({"script": script}))
    payload = json.loads(result[0].text)
    assert payload["error"] is not None
    assert "boom" in payload["error"]["message"]


def test_ts_execute_logs_captured():
    """console.log output is captured in the logs array."""
    from token_savior.server import _handle_ts_execute

    script = """
        console.log("hello", { a: 1 });
        console.warn("careful");
        return 42;
    """
    result = asyncio.run(_handle_ts_execute({"script": script}))
    payload = json.loads(result[0].text)
    assert payload["value"] == 42
    assert payload["error"] is None
    assert len(payload["logs"]) == 2
    assert any("hello" in log for log in payload["logs"])


def test_ts_execute_in_tool_manifest():
    """ts_execute should be advertised in the MCP tools list."""
    from token_savior.server import TOOLS

    names = {t.name for t in TOOLS}
    assert "ts_execute" in names
    spec = next(t for t in TOOLS if t.name == "ts_execute")
    assert "script" in spec.inputSchema.get("required", [])


@pytest.mark.skipif(
    os.environ.get("TS_CODE_MODE_DISABLE") != "1",
    reason="only meaningful when explicitly disabled",
)
def test_ts_execute_disabled_via_env():
    """When TS_CODE_MODE_DISABLE=1, ts_execute is not advertised."""
    import importlib
    import token_savior.server as srv
    importlib.reload(srv)
    names = {t.name for t in srv.TOOLS}
    assert "ts_execute" not in names
