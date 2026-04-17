-- Token Savior Memory Engine — SQLite schema
-- FTS5 full-text search, WAL mode, foreign keys ON

CREATE TABLE IF NOT EXISTS sessions (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    project_root    TEXT NOT NULL,
    status          TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'completed', 'failed')),
    summary         TEXT,
    symbols_changed TEXT,          -- JSON array
    files_changed   TEXT,          -- JSON array
    events_count    INTEGER NOT NULL DEFAULT 0,
    created_at      TEXT NOT NULL,
    created_at_epoch INTEGER NOT NULL,
    completed_at    TEXT,
    completed_at_epoch INTEGER
);

CREATE INDEX IF NOT EXISTS idx_sessions_project ON sessions(project_root);
CREATE INDEX IF NOT EXISTS idx_sessions_status ON sessions(status);
CREATE INDEX IF NOT EXISTS idx_sessions_epoch ON sessions(created_at_epoch DESC);

CREATE TABLE IF NOT EXISTS observations (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id      INTEGER REFERENCES sessions(id) ON DELETE SET NULL,
    project_root    TEXT NOT NULL,
    type            TEXT NOT NULL,  -- user/feedback/project/reference/guardrail/error_pattern/decision/convention
    title           TEXT NOT NULL,
    content         TEXT NOT NULL,
    why             TEXT,
    how_to_apply    TEXT,
    symbol          TEXT,           -- linked symbol name (Token Savior symbol_table key)
    file_path       TEXT,           -- linked file (relative path)
    tags            TEXT,           -- JSON array
    private         INTEGER NOT NULL DEFAULT 0,
    importance      INTEGER NOT NULL DEFAULT 5 CHECK (importance BETWEEN 1 AND 10),
    relevance_score REAL NOT NULL DEFAULT 1.0,
    access_count    INTEGER NOT NULL DEFAULT 0,
    content_hash    TEXT NOT NULL,  -- SHA-256 first 16 hex chars for dedup
    last_accessed_at TEXT,
    created_at      TEXT NOT NULL,
    created_at_epoch INTEGER NOT NULL,
    updated_at      TEXT NOT NULL,
    archived        INTEGER NOT NULL DEFAULT 0,
    agent_id        TEXT,  -- Step C: subagent identifier for the inter-agent memory bus
    narrative       TEXT,           -- A5: free-form narrative explaining the obs
    facts           TEXT,           -- A5: JSON array or bullet list of atomic facts
    concepts        TEXT            -- A5: JSON array or comma list of conceptual tags
);

CREATE INDEX IF NOT EXISTS idx_obs_project ON observations(project_root);
CREATE INDEX IF NOT EXISTS idx_obs_type ON observations(type);
CREATE INDEX IF NOT EXISTS idx_obs_symbol ON observations(symbol);
CREATE INDEX IF NOT EXISTS idx_obs_file ON observations(file_path);
CREATE INDEX IF NOT EXISTS idx_obs_hash ON observations(content_hash, project_root);
CREATE INDEX IF NOT EXISTS idx_obs_epoch ON observations(created_at_epoch DESC);
CREATE INDEX IF NOT EXISTS idx_obs_archived ON observations(archived);

-- Step C: inter-agent memory bus (volatile observations carry agent_id).
-- Note: column is added via ALTER TABLE migration in get_db() for legacy DBs;
-- this index is created here so fresh installs get it consistently.
CREATE INDEX IF NOT EXISTS idx_obs_agent ON observations(agent_id) WHERE agent_id IS NOT NULL;

CREATE VIRTUAL TABLE IF NOT EXISTS observations_fts USING fts5(
    title,
    content,
    why,
    how_to_apply,
    tags,
    narrative,
    facts,
    concepts,
    content='observations',
    content_rowid='id'
);

-- FTS sync triggers
CREATE TRIGGER IF NOT EXISTS obs_fts_insert AFTER INSERT ON observations BEGIN
    INSERT INTO observations_fts(rowid, title, content, why, how_to_apply, tags, narrative, facts, concepts)
    VALUES (new.id, new.title, new.content, new.why, new.how_to_apply, new.tags, new.narrative, new.facts, new.concepts);
