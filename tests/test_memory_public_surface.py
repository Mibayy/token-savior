"""Snapshot test for the public surface of token_savior.memory_db.

Locks the names exported by ``memory_db`` so the planned split into a
``memory/`` subpackage cannot silently drop or rename anything that
external code (tests, handlers, dashboard, server) imports from it.

If this test fails after a refactor step:
- a name was removed → re-export it from the façade in memory_db.py
- a name was added → update EXPECTED_PUBLIC below intentionally

Monkey-patch surface that callers depend on (do NOT break):
- memory_db.MEMORY_DB_PATH         (tests rebind for isolated DBs)
- memory_db.get_db / db_session    (handlers, dashboard, tests)
- memory_db.run_migrations         (server boot)
"""

import token_savior.memory_db as memory_db

EXPECTED_PUBLIC = [
    "ACTIVITY_TRACKER_PATH",
    "AbstractContextManager",
    "Any",
    "CONSISTENCY_QUARANTINE_THRESHOLD",
    "CONSISTENCY_STALE_THRESHOLD",
    "DEFAULT_MODES",
    "DEFAULT_SESSION_BUDGET_TOKENS",
    "DEFAULT_VOLATILE_TTL_DAYS",
    "LATTICE_CONTEXTS",
    "LATTICE_LEVELS",
    "MEMORY_DB_PATH",
    "MODE_CONFIG_PATH",
    "Path",
    "SESSION_OVERRIDE_PATH",
    "_CONTRADICTION_OPPOSITES",
    "_CORRUPTION_MARKERS",
    "_DECAY_IMMUNE_TYPES",
    "_DECAY_MAX_AGE_SEC",
    "_DECAY_MIN_ACCESS",
    "_DECAY_UNREAD_SEC",
    "_DEFAULT_TTL_DAYS",
    "_PROMOTION_RULES",
    "_PROMOTION_TYPE_RANK",
    "_ROI_HORIZON_DAYS",
    "_ROI_LAMBDA",
    "_ROI_THRESHOLD",
    "_ROI_TOKENS_PER_HIT",
    "_ROI_TYPE_MULTIPLIER",
    "_RULE_TYPES_FOR_CONTRADICTION",
    "_SCHEMA_PATH",
    "_STOPWORDS",
    "_TOKEN_RE",
    "_TYPE_PRIORITY",
    "_TYPE_SCORES",
    "_ZERO_ACCESS_RULES",
    "_bump_access",
    "_decay_candidates_sql",
    "_detect_context_type",
    "_ensure_lattice_row",
    "_ensure_links_index",
    "_ensure_memory_cache",
    "_fts5_safe_query",
    "_is_corrupted_content",
    "_jaccard",
    "_json_dumps",
    "_load_mode_file",
    "_migrated_paths",
    "_now_epoch",
    "_now_iso",
    "_read_activity_tracker",
    "_read_session_override",
    "_recalculate_relevance_scores",
    "_write_activity_tracker",
    "analyze_prompt_patterns",
    "annotations",
    "auto_link_observation",
    "check_symbol_staleness",
    "clear_session_override",
    "compute_continuity_score",
    "compute_obs_score",
    "compute_observation_roi",
    "content_hash",
    "corpus_build",
    "corpus_get",
    "db_core",
    "db_session",
    "dcp_stats",
    "dedup_sweep",
    "detect_contradictions",
    "event_save",
    "explain_observation",
    "format_session_budget_box",
    "get_consistency_stats",
    "get_current_mode",
    "get_db",
    "get_injection_stats",
    "get_lattice_stats",
    "get_linked_observations",
    "get_mdl_stats",
    "get_recent_index",
    "get_roi_stats",
    "get_session_budget_stats",
    "get_stats",
    "get_timeline_around",
    "get_top_observations",
    "get_validity_score",
    "global_dedup_check",
    "invalidate_memory_cache",
    "json",
    "list_modes",
    "list_quarantined_observations",
    "memory_bus_list",
    "notify_telegram",
    "observation_delete",
    "observation_get",
    "observation_get_by_file",
    "observation_get_by_session",
    "observation_get_by_symbol",
    "observation_hash",
    "observation_list_archived",
    "observation_restore",
    "observation_save",
    "observation_save_ruled_out",
    "observation_save_volatile",
    "observation_search",
    "observation_update",
    "optimize_output_order",
    "os",
    "prompt_save",
    "prompt_search",
    "re",
    "reasoning_inject",
    "reasoning_list",
    "reasoning_save",
    "reasoning_search",
    "record_lattice_feedback",
    "register_chunks",
    "relative_age",
    "relink_all",
    "run_consistency_check",
    "run_decay",
    "run_health_check",
    "run_mdl_distillation",
    "run_migrations",
    "run_promotions",
    "run_roi_gc",
    "semantic_dedup_check",
    "session_end",
    "session_start",
    "set_mode",
    "set_project_mode",
    "set_session_override",
    "sqlite3",
    "strip_private",
    "summary_parse",
    "summary_save",
    "sys",
    "thompson_sample_level",
    "time",
    "update_consistency_score",
]


def test_memory_db_public_surface_snapshot():
    actual = sorted(name for name in dir(memory_db) if not name.startswith("__"))
    missing = sorted(set(EXPECTED_PUBLIC) - set(actual))
    extra = sorted(set(actual) - set(EXPECTED_PUBLIC))
    assert not missing, f"Public names removed from memory_db: {missing}"
    assert not extra, f"New public names in memory_db (update snapshot intentionally): {extra}"


def test_monkey_patch_surface_intact():
    """The names tests/handlers patch must remain attributes of memory_db."""
    assert hasattr(memory_db, "MEMORY_DB_PATH")
    assert hasattr(memory_db, "get_db")
    assert hasattr(memory_db, "db_session")
    assert hasattr(memory_db, "run_migrations")
