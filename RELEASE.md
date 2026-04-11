# Token Savior v1.0.0 -- Release Notes

**25 files changed, 217 symbols affected. 865 tests passing.**

## Architecture overhaul

The server has been restructured from a monolithic 2,400-line `server.py` into focused modules:

- **`tool_schemas.py`** -- all 53 MCP tool schemas extracted (server.py reduced to 1,002 lines)
- **`cache_ops.py`** -- `CacheManager` class for persistent JSON cache (save, load, legacy migration)
- **`slot_manager.py`** -- `SlotManager` + `_ProjectSlot` for multi-project lifecycle
- **`brace_matcher.py`** -- shared `find_brace_end_*` for C, C#, Rust, Go annotators
- **`query_api.py`** -- `ProjectQueryEngine` class (22 methods + `as_dict()`) replaces 705-line closure

## Performance

- **LazyLines**: file content lazy-loaded from disk on demand instead of stored in cache. Cache size reduced ~57%, idle RAM reduced proportionally.
- **Manual serialization**: `CacheManager.index_to_dict()` does zero-copy field-by-field serialization instead of `dataclasses.asdict()`.
- **scandir batching**: `_check_mtime_changes` uses `os.scandir()` per directory.
- **Regex cache**: module-level `_WORD_BOUNDARY_CACHE` avoids recompiling patterns.
- **File limits**: `ProjectIndexer` gains `max_files` param (env: `TOKEN_SAVIOR_MAX_FILES`, default 10,000).

## Bug fixes

- **Path traversal**: `create_checkpoint` validates paths with `os.path.commonpath`.
- **Triple save**: `_dirty` flag pattern ensures `_save_cache` called at most once per execution path.
- **Output truncation**: `get_dependents` and `get_change_impact` gained `max_total_chars` (default 50,000).

## Tool fusions

- **`get_changed_symbols`**: now accepts optional `ref` parameter (replaces `get_changed_symbols_since_ref`)
- **`apply_symbol_change_and_validate`**: now accepts `rollback_on_failure` parameter (replaces `apply_symbol_change_validate_with_rollback`)

## Deprecated (removal in v1.1.0)

| Deprecated tool | Use instead |
|----------------|-------------|
| `get_changed_symbols_since_ref` | `get_changed_symbols(ref=...)` |
| `apply_symbol_change_validate_with_rollback` | `apply_symbol_change_and_validate(rollback_on_failure=true)` |

Both inject a `_deprecated` field in their response with migration instructions. Schemas marked `"deprecated": true`.

## Annotator refactoring

- **`annotate_rust`**: 6 extracted handlers (`_handle_rust_impl`, `_handle_rust_macro`, `_handle_rust_struct`, `_handle_rust_enum`, `_handle_rust_trait`, `_handle_rust_fn`). Complexity dropped from 211 to under 150.
- **`annotate_csharp`**: 8 extracted handlers (`_handle_csharp_namespace`, `_handle_csharp_type`, `_extract_type_methods`, etc.). Complexity dropped from 201 to under 150.
- **`AnnotatorProtocol`**: `typing.Protocol` + `runtime_checkable` for annotator type safety.

## New modules

| Module | Purpose |
|--------|---------|
| `src/token_savior/tool_schemas.py` | 53 tool schemas + `DEPRECATED_TOOLS` set |
| `src/token_savior/cache_ops.py` | `CacheManager` (save/load/migrate) |
| `src/token_savior/slot_manager.py` | `SlotManager` + `_ProjectSlot` |
| `src/token_savior/brace_matcher.py` | Per-language brace matching |

## Test coverage

| Suite | Tests |
|-------|-------|
| `test_cache_ops.py` | 12 |
| `test_slot_manager.py` | 13 |
| `test_server_integration.py` | 5 |
| `test_annotator_protocol.py` | 4 |
| `test_tool_schemas.py` | 9 |
| Total project | **865** |

## Benchmarks

- `benchmarks/run_benchmarks.py`: automated benchmarks on FastAPI + CPython measuring index time, RAM, query response time, and cache size.
- `.github/workflows/benchmark.yml`: GitHub Action for release benchmarks.