END;

CREATE TRIGGER IF NOT EXISTS obs_fts_delete AFTER DELETE ON observations BEGIN
    INSERT INTO observations_fts(observations_fts, rowid, title, content, why, how_to_apply, tags, narrative, facts, concepts)
    VALUES ('delete', old.id, old.title, old.content, old.why, old.how_to_apply, old.tags, old.narrative, old.facts, old.concepts);
END;

CREATE TRIGGER IF NOT EXISTS obs_fts_update AFTER UPDATE ON observations BEGIN
    INSERT INTO observations_fts(observations_fts, rowid, title, content, why, how_to_apply, tags, narrative, facts, concepts)
    VALUES ('delete', old.id, old.title, old.content, old.why, old.how_to_apply, old.tags, old.narrative, old.facts, old.concepts);
    INSERT INTO observations_fts(rowid, title, content, why, how_to_apply, tags, narrative, facts, concepts)
    VALUES (new.id, new.title, new.content, new.why, new.how_to_apply, new.tags, new.narrative, new.facts, new.concepts);
END;

-- P5: structured session-end rollups (request/investigated/learned/...).
-- FTS5 mirror so memory_search can surface past sessions in a dedicated
-- section. Summaries live in their own table so observations decay/roi
-- rules don't touch them.
CREATE TABLE IF NOT EXISTS session_summaries (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id       INTEGER REFERENCES sessions(id) ON DELETE CASCADE,
    project_root     TEXT NOT NULL,
    request          TEXT,
    investigated     TEXT,
    learned          TEXT,
    completed        TEXT,
    next_steps       TEXT,
    notes            TEXT,
    created_at       TEXT NOT NULL,
    created_at_epoch INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_sess_sum_project ON session_summaries(project_root);
CREATE INDEX IF NOT EXISTS idx_sess_sum_epoch ON session_summaries(created_at_epoch DESC);
CREATE INDEX IF NOT EXISTS idx_sess_sum_session ON session_summaries(session_id);

CREATE VIRTUAL TABLE IF NOT EXISTS session_summaries_fts USING fts5(
    request, investigated, learned, completed, next_steps, notes,
    content='session_summaries',
    content_rowid='id'
);

CREATE TRIGGER IF NOT EXISTS sess_sum_fts_insert AFTER INSERT ON session_summaries BEGIN
    INSERT INTO session_summaries_fts(rowid, request, investigated, learned, completed, next_steps, notes)
    VALUES (new.id, new.request, new.investigated, new.learned, new.completed, new.next_steps, new.notes);
END;

CREATE TRIGGER IF NOT EXISTS sess_sum_fts_delete AFTER DELETE ON session_summaries BEGIN
    INSERT INTO session_summaries_fts(session_summaries_fts, rowid, request, investigated, learned, completed, next_steps, notes)
    VALUES ('delete', old.id, old.request, old.investigated, old.learned, old.completed, old.next_steps, old.notes);
END;

CREATE TRIGGER IF NOT EXISTS sess_sum_fts_update AFTER UPDATE ON session_summaries BEGIN
    INSERT INTO session_summaries_fts(session_summaries_fts, rowid, request, investigated, learned, completed, next_steps, notes)
    VALUES ('delete', old.id, old.request, old.investigated, old.learned, old.completed, old.next_steps, old.notes);
    INSERT INTO session_summaries_fts(rowid, request, investigated, learned, completed, next_steps, notes)
    VALUES (new.id, new.request, new.investigated, new.learned, new.completed, new.next_steps, new.notes);
END;

CREATE TABLE IF NOT EXISTS observation_links (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    source_id   INTEGER NOT NULL REFERENCES observations(id) ON DELETE CASCADE,
    target_id   INTEGER NOT NULL REFERENCES observations(id) ON DELETE CASCADE,
    link_type   TEXT NOT NULL CHECK (link_type IN ('related', 'contradicts', 'supersedes', 'consolidation')),
    auto_detected INTEGER NOT NULL DEFAULT 0,
    created_at  TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_links_source ON observation_links(source_id);
CREATE INDEX IF NOT EXISTS idx_links_target ON observation_links(target_id);
CREATE UNIQUE INDEX IF NOT EXISTS idx_links_unique
    ON observation_links(source_id, target_id, link_type);

-- LRU cache for get_recent_index / memory injection ----------------------
CREATE TABLE IF NOT EXISTS memory_cache (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    cache_key        TEXT UNIQUE NOT NULL,
    obs_ids_ordered  TEXT NOT NULL,
    scores           TEXT NOT NULL,
    created_at_epoch INTEGER NOT NULL
);

-- Reasoning Trace Compression (v2.2 Step A) --------------------------------
CREATE TABLE IF NOT EXISTS reasoning_chains (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    project_root      TEXT NOT NULL,
    goal              TEXT NOT NULL,
    goal_hash         TEXT NOT NULL,
    steps             TEXT NOT NULL,           -- JSON array of {tool,args,observation}
    conclusion        TEXT NOT NULL,
    confidence        REAL NOT NULL DEFAULT 0.8,
    evidence_hash     TEXT,
    access_count      INTEGER NOT NULL DEFAULT 0,
    created_at        TEXT NOT NULL,
    created_at_epoch  INTEGER NOT NULL,
    expires_at_epoch  INTEGER
);

CREATE INDEX IF NOT EXISTS idx_rc_project ON reasoning_chains(project_root);
CREATE INDEX IF NOT EXISTS idx_rc_hash ON reasoning_chains(goal_hash);

CREATE VIRTUAL TABLE IF NOT EXISTS reasoning_chains_fts USING fts5(
    goal, conclusion,
    content='reasoning_chains', content_rowid='id',
    tokenize='porter unicode61'
);

CREATE TRIGGER IF NOT EXISTS rc_fts_insert AFTER INSERT ON reasoning_chains BEGIN
    INSERT INTO reasoning_chains_fts(rowid, goal, conclusion)
    VALUES (new.id, new.goal, new.conclusion);
END;

CREATE TRIGGER IF NOT EXISTS rc_fts_delete AFTER DELETE ON reasoning_chains BEGIN
    INSERT INTO reasoning_chains_fts(reasoning_chains_fts, rowid, goal, conclusion)
    VALUES ('delete', old.id, old.goal, old.conclusion);
END;

CREATE TRIGGER IF NOT EXISTS rc_fts_update AFTER UPDATE ON reasoning_chains BEGIN
    INSERT INTO reasoning_chains_fts(reasoning_chains_fts, rowid, goal, conclusion)
    VALUES ('delete', old.id, old.goal, old.conclusion);
    INSERT INTO reasoning_chains_fts(rowid, goal, conclusion)
    VALUES (new.id, new.goal, new.conclusion);
END;

CREATE TABLE IF NOT EXISTS summaries (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id          INTEGER REFERENCES sessions(id) ON DELETE SET NULL,
    project_root        TEXT NOT NULL,
    content             TEXT NOT NULL,
    observation_ids     TEXT,           -- JSON array of observation IDs covered
    covers_until_epoch  INTEGER,
    created_at          TEXT NOT NULL,
    created_at_epoch    INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_summaries_project ON summaries(project_root);
CREATE INDEX IF NOT EXISTS idx_summaries_epoch ON summaries(created_at_epoch DESC);

CREATE TABLE IF NOT EXISTS events (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id      INTEGER REFERENCES sessions(id) ON DELETE SET NULL,
    type            TEXT NOT NULL,   -- build_fail/deploy/test_fail/test_pass/error/breaking_change/config_issue/milestone
    severity        TEXT NOT NULL DEFAULT 'info' CHECK (severity IN ('info', 'warning', 'critical')),
    data            TEXT,            -- JSON payload
    symbol          TEXT,
    file_path       TEXT,
    auto_obs_id     INTEGER REFERENCES observations(id) ON DELETE SET NULL,
    created_at      TEXT NOT NULL,
    created_at_epoch INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_events_session ON events(session_id);
CREATE INDEX IF NOT EXISTS idx_events_type ON events(type);
CREATE INDEX IF NOT EXISTS idx_events_epoch ON events(created_at_epoch DESC);

CREATE TABLE IF NOT EXISTS user_prompts (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id          INTEGER REFERENCES sessions(id) ON DELETE SET NULL,
    project_root        TEXT,
    prompt_text         TEXT NOT NULL,
    prompt_number       INTEGER,
    created_at          TEXT NOT NULL,
    created_at_epoch    INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_prompts_session ON user_prompts(session_id);
CREATE INDEX IF NOT EXISTS idx_prompts_project ON user_prompts(project_root);
CREATE INDEX IF NOT EXISTS idx_prompts_epoch ON user_prompts(created_at_epoch DESC);

CREATE VIRTUAL TABLE IF NOT EXISTS user_prompts_fts USING fts5(
    prompt_text,
    content='user_prompts',
    content_rowid='id',
    tokenize='porter unicode61'
);

CREATE TRIGGER IF NOT EXISTS prompts_fts_insert AFTER INSERT ON user_prompts BEGIN
    INSERT INTO user_prompts_fts(rowid, prompt_text) VALUES (new.id, new.prompt_text);
END;

CREATE TRIGGER IF NOT EXISTS prompts_fts_delete AFTER DELETE ON user_prompts BEGIN
    INSERT INTO user_prompts_fts(user_prompts_fts, rowid, prompt_text)
    VALUES ('delete', old.id, old.prompt_text);
END;

CREATE TRIGGER IF NOT EXISTS prompts_fts_update AFTER UPDATE ON user_prompts BEGIN
    INSERT INTO user_prompts_fts(user_prompts_fts, rowid, prompt_text)
    VALUES ('delete', old.id, old.prompt_text);
    INSERT INTO user_prompts_fts(rowid, prompt_text) VALUES (new.id, new.prompt_text);
END;

CREATE TABLE IF NOT EXISTS decay_config (
    type        TEXT PRIMARY KEY,
    decay_rate  REAL NOT NULL DEFAULT 1.0,
    min_score   REAL NOT NULL DEFAULT 0.1,
    boost_on_access REAL NOT NULL DEFAULT 0.1
);

CREATE TABLE IF NOT EXISTS corpora (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    project_root     TEXT NOT NULL,
    name             TEXT NOT NULL,
    filter_type      TEXT,
    filter_tags      TEXT,           -- JSON array
    filter_symbol    TEXT,
    observation_ids  TEXT NOT NULL,  -- JSON array
    created_at       TEXT NOT NULL,
    created_at_epoch INTEGER NOT NULL,
    UNIQUE (project_root, name)
);

CREATE INDEX IF NOT EXISTS idx_corpora_project ON corpora(project_root);

-- DCP — Differential Context Protocol chunk registry (v2.2 Prompt 3 Step A)
CREATE TABLE IF NOT EXISTS dcp_chunk_registry (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    fingerprint      TEXT UNIQUE NOT NULL,
    content_preview  TEXT NOT NULL,
    seen_count       INTEGER NOT NULL DEFAULT 1,
    last_seen_epoch  INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_dcp_last_seen ON dcp_chunk_registry(last_seen_epoch DESC);

-- Default decay rates per observation type
INSERT OR IGNORE INTO decay_config VALUES ('guardrail',     1.0,   1.0, 0.0);
INSERT OR IGNORE INTO decay_config VALUES ('user',          1.0,   0.8, 0.0);
INSERT OR IGNORE INTO decay_config VALUES ('convention',    1.0,   0.8, 0.0);
INSERT OR IGNORE INTO decay_config VALUES ('feedback',      0.999, 0.5, 0.1);
INSERT OR IGNORE INTO decay_config VALUES ('decision',      0.998, 0.3, 0.1);
INSERT OR IGNORE INTO decay_config VALUES ('error_pattern', 0.997, 0.2, 0.15);
INSERT OR IGNORE INTO decay_config VALUES ('reference',     0.995, 0.2, 0.1);
INSERT OR IGNORE INTO decay_config VALUES ('project',       0.99,  0.1, 0.2);
