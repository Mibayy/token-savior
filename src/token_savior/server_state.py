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
from pathlib import Path

from mcp.server import Server

from token_savior.leiden_communities import LeidenCommunities
from token_savior.linucb_injector import LinUCBInjector
from token_savior.markov_prefetcher import PPMPrefetcher
from token_savior.session_warmstart import SessionWarmStart
from token_savior.slot_manager import SlotManager
from token_savior.tca_engine import TCAEngine

# ---------------------------------------------------------------------------
# MCP server instance
# ---------------------------------------------------------------------------

server: Server = Server("token-savior-recall")

# ---------------------------------------------------------------------------
# Persistent cache versioning
# ---------------------------------------------------------------------------

_CACHE_VERSION: int = 2  # Bumped: switched from pickle to JSON

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

_STATS_DIR: str = os.path.expanduser(
    os.environ.get("TOKEN_SAVIOR_STATS_DIR", "~/.local/share/token-savior")
)
_MAX_SESSION_HISTORY: int = 200

# ---------------------------------------------------------------------------
# Optimization engines (instantiated once at module import)
# ---------------------------------------------------------------------------

# Markov prefetcher (P8) — PPM variable-order model on tool-call sequences.
_prefetcher: PPMPrefetcher = PPMPrefetcher(Path(_STATS_DIR))
# TCA — Tenseur de Co-Activation (PMI on symbol co-activation).
_tca_engine: TCAEngine = TCAEngine(Path(_STATS_DIR))
# Leiden community detector — clusters the symbol dependency graph.
_leiden: LeidenCommunities = LeidenCommunities(Path(_STATS_DIR))
# LinUCB contextual bandit — ranks observations for injection.
_linucb: LinUCBInjector = LinUCBInjector(Path(_STATS_DIR))
# Cross-session warm start — finds historical sessions with similar signature.
_warm_start: SessionWarmStart = SessionWarmStart(Path(_STATS_DIR))
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
})

_PREFETCHABLE_TOOLS: frozenset[str] = frozenset({
    "get_function_source",
    "get_class_source",
    "get_dependents",
    "get_dependencies",
    "find_symbol",
})
