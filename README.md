<!-- mcp-name: io.github.Mibayy/token-savior-recall -->

<div align="center">

# ⚡ Token Savior Recall

> **97% token reduction** on code navigation · **Persistent memory** across sessions · **69 MCP tools** for Claude Code

[![Version](https://img.shields.io/badge/version-2.0.0-blue)](https://github.com/Mibayy/token-savior/releases/tag/v2.0.0)
[![Tools](https://img.shields.io/badge/tools-69-green)]()
[![Savings](https://img.shields.io/badge/token%20savings-97%25-cyan)]()
[![Memory](https://img.shields.io/badge/memory-SQLite%20WAL%20%2B%20FTS5-orange)]()
[![CI](https://github.com/Mibayy/token-savior/actions/workflows/ci.yml/badge.svg)](https://github.com/Mibayy/token-savior/actions/workflows/ci.yml)
[![Python 3.11+](https://img.shields.io/badge/python-3.11+-blue.svg)](https://www.python.org/downloads/)
[![MCP](https://img.shields.io/badge/MCP-compatible-purple.svg)](https://modelcontextprotocol.io)

</div>

---

## What is Token Savior Recall?

Token Savior Recall is a Claude Code MCP server that does two things:

1. **Saves tokens.** Instead of reading entire files, it navigates your codebase by symbols and returns only what the agent needs. 97% reduction measured across 170 real sessions.
2. **Remembers everything.** A persistent memory engine captures observations across sessions, injects relevant context at startup, and surfaces the right knowledge at the right time.

```
find_symbol("send_message")           →  67 chars    (was: 41M chars of source)
get_change_impact("LLMClient")        →  16K chars   (154 direct + 492 transitive deps)
get_function_source("compile")        →  4.5K chars  (exact source, no grep, no cat)
memory_search("auth migration")       →  ranked past decisions, bugs, conventions
analyze_config()                      →  duplicates, secrets, orphan keys
```

---

## Why it exists

Every AI coding session starts the same way: the agent grabs `cat` or `grep`, reads a dozen files to find one function, then bloats its context trying to understand what else might break. By the end, half your token budget is gone before the first edit — and the next session forgets everything you just figured out.

Token Savior Recall replaces that pattern entirely:

- A **structural index** answers "where is X", "what calls X", and "what breaks if I change X" in sub-millisecond time, with responses sized to the answer, not the codebase.
- A **persistent memory engine** captures bugfixes, decisions, conventions and warnings, then re-injects only the relevant delta at the start of the next session.

---

## Token savings

| Metric | Value |
|--------|-------|
| Token reduction | **97%** |
| Sessions tracked | **170** |
| Tokens saved | **~203M** |
| Estimated cost saved | **~$609** |
| Projects supported | **17** |
| Tool count | **69** |

> "Tokens saved" = estimated tokens the agent would have consumed navigating with `cat`/`grep` versus with Token Savior Recall. Model-agnostic: the index reduces context-window pressure regardless of provider. Updated at each release via automated benchmarks.

### Query response time (sub-millisecond at 1.1M lines)

| Query | FastAPI | Django | CPython |
|-------|--------:|-------:|--------:|
| `find_symbol` | 0.01ms | 0.03ms | 0.08ms |
| `get_dependencies` | 0.00ms | 0.00ms | 0.01ms |
| `get_change_impact` | 0.00ms | 2.81ms | 0.45ms |
| `get_function_source` | 0.02ms | 0.03ms | 0.10ms |

### Index build performance

| Project | Files | Lines | Index time | Memory | Cache size |
|---------|------:|------:|-----------:|-------:|-----------:|
| FastAPI | 2,556 | 332,160 | 5.7s | 55 MB | 6 MB |
| Django | 3,714 | 707,493 | 36.2s | 126 MB | 14 MB |
| **CPython** | **2,464** | **1,115,334** | **55.9s** | **197 MB** | **22 MB** |

Cache is persistent — restarts skip the full build. CPython goes from 56s to under 1s on a cache hit.

---

## Memory Engine

A structural index answers "what's in the code". The memory engine answers "what did we learn about it".

| Feature | Details |
|---------|---------|
| Storage | SQLite WAL + FTS5 |
| Observation types | 12 — `bugfix`, `decision`, `convention`, `warning`, `guardrail`, `error_pattern`, `note`, `command`, `research`, `infra`, `config`, `idea` |
| Hooks | 8 Claude Code lifecycle hooks (SessionStart, Stop, SessionEnd, PreCompact, PreToolUse ×2, UserPromptSubmit, PostToolUse) |
| Ranking | LRU score — `0.4 × recency + 0.3 × access + 0.3 × type_priority` |
| Dedup | Exact hash + Jaccard semantic (~0.85 threshold) |
| Delta injection | Only the diff since last session is re-injected at startup |
| TTL | Per-type expiry (command 60d, research 90d, note 60d, etc.) |
| Auto-promotion | `note × 5` accesses → `convention`; `warning × 5` → `guardrail` |
| Contradiction check | Flags observations that contradict existing ones at save time |
| Auto-linking | Links related observations by symbol, context, tags |
| Modes | `code`, `review`, `debug`, `infra`, `silent` — auto-detected |
| Corpus | Thematic Q&A over all observations |
| Export | Versioned markdown export (git-tracked) |
| CLI | `ts memory {status,list,search,get,save,delete,top,why,doctor,relink}` |
| Dashboard | Memory tab with type breakdown + session timeline |

---

## Installation

### Quick start (uvx)

```bash
uvx token-savior-recall
```

No venv, no clone. Runs directly from PyPI.

### Development install

```bash
git clone https://github.com/Mibayy/token-savior
cd token-savior
python3 -m venv .venv
.venv/bin/pip install -e ".[mcp]"
```

---

## Configuration

### Claude Code / Cursor / Windsurf / Cline

Add to `.mcp.json` (or `~/.claude/settings.json`):

```json
{
  "mcpServers": {
    "token-savior-recall": {
      "command": "uvx",
      "args": ["token-savior-recall"],
      "env": {
        "WORKSPACE_ROOTS": "/path/to/project1,/path/to/project2",
        "TOKEN_SAVIOR_CLIENT": "claude-code",
        "TELEGRAM_BOT_TOKEN": "YOUR_TELEGRAM_BOT_TOKEN",
        "TELEGRAM_CHAT_ID": "YOUR_TELEGRAM_CHAT_ID"
      }
    }
  }
}
```

`TELEGRAM_BOT_TOKEN` / `TELEGRAM_CHAT_ID` are optional — they enable the critical-observation feed to Telegram (guardrails, warnings, error patterns).

### Custom MCP client (YAML example)

```yaml
mcp_servers:
  token-savior-recall:
    command: /path/to/venv/bin/token-savior-recall
    env:
      WORKSPACE_ROOTS: /path/to/project1,/path/to/project2
      TOKEN_SAVIOR_CLIENT: my-client
    timeout: 120
    connect_timeout: 30
```

### Make the agent actually use it

AI assistants default to `grep` and `cat` even when better tools are available. Add this to your `CLAUDE.md` or equivalent:

```
## Codebase Navigation — MANDATORY

You MUST use token-savior-recall MCP tools FIRST.

- ALWAYS start with: find_symbol, get_function_source, get_class_source,
  search_codebase, get_dependencies, get_dependents, get_change_impact
- For past context: memory_search, memory_get, memory_why
- Only fall back to Read/Grep when tools genuinely don't cover it
- If you catch yourself reaching for grep to find code, STOP
```

---

## Tools (69)

### Core navigation (11)
`find_symbol` · `get_function_source` · `get_class_source` · `get_functions` · `get_classes` · `get_imports` · `get_structure_summary` · `list_files` · `get_project_summary` · `search_codebase` · `reindex`

### Context & discovery (5)
`get_edit_context` · `get_feature_files` · `get_routes` · `get_components` · `get_env_usage`

### Impact analysis (8)
`get_dependencies` · `get_dependents` · `get_change_impact` · `get_call_chain` · `get_file_dependencies` · `get_file_dependents` · `get_symbol_cluster` · `get_entry_points`

### Git & diffs (4)
`get_git_status` · `get_changed_symbols` · `summarize_patch_by_symbol` · `build_commit_summary`

### Safe editing & checkpoints (6)
`replace_symbol_source` · `insert_near_symbol` · `create_checkpoint` · `restore_checkpoint` · `compare_checkpoint_by_symbol` · `list_checkpoints` + `delete_checkpoint` / `prune_checkpoints`

### Test & run (6)
`find_impacted_test_files` · `run_impacted_tests` · `apply_symbol_change_and_validate` · `discover_project_actions` · `run_project_action` · `get_usage_stats`

### Quality & analysis (5)
`find_dead_code` · `find_hotspots` · `detect_breaking_changes` · `analyze_config` · `analyze_docker`

### Multi-project (4)
`list_projects` · `switch_project` · `set_project_root` · `find_cross_project_deps`

### Memory Engine (16)
`memory_save` · `memory_search` · `memory_get` · `memory_delete` · `memory_index` · `memory_status` · `memory_timeline` · `memory_top` · `memory_why` · `memory_doctor` · `memory_from_bash` · `memory_mode` · `memory_archive` · `memory_maintain` · `memory_set_global` · `memory_prompts`

---

## Supported languages & formats

| Language / Format | Files | Extracts |
|-------------------|-------|----------|
| Python | `.py`, `.pyw` | Functions, classes, methods, imports, dependency graph |
| TypeScript / JS | `.ts`, `.tsx`, `.js`, `.jsx` | Functions, arrow functions, classes, interfaces, type aliases |
| Go | `.go` | Functions, methods, structs, interfaces, type aliases |
| Rust | `.rs` | Functions, structs, enums, traits, impl blocks, macro_rules |
| C# | `.cs` | Classes, interfaces, structs, enums, methods, XML doc comments |
| C / C++ | `.c`, `.cc`, `.cpp`, `.h`, `.hpp` | Functions, structs/unions/enums, typedefs, macros, includes |
| GLSL | `.glsl`, `.vert`, `.frag`, `.comp` | Functions, structs, uniforms |
| JSON / YAML / TOML | config files | Nested keys, `$ref` cross-refs |
| INI / ENV / HCL / Terraform | config files | Sections, key-value pairs, secret masking |
| XML / Plist / SVG | markup files | Element hierarchy, attributes |
| Dockerfile | `Dockerfile`, `*.dockerfile` | Instructions, multi-stage builds, FROM/RUN/COPY/ENV |
| Markdown / Text | `.md`, `.txt`, `.rst` | Sections via heading detection |
| Everything else | `*` | Line counts (generic fallback) |

---

## vs LSP

LSP answers "where is this defined?" — Token Savior Recall answers "what breaks if I change it, what did we learn last time, and what should we do about it?"

LSP is point queries: one symbol, one file, one position. It can find where `LLMClient` is defined. Ask "what breaks transitively if I refactor `LLMClient`, and did we already hit this bug six weeks ago?" and LSP has nothing.

`get_change_impact("TestCase")` on CPython finds 154 direct and 492 transitive dependents in 0.45ms, returning 16K chars instead of reading 41M. Pair it with `memory_search("TestCase refactor")` and you get prior decisions, past bugs, and conventions in the same round-trip — with zero language servers required.

---

## Programmatic usage

```python
from token_savior.project_indexer import ProjectIndexer
from token_savior.query_api import ProjectQueryEngine

indexer = ProjectIndexer("/path/to/project")
index = indexer.index()
engine = ProjectQueryEngine(index)

print(engine.get_project_summary())
print(engine.find_symbol("MyClass"))
print(engine.get_change_impact("send_message"))
```

---

## Architecture

```
src/token_savior/
  server.py            MCP transport, tool routing
  tool_schemas.py      69 tool schemas
  slot_manager.py      Multi-project lifecycle, incremental mtime updates
  cache_ops.py         JSON persistence, legacy cache migration
  query_api.py         ProjectQueryEngine — query methods + as_dict()
  models.py            ProjectIndex, LazyLines, AnnotatorProtocol
  project_indexer.py   File discovery, structural indexing, dependency graphs
  memory_db.py         SQLite WAL + FTS5 memory engine
  annotator.py         Language dispatch
  *_annotator.py       Per-language annotators
```

---

## Development

```bash
pip install -e ".[dev,mcp]"
pytest tests/ -v
ruff check src/ tests/
```

---

## Known limitations

- **Live-editing window:** the index updates on query, not on save. Right after an edit you may briefly see the pre-edit version; the next git-tracked change triggers re-indexing.
- **Cross-language tracing:** `get_change_impact` stops at language boundaries.
- **JSON value semantics:** the JSON annotator indexes key structure, not value meaning.
- **Windows paths:** not tested. Contributions welcome.
- **Max files:** default 10,000 per project (`TOKEN_SAVIOR_MAX_FILES`).
- **Max file size:** default 1 MB (`TOKEN_SAVIOR_MAX_FILE_SIZE_MB`).

---

## License

MIT — see [LICENSE](LICENSE).

---

<div align="center">

**Works with any MCP-compatible AI coding tool.**
Claude Code · Cursor · Windsurf · Cline · Continue · any custom MCP client

</div>
