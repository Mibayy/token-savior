"""Mutable session state for the Token Savior MCP server.

Single source of truth for all module-level globals previously held in
server.py. Handler modules read/write these via ``server_state.<name>``
so that mutations propagate consistently across split modules.
"""

from __future__ import annotations

import os
import threading
import time
import uuid
from collections import deque
from pathlib import Path

from token_savior import telemetry
from token_savior.leiden_communities import LeidenCommunities
from token_savior.linucb_injector import LinUCBInjector
from token_savior.markov_prefetcher import PPMPrefetcher
from token_savior.session_warmstart import SessionWarmStart
from token_savior.slot_manager import SlotManager
from token_savior.tca_engine import TCAEngine

# ---------------------------------------------------------------------------
# MCP server instance -- LAZY.
# `from mcp.server import Server` charge ~1.24s du SDK MCP. server_state etant
# importe par presque tout TS, ce cout etait paye a chaque import (CLI, scripts,
# tests, daemon cold-start). On expose une fabrique lazy `get_server()` qui
# instantie au premier appel. Les decorateurs @server.X sont appliques dans
# `token_savior.server.run()`, pas en module-load. Voir server.py::run.
# ---------------------------------------------------------------------------

_server_singleton = None


def get_server():
    """Lazy MCP Server() singleton. Triggers `import mcp.server` on first call."""
    global _server_singleton
    if _server_singleton is None:
        from mcp.server import Server  # lazy : ~1.24s SDK MCP load
        _server_singleton = Server("token-savior-recall")
    return _server_singleton


# Moteurs d'optimisation, construits au premier acces et non a l'import.
#
# Les construire a l'import figeait le repertoire de statistiques avant que
# quiconque ait pu le choisir -- et `TCAEngine.__init__` fait un
# `mkdir(parents=True)`, donc importer ce module creait le repertoire fige sur
# le disque (#90). Ils restent des attributs de module : `state._prefetcher`
# marche comme avant, et un test qui fait `monkeypatch.setattr(server_state,
# "_tca_engine", espion)` gagne toujours, puisque le `__getattr__` d'un module
# n'est consulte que pour un nom absent.
#
# Declare avant `__getattr__` pour qu'un acces pendant l'import ne tombe jamais
# sur un nom pas encore defini.
_MOTEURS = {
    # Markov prefetcher (P8) — PPM variable-order model on tool-call sequences.
    "_prefetcher": PPMPrefetcher,
    # TCA — Tenseur de Co-Activation (PMI on symbol co-activation).
    "_tca_engine": TCAEngine,
    # Leiden community detector — clusters the symbol dependency graph.
    "_leiden": LeidenCommunities,
    # LinUCB contextual bandit — ranks observations for injection.
    "_linucb": LinUCBInjector,
    # Cross-session warm start — finds historical sessions with similar signature.
    "_warm_start": SessionWarmStart,
}


def _construire_moteur(nom: str):
    """Construit un moteur au premier acces, puis le fige comme un vrai attribut."""
    instance = _MOTEURS[nom](Path(stats_dir()))
    globals()[nom] = instance
    return instance


# Backward-compat alias. Many call sites do `from token_savior.server_state
# import server` then access `server.run(...)`. We expose `server` as a
# property-like attribute via __getattr__ on the module so that the first
# real access (e.g. `server.run`) instantiates the Server. Python 3.7+.
def __getattr__(name):
    if name == "server":
        return get_server()
    if name in _MOTEURS:
        return _construire_moteur(name)
    raise AttributeError(f"module 'token_savior.server_state' has no attribute {name!r}")

# ---------------------------------------------------------------------------
# Persistent cache versioning
# ---------------------------------------------------------------------------

_CACHE_VERSION: int = 3  # Bumped: StructuralMetadata.variables + ProjectIndex.variable_table

# ---------------------------------------------------------------------------
# Project state — slot manager owns the project dict and active root
# ---------------------------------------------------------------------------

_slot_mgr: SlotManager = SlotManager(_CACHE_VERSION)

# ---------------------------------------------------------------------------
# Session usage counters (aggregated across all projects in this session)
# ---------------------------------------------------------------------------

_session_start: float = time.time()
_session_id: str = uuid.uuid4().hex[:12]
_tool_call_counts: dict[str, int] = {}
_total_chars_returned: int = 0
_total_naive_chars: int = 0

# ---------------------------------------------------------------------------
# Compact Symbol Cache (CSC) — per-session, in-memory.
# Tracks symbols already sent this session so repeat reads return a compact
# stub (cache_token + signature) instead of the full body. Reset on restart.
# key = f"{kind}:{project_root}:{qualified_name}"
# value = {"cache_token": str, "body_hash": str, "view_count": int,
#          "full_source": str, "signature": str}
# ---------------------------------------------------------------------------

_session_symbol_cache: dict[str, dict] = {}
_csc_hits: int = 0
_csc_tokens_saved: int = 0  # naive_chars - actual_chars summed across hits

# ---------------------------------------------------------------------------
# Session Result Cache (SRC) — memoizes find_symbol / get_functions /
# get_dependents return values within the current MCP server process.
# key = f"{kind}:{project_root}:{cache_gen}:{args_repr}"
# Cleared implicitly when cache_gen bumps (old keys just never match).
# ---------------------------------------------------------------------------

