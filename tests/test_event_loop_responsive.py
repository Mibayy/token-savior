"""The event loop must stay responsive while a tool call runs (#40).

`call_tool` is async but the work under it is synchronous: `_prep` builds or
updates the index inline, and a cold build on a large repository takes a long
time. With no offload, that blocks the whole asyncio loop, so the stdio
transport cannot read or write protocol traffic either. A client with a
timeout concludes the server is gone and drops the connection, which is what
"MCP server loses connection and doesn't auto reconnect" looks like from the
outside. It shows up on subagent calls because those often hit a project slot
that is not warm yet.

No pytest-asyncio in this repo, so the loop is driven with asyncio.run like
the other async-touching tests.
"""
from __future__ import annotations

import asyncio
import time

from token_savior import server


def test_loop_keeps_ticking_during_a_slow_tool_call(monkeypatch):
    ticks = 0

    def slow_dispatch(name, arguments, record_symbol=None):
        time.sleep(0.30)  # a cold index build, compressed
        return [{"ok": True}]

    monkeypatch.setattr(server, "_dispatch_tool", slow_dispatch)

    async def main() -> None:
        nonlocal ticks

        async def heartbeat() -> None:
            nonlocal ticks
            while True:
                await asyncio.sleep(0.005)
                ticks += 1

        beat = asyncio.create_task(heartbeat())
        await asyncio.sleep(0)  # let the heartbeat reach its first await
        await server.call_tool("find_symbol", {"name": "x"})
        beat.cancel()

    asyncio.run(main())

    # Blocking the loop pins this at 0. Anything above a couple of ticks means
    # the transport could still have answered while the call was running.
    assert ticks > 5, f"event loop was blocked for the whole call (ticks={ticks})"


def test_calls_stay_serialized(monkeypatch):
    """Offloading must not turn a single-threaded server into a concurrent one:
    the slots and their indexes are not written for parallel access."""
    concurrent = 0
    peak = 0

    def counting_dispatch(name, arguments, record_symbol=None):
        nonlocal concurrent, peak
        concurrent += 1
        peak = max(peak, concurrent)
        time.sleep(0.05)
        concurrent -= 1
        return [{"ok": True}]

    monkeypatch.setattr(server, "_dispatch_tool", counting_dispatch)

    async def main() -> None:
        await asyncio.gather(
            *(server.call_tool("find_symbol", {"name": "x"}) for _ in range(4))
        )

    asyncio.run(main())

    assert peak == 1, f"tool calls ran in parallel (peak={peak})"


def test_lock_survives_successive_event_loops(monkeypatch):
    """A module-level asyncio.Lock binds to the loop that first contends on it
    and raises for any other. The server has one loop forever, but the CLI and
    the daemon do not, so the lock is resolved per running loop."""
    def dispatch(name, arguments, record_symbol=None):
        time.sleep(0.02)
        return [{"ok": True}]

    monkeypatch.setattr(server, "_dispatch_tool", dispatch)

    async def contended() -> None:
        # Two concurrent calls force real contention, which is what binds it.
        await asyncio.gather(
            server.call_tool("find_symbol", {"name": "x"}),
            server.call_tool("find_symbol", {"name": "y"}),
        )

    for _ in range(3):
        asyncio.run(contended())
