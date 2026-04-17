# Changelog

## v2.6.0 — Memory Engine Phase 1+2 (2026-04-17)

### Phase 1 — Gap closure
- P1: `<private>` tag stripper (UserPromptSubmit hook)
- P2: content_hash persisté, dedup O(1) + backfill
- P3: `ts://obs/{id}` citation URIs dans injection output
- P4: PreToolUse-Read hook — file-context injection
- P5: session-end rollup structuré (FTS5, 6 champs)

### Phase 2 — Feature parity + differentiation
- A4: Progressive disclosure formalisé (Layer 1/2/3, cost table)
- A5: narrative / facts / concepts fields sur observations
- A1: sqlite-vec hybrid search + RRF fusion (FTS fallback graceful)
- A2: Web viewer opt-in `127.0.0.1:$TS_VIEWER_PORT` (htmx + SSE)
- A3: LLM auto-extraction PostToolUse (opt-in `TS_AUTO_EXTRACT=1`)

### Stats
- Tools : 105
- Tests : 1318/1318
- Vector search : `sqlite-vec` + `sentence-transformers/all-MiniLM-L6-v2`

## v2.0.0 — Token Savior Recall (2026-04-13)

### Memory Engine (new)

- SQLite WAL + FTS5: cross-session persistent memory
- 21 memory tools: save, search, get, delete, index, timeline, status, why, top
- 8 Claude Code lifecycle hooks: SessionStart, Stop, SessionEnd, PreCompact,
  PreToolUse ×2, UserPromptSubmit, PostToolUse
- LRU scoring: `0.4 × recency + 0.3 × access + 0.3 × type_priority`
- Delta injection: only the diff since last session is re-injected at start
- Explicit TTL per observation type (command 60d, research 90d, note 60d)
- Semantic dedup: exact hash + Jaccard (~0.85 threshold)
- Auto-promotion: note × 5 accesses → convention, warning × 5 → guardrail
- Contradiction detection at save time
- Auto-linking between observations (symbol, context, tags)
- Telegram feed for critical observations (warning / guardrail / error_pattern)
- Mode system: `code`, `review`, `debug`, `infra`, `silent` with auto-detection
- Thematic corpus Q&A
- Versioned markdown export (git-tracked)
- CLI: `ts memory {status,list,search,get,save,delete,top,why,doctor,relink}`
- Dashboard Memory tab
- 12 observation types: `bugfix`, `decision`, `convention`, `warning`,
  `guardrail`, `error_pattern`, `note`, `command`, `research`, `infra`,
  `config`, `idea`

### Manifest optimizations

- 80 → 69 tools (−11)
- 42,251 → 36,153 chars manifest (−14%)
- ~1,524 tokens saved per session on MCP manifest alone

### Cleanup

- Removed DEPRECATED tools (`apply_symbol_change_validate_with_rollback`,
  `get_changed_symbols_since_ref`)
- Fused 10 memory tools → 5 (`memory_mode`, `memory_archive`,
  `memory_maintain`, `memory_set_global`, `memory_prompts`)

### Core Token Savior (unchanged)

- 69 MCP tools total (53 core + 16 memory)
- 97% token savings measured across 170 real sessions
- ~$609 estimated cost saved
- 17 indexed projects
- Annotators: Python, TypeScript/JS, Rust, Go, C/C++, C#, JSON, YAML,
  TOML, XML, INI, ENV, HCL, Dockerfile, Markdown

### Rename

- Project renamed: **Token Savior → Token Savior Recall**
- MCP server identifier: `token-savior` → `token-savior-recall`
- PyPI package: `token-savior` → `token-savior-recall`

---

## v1.0.0 (2026-04-11)

### Architecture

- **ProjectQueryEngine**: Refactored 705-line closure `create_project_query_functions` into a class with one method per query tool. `as_dict()` preserves backward compatibility.
- **CacheManager**: Extracted cache persistence logic from `server.py` into `src/token_savior/cache_ops.py`.
- **SlotManager**: Extracted project slot management from `server.py` into `src/token_savior/slot_manager.py`.
- **Tool schemas**: Extracted all 53 MCP tool schemas from `server.py` into `src/token_savior/tool_schemas.py`. Server reduced from 2,439 to 990 lines.
- **Brace matcher**: Factored `_find_brace_end` from 4 annotators into `src/token_savior/brace_matcher.py` with per-language variants.
- **Annotator refactoring**: Table-driven dispatch in `annotate_rust` and `annotate_csharp` to reduce complexity below 150.
- **AnnotatorProtocol**: Added `typing.Protocol` for annotator type safety in `models.py`.

### Performance

- **LazyLines**: File lines are lazy-loaded from disk on demand instead of stored in cache. Cache size reduced by ~57%, idle RAM reduced proportionally.
- **Manual serialization**: Replaced `dataclasses.asdict()` in cache persistence with zero-copy field-by-field serialization.
- **scandir batching**: `_check_mtime_changes` uses `os.scandir()` per directory instead of individual `os.path.getmtime()` calls.
- **Regex cache**: Module-level `_WORD_BOUNDARY_CACHE` avoids recompiling patterns on every call.
- **File limits**: `ProjectIndexer` gains `max_files` param (env: `TOKEN_SAVIOR_MAX_FILES`, default 10,000).

### Bug fixes

- **Path traversal**: `create_checkpoint` validates file paths with `os.path.commonpath` to prevent `../../../etc/passwd` attacks.
- **Triple save**: `_maybe_incremental_update` uses `_dirty` flag pattern to call `_save_cache` at most once per execution path.
- **Output truncation**: `get_dependents` and `get_change_impact` gained `max_total_chars` (default 50,000) to prevent oversized responses.

### Tool fusions

- **get_changed_symbols**: Unified with `get_changed_symbols_since_ref` via optional `ref` parameter.
- **apply_symbol_change_and_validate**: Unified with rollback variant via `rollback_on_failure` parameter.

### Deprecated (removal planned for v1.1.0)

- **get_changed_symbols_since_ref**: Use `get_changed_symbols(ref=...)` instead.
- **apply_symbol_change_validate_with_rollback**: Use `apply_symbol_change_and_validate(rollback_on_failure=true)` instead.

Both deprecated tools inject a `_deprecated` field in their response with migration instructions. Their schemas are marked with `"deprecated": true` in `tool_schemas.py`.

### Tests

- `tests/test_cache_ops.py` (12 tests)
- `tests/test_slot_manager.py` (13 tests)
- `tests/test_server_integration.py` (5 end-to-end tests)
- `tests/test_annotator_protocol.py` (4 tests)
- `tests/test_tool_schemas.py` (7 tests)

### Benchmarks

- `benchmarks/run_benchmarks.py`: Automated benchmarks on FastAPI + CPython measuring index time, RAM, query response time, and cache size.
- `.github/workflows/benchmark.yml`: GitHub Action for release benchmarks.