_session_result_cache: dict[str, object] = {}
_src_hits: int = 0
_src_misses: int = 0

# Tools whose result is memoizable across calls within a single MCP server
# process. They all return pure functions of (slot index state, args).
_SRC_CACHEABLE_TOOLS: frozenset[str] = frozenset({
    "find_symbol",
    "get_functions",
    "get_dependents",
})

# ---------------------------------------------------------------------------
# Persistent stats configuration
# ---------------------------------------------------------------------------

def stats_dir() -> str:
    """Ou ce module ecrit, demande au moment ou il ecrit (#90).

    `_STATS_DIR` etait une constante calculee a l'import, et les cinq moteurs
    ci-dessous etaient construits avec elle, egalement a l'import. Poser
    `TOKEN_SAVIOR_STATS_DIR` apres un `import token_savior.server_state` ne
    changeait donc plus rien ici, alors que `telemetry` en tenait compte : le
    meme processus ecrivait dans deux repertoires.
    """
    return str(telemetry.stats_dir())


_MAX_SESSION_HISTORY: int = 200

# Les moteurs eux-memes sont declares plus haut, avec `__getattr__`.
# Track symbols injected by memory_index so we can credit them as reward
# when a subsequent call references them.
_linucb_pending: dict[int, dict] = {}  # obs_id -> {features, context, injected_epoch}

# ---------------------------------------------------------------------------
# Pre-warm cache populated by the daemon thread; key = predicted state.
# ---------------------------------------------------------------------------

_prefetch_cache: dict[str, str] = {}
_prefetch_lock: threading.Lock = threading.Lock()

# ---------------------------------------------------------------------------
# STTE (Speculative Tool Tree Execution) counters
# ---------------------------------------------------------------------------

_spec_branches_explored: int = 0
_spec_branches_warmed: int = 0
_spec_branches_hit: int = 0
_spec_tokens_saved: int = 0

# ---------------------------------------------------------------------------
# TCS (Schema compression) counters
# ---------------------------------------------------------------------------

_tcs_calls: int = 0
_tcs_chars_before: int = 0
_tcs_chars_after: int = 0

# ---------------------------------------------------------------------------
# DCP (Differential Context Protocol) counters
# ---------------------------------------------------------------------------

_dcp_calls: int = 0
_dcp_stable_chunks: int = 0
_dcp_total_chunks: int = 0

# ---------------------------------------------------------------------------
# Tool-set constants used by dispatch and counters
# ---------------------------------------------------------------------------

_DCP_ELIGIBLE_TOOLS: frozenset[str] = frozenset({
    "get_functions",
    "get_classes",
    "get_imports",
    "find_symbol",
    "get_dependents",
    "get_dependencies",
    "memory_search",
    "memory_index",
})
_DCP_MIN_BYTES: int = 500

_COMPRESSIBLE_TOOLS: frozenset[str] = frozenset({
    "get_functions",
    "get_classes",
    "get_imports",
    "find_symbol",
    "get_dependents",
    "get_dependencies",
    "get_call_chain",
    "get_change_impact",
    "find_impacted_test_files",
    "get_structure_summary",
})

_PREFETCHABLE_TOOLS: frozenset[str] = frozenset({
    "get_function_source",
    "get_class_source",
    "get_dependents",
    "get_dependencies",
    "find_symbol",
})

# ---------------------------------------------------------------------------
# Auto-save tracking (TOKEN_SAVIOR_MEMORY_AUTO_SAVE=1)
# ---------------------------------------------------------------------------

_auto_save_enabled: bool = os.environ.get("TOKEN_SAVIOR_MEMORY_AUTO_SAVE", "") == "1"
_auto_save_symbols: list[str] = []
_auto_save_tools: list[str] = []
_auto_save_project: str | None = None

# ---------------------------------------------------------------------------
# Chain-nudge tracker
# ---------------------------------------------------------------------------
# Rolling buffer of recent (epoch, tool, symbol) calls. Used by server.py to
# detect chains like find_symbol(X) -> get_function_source(X) that should
# collapse into a single get_full_context(X) call. Data from 9 days of usage
# showed 258 search_codebase->get_function_source and 42 find_symbol->
# get_function_source chains on the same symbol within 60s; trailing _hints
# were ignored, so the nudge is prepended at the top of the next response.

_chain_calls: deque[tuple[float, str, str]] = deque(maxlen=8)
_chain_nudges_emitted: int = 0  # telemetry: count of nudges actually fired
_CHAIN_NUDGE_DISABLED: bool = (
    os.environ.get("TOKEN_SAVIOR_CHAIN_NUDGE", "1").lower() in ("0", "false", "off")
)

# Opt-in: bridge the ts_search cold start by delegating the first (cold) call
# to a warm `ts _daemon-serve` over its Unix socket. Off by default -- most
# installs have no daemon; enable where one runs (see daemon_client.py).
_TS_SEARCH_COLD_DELEGATE: bool = (
    os.environ.get("TS_SEARCH_COLD_DELEGATE", "0").lower() in ("1", "true", "on")
)
