<!-- mcp-name: io.github.Mibayy/token-savior-recall -->

<div align="center">

# ⚡ Token Savior Recall

> **97% token reduction** · **Persistent memory** · **75 MCP tools** · **Python 3.11+**

[![Version](https://img.shields.io/badge/version-2.1.0-blue)](https://github.com/Mibayy/token-savior/releases/tag/v2.1.0)
[![Tools](https://img.shields.io/badge/tools-75-green)]()
[![Savings](https://img.shields.io/badge/token%20savings-97%25-cyan)]()
[![Tests](https://img.shields.io/badge/tests-891%2F891-brightgreen)]()
[![Memory](https://img.shields.io/badge/memory-SQLite%20WAL%20%2B%20FTS5-orange)]()
[![CI](https://github.com/Mibayy/token-savior/actions/workflows/ci.yml/badge.svg)](https://github.com/Mibayy/token-savior/actions/workflows/ci.yml)
[![Python 3.11+](https://img.shields.io/badge/python-3.11+-blue.svg)](https://www.python.org/downloads/)
[![MCP](https://img.shields.io/badge/MCP-compatible-purple.svg)](https://modelcontextprotocol.io)

</div>

---

## What it does

Token Savior Recall is a Claude Code MCP server that solves two problems:

**1. Token waste** — Claude reads entire files to answer questions about 3 lines.
Token Savior navigates your codebase by symbols, returning only what's needed.
97% reduction on 170+ real sessions.

**2. Amnesia** — Claude starts from zero every session.
Token Savior Recall captures observations across sessions, injects relevant
context at startup, and surfaces the right knowledge before you ask.

```
find_symbol("send_message")           →  67 chars    (was: 41M chars of source)
get_change_impact("LLMClient")        →  16K chars   (154 direct + 492 transitive deps)
get_function_source("compile")        →  4.5K chars  (exact source, no grep, no cat)
memory_search("auth migration")       →  ranked past decisions, bugs, conventions
get_backward_slice("parse_invoice", variable="total", line=42)
                                       →  12 lines / 130 (92% reduction)
```

---

## Performance

| Metric | Value |
|--------|-------|
| Token reduction (navigation) | **97%** |
| Symbol reindex speedup | **19x** (symbol-level hashing) |
| Re-access savings (CSC) | **93%** |
| Abstraction compression L3 | **94-97%** vs full source |
| Program slice reduction | **92%** |
| Sessions tracked | 170+ |
| Tokens saved | ~203M |
| Estimated cost saved | $609+ |
| Projects supported | 17 |
| Tool count | **75** |

> "Tokens saved" = estimated tokens the agent would have consumed navigating
> with `cat`/`grep` versus with Token Savior Recall. Model-agnostic: the index
> reduces context-window pressure regardless of provider.

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

Cache is persistent — restarts skip the full build. CPython goes from 56s to
under 1s on a cache hit. Symbol-level content hashing (v2.1.0) reduces the
incremental reindex cost by **19x** on targeted edits.

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
      "command": "/path/to/venv/bin/python",
      "args": ["-m", "token_savior.server"],
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

`TELEGRAM_BOT_TOKEN` / `TELEGRAM_CHAT_ID` are optional — they enable the
critical-observation feed (guardrails, warnings, error patterns).

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

AI assistants default to `grep` and `cat` even when better tools are available.
Add this to your `CLAUDE.md` or equivalent:

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

## Tools (75)

### Core Navigation (14)
`get_function_source` (level=0-3) · `get_class_source` · `find_symbol` ·
`get_functions` · `get_classes` · `get_imports` · `get_structure_summary` ·
`get_project_summary` · `list_files` · `search_codebase` · `get_routes` ·
`get_env_usage` · `get_components` · `get_feature_files`

### Memory Engine (16)
`memory_save` · `memory_search` · `memory_get` · `memory_delete` ·
`memory_index` · `memory_timeline` · `memory_status` · `memory_top` ·
`memory_why` · `memory_doctor` · `memory_from_bash` · `memory_set_global` ·
`memory_mode` · `memory_archive` · `memory_maintain` · `memory_prompts`

### Advanced Context (6)
`get_backward_slice` · `pack_context` · `get_relevance_cluster` ·
`get_call_predictions` · `verify_edit` · `find_semantic_duplicates`

### Dependencies (7)
`get_dependencies` · `get_dependents` · `get_change_impact` ·
`get_call_chain` · `get_file_dependencies` · `get_file_dependents` ·
`get_symbol_cluster`

### Git & Diff (5)
`get_git_status` · `get_changed_symbols` ·
`summarize_patch_by_symbol` · `build_commit_summary` · `get_edit_context`

### Checkpoints (6)
`create_checkpoint` · `list_checkpoints` · `delete_checkpoint` ·
`prune_checkpoints` · `restore_checkpoint` · `compare_checkpoint_by_symbol`

### Edit & Validate (4)
`replace_symbol_source` · `insert_near_symbol` ·
`apply_symbol_change_and_validate` · `find_impacted_test_files`

### Analysis (6)
`find_hotspots` · `find_dead_code` · `detect_breaking_changes` ·
`analyze_config` · `analyze_docker` · `run_impacted_tests`

### Project (7)
`list_projects` · `switch_project` · `set_project_root` · `reindex` ·
`get_usage_stats` · `discover_project_actions` · `run_project_action`

---

## Memory Engine

### Architecture
- **Storage** — SQLite WAL + FTS5 (fast full-text search, concurrent reads)
- **Hooks** — 8 Claude Code lifecycle hooks (SessionStart, Stop, SessionEnd,
  PreCompact, PreToolUse ×2, UserPromptSubmit, PostToolUse)
- **Types** — 12 observation types (`bugfix`, `guardrail`, `convention`,
  `warning`, `decision`, `error_pattern`, `note`, `command`, `research`,
  `infra`, `config`, `idea`)
- **CLI** — `ts memory {status,list,search,get,save,top,why,doctor,relink}`

### How it works
1. **SessionStart** — injects a delta-based memory index (only new/changed obs)
2. **PreToolUse** — injects file/symbol history before each relevant tool call
3. **UserPromptSubmit** — auto-captures trigger phrases, injects relevant obs
4. **PostToolUse** — auto-saves significant bash commands and research hints
5. **Stop / SessionEnd** — generates a structured session summary via `claude -p`

### LRU Scoring
Observations are ranked by:
`0.4 × recency + 0.3 × access_count + 0.3 × type_priority`

Type priority: guardrail (1.0) > convention (0.9) > warning (0.8) >
command (0.7) > note (0.2)

### Delta injection
Only changed observations are re-injected at SessionStart. Unchanged sessions
inject a single line instead of 30 observations. Estimated savings: 50-70% vs
full refresh on repeated sessions.

---

## Advanced Context (v2.1.0)

### Program Slicing
```
get_backward_slice(name="parse_invoice", variable="total", line=42)
→ 12 lines / 130 total (92% reduction)
```
Returns the minimal set of instructions affecting a variable at a given line.
Built on Data Dependency Graph analysis via Python AST.

### Knapsack Context Packing
```
pack_context(query="authentication flow", budget_tokens=4000)
→ optimal symbol bundle ≤ 4000 tokens
```
Greedy fractional knapsack (Dantzig 1957). Scores symbols by query match +
dependency proximity + recency + access count.

### PageRank / Random Walk with Restart
```
get_relevance_cluster(name="parseInvoice", budget=10)
→ mathematically ranked relevant symbols
```
RWR (Tong, Faloutsos, Pan 2006) on the dependency graph. Captures indirect
relevance that BFS misses.

### Predictive Prefetching
Markov model on tool call sequences. After `get_function_source(X)`,
pre-computes `get_dependents(X)` with **77.8%** accuracy. Background daemon
threads keep the warm cache fresh without blocking.

### Proof-Carrying Edits
```
verify_edit(symbol_name="parse_config", new_source="...")
→ EditSafety: SAFE TO APPLY
   signature: preserved
   exceptions: unchanged
   side-effects: unchanged
```
Static analysis certificate attached to every `apply_symbol_change_and_validate`.
Never blocks the edit — surfaces risk for the agent to weigh.

### Semantic Hash (AST-normalized)
```
find_semantic_duplicates()
→ 5 groups detected (including _build_line_offsets ×9 across annotators)
```
Two functions equivalent modulo variable renaming → same hash.
α-conversion + docstring stripping + AST normalization. Falls back to text
hash on syntax errors so non-Python annotators are still covered.

---

## What's New in v2.1.0

**Advanced Context Engine (Phase 2)**
- Program slicing via backward AST analysis (92% token reduction on debug)
- Knapsack context packing — optimal bundle at fixed token budget
- PageRank / RWR on dependency graph — mathematically ranked context
- Markov predictive prefetching — 77.8% accuracy on next tool call
- Proof-carrying edits — EditSafety certificate before every write
- Semantic AST hash — cross-file duplicate detection

**Core Optimizations (Phase 1)**
- Symbol-level content hashing — 19x reindex speedup on targeted edits
- 2-level semantic hash (signature + body) — precise breaking change detection
- Conversation Symbol Cache (CSC) — 93% token savings on re-accessed symbols
- Lattice of Abstractions L0→L3 — 94-97% compression vs full source

**Memory Engine**
- 16 memory tools, 8 lifecycle hooks, 12 observation types
- LRU scoring, delta injection, TTL, semantic dedup (Jaccard ~0.85)
- Auto-promotion, contradiction detection, auto-linking
- Mode system (`code` / `review` / `debug` / `infra` / `silent`) + auto-detect
- CLI `ts` — full memory management from any terminal
- Telegram feed for critical observations
- Markdown export + git versioning

**Manifest optimization**
- 80 → 75 tools (-6%), 42K → 36K chars (-14%), ~1500 tokens/session saved

**Refactor**
- `_build_line_offsets` extracted to shared helper (9x dedup across annotators)

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

LSP answers "where is this defined?" — Token Savior Recall answers "what
breaks if I change it, what did we learn last time, and what should we do
about it?"

LSP is point queries: one symbol, one file, one position. It can find where
`LLMClient` is defined. Ask "what breaks transitively if I refactor
`LLMClient`, and did we already hit this bug six weeks ago?" and LSP has
nothing.

`get_change_impact("TestCase")` on CPython finds 154 direct and 492 transitive
dependents in 0.45ms, returning 16K chars instead of reading 41M. Pair it with
`memory_search("TestCase refactor")` and you get prior decisions, past bugs,
and conventions in the same round-trip — with zero language servers required.

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
  tool_schemas.py      75 tool schemas
  slot_manager.py      Multi-project lifecycle, incremental mtime updates
  cache_ops.py         JSON persistence, legacy cache migration
  query_api.py         ProjectQueryEngine — query methods + as_dict()
  models.py            ProjectIndex, LazyLines, AnnotatorProtocol, build_line_char_offsets
  project_indexer.py   File discovery, structural indexing, dependency graphs
  memory_db.py         SQLite WAL + FTS5 memory engine
  program_slicer.py    Backward slicing via Data Dependency Graph
  context_packer.py    Greedy fractional knapsack
  graph_ranker.py      Random Walk with Restart on dependency graph
  markov_prefetcher.py Predictive prefetching, daemon warm cache
  semantic_hasher.py   AST-normalized semantic hash (alpha-conversion)
  edit_verifier.py     EditSafety static-analysis certificate
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

- **Live-editing window:** the index updates on query, not on save. Right
  after an edit you may briefly see the pre-edit version; the next git-tracked
  change triggers re-indexing.
- **Cross-language tracing:** `get_change_impact` stops at language boundaries.
- **JSON value semantics:** the JSON annotator indexes key structure, not
  value meaning.
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
