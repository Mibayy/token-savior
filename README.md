<!-- mcp-name: io.github.Mibayy/token-savior -->

<div align="center">

# ⚔ token-savior

**Stop feeding your AI entire codebases. Give it a scalpel instead.**

[![CI](https://github.com/Mibayy/token-savior/actions/workflows/ci.yml/badge.svg)](https://github.com/Mibayy/token-savior/actions/workflows/ci.yml)
[![PyPI version](https://img.shields.io/pypi/v/token-savior)](https://pypi.org/project/token-savior/)
[![Python 3.11+](https://img.shields.io/badge/python-3.11+-blue.svg)](https://www.python.org/downloads/)
[![MCP](https://img.shields.io/badge/MCP-compatible-purple.svg)](https://modelcontextprotocol.io)
[![Zero Dependencies](https://img.shields.io/badge/dependencies-zero-brightgreen.svg)](https://pypi.org/project/token-savior/)

</div>

---

An MCP server that indexes your codebase structurally and exposes surgical query tools — so your AI agent reads 200 characters instead of 200 files.

```
find_symbol("send_message")           →  67 chars    (was: 41M chars of source)
get_change_impact("LLMClient")        →  16K chars   (154 direct + 492 transitive deps)
get_function_source("compile")        →  4.5K chars  (exact source, no grep, no cat)
```

**Measured across 782 real sessions: 99% token reduction.**

---

## Why this exists

Every AI coding session starts the same way: the agent grabs `cat` or `grep`, reads a dozen files to find one function, then bloats its context trying to understand what else might break. By the end, half your token budget is gone before the first edit.

`token-savior` replaces that pattern entirely. It builds a structural index once, keeps it in sync with git automatically, and answers "where is X", "what calls X", and "what breaks if I change X" in sub-millisecond time — with responses sized to the answer, not the codebase.

---

## Numbers

### Token savings across real sessions

| Project | Sessions | Queries | Chars used | Chars (naive) | Saving |
|---------|----------|---------|------------|---------------|--------|
| project-alpha | 35 | 360 | 4,801,108 | 639,560,872 | **99%** |
| project-beta | 26 | 189 | 766,508 | 20,936,204 | **96%** |
| project-gamma | 30 | 232 | 410,816 | 3,679,868 | **89%** |
| **TOTAL** | **92** | **782** | **5,981,476** | **664,229,092** | **99%** |

> "Chars (naive)" = total source size of all files the agent would have read with `cat`/`grep`. These savings are model-agnostic — the index reduces context window pressure regardless of provider.

### Query response time (sub-millisecond at 1.1M lines)

| Query | RMLPlus | FastAPI | Django | CPython |
|-------|--------:|--------:|-------:|--------:|
| `find_symbol` | 0.01ms | 0.01ms | 0.03ms | 0.08ms |
| `get_dependencies` | 0.00ms | 0.00ms | 0.00ms | 0.01ms |
| `get_change_impact` | 0.02ms | 0.00ms | 2.81ms | 0.45ms |
| `get_function_source` | 0.01ms | 0.02ms | 0.03ms | 0.10ms |

### Index build performance

| Project | Files | Lines | Index time | Memory |
|---------|------:|------:|-----------:|-------:|
| Small project | 36 | 7,762 | 0.9s | 2.4 MB |
| FastAPI | 2,556 | 332,160 | 5.7s | 55 MB |
| Django | 3,714 | 707,493 | 36.2s | 126 MB |
| **CPython** | **2,464** | **1,115,334** | **55.9s** | **197 MB** |

With the persistent cache, subsequent restarts skip the full build. CPython goes from 56s → under 1s on cache hit.

---

## What it covers

| Language / Type | Files | Extracts |
|-----------------|-------|----------|
| Python | `.py`, `.pyw` | Functions, classes, methods, imports, dependency graph |
| TypeScript / JS | `.ts`, `.tsx`, `.js`, `.jsx` | Functions, arrow functions, classes, interfaces, type aliases |
| Go | `.go` | Functions, methods (receiver), structs, interfaces, type aliases |
| Rust | `.rs` | Functions, structs, enums, traits, impl blocks, macro_rules |
| C# | `.cs` | Classes, interfaces, structs, enums, methods, XML doc comments |
| Markdown / Text | `.md`, `.txt`, `.rst` | Sections via heading detection |
| JSON | `.json` | Nested key structure up to depth 4, `$ref` cross-references |
| Everything else | `*` | Line counts (generic fallback) |

A workspace pointing at `/root` indexes Python bots, docker-compose files, READMEs, skill files, and API configs in one pass. Any agent task benefits — not only code refactoring.

---

## 34 tools

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

### Impact analysis
| Tool | What it does |
|------|-------------|
| `get_dependencies` | What a symbol calls/uses |
| `get_dependents` | What calls/uses a symbol |
| `get_change_impact` | Direct + transitive dependents in one call |
| `get_call_chain` | Shortest dependency path between two symbols (BFS) |
| `get_file_dependencies` | Files imported by a given file |
| `get_file_dependents` | Files that import from a given file |

### Git & diffs
| Tool | What it does |
|------|-------------|
| `get_git_status` | Branch, ahead/behind, staged, unstaged, untracked |
| `get_changed_symbols` | Changed files as symbol-level summaries, not diffs |
| `get_changed_symbols_since_ref` | Symbol-level changes since any git ref |
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
| `apply_symbol_change_and_validate` | Edit + run impacted tests in one call |
| `apply_symbol_change_validate_with_rollback` | Edit + validate + auto-rollback on failure |
| `discover_project_actions` | Detect test/lint/build/run commands from project files |
| `run_project_action` | Execute a discovered action with bounded output |

### Stats
| Tool | What it does |
|------|-------------|
| `get_usage_stats` | Cumulative token savings per project across sessions |

---

## vs LSP

LSP answers "where is this defined?" — `token-savior` answers "what breaks if I change it?"

LSP is point queries: one symbol, one file, one position. It can find where `LLMClient` is defined and who references it directly. Ask "what breaks transitively if I refactor `LLMClient`?" and LSP has nothing — the AI would need to chain dozens of find-reference calls recursively, reading files at every step.

`get_change_impact("TestCase")` on CPython finds 154 direct dependents and 492 transitive dependents in 0.45ms, returning 16K chars instead of reading 41M. And unlike LSP, it requires zero language servers — one binary covers Python + TS/JS + Go + Rust + C# + Markdown + JSON out of the box.

---

## Install

```bash
python3 -m venv ~/.local/token-savior-venv
~/.local/token-savior-venv/bin/pip install token-savior
```

---

## Configure

### Claude Code / Cursor / Windsurf / Cline

Add to `.mcp.json` in your project root:

```json
{
  "mcpServers": {
    "token-savior": {
      "command": "/path/to/.local/token-savior-venv/bin/token-savior",
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

The server checks `git diff` and `git status` before every query (~1-2ms). Changed files are re-parsed incrementally. No manual `reindex` after edits, branch switches, or pulls.

The index is saved to `.codebase-index-cache.json` after every build — human-readable JSON, inspectable when things go wrong, safe across Python versions.

---

## Programmatic usage

```python
from token_savior.project_indexer import ProjectIndexer
from token_savior.query_api import create_project_query_functions

indexer = ProjectIndexer("/path/to/project")
index = indexer.index()
query = create_project_query_functions(index)

print(query["get_project_summary"]())
print(query["find_symbol"]("MyClass"))
print(query["get_change_impact"]("send_message"))
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

- **Live-editing window:** The index is git-aware and updates on query, not on save. If you edit a file and immediately call `get_function_source`, you may get the pre-edit version. The next git-tracked change triggers a re-index.
- **Cross-language tracing:** `get_change_impact` stops at language boundaries. Python calling a shell script calling a JSON config — the chain breaks after Python.
- **JSON value semantics:** The JSON annotator indexes key structure, not value meaning. Tracing what a config value propagates to across files is still manual.

---

<div align="center">

**Works with any MCP-compatible AI coding tool.**  
Claude Code · Cursor · Windsurf · Cline · Continue · Hermes · any custom MCP client

</div>
