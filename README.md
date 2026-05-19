<!-- mcp-name: io.github.Mibayy/token-savior-recall -->

<div align="center">

# Token Savior -- v4.3

> One MCP server. One profile. **97.9% on tsbench at -80% tokens.**
> Structural code navigation, persistent memory, and Bash output compaction for AI coding agents.

[![Version](https://img.shields.io/badge/version-4.3.0-blue)](https://github.com/Mibayy/token-savior/releases/tag/v4.3.0)
[![PyPI](https://img.shields.io/badge/pypi-token--savior--recall-orange)](https://pypi.org/project/token-savior-recall/)
[![Tests](https://img.shields.io/badge/tests-1688%2F1688-brightgreen)]()
[![Benchmark](https://img.shields.io/badge/tsbench-97.9%25%20(188%2F192)-brightgreen)](https://mibayy.github.io/token-savior/)
[![Python 3.11+](https://img.shields.io/badge/python-3.11+-blue.svg)](https://www.python.org/downloads/)
[![MCP](https://img.shields.io/badge/MCP-compatible-purple.svg)](https://modelcontextprotocol.io)
[![CI](https://github.com/Mibayy/token-savior/actions/workflows/ci.yml/badge.svg)](https://github.com/Mibayy/token-savior/actions/workflows/ci.yml)

**[mibayy.github.io/token-savior](https://mibayy.github.io/token-savior/)** -- project site + benchmark landing
**[github.com/Mibayy/tsbench](https://github.com/Mibayy/tsbench)** -- benchmark source + fixtures

---

### Benchmark -- 96 real coding tasks (Claude Opus 4.7, May 2026)

| | Plain Claude Code | With Token Savior |
|---|---:|---:|
| **Score** | 141 / 180 (78.3%) | **188 / 192 (97.9%)** |
| **Active tokens / task** | 17 221 | **3 395** (-80%) |
| **Wall time / task** | 110.6 s | **18.9 s** (-83%) |

Reproduces with the `optimized` profile (single env var). See [BENCHMARK-SUMMARY](https://github.com/Mibayy/tsbench/blob/main/BENCHMARK-SUMMARY.md).

</div>

---

## What's new

### v4.3.0 -- bench-driven coverage push (May 2026)

Real-world bench against 7 days of transcripts (1130 Bash outputs) drove
this release. Cumulative savings now sit at **~20.4 K tokens/week**
(19.3% match rate, 68.9% mean compaction) vs ~12 K/week on v4.2.0.

- Fixed `pytest` regex: now matches `python3 -m pytest`, `uv run pytest`,
  venv-prefixed forms, `poetry/hatch/pdm/rye run pytest`.
- 5 more git compactors: `fetch`, `checkout`, `branch`, `worktree list`,
  `stash list`.
- 4 more gh compactors: `gh repo view`, `gh pr view`, `gh issue view`,
  `gh pr diff`.
- `grep`, `find`, `cat` compactors. Group hits by file, strip common
  prefix, head/tail truncation. 83-96% savings on the fixtures.
- Compound command splitter: `cd /root/foo && git status` now compacts
  by picking the last meaningful segment of `&&`/`;` chains. Bails on
  subshells, heredocs, pipes, loops, unterminated quotes.

### v4.2.0 -- coverage + hybrid mode + ts init

- 12 more compactors: `jest`, `vitest`, `eslint`, `biome`, `kubectl
  get/logs`, `aws sts/ec2/lambda/logs/iam/dynamodb/s3`, `npm/yarn/pnpm
  list`, `pip list/show`, `curl`. Peaks: 91.7% on `aws ec2`, 95% on
  `jest` all-green.
- Hybrid sandbox+compact mode. When a compactor matches but the compact
  text is still bulky (> 4 KB), the hook emits the compact preview AND
  sandboxes the full original. The agent can pull it via `capture_get`
  if it needs the detail.
- `ts init --agent {claude,cursor,gemini,codex}` CLI. Detects agent
  settings, deep-merges the hook config, dedups by `(matcher, command)`,
  prints a unified diff, backs up `settings.json`, idempotent on re-run.
- `ts_discover` cross-project + `format="adoption"` reports TS-vs-native
  ratios per session with first/second-half trend.

### v4.1.0 -- RTK-inspired Bash compaction + discover

- 14 Bash output compactors in a PostToolUse hook: `git status/diff/log/
  push/commit/add`, `pytest`, `cargo test/build/clippy`, `tsc`, `docker
  ps/logs`, `gh run list/view`. Median 63%, peak 100% (a green `pytest
  -q` collapses to one line).
- PreToolUse Bash rewriter. Bare commands get denser variants before
  execution: `git status` -> `--porcelain=v2 --branch`, `tsc` ->
  `--pretty false`, `pytest` -> `-q --tb=line`, etc. 10 safe rules,
  guarded against composition operators and explicit verbose flags.
- `get_usage_stats` v2. ASCII sparkline (30 d), daily breakdown table
  (7 d), top-tools cumulative, `format="json"`.
- New MCP tool `ts_discover`. Scans `~/.claude/projects/*/*.jsonl`
  transcripts and flags missed TS opportunities (Read->Grep->Read chains,
  sequential `find_symbol`, edits without `get_edit_context`,
  `memory_search` without prior `memory_index`, native shell on code
  files). 30-day scan in ~2.5 s on a 343 MB transcript dir.

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

That's it. **`TOKEN_SAVIOR_PROFILE=optimized`** ships the Pareto-optimum
config that wins tsbench. It bundles:

- `tiny_plus` (15 hot tools manifest)
- thin inputSchema (-44% manifest)
- capture sandbox disabled
- memory hooks gated for cross-project safety

No other tuning needed.

---

## Activation (Bash compaction + rewriting)

Bash compaction and the PreToolUse rewriter are opt-in. Two env vars and
one CLI call:

```bash
export TS_BASH_COMPACT=1       # PostToolUse output compactors (34 of them)
export TS_BASH_REWRITE=1       # PreToolUse command rewriter (10 rules)

ts init --agent claude --yes   # auto-merge hooks into ~/.claude/settings.json
```

`ts init` is idempotent. It detects existing hook entries, dedups by
`(matcher, command)`, prints a unified diff, and backs up `settings.json`
to `.bak-YYYYMMDD-HHMMSS` (UTC) before writing. Supported agents:
`claude`, `cursor`, `gemini`, `codex`. Pass `--dry-run` to preview, or
`--global` to write the user-level config.

Optional audit log of every rewrite:

```bash
export TS_BASH_REWRITE_LOG=$HOME/.local/state/token-savior/rewrites.jsonl
```

---

## Compactor catalog (34)

| Family | Compactors |
|---|---|
| git | `status`, `diff`, `log`, `push`/`pull`, `commit`, `add`, `fetch`, `checkout`, `branch`, `worktree list`, `stash list` |
| gh | `run list`, `run view`, `pr diff`, `pr view`, `issue view`, `repo view` |
| test/lint | `pytest`, `jest`, `vitest`, `eslint`, `biome`, `cargo test`, `cargo build`/`clippy`, `tsc` |
| cloud | `kubectl get`, `kubectl logs`, `aws sts`, `aws ec2`, `aws lambda`, `aws logs`, `aws iam`, `aws dynamodb`, `aws s3` |
| docker | `docker ps`, `docker logs` |
| packaging | `npm/yarn/pnpm list`, `pip list`/`show` |
| shell catch-alls | `grep`, `find`, `cat`, `curl` |

Each compactor is a pure function (no I/O, no globals) returning a
token-efficient rendering. The dispatcher returns `None` when no matcher
fires, leaving the existing sandbox path untouched. Compound commands
(`cd ... && cmd`) fall through to the last meaningful segment.

---

## `ts_discover` -- find missed TS opportunities

New MCP tool that scans your Claude Code transcripts for patterns where
TS tools would have been cheaper than what the agent actually did.

```python
ts_discover()                       # active project, last 30 days
ts_discover(project=None)           # ALL transcript projects
ts_discover(format="adoption")      # TS vs native ratio per session
ts_discover(format="adoption_json") # same, JSON
```

Findings: Read->Grep->Read chains, sequential `find_symbol`, edits
without `get_edit_context`, `memory_search` without `memory_index`,
native shell on code files. Args are pruned to load-bearing keys
(PII-safe). Streams JSONL with mtime fast-skip.

---

## `ts init` CLI

```bash
ts init --agent claude [--global] [--dry-run] [--yes]
ts init --agent cursor
ts init --agent gemini
ts init --agent codex
```

Detects the target agent's settings location, deep-merges the Token
Savior hook config (`PostToolUse` + `PreToolUse`), preserves existing
hooks, dedups, prints a unified diff. Backs up to
`settings.json.bak-YYYYMMDD-HHMMSS` (UTC). Re-running is a no-op.

---

## What it does

Claude Code reads whole files to answer questions about three lines, and
forgets everything the moment a session ends. Token Savior fixes both,
plus a third axis: it now compacts the noisy Bash output that bloats
turn budgets between code reads.

It indexes your codebase by symbol -- functions, classes, imports, call
graph -- so the model navigates by pointer instead of by `cat`. Measured
reduction: 97% fewer chars injected across 170+ real sessions.

On top of that sits a persistent memory engine. Every decision, bugfix,
convention, guardrail and session rollup is stored in SQLite WAL + FTS5
+ vector embeddings, ranked by Bayesian validity and ROI, and
re-injected as a compact delta at the start of the next session.

And on top of *that*, since v4.1, sit the Bash compactors and the
PreToolUse rewriter. Bench numbers above.

---

## Profile comparison

| Profile | Tools exposed | Manifest tokens | When to use |
|---|---:|---:|---|
| **`optimized`** | **15** | **~1.5 KT** | **Recommended default -- Pareto win on tsbench** |
| `auto` | adaptive | ~1-2 KT | Per-client telemetry-based (experimental) |
| `tiny` | 6 | ~0.6 KT | Minimal hot loop |
| `lean` | 51 | ~4 KT | Legacy -- broader surface |
| `full` | 68 | ~6 KT | Everything exposed |

You probably want `optimized`.

---

## Token savings

| Operation | Plain Claude | Token Savior | Reduction |
|-----------|-------------:|-------------:|----------:|
| `find_symbol("send_message")` | 41M chars (full read) | 67 chars | **-99.9%** |
| `get_function_source("compile")` | grep + cat chain | 4.5K chars | direct |
| `get_change_impact("LLMClient")` | impossible | 16K chars | new capability |
| 96-task tsbench (Opus, plain vs ts) | 17 221 active/task | **3 395 active/task** | **-80%** |
| 7-day Bash output bench (v4.3) | ~30 K tokens/week | ~9.6 K tokens/week | **~20.4 K/week** |

---

## Install

### pip (MCP server)

```bash
pip install "token-savior-recall[mcp]"
# Optional hybrid vector search:
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

Suite size: **1688 passed, 55 skipped** on main. CI green on Python
3.11 / 3.12 / 3.13.

---

## Bench it yourself

The compactor numbers above come from replaying real Claude Code
transcripts through the dispatcher. Two scripts live under `scripts/`:

```bash
python3 scripts/bench_compactors_real.py       # match rate + mean savings
python3 scripts/bench_compactors_unmatched.py  # top unmatched commands
```

The first walks `~/.claude/projects/*/*.jsonl`, replays every Bash
output through the registry, and reports per-family savings + overall
match rate. The second buckets the unmatched commands so the next
compactor target is obvious from the histogram.

To reproduce the tsbench score:

```bash
git clone https://github.com/Mibayy/tsbench && cd tsbench
python3 generate.py --seed 42
git tag v1
python3 breaking_changes.py
git tag v2
TS_PROFILE=tiny_plus TS_CAPTURE_DISABLED=1 python3 bench.py --tasks all --run B
```

---

## Bonus: `ts` CLI for non-MCP agents

For agents without MCP (Cursor, Aider, Continue, scripts, CI), the `ts`
command exposes a subset of the tools via shell:

```bash
ts use /path/to/project
ts get my_function          # JSON output
ts search 'pattern'
ts daemon start             # ~145ms per call vs 1.5s cold fork
ts init --agent cursor      # wire up Bash hooks for non-Claude agents
```

On Claude Code, prefer the MCP server -- measured cheaper than CLI on
Opus 4.7. The CLI is there for the portability case.

---

## Optional env vars

| Var | Purpose |
|---|---|
| `TS_BASH_COMPACT=1` | Enable PostToolUse Bash output compactors |
| `TS_BASH_REWRITE=1` | Enable PreToolUse Bash command rewriter |
| `TS_BASH_REWRITE_LOG` | JSONL audit log of every rewrite |
| `TS_COMPACT_INLINE_THRESHOLD` | Hybrid mode threshold (default 4 KB) |
| `TS_COMPACT_TINY_THRESHOLD` | Skip-sandbox threshold (default 256 B) |
| `TELEGRAM_BOT_TOKEN` + `TELEGRAM_CHAT_ID` | Critical-observation feed |
| `TS_VIEWER_PORT` | Web viewer dashboard |
| `TS_AUTO_EXTRACT=1` + `TS_API_KEY` | LLM auto-extraction of memory observations |
| `TS_CAPTURE_DISABLED=1` | Skip read-side capture sandboxing (default in `optimized`) |
| `TS_MEMORY_DISABLE=1` | Silence memory hooks (clean-context workloads) |

---

## License

MIT
