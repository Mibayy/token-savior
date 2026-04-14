"""Token Savior Memory Engine — SQLite persistence layer.

Core DB primitives + shared utils live in `db_core`; this module re-exports
them for backward compatibility and owns the higher-level memory operations.
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
import sys
import time
from contextlib import AbstractContextManager
from pathlib import Path
from typing import Any

from . import db_core
from .db_core import (
    MEMORY_DB_PATH,
    _SCHEMA_PATH,
    _fts5_safe_query,
    _json_dumps,
    _migrated_paths,
    _now_epoch,
    _now_iso,
    observation_hash,
    relative_age,
    strip_private,
)

__all__ = [
    "MEMORY_DB_PATH", "_SCHEMA_PATH", "_migrated_paths",
    "run_migrations", "get_db", "db_session",
    "_now_iso", "_now_epoch", "_json_dumps",
    "observation_hash", "strip_private", "relative_age", "_fts5_safe_query",
]


# Thin wrappers so tests can patch `memory_db.MEMORY_DB_PATH` and affect
# connections opened via `memory_db.get_db()` / `memory_db.db_session()`.
def get_db(db_path: Path | str | None = None) -> sqlite3.Connection:
    return db_core.get_db(db_path or MEMORY_DB_PATH)


def db_session(
    db_path: Path | str | None = None,
) -> AbstractContextManager[sqlite3.Connection]:
    return db_core.db_session(db_path or MEMORY_DB_PATH)


def run_migrations(db_path: Path | str | None = None) -> None:
    return db_core.run_migrations(db_path or MEMORY_DB_PATH)


from token_savior.memory.consistency import (  # noqa: E402,F401  re-exports
    CONSISTENCY_QUARANTINE_THRESHOLD,
    CONSISTENCY_STALE_THRESHOLD,
    check_symbol_staleness,
    compute_continuity_score,
    get_consistency_stats,
    get_validity_score,
    list_quarantined_observations,
    run_consistency_check,
    update_consistency_score,
)


# ---------------------------------------------------------------------------
# Sessions
# ---------------------------------------------------------------------------


from token_savior.memory.sessions import session_end, session_start  # noqa: E402,F401  re-exports


# ---------------------------------------------------------------------------
# Observations
# ---------------------------------------------------------------------------

from token_savior.memory.decay import (  # noqa: E402,F401  re-exports (constants)
    _DECAY_IMMUNE_TYPES,
    _DECAY_MAX_AGE_SEC,
    _DECAY_MIN_ACCESS,
    _DECAY_UNREAD_SEC,
    _DEFAULT_TTL_DAYS,
)


from token_savior.memory.consistency import (  # noqa: E402,F401  re-exports
    _CONTRADICTION_OPPOSITES,
    _RULE_TYPES_FOR_CONTRADICTION,
    detect_contradictions,
)


from token_savior.memory.observations import (  # noqa: E402,F401  re-exports
    _CORRUPTION_MARKERS,
    _is_corrupted_content,
    observation_delete,
    observation_get,
    observation_get_by_session,
    observation_get_by_symbol,
    observation_list_archived,
    observation_restore,
    observation_save,
    observation_save_ruled_out,
    observation_save_volatile,
    observation_search,
    observation_update,
)

# ---------------------------------------------------------------------------
# Step C: inter-agent memory bus
# ---------------------------------------------------------------------------

# Volatile observations are short-lived signals between subagents (or between
# a subagent and the parent). They expire fast (default 1 day) so the bus
# never accumulates stale chatter.
from token_savior.memory.bus import DEFAULT_VOLATILE_TTL_DAYS  # noqa: E402,F401  re-export


from token_savior.memory.bus import memory_bus_list  # noqa: E402,F401  re-export


# ---------------------------------------------------------------------------
# Reasoning Trace Compression (v2.2 Step A)
# ---------------------------------------------------------------------------


from token_savior.memory.reasoning import (  # noqa: E402,F401  re-exports
    dcp_stats,
    optimize_output_order,
    reasoning_inject,
    reasoning_list,
    reasoning_save,
    reasoning_search,
    register_chunks,
)



# ---------------------------------------------------------------------------
# Step D: Adaptive Lattice (Beta-Binomial Thompson sampling on granularity)
# ---------------------------------------------------------------------------

# Granularity levels for source-fetching tools:
#   0 = full source (no compression)
#   1 = signature + docstring + first/last lines
#   2 = signature only
#   3 = name + line range only
from token_savior.memory.lattice import (  # noqa: E402,F401  re-exports
    LATTICE_CONTEXTS,
    LATTICE_LEVELS,
    _detect_context_type,
    _ensure_lattice_row,
    get_lattice_stats,
    record_lattice_feedback,
    thompson_sample_level,
)





# ---------------------------------------------------------------------------
# Summaries
# ---------------------------------------------------------------------------


from token_savior.memory.summaries import summary_parse, summary_save  # noqa: E402,F401  re-exports


# ---------------------------------------------------------------------------
# Index & Timeline (progressive disclosure)
# ---------------------------------------------------------------------------


from token_savior.memory.index import (  # noqa: E402,F401  re-exports
    _TYPE_SCORES,
    _ensure_memory_cache,
    compute_obs_score,
    get_recent_index,
    get_timeline_around,
    get_top_observations,
    invalidate_memory_cache,
)


# ---------------------------------------------------------------------------
# Events
# ---------------------------------------------------------------------------


from token_savior.memory.events import event_save  # noqa: E402,F401  re-export


# ---------------------------------------------------------------------------
# User prompts
# ---------------------------------------------------------------------------


from token_savior.memory.prompts import prompt_save, prompt_search  # noqa: E402,F401  re-exports


# ---------------------------------------------------------------------------
# Stats
# ---------------------------------------------------------------------------


from token_savior.memory.stats import get_stats  # noqa: E402,F401  re-export


# ---------------------------------------------------------------------------
# Decay
# ---------------------------------------------------------------------------




# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------




# ---------------------------------------------------------------------------
# Decay
# ---------------------------------------------------------------------------


from token_savior.memory.decay import (  # noqa: E402,F401  re-exports
    _ZERO_ACCESS_RULES,
    _bump_access,
    _decay_candidates_sql,
    _recalculate_relevance_scores,
    run_decay,
)


# ---------------------------------------------------------------------------
# Token Economy ROI — Garbage Collection based on expected value of retention.
# ---------------------------------------------------------------------------
# ROI(o) = tokens_saved_per_hit × P(hit) × horizon_days × TYPE_MULTIPLIER − tokens_stored
# P(hit) = exp(−λ × days_since_access) × (1 + 0.1 × access_count)
# An observation with ROI below ROI_THRESHOLD is a candidate for archival.

from token_savior.memory.roi import (  # noqa: E402,F401  re-exports
    _ROI_HORIZON_DAYS,
    _ROI_LAMBDA,
    _ROI_THRESHOLD,
    _ROI_TOKENS_PER_HIT,
    _ROI_TYPE_MULTIPLIER,
    compute_observation_roi,
    get_roi_stats,
    run_roi_gc,
)


# ---------------------------------------------------------------------------
# MDL Memory Distillation — crystallize similar obs into abstractions.
# ---------------------------------------------------------------------------

from token_savior.memory.distillation import get_mdl_stats, run_mdl_distillation  # noqa: E402,F401  re-exports


from token_savior.memory.links import (  # noqa: E402,F401  re-exports
    _PROMOTION_RULES,
    _PROMOTION_TYPE_RANK,
    _ensure_links_index,
    auto_link_observation,
)


from token_savior.memory.links import (  # noqa: E402,F401  re-exports
    _TYPE_PRIORITY,
    explain_observation,
)


from token_savior.memory.dedup import (  # noqa: E402,F401  re-exports
    get_injection_stats,
    global_dedup_check,
    semantic_dedup_check,
)


# ---------------------------------------------------------------------------
# Closed-loop budget (Step B)
# ---------------------------------------------------------------------------

# Claude Max effective context window. Treat as a soft ceiling for budgeting;
# we measure observable consumption only (tokens we injected via hooks).
from token_savior.memory.budget import (  # noqa: E402,F401  re-exports
    DEFAULT_SESSION_BUDGET_TOKENS,
    format_session_budget_box,
    get_session_budget_stats,
)


from token_savior.memory._text_utils import _jaccard  # noqa: E402,F401  re-export


from token_savior.memory.health import run_health_check  # noqa: E402,F401  re-export


from token_savior.memory.links import relink_all  # noqa: E402,F401  re-export


from token_savior.memory.links import get_linked_observations  # noqa: E402,F401  re-export


from token_savior.memory._text_utils import _STOPWORDS, _TOKEN_RE  # noqa: E402,F401  re-export


from token_savior.memory.prompts import analyze_prompt_patterns  # noqa: E402,F401  re-export


from token_savior.memory.links import run_promotions  # noqa: E402,F401  re-export


# ---------------------------------------------------------------------------
# Corpora (thematic bundles)
# ---------------------------------------------------------------------------


from token_savior.memory.corpora import corpus_build, corpus_get  # noqa: E402,F401  re-exports


# ---------------------------------------------------------------------------
# Capture modes (split into memory/modes.py)
# ---------------------------------------------------------------------------

from token_savior.memory.modes import (  # noqa: E402,F401  re-exports
    ACTIVITY_TRACKER_PATH,
    DEFAULT_MODES,
    MODE_CONFIG_PATH,
    SESSION_OVERRIDE_PATH,
    _load_mode_file,
    _read_activity_tracker,
    _read_session_override,
    _write_activity_tracker,
    clear_session_override,
    get_current_mode,
    list_modes,
    set_mode,
    set_project_mode,
    set_session_override,
)


# ---------------------------------------------------------------------------
# Telegram notifications
# ---------------------------------------------------------------------------


from token_savior.memory.notifications import notify_telegram  # noqa: E402,F401  re-export
