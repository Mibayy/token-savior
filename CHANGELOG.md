# Changelog

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
