<!-- mcp-name: io.github.Mibayy/token-savior-recall -->

<div align="center">

# ⚡ Token Savior — v4.0

> One MCP server. One profile. **97.9% on tsbench at −80% tokens.**
> Structural code navigation + persistent memory engine for AI coding agents.

[![Version](https://img.shields.io/badge/version-4.0.0-blue)](https://github.com/Mibayy/token-savior/releases/tag/v4.0.0)
[![PyPI](https://img.shields.io/badge/pypi-token--savior--recall-orange)](https://pypi.org/project/token-savior-recall/)
[![Tests](https://img.shields.io/badge/tests-1469%2F1469-brightgreen)]()
[![Benchmark](https://img.shields.io/badge/tsbench-97.9%25%20(188%2F192)-brightgreen)](https://mibayy.github.io/token-savior/)
[![Python 3.11+](https://img.shields.io/badge/python-3.11+-blue.svg)](https://www.python.org/downloads/)
[![MCP](https://img.shields.io/badge/MCP-compatible-purple.svg)](https://modelcontextprotocol.io)
[![CI](https://github.com/Mibayy/token-savior/actions/workflows/ci.yml/badge.svg)](https://github.com/Mibayy/token-savior/actions/workflows/ci.yml)

**📖 [mibayy.github.io/token-savior](https://mibayy.github.io/token-savior/)** — project site + benchmark landing
**🧪 [github.com/Mibayy/tsbench](https://github.com/Mibayy/tsbench)** — benchmark source + fixtures

---

### Benchmark — 96 real coding tasks (Claude Opus 4.7, May 2026)

| | Plain Claude Code | With Token Savior v4.0 |
|---|---:|---:|
| **Score** | 141 / 180 (78.3%) | **188 / 192 (97.9%)** |
| **Active tokens / task** | 17 221 | **3 395** (−80%) |
| **Wall time / task** | 110.6 s | **18.9 s** (−83%) |

Reproduces with the `optimized` profile (single env var). See [BENCHMARK-SUMMARY](https://github.com/Mibayy/tsbench/blob/main/BENCHMARK-SUMMARY.md).

</div>

---

## Quick start

```bash
pip install "token-savior-recall[mcp]"
```

Add to your MCP config (e.g. Claude Code):

```json
{
  "mcpServers": {
    "token-savior-recall": {
      "command": "/path/to/venv/bin/token-savior",
      "env": {
        "WORKSPACE_ROOTS": "/path/to/project1,/path/to/project2",
        "TOKEN_SAVIOR_CLIENT": "claude-code",
        "TOKEN_SAVIOR_PROFILE": "optimized"
      }
    }
  }
}
```

That's it. **`TOKEN_SAVIOR_PROFILE=optimized`** ships the Pareto-optimum config that wins tsbench. It bundles :
- `tiny_plus` (15 hot tools manifest)
- thin inputSchema (−44% manifest)
- capture sandbox disabled
- memory hooks gated for cross-project safety

No other tuning needed.

---

## What it does

Claude Code reads whole files to answer questions about three lines, and forgets
everything the moment a session ends. Token Savior fixes both.

It indexes your codebase by symbol — functions, classes, imports, call graph — so
the model navigates by pointer instead of by `cat`. Measured reduction: **97%
fewer chars injected** across 170+ real sessions.

On top of that sits a persistent memory engine. Every decision, bugfix,
convention, guardrail and session rollup is stored in SQLite WAL + FTS5 + vector
embeddings, ranked by Bayesian validity and ROI, and re-injected as a compact
delta at the start of the next session.

---

## Profile comparison

| Profile | Tools exposed | Manifest tokens | When to use |
|---|---:|---:|---|
| **`optimized`** | **15** | **~1.5 KT** | **Recommended default — Pareto win on tsbench** |
| `auto` | adaptive | ~1-2 KT | Per-client telemetry-based (experimental) |
| `tiny` | 6 | ~0.6 KT | Minimal hot loop |
| `lean` | 51 | ~4 KT | Legacy — broader surface |
| `full` | 68 | ~6 KT | Everything exposed |

You probably want `optimized`.

---

## Token savings

| Operation | Plain Claude | Token Savior | Reduction |
|-----------|-------------:|-------------:|----------:|
| `find_symbol("send_message")` | 41M chars (full read) | 67 chars | **−99.9%** |
| `get_function_source("compile")` | grep + cat chain | 4.5K chars | direct |
| `get_change_impact("LLMClient")` | impossible | 16K chars | new capability |
| 96-task tsbench (Opus, plain vs ts) | 17 221 active/task | **3 395 active/task** | **−80%** |

---

## Install

### pip (MCP server)

```bash
pip install "token-savior-recall[mcp]"
# Optional hybrid vector search :
pip install "token-savior-recall[mcp,memory-vector]"
```

### uvx (no venv, no clone)

```bash
uvx token-savior-recall
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

---

## Bonus : `ts` CLI for non-MCP agents

If you use an agent without MCP support (Cursor, Aider, Continue, scripts, CI), there's also a `ts` command that exposes a subset of the tools via shell :

```bash
ts use /path/to/project
ts get my_function          # JSON output
ts search 'pattern'
ts daemon start             # ~145ms per call vs 1.5s cold fork
```

**On Claude Code, prefer the MCP server** — measured cheaper than CLI on Opus 4.7. The CLI is there for the portability case.

---

## Optional env vars

| Var | Purpose |
|---|---|
| `TELEGRAM_BOT_TOKEN` + `TELEGRAM_CHAT_ID` | Critical-observation feed |
| `TS_VIEWER_PORT` | Web viewer dashboard |
| `TS_AUTO_EXTRACT=1` + `TS_API_KEY` | LLM auto-extraction of memory observations |
| `TS_CAPTURE_DISABLED=1` | Skip read-side capture sandboxing (default in `optimized`) |
| `TS_MEMORY_DISABLE=1` | Silence memory hooks (clean-context workloads) |

---

## License

MIT
