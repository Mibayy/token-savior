"""Le serveur sert le protocole 2026-07-28 quand le SDK MCP 2.x est installe.

Claude Code negocie ce protocole par defaut sur stdio depuis la 2.1.292. Le
SDK 2.x a retire les decorateurs `list_tools()` / `call_tool()` : sans
`_serveur_sdk2`, le serveur plantait au demarrage (`'Server' object has no
attribute 'list_tools'`). Ignore sous le SDK 1.x, ou le chemin historique est
couvert par le reste de la suite.
"""
from __future__ import annotations

import asyncio

import pytest

from token_savior.server import _sdk_mcp_majeur

pytestmark = pytest.mark.skipif(_sdk_mcp_majeur() < 2, reason="SDK MCP 1.x")


@pytest.mark.parametrize("mode", ["legacy", "2026-07-28"])
def test_les_deux_epoques_listent_et_appellent(mode: str) -> None:
    from mcp import Client

    from token_savior.server import _serveur_sdk2

    async def scenario():
        async with Client(_serveur_sdk2(), mode=mode) as client:
            outils = await client.list_tools()
            noms = {t.name for t in outils.tools}
            assert "read_lines" in noms
            r = await client.call_tool("list_projects", {})
            assert r.content and r.content[0].text
            return client.protocol_version

    version = asyncio.run(scenario())
    if mode != "legacy":
        assert version == "2026-07-28"
