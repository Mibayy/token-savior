<!-- mcp-name: io.github.Mibayy/token-savior -->

<div align="center">

# ⚔ token-savior

**Stop feeding your AI entire codebases. Give it a scalpel instead.**

[![CI](https://github.com/Mibayy/token-savior/actions/workflows/ci.yml/badge.svg)](https://github.com/Mibayy/token-savior/actions/workflows/ci.yml)
[![Python 3.11+](https://img.shields.io/badge/python-3.11+-blue.svg)](https://www.python.org/downloads/)
[![MCP](https://img.shields.io/badge/MCP-compatible-purple.svg)](https://modelcontextprotocol.io)
[![Version](https://img.shields.io/badge/version-1.0.0-green.svg)](https://github.com/Mibayy/token-savior/releases/tag/v1.0.0)

</div>

---

### What's new in v1.0.0

- `server.py`: 2,439 -> 1,002 lines (-59%), split into focused modules
- Cache size: -57% via LazyLines (file content lazy-loaded from disk)
- 865 tests (up from 856)
- `ProjectQueryEngine` class replaces 705-line closure
- Automated benchmarks (`benchmarks/run_benchmarks.py`)
- Path traversal fix in `create_checkpoint`
- Output cap on `get_change_impact` / `get_dependents` (default 50,000 chars)
- 2 deprecated tool aliases with migration path (removal in v1.1.0)

---

An MCP server that indexes your codebase structurally and exposes surgical query tools — so your AI agent reads 200 characters instead of 200 files.

```
find_symbol("send_message")           →  67 chars    (was: 41M chars of source)
get_change_impact("LLMClient")        →  16K chars   (154 direct + 492 transitive deps)
get_function_source("compile")        →  4.5K chars  (exact source, no grep, no cat)
analyze_config()                      →  finds duplicates, secrets, orphan keys
```

**Measured across 5,074 queries in 137 real sessions: 97% token reduction.**

---

## Why this exists

Every AI coding session starts the same way: the agent grabs `cat` or `grep`, reads a dozen files to find one function, then bloats its context trying to understand what else might break. By the end, half your token budget is gone before the first edit.

`token-savior` replaces that pattern entirely. It builds a structural index once, keeps it in sync with git automatically, and answers "where is X", "what calls X", and "what breaks if I change X" in sub-millisecond time — with responses sized to the answer, not the codebase.

---

## Numbers

### Token savings across real sessions

| Project | Sessions | Queries | Tokens used | Tokens (naive) | Saving |
|---------|----------|---------|-------------|----------------|--------|
| improvence | 51 | 2,223 | 1,731,335 | 62,676,343 | **97%** |
| estalle | 44 | 1,897 | 1,132,604 | 43,183,352 | **97%** |
| token-savior | 14 | 808 | 1,128,734 | 11,908,629 | **91%** |
| hermes-agent | 3 | 22 | 121,177 | 4,359,176 | **97%** |
| hermes-pc-mcp | 1 | 38 | 15,207 | 684,942 | **98%** |
| claude-code | 24 | 86 | 46,912 | 384,476 | **88%** |
| **TOTAL** | **137** | **5,074** | **4,175,972** | **123,196,921** | **97%** |

> "Tokens (naive)" = estimated tokens the agent would have consumed with `cat`/`grep`. These savings are model-agnostic -- the index reduces context window pressure regardless of provider. Updated at each release via automated benchmarks.

### Query response time (sub-millisecond at 1.1M lines)

| Query | RMLPlus | FastAPI | Django | CPython |
|-------|--------:|--------:|-------:|--------:|
| `find_symbol` | 0.01ms | 0.01ms | 0.03ms | 0.08ms |
| `get_dependencies` | 0.00ms | 0.00ms | 0.00ms | 0.01ms |
| `get_change_impact` | 0.02ms | 0.00ms | 2.81ms | 0.45ms |
| `get_function_source` | 0.01ms | 0.02ms | 0.03ms | 0.10ms |

### Index build performance

| Project | Files | Lines | Index time | Memory | Cache size |
|---------|------:|------:|-----------:|-------:|-----------:|
| Small project | 36 | 7,762 | 0.9s | 2.4 MB | 0.3 MB |
| FastAPI | 2,556 | 332,160 | 5.7s | 55 MB | 6 MB |
| Django | 3,714 | 707,493 | 36.2s | 126 MB | 14 MB |
| **CPython** | **2,464** | **1,115,334** | **55.9s** | **197 MB** | **22 MB** |

Since v1.0.0, file content is **not** stored in the cache (lazy-loaded from disk via `LazyLines`), reducing cache size by ~57%. With the persistent cache, subsequent restarts skip the full build. CPython goes from 56s -> under 1s on cache hit.

---

## What it covers

| Language / Format | Files | Extracts |
|-------------------|-------|----------|
| Python | `.py`, `.pyw` | Functions, classes, methods, imports, dependency graph |
| TypeScript / JS | `.ts`, `.tsx`, `.js`, `.jsx` | Functions, arrow functions, classes, interfaces, type aliases |
| Go | `.go` | Functions, methods (receiver), structs, interfaces, type aliases |
| Rust | `.rs` | Functions, structs, enums, traits, impl blocks, macro_rules |
| C# | `.cs` | Classes, interfaces, structs, enums, methods, XML doc comments |
| C / C99 / C11 | `.c`, `.h` | Functions (static/inline/extern), structs/unions/enums, typedefs, `#define` macros, `#include`, Doxygen comments, dependency graph |
| GLSL | `.glsl`, `.vert`, `.frag`, `.comp` | Functions, structs, uniforms (via C annotator) |
| Markdown / Text | `.md`, `.txt`, `.rst` | Sections via heading detection |
| JSON | `.json` | Nested key structure up to depth 4, `$ref` cross-references |
| YAML | `.yaml`, `.yml` | Nested key hierarchy, array markers, depth cap 4 |
| TOML | `.toml` | Tables, key-value pairs, nested structure |
| INI / Properties | `.ini`, `.cfg`, `.properties` | Sections, key-value pairs |
| Environment | `.env` | Variable names, values (with secret masking) |
| XML / Plist / SVG | `.xml`, `.plist`, `.svg`, `.xhtml` | Element hierarchy, attributes |
| HCL / Terraform | `.hcl`, `.tf` | Blocks, nested resources, key-value pairs |
| Conf | `.conf` | Key-value pairs, block structure |
| Dockerfile | `Dockerfile`, `*.dockerfile` | Instructions, multi-stage builds, FROM/RUN/COPY/ENV |
| Everything else | `*` | Line counts (generic fallback) |

---

## 53 tools

### Navigation
| Tool | What it does |
|------|-------------|
| `find_symbol` | Where a symbol is defined — file, line, type, 20-line preview |
| `get_function_source` | Full source of a function or method |
| `get_class_source` | Full source of a class |
| `get_functions` | All functions in a file or project |
| `get_classes` | All classes with methods and bases |
| `get_imports` | All imports with module, names, line |
| `get_structure_summary` | File or project structure at a glance |
| `list_files` | Indexed files with optional glob filter |
| `get_project_summary` | File count, packages, top classes/functions |
| `search_codebase` | Regex search across all indexed files |
| `reindex` | Force full re-index (rarely needed) |

### Context & discovery
| Tool | What it does |
|------|-------------|
| `get_edit_context` | All-in-one: symbol source + dependencies + callers in one call (saves 3 calls) |
| `get_feature_files` | Find all files related to a feature keyword, then trace imports transitively |
| `get_routes` | Detect API routes and pages (Next.js App Router, Express, pages/api) |
| `get_components` | Detect React components (functions returning JSX) in `.tsx`/`.jsx` files |
| `get_env_usage` | Find all references to an env variable across the codebase |

### Impact analysis
| Tool | What it does |
|------|-------------|
| `get_dependencies` | What a symbol calls/uses |
| `get_dependents` | What calls/uses a symbol |
| `get_change_impact` | Direct + transitive dependents with **confidence score** (1.0 = direct, 0.6/hop) and depth |
| `get_call_chain` | Shortest dependency path between two symbols (BFS) |
| `get_file_dependencies` | Files imported by a given file |
| `get_file_dependents` | Files that import from a given file |
| `get_symbol_cluster` | All functionally related symbols via label propagation community detection — one call instead of chaining dependency queries |
| `get_entry_points` | Score functions by likelihood of being execution entry points (routes ×3, handlers ×1.5, main ×2, zero callers) |

### Git & diffs
| Tool | What it does |
|------|-------------|
| `get_git_status` | Branch, ahead/behind, staged, unstaged, untracked |
| `get_changed_symbols` | Changed files as symbol-level summaries, not diffs. Optional `ref` param for changes since any git ref |
| `get_changed_symbols_since_ref` | **Deprecated** -- use `get_changed_symbols(ref=...)` instead. Removal in v1.1.0 |
| `summarize_patch_by_symbol` | Compact review view — symbols instead of textual diffs |
| `build_commit_summary` | Compact commit summary from changed files |

### Safe editing
| Tool | What it does |
|------|-------------|
| `replace_symbol_source` | Replace a symbol's source without touching the rest of the file |
| `insert_near_symbol` | Insert content before or after a symbol |
| `create_checkpoint` | Snapshot a set of files before editing |
| `restore_checkpoint` | Restore from checkpoint |
| `compare_checkpoint_by_symbol` | Diff checkpoint vs current at symbol level |
| `list_checkpoints` | List available checkpoints |

### Test & run
| Tool | What it does |
|------|-------------|
| `find_impacted_test_files` | Infer likely impacted pytest files from changed symbols |
| `run_impacted_tests` | Run only impacted tests — compact summary, not raw logs |
| `apply_symbol_change_and_validate` | Edit + run impacted tests in one call. Optional `rollback_on_failure` for auto-rollback |
| `apply_symbol_change_validate_with_rollback` | **Deprecated** -- use `apply_symbol_change_and_validate(rollback_on_failure=true)`. Removal in v1.1.0 |
| `discover_project_actions` | Detect test/lint/build/run commands from project files |
| `run_project_action` | Execute a discovered action with bounded output |

### Config analysis
| Tool | What it does |
|------|-------------|
| `analyze_config` | Scan config files for duplicates, secrets, typos, and orphan keys |

Runs three checks (individually toggleable via the `checks` parameter):

- **Duplicates** — Same key defined twice in the same file, plus Levenshtein-based typo detection (e.g. `db_hsot` vs `db_host`)
- **Secrets** — Regex patterns for known secret formats (API keys, tokens, private keys) plus Shannon entropy analysis for high-entropy strings
- **Orphans** — Cross-references config keys against actual code usage. Detects keys your code never reads and env vars your code expects but aren't set. Understands `os.environ`, `process.env`, `os.Getenv`, `std::env::var`, and more.

Supported formats: `.yaml`, `.yml`, `.toml`, `.ini`, `.cfg`, `.properties`, `.env`, `.xml`, `.plist`, `.hcl`, `.tf`, `.conf`, `.json`

### Code quality
| Tool | What it does |
|------|-------------|
| `find_dead_code` | Find functions/classes with zero callers (excludes entry points, tests, decorated routes) |
| `find_hotspots` | Rank functions by complexity score (lines, branches, nesting, parameter count) |
| `detect_breaking_changes` | Compare current function signatures against a git ref — flags removed/renamed params, changed defaults |

### Docker
| Tool | What it does |
|------|-------------|
| `analyze_docker` | Audit Dockerfiles: base images, exposed ports, ENV/ARG cross-reference, `latest` tag warnings |

### Multi-project
| Tool | What it does |
|------|-------------|
| `find_cross_project_deps` | Cross-reference imports across projects to find shared dependencies |

### Stats
| Tool | What it does |
|------|-------------|
| `get_usage_stats` | Cumulative token savings per project across sessions |

### Project management
| Tool | What it does |
|------|-------------|
| `list_projects` | All registered projects and their index state |
| `switch_project` | Set the active project for subsequent calls |
| `set_project_root` | Register a new project root and trigger indexing |

---

## vs LSP

LSP answers "where is this defined?" — `token-savior` answers "what breaks if I change it?"

LSP is point queries: one symbol, one file, one position. It can find where `LLMClient` is defined and who references it directly. Ask "what breaks transitively if I refactor `LLMClient`?" and LSP has nothing — the AI would need to chain dozens of find-reference calls recursively, reading files at every step.

`get_change_impact("TestCase")` on CPython finds 154 direct dependents and 492 transitive dependents in 0.45ms, returning 16K chars instead of reading 41M. And unlike LSP, it requires zero language servers — one binary covers Python + TS/JS + Go + Rust + C# + C/GLSL + config files + Dockerfiles out of the box.

---

## Install

### Quick start (uvx)

```bash
uvx token-savior
```

No venv, no clone. Runs directly from PyPI.

### Advanced (venv for development)

```bash
git clone https://github.com/Mibayy/token-savior
cd token-savior
python3 -m venv ~/.local/token-savior-venv
~/.local/token-savior-venv/bin/pip install -e ".[mcp]"
```

---

## Configure

### Claude Code / Cursor / Windsurf / Cline

Add to `.mcp.json` in your project root:

```json
{
  "mcpServers": {
    "token-savior": {
      "command": "uvx",
      "args": ["token-savior"],
      "env": {
        "WORKSPACE_ROOTS": "/path/to/project1,/path/to/project2",
        "TOKEN_SAVIOR_CLIENT": "claude-code"
      }
    }
  }
}
```

### Hermes Agent

Add to `~/.hermes/config.yaml`:

```yaml
mcp_servers:
  token-savior:
    command: ~/.local/token-savior-venv/bin/token-savior
    env:
      WORKSPACE_ROOTS: /path/to/project1,/path/to/project2
      TOKEN_SAVIOR_CLIENT: hermes
    timeout: 120
    connect_timeout: 30
```

`TOKEN_SAVIOR_CLIENT` is optional but lets the live dashboard attribute savings by client.

---

## Make the agent actually use it

AI assistants default to `grep` and `cat` even when better tools are available. Soft instructions get rationalized away. Add this to your `CLAUDE.md` or equivalent:

```
## Codebase Navigation — MANDATORY

You MUST use token-savior MCP tools FIRST.

- ALWAYS start with: find_symbol, get_function_source, get_class_source,
  search_codebase, get_dependencies, get_dependents, get_change_impact
- Only fall back to Read/Grep when token-savior tools genuinely don't cover it
- If you catch yourself reaching for grep to find code, STOP
```

---

## Multi-project workspaces

One server instance covers every project on the machine:

```bash
WORKSPACE_ROOTS=/root/myapp,/root/mybot,/root/docs token-savior
```

Each root gets its own isolated index, loaded lazily on first use. `list_projects` shows all registered roots. `switch_project` sets the active one.

---

## How it stays in sync

The server detects changes via two mechanisms:

1. **mtime scanning** -- `os.scandir()` checks directory modification times before every query (~1-2ms). Changed files are re-parsed incrementally.
2. **git tracking** -- `git diff` and `git status` for symbol-level change detection.

No manual `reindex` after edits, branch switches, or pulls.

The index is saved to `.token-savior-cache.json` after every build -- human-readable JSON, inspectable when things go wrong, safe across Python versions. File content is **not** stored in the cache (lazy-loaded from disk on demand), reducing cache size by ~57%.

---

## Programmatic usage

```python
from token_savior.project_indexer import ProjectIndexer
from token_savior.query_api import ProjectQueryEngine

indexer = ProjectIndexer("/path/to/project")
index = indexer.index()
engine: ProjectQueryEngine = ProjectQueryEngine(index)

print(engine.get_project_summary())
print(engine.find_symbol("MyClass"))
print(engine.get_change_impact("send_message"))

# Dict-based access (backward compatible)
query = engine.as_dict()
print(query["find_symbol"]("MyClass"))
```

---

## Architecture (v1.0.0)

```
src/token_savior/
  server.py            MCP transport, tool routing, usage stats (~1,000 lines)
  tool_schemas.py       53 tool definitions, DEPRECATED_TOOLS set (extracted v1.0.0)
  slot_manager.py       Multi-project lifecycle, incremental mtime updates
  cache_ops.py          JSON persistence, legacy cache migration
  query_api.py          ProjectQueryEngine -- 22 query methods + as_dict()
  models.py             ProjectIndex, LazyLines, AnnotatorProtocol
  project_indexer.py    File discovery, structural indexing, dependency graphs
  brace_matcher.py      Shared brace matching for C/C#/Rust/Go (factored v1.0.0)
  annotator.py          Language dispatch via _ANNOTATOR_MAP
  *_annotator.py        Per-language annotators (Python, TS, Go, Rust, C#, C/GLSL)
```

## Development

```bash
pip install -e ".[dev,mcp]"
pytest tests/ -v          # 865 tests
ruff check src/ tests/
```

---

## Known limitations

- **Live-editing window:** The index is git-aware and updates on query, not on save. If you edit a file and immediately call `get_function_source`, you may get the pre-edit version. The next git-tracked change triggers a re-index.
- **Cross-language tracing:** `get_change_impact` stops at language boundaries. Python calling a shell script calling a JSON config — the chain breaks after Python.
- **JSON value semantics:** The JSON annotator indexes key structure, not value meaning. Tracing what a config value propagates to across files is still manual.
- **Windows paths:** Not tested. Contributions welcome.
- **Max files:** Default 10,000 per project (configurable via `TOKEN_SAVIOR_MAX_FILES` env var).
- **Max file size:** Default 1 MB (configurable via `TOKEN_SAVIOR_MAX_FILE_SIZE_MB` env var).

---

<div align="center">

**Works with any MCP-compatible AI coding tool.**  
Claude Code · Cursor · Windsurf · Cline · Continue · Hermes · any custom MCP client

</div>
