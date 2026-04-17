"""Token Savior Memory Engine — façade re-exporting the memory/ subpackage.

Core DB primitives live in `db_core`; higher-level memory operations live
in `token_savior.memory.*`. This module stays thin so tests that monkey-patch
`memory_db.MEMORY_DB_PATH` keep affecting every submodule that opens a
connection via `memory_db.get_db()` / `memory_db.db_session()`.
"""
from __future__ import annotations

import json, os, re, sqlite3, sys, time  # noqa: F401,E401  historic public surface
from contextlib import AbstractContextManager
from pathlib import Path
from typing import Any  # noqa: F401  historic public surface

from . import db_core
from .db_core import (  # noqa: F401
    MEMORY_DB_PATH, _SCHEMA_PATH, _fts5_safe_query, _json_dumps, _migrated_paths,
    _now_epoch, _now_iso, content_hash, observation_hash, relative_age, strip_private,
)


def get_db(db_path: Path | str | None = None) -> sqlite3.Connection:
    return db_core.get_db(db_path or MEMORY_DB_PATH)


def db_session(db_path: Path | str | None = None) -> AbstractContextManager[sqlite3.Connection]:
    return db_core.db_session(db_path or MEMORY_DB_PATH)


def run_migrations(db_path: Path | str | None = None) -> None:
    return db_core.run_migrations(db_path or MEMORY_DB_PATH)


# ---- memory/ subpackage re-exports (public surface) ----------------------
from token_savior.memory._text_utils import _jaccard, _STOPWORDS, _TOKEN_RE  # noqa: E402,F401
from token_savior.memory.budget import DEFAULT_SESSION_BUDGET_TOKENS, format_session_budget_box, get_session_budget_stats  # noqa: E402,F401
from token_savior.memory.bus import DEFAULT_VOLATILE_TTL_DAYS, memory_bus_list  # noqa: E402,F401
from token_savior.memory.consistency import (  # noqa: E402,F401
    CONSISTENCY_QUARANTINE_THRESHOLD, CONSISTENCY_STALE_THRESHOLD, _CONTRADICTION_OPPOSITES,
    _RULE_TYPES_FOR_CONTRADICTION, check_symbol_staleness, compute_continuity_score,
    detect_contradictions, get_consistency_stats, get_validity_score,
    list_quarantined_observations, run_consistency_check, update_consistency_score,
)
from token_savior.memory.corpora import corpus_build, corpus_get  # noqa: E402,F401
from token_savior.memory.decay import (  # noqa: E402,F401
    _DECAY_IMMUNE_TYPES, _DECAY_MAX_AGE_SEC, _DECAY_MIN_ACCESS, _DECAY_UNREAD_SEC,
    _DEFAULT_TTL_DAYS, _ZERO_ACCESS_RULES, _bump_access, _decay_candidates_sql,
    _recalculate_relevance_scores, run_decay,
)
from token_savior.memory.dedup import dedup_sweep, get_injection_stats, global_dedup_check, semantic_dedup_check  # noqa: E402,F401
from token_savior.memory.distillation import get_mdl_stats, run_mdl_distillation  # noqa: E402,F401
from token_savior.memory.events import event_save  # noqa: E402,F401
from token_savior.memory.health import run_health_check  # noqa: E402,F401
from token_savior.memory.index import (  # noqa: E402,F401
    _TYPE_SCORES, _ensure_memory_cache, compute_obs_score, get_recent_index,
    get_timeline_around, get_top_observations, invalidate_memory_cache,
)
from token_savior.memory.lattice import (  # noqa: E402,F401
    LATTICE_CONTEXTS, LATTICE_LEVELS, _detect_context_type, _ensure_lattice_row,
    get_lattice_stats, record_lattice_feedback, thompson_sample_level,
)
from token_savior.memory.links import (  # noqa: E402,F401
    _PROMOTION_RULES, _PROMOTION_TYPE_RANK, _TYPE_PRIORITY, _ensure_links_index,
    auto_link_observation, explain_observation, get_linked_observations, relink_all, run_promotions,
)
from token_savior.memory.modes import (  # noqa: E402,F401
    ACTIVITY_TRACKER_PATH, DEFAULT_MODES, MODE_CONFIG_PATH, SESSION_OVERRIDE_PATH,
    _load_mode_file, _read_activity_tracker, _read_session_override, _write_activity_tracker,
    clear_session_override, get_current_mode, list_modes, set_mode, set_project_mode, set_session_override,
)
from token_savior.memory.notifications import notify_telegram  # noqa: E402,F401
from token_savior.memory.observations import (  # noqa: E402,F401
    _CORRUPTION_MARKERS, _is_corrupted_content, observation_delete, observation_get,
    observation_get_by_file, observation_get_by_session, observation_get_by_symbol,
    observation_list_archived, observation_restore, observation_save,
    observation_save_ruled_out, observation_save_volatile, observation_search,
    observation_update,
)
from token_savior.memory.prompts import analyze_prompt_patterns, prompt_save, prompt_search  # noqa: E402,F401
from token_savior.memory.reasoning import (  # noqa: E402,F401
    dcp_stats, optimize_output_order, reasoning_inject, reasoning_list,
    reasoning_save, reasoning_search, register_chunks,
)
from token_savior.memory.roi import (  # noqa: E402,F401
    _ROI_HORIZON_DAYS, _ROI_LAMBDA, _ROI_THRESHOLD, _ROI_TOKENS_PER_HIT, _ROI_TYPE_MULTIPLIER,
    compute_observation_roi, get_roi_stats, run_roi_gc,
)
from token_savior.memory.sessions import session_end, session_start  # noqa: E402,F401
from token_savior.memory.stats import get_stats  # noqa: E402,F401
from token_savior.memory.summaries import summary_parse, summary_save  # noqa: E402,F401
