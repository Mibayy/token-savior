<!-- mcp-name: io.github.Mibayy/mcp-codebase-index -->
# mcp-codebase-index

[![CI](https://github.com/MikeRecognex/mcp-codebase-index/actions/workflows/ci.yml/badge.svg)](https://github.com/MikeRecognex/mcp-codebase-index/actions/workflows/ci.yml)
[![PyPI version](https://img.shields.io/pypi/v/mcp-codebase-index)](https://pypi.org/project/mcp-codebase-index/)
[![Python 3.11+](https://img.shields.io/badge/python-3.11+-blue.svg)](https://www.python.org/downloads/)
[![License: AGPL-3.0](https://img.shields.io/badge/License-AGPL--3.0-blue.svg)](https://www.gnu.org/licenses/agpl-3.0)
[![MCP](https://img.shields.io/badge/MCP-compatible-purple.svg)](https://modelcontextprotocol.io)
[![Zero Dependencies](https://img.shields.io/badge/dependencies-zero-brightgreen.svg)](https://pypi.org/project/mcp-codebase-index/)

A structural indexer with an [MCP](https://modelcontextprotocol.io) server for AI-assisted work — code navigation, doc search, config tracing, all from a single index. Zero runtime dependencies. Requires Python 3.11+.

## What it does

Instead of the agent reading entire files to find one thing, it queries a pre-built structural index. `find_symbol("_honcho_prefetch")` returns a 20-line preview, the file, and the line number. `get_change_impact("send_message")` returns 11 direct dependents and 31 transitive ones in a single call — 204 characters, sub-millisecond.

The index covers more than just code. This fork extends the upstream with annotators for Markdown/text files, JSON configs, and a generic fallback — so a workspace pointing at `/root` indexes Python bots, docker-compose files, READMEs, skill files, and API configs in one pass. Any agent task benefits, not only refactoring sessions.

**Automatic incremental re-indexing:** In git repositories, the server checks `git diff` and `git status` before every query (~1-2ms) and re-parses only changed files. No manual `reindex` calls needed after edits, branch switches, or pulls.

**Persistent disk cache:** The index is saved to `.codebase-index-cache.json` after every build. On restart, it loads from the cache and validates against the current git HEAD — exact match means instant startup, no parsing. Small changesets (≤20 files) are applied incrementally. Cold starts on large projects go from tens of seconds to under a second.

## Language and file support

| Type | Files | Extracts |
|------|-------|----------|
| Python | `.py`, `.pyw` | Functions, classes, methods, imports, dependency graph |
| TypeScript / JS | `.ts`, `.tsx`, `.js`, `.jsx` | Functions, arrow functions, classes, interfaces, type aliases, imports |
| Go | `.go` | Functions, methods (receiver), structs, interfaces, type aliases, imports, doc comments |
| Rust | `.rs` | Functions (`pub`/`async`/`const`/`unsafe`), structs, enums, traits, impl blocks, use statements, doc comments, macro_rules |
| C# | `.cs` | Classes, interfaces, structs, enums, records, methods, constructors, using directives, XML doc comments |
| Markdown / Text | `.md`, `.txt`, `.rst` | Sections via heading detection (`#`, underlines, numbered `1.2.3`, ALL-CAPS) |
| JSON | `.json` | Nested key structure up to depth 4, `$ref` cross-references as imports |
| Everything else | `*` | Line counts (generic fallback) |

The Markdown, JSON, and generic annotators are what enable full-workspace indexing. Without them, pointing `WORKSPACE_ROOTS` at a mixed directory would produce empty results for half the files.

## How it operates

### Indexing

On first use, the server walks every file under `WORKSPACE_ROOTS`, dispatches each file to the appropriate annotator based on extension, and builds two structures:

1. **Symbol table** — maps every function, class, method, and section heading to its file, line range, signature, and a source preview.
2. **Dependency graph** — directed graph of what calls what, built from import analysis and call-site detection. Used for `get_dependencies`, `get_dependents`, and `get_change_impact`.

The result is saved to `.codebase-index-cache.json` (human-readable, unlike the upstream's pickle format — safer across Python versions and inspectable when things go wrong).

### Querying

All queries are in-memory lookups against the symbol table or graph traversals. They do not re-read source files. Response size scales with the answer, not the codebase — `find_symbol` returns the same ~60 characters whether the project is 7K lines or 1.1M lines.

### Multi-project workspaces

`WORKSPACE_ROOTS` takes a comma-separated list of absolute paths. Each path gets its own isolated index, loaded lazily on first use. `list_projects` shows all registered roots and their status. `switch_project` sets the active one for subsequent queries. This is the intended setup for a VPS or development machine with multiple projects — one server instance covers everything.

```bash
WORKSPACE_ROOTS=/root/myapp,/root/mybot,/root/docs mcp-codebase-index
```

## Installation

```bash
python -m venv ~/.local/mcp-codebase-index-venv
~/.local/mcp-codebase-index-venv/bin/pip install mcp-codebase-index
```

Installing in a dedicated venv avoids dependency conflicts with other tools on the same machine.

## Configuring with Hermes Agent

Add to `~/.hermes/cli-config.yaml`:

```yaml
mcp_servers:
  codebase-index:
    command: ~/.local/mcp-codebase-index-venv/bin/mcp-codebase-index
    env:
      WORKSPACE_ROOTS: /path/to/project1,/path/to/project2
    timeout: 120
    connect_timeout: 30
```

Restart Hermes after editing the config. Verify with:

```
hermes> list all indexed projects
```

The agent will call `list_projects` and show each root with its index status.

## Configuring with Claude Code

Add to your project's `.mcp.json`:

```json
{
  "mcpServers": {
    "codebase-index": {
      "command": "/path/to/.venv/bin/mcp-codebase-index",
      "env": {
        "WORKSPACE_ROOTS": "/path/to/project1,/path/to/project2"
      }
    }
  }
}
```

### Making the agent actually use the tools

AI assistants default to `grep` and `cat` even when index tools are available. Add this to your `CLAUDE.md` or equivalent instructions file with mandatory language — soft language like "prefer" gets rationalized away:

```
## Codebase Navigation — MANDATORY

You MUST use codebase-index MCP tools FIRST when exploring or navigating the codebase.

- ALWAYS start with: find_symbol, get_function_source, get_class_source,
  search_codebase, get_dependencies, get_dependents, get_change_impact
- Only fall back to Read/Grep when codebase-index tools genuinely don't have
  what you need (e.g. binary files)
- If you catch yourself reaching for grep to find code, STOP and use
  search_codebase instead
```

## Benchmarks

### Token savings across real sessions

Measured across 92 sessions on production codebases running Hermes Agent with this fork. All projects were mixed workspaces (code + docs + configs):

| Project | Sessions | Queries | Chars used | Chars (naive) | Chars saved | Saving |
|---------|----------|---------|------------|---------------|-------------|--------|
| project-alpha | 35 | 360 | 4,801,108 | 639,560,872 | 634,759,764 | **99%** |
| project-beta | 26 | 189 | 766,508 | 20,936,204 | 20,169,696 | **96%** |
| project-gamma | 30 | 232 | 410,816 | 3,679,868 | 3,269,052 | **89%** |
| project-delta | 1 | 1 | 3,036 | 52,148 | 49,112 | **94%** |
| **TOTAL** | **92** | **782** | **5,981,476** | **664,229,092** | **658,247,616** | **99%** |

"Chars (naive)" is the total source size of all files the agent would have read with `cat`/`grep`. "Chars used" is what the index actually returned. These savings are model-agnostic — the index reduces what enters the context window regardless of provider.

### Index build performance

Tested on real-world projects from small to CPython's 1.1M lines:

| Project | Files | Lines | Functions | Classes | Index time | Peak memory |
|---------|------:|------:|----------:|--------:|-----------:|------------:|
| RMLPlus | 36 | 7,762 | 237 | 55 | 0.9s | 2.4 MB |
| FastAPI | 2,556 | 332,160 | 4,139 | 617 | 5.7s | 55 MB |
| Django | 3,714 | 707,493 | 29,995 | 7,371 | 36.2s | 126 MB |
| **CPython** | **2,464** | **1,115,334** | **59,620** | **9,037** | **55.9s** | **197 MB** |

With the persistent cache, subsequent restarts skip the full build entirely. A cache hit on CPython restores the index in under a second instead of 56s.

### Query response size vs total source (CPython — 41M chars)

| Query | Response | Total source | Reduction |
|-------|-------:|------------:|----------:|
| `find_symbol("TestCase")` | 67 chars | 41,077,561 chars | **99.9998%** |
| `get_dependencies("compile")` | 115 chars | 41,077,561 chars | **99.9997%** |
| `get_change_impact("TestCase")` | 16,812 chars | 41,077,561 chars | **99.96%** |
| `get_function_source("compile")` | 4,531 chars | 41,077,561 chars | **99.99%** |

### Query response time

All targeted queries return in sub-millisecond time even on 1.1M lines:

| Query | RMLPlus | FastAPI | Django | CPython |
|-------|--------:|--------:|-------:|--------:|
| `find_symbol` | 0.01ms | 0.01ms | 0.03ms | 0.08ms |
| `get_dependencies` | 0.00ms | 0.00ms | 0.00ms | 0.01ms |
| `get_change_impact` | 0.02ms | 0.00ms | 2.81ms | 0.45ms |
| `get_function_source` | 0.01ms | 0.02ms | 0.03ms | 0.10ms |

Run the benchmarks yourself: `python benchmarks/benchmark.py`

## Available tools (18)

| Tool | What it does |
|------|-------------|
| `get_project_summary` | File count, packages, top classes/functions |
| `list_files` | Indexed files with optional glob filter |
| `get_structure_summary` | Structure of a file or whole project |
| `get_functions` | All functions with name, lines, params |
| `get_classes` | All classes with name, lines, methods, bases |
| `get_imports` | All imports with module, names, line |
| `get_function_source` | Full source of a function/method |
| `get_class_source` | Full source of a class |
| `find_symbol` | Where a symbol is defined — file, line, type, preview |
| `get_dependencies` | What a symbol calls/uses |
| `get_dependents` | What calls/uses a symbol |
| `get_change_impact` | Direct + transitive dependents |
| `get_call_chain` | Shortest dependency path (BFS) |
| `get_file_dependencies` | Files imported by a given file |
| `get_file_dependents` | Files that import from a given file |
| `search_codebase` | Regex search across all indexed files (max 100 results) |
| `reindex` | Force full re-index (rarely needed — incremental updates handle git changes automatically) |
| `get_usage_stats` | Cumulative token savings per project across sessions |

## How is this different from LSP?

LSP answers "where is this defined?" — mcp-codebase-index answers "what breaks if I change it?" LSP is point queries: one symbol, one file, one position. It can find where `LLMClient` is defined and who references it directly. Ask "what breaks transitively if I refactor `LLMClient`?" and LSP has nothing — the AI would need to chain dozens of find-reference calls recursively, reading files at every step.

`get_change_impact("TestCase")` on CPython finds 154 direct dependents and 492 transitive dependents in 0.45ms, returning 16K chars instead of reading 41M. LSP also requires a separate language server per language. This tool is zero dependencies, covers Python + TS/JS + Go + Rust + C# + Markdown + JSON out of the box, and every response has built-in token budget controls (`max_results`, `max_lines`, `max_direct`, `max_transitive`).

## Programmatic usage

```python
from mcp_codebase_index.project_indexer import ProjectIndexer
from mcp_codebase_index.query_api import create_project_query_functions

indexer = ProjectIndexer("/path/to/project", include_patterns=["**/*.py"])
index = indexer.index()
query_funcs = create_project_query_functions(index)

print(query_funcs["get_project_summary"]())
print(query_funcs["find_symbol"]("MyClass"))
print(query_funcs["get_change_impact"]("some_function"))
```

## Development

```bash
pip install -e ".[dev,mcp]"
pytest tests/ -v
ruff check src/ tests/
```

## Known limitations

- **Live-editing window:** The index is git-aware but updates on query, not on save. If you edit a file and immediately call `get_function_source`, you may get the pre-edit version. The next git-tracked change triggers a re-index.
- **Cross-language dependency tracing:** `get_change_impact` stops at language boundaries. A Python script calling a shell script that modifies a JSON config — the chain breaks after Python.
- **JSON value semantics:** The JSON annotator indexes key structure, not value meaning. Tracing what a config value propagates to across files is still manual.

## References

The structural indexer was originally developed as part of the [RMLPlus](https://github.com/MikeRecognex/RMLPlus) project, an implementation of the [Recursive Language Models](https://arxiv.org/abs/2512.24601) framework.

## License

This project is dual-licensed:

- **AGPL-3.0** for open-source use — see [LICENSE](LICENSE)
- **Commercial License** for proprietary use — see [COMMERCIAL-LICENSE.md](COMMERCIAL-LICENSE.md)
