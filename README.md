<!-- mcp-name: io.github.Mibayy/token-savior-recall -->

<div align="center">

# ⚡ Token Savior Recall

> Structural code navigation + persistent memory engine for Claude Code.
> **97% fewer tokens. Nothing forgotten between sessions.**

[![Version](https://img.shields.io/badge/version-2.6.0-blue)](https://github.com/Mibayy/token-savior/releases/tag/v2.6.0)
[![Tools](https://img.shields.io/badge/tools-105-green)]()
[![Tests](https://img.shields.io/badge/tests-1318%2F1318-brightgreen)]()
[![Savings](https://img.shields.io/badge/token%20savings-97%25-cyan)]()
[![Vector](https://img.shields.io/badge/vector%20search-enabled-purple)]()
[![CI](https://github.com/Mibayy/token-savior/actions/workflows/ci.yml/badge.svg)](https://github.com/Mibayy/token-savior/actions/workflows/ci.yml)
[![Python 3.11+](https://img.shields.io/badge/python-3.11+-blue.svg)](https://www.python.org/downloads/)
[![MCP](https://img.shields.io/badge/MCP-compatible-purple.svg)](https://modelcontextprotocol.io)

**📖 [mibayy.github.io/token-savior](https://mibayy.github.io/token-savior/)** — project site

</div>

---

## What it does

Claude Code reads whole files to answer questions about three lines, and forgets
everything the moment a session ends. Token Savior Recall fixes both. It
indexes your codebase by symbol — functions, classes, imports, call graph — so
the model navigates by pointer instead of by `cat`. Measured reduction: **97%
fewer chars injected** across 170+ real sessions.

On top of that sits a persistent memory engine. Every decision, bugfix,
convention, guardrail and session rollup is stored in SQLite WAL + FTS5 + vector
embeddings, ranked by Bayesian validity and ROI, and re-injected as a compact
delta at the start of the next session. Contradictions are detected at save
time; observations decay with explicit TTLs; a 3-layer progressive-disclosure
contract keeps lookup cost bounded.

---

## Token savings

| Operation | Plain Claude | Token Savior | Reduction |
|-----------|-------------:|-------------:|----------:|
| `find_symbol("send_message")` | 41M chars (full read) | 67 chars | **−99.9%** |
| `get_function_source("compile")` | grep + cat chain | 4.5K chars | direct |
| `get_change_impact("LLMClient")` | impossible | 16K chars (154 direct + 492 transitive) | new capability |
| `get_backward_slice(var, line)` | 130 lines | 12 lines | **−92%** |
| `memory_index` (Layer 1) | n/a | ~15 tokens/result | Layer 1 shortlist |
| 60-task tsbench run | 1,431,624 chars | 234,805 chars | **−84%** |
| tsbench score | 67/120 (56%) | **115/120 (96%)** | **+40 pts** |

Full benchmark methodology and per-task results: [tsbench](https://github.com/Mibayy/tsbench).

---

## Memory engine

| Capability | How it works |
|-----------|--------------|
| **Storage** | SQLite WAL + FTS5 + `sqlite-vec` (optional), 12 observation types |
| **Hybrid search** | BM25 + vector (`all-MiniLM-L6-v2`, 384d) fused via RRF, FTS fallback graceful |
| **Progressive disclosure** | 3-layer contract: `memory_index` → `memory_search` → `memory_get` |
| **Citation URIs** | `ts://obs/{id}` — reusable across layers, agent-native pointers |
| **Bayesian validity** | Each obs carries a validity prior + update rule; stale obs are surfaced, not silently trusted |
| **Contradiction detection** | Triggered at save time against existing index; flagged in hook output |
| **Decay + TTL** | Per-type TTL (command 60d, research 90d, note 60d) + LRU scoring `0.4·recency + 0.3·access + 0.3·type` |
| **Symbol staleness** | Obs linked to symbols are invalidated when the symbol's content hash changes |
| **ROI tracking** | Access count × context weight — unused obs age out, high-ROI obs are promoted |
| **MDL distillation** | Minimum Description Length grouping compresses redundant observations into conventions |
| **Auto-promotion** | note ×5 accesses → convention; warning ×5 → guardrail |
| **Hooks** | 8 Claude Code lifecycle hooks (SessionStart/Stop/End, PreCompact, PreToolUse ×2, UserPromptSubmit, PostToolUse) |
| **Web viewer** | `127.0.0.1:$TS_VIEWER_PORT` — htmx + SSE, opt-in |
| **LLM auto-extraction** | Opt-in `TS_AUTO_EXTRACT=1` — PostToolUse tool uses extracted into 0-3 observations via small-model call |

---

## vs claude-mem

Two projects share the goal — persistent memory for Claude Code. The axes
below are measured, not marketing.

| Axis | claude-mem | Token Savior Recall |
|------|:----------:|:-------------------:|
| Bayesian validity | no | **yes** |
| Contradiction detection at save | no | **yes** |
| Per-type decay + TTL | no | **yes** |
| Symbol staleness (content-hash linked obs) | no | **yes** |
| ROI tracking + auto-promotion | no | **yes** |
| MDL distillation into conventions | no | **yes** |
| Code graph / AST navigation | no | **yes** (105 tools, cross-language) |
| Progressive disclosure contract | no | **yes** (3 layers, ~15/60/200 tokens) |
| Hybrid FTS + vector search (RRF) | no | **yes** |

Token Savior Recall is a superset: it ships the memory engine *plus* the
structural codebase server that gave the project its name.

---

## Install

### uvx (no venv, no clone)

```bash
uvx token-savior-recall
```

### pip

```bash
pip install "token-savior-recall[mcp]"
# Optional hybrid vector search:
pip install "token-savior-recall[mcp,memory-vector]"
```

### Claude Code one-liner

```bash
claude mcp add token-savior -- /path/to/venv/bin/token-savior
```

### Development

```bash
git clone https://github.com/Mibayy/token-savior
cd token-savior
python3 -m venv .venv
.venv/bin/pip install -e ".[mcp,dev]"
pytest tests/ -q
```

### Configure

```json
{
  "mcpServers": {
    "token-savior-recall": {
      "command": "/path/to/venv/bin/token-savior",
      "env": {
        "WORKSPACE_ROOTS": "/path/to/project1,/path/to/project2",
        "TOKEN_SAVIOR_CLIENT": "claude-code"
      }
    }
  }
}
```

Optional env: `TELEGRAM_BOT_TOKEN` + `TELEGRAM_CHAT_ID` (critical-observation
feed), `TS_VIEWER_PORT` (web viewer), `TS_AUTO_EXTRACT=1` + `TS_API_KEY`
(LLM auto-extraction), `TOKEN_SAVIOR_PROFILE` (`full` / `core` / `nav` — filters
advertised tool set).

---

## Tools (105)

Category counts — full catalog is served via MCP `tools/list`.

| Category | Count |
|----------|------:|
| Core navigation | 14 |
| Dependencies & graph | 9 |
| Git & diffs | 5 |
| Safe editing | 8 |
| Checkpoints | 6 |
| Test & run | 6 |
| Config & quality | 8 |
| Docker & multi-project | 2 |
| Advanced context (slicing, packing, RWR, prefetch, verify) | 6 |
| **Memory engine** | **21** |
| Reasoning (plan/decision traces) | 3 |
| Stats, budget, health | 10 |
| Project management | 7 |

### Profiles

`TOKEN_SAVIOR_PROFILE` filters the advertised `tools/list` payload while
keeping handlers live.

| Profile | Advertised | ~Tokens | Use case |
|---------|-----------:|--------:|----------|
| `full` *(default)* | 105 | ~10 200 | All capabilities |
| `core`             | 54  | ~5 800  | Daily coding, no memory engine |
| `nav`              | 28  | ~3 100  | Read-only exploration |

---

## Progressive disclosure — memory search

Three layers, increasing cost. Always start at Layer 1. Escalate only if the
previous layer paid off. Full contract: [docs/progressive-disclosure.md](docs/progressive-disclosure.md).

| Layer | Tool            | Tokens/result | When                        |
|-------|-----------------|--------------:|-----------------------------|
| 1     | `memory_index`  | ~15           | Always first                |
| 2     | `memory_search` | ~60           | If Layer 1 matched          |
| 3     | `memory_get`    | ~200          | If Layer 2 confirmed        |

Each Layer 1 row ends with `[ts://obs/{id}]` — pass it straight to Layer 3.

---

## Links

- **Site** — <https://mibayy.github.io/token-savior/>
- **Repo** — <https://github.com/Mibayy/token-savior>
- **PyPI** — <https://pypi.org/project/token-savior-recall/>
- **Benchmark** — <https://github.com/Mibayy/tsbench>
- **Changelog** — [CHANGELOG.md](CHANGELOG.md)
- **Progressive disclosure** — [docs/progressive-disclosure.md](docs/progressive-disclosure.md)

## License

MIT — see [LICENSE](LICENSE).

<div align="center">

**Works with any MCP-compatible AI coding tool.**
Claude Code · Cursor · Windsurf · Cline · Continue · any custom MCP client

</div>
