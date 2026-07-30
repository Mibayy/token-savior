<!-- mcp-name: io.github.Mibayy/token-savior -->

<div align="center">

# Token Savior

> One MCP server. One profile. **97.9% on tsbench at -80% tokens.**
> Structural code navigation, persistent memory, and Bash output compaction for AI coding agents.

[![PyPI](https://img.shields.io/pypi/v/token-savior-recall?color=orange&label=pypi)](https://pypi.org/project/token-savior-recall/)
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

## Who starred this repo?

[![starscope](https://starscope.duckdns.org/badge/Mibayy/token-savior.svg)](https://starscope.duckdns.org/r/_Na5VtAKXt-H)

On June 30, 2026 GitHub restricted stargazer and watcher lists to repo admins and
collaborators, which broke every "who starred my repo" tool at once. I rebuilt one
that still works, precisely because it only reads repos you own or can push to:
**[starscope](https://starscope.duckdns.org)** ranks the people who starred or
forked your repo by influence, and surfaces their social accounts when their
GitHub profile declares them.

Numbers on this very repo, computed with it: 1,147 people, 27% with a public
social account, and the most followed carries 18,922 followers. The named list is
visible to the repo owner and to nobody else — the [public
page](https://starscope.duckdns.org/r/_Na5VtAKXt-H) shows aggregates only.

---

## What's new

Release notes live where they can't drift out of sync with the code:

- [CHANGELOG.md](CHANGELOG.md) for the full history
- [Releases](https://github.com/Mibayy/token-savior/releases) for the tagged builds
- [PyPI](https://pypi.org/project/token-savior-recall/) for what `pip` will actually give you

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
`claude`, `cursor`, `gemini`, `codex`, `openclaw`. Pass `--dry-run` to preview, or
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
ts init --agent openclaw
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
| **`compact-only`** | **1** | **~0.3 KT** | **Bash compaction only -- you already run symbol nav elsewhere** |
| `full` | 68 | ~6 KT | Everything exposed |

You probably want `optimized`.

---

## How it composes with adjacent tools

Token Savior spans several layers, and most neighbouring tools occupy exactly
one of them. Overlap is opt-out per layer, so running both is usually fine once
you disable the half you already have. Thanks to @chirag127 for mapping this
out in #45.

| Tool | Layer | Overlap | What to do |
|---|---|---|---|
| [RTK](https://github.com/paths-technology/rtk) | PostToolUse Bash output compression | Direct, with the 34 compactors | Pick one. `TS_BASH_COMPACT=0` to defer to RTK |
| [serena](https://github.com/oraios/serena) | Symbol-graph navigation | Direct, with `find_symbol` / `get_dependents` | Run TS as `compact-only` if serena is your navigator |
| codebase-memory | Persistent code graph | With the memory engine | `TS_MEMORY_DISABLE=1` |
| Ponytail, Caveman | Output-side compression (code and prose) | Partial, output side only | Complementary, no knob needed |

The layers Token Savior owns that these do not: the PreToolUse Bash **rewriter**
(it shrinks the command before it runs, not the output after), structural
**editing** that keeps the index in sync, and the audit tools
(`detect_breaking_changes`, `find_dead_code`, `analyze_config`).

If you only want the Bash layer, `TOKEN_SAVIOR_PROFILE=compact-only` advertises
a single tool and leaves the compactors and rewriter running.

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

Suite size: **1898 passed, 2 skipped** on main. CI green on Python
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

## Environment variables

All optional. Values shown as `=1` also accept nothing else — set exactly `1`;
values shown as *bool* accept `1`/`true`/`yes` (and `on` where noted).

### Server & tool manifest

| Var | Default | Purpose |
|---|---|---|
| `WORKSPACE_ROOTS` | current dir | Comma-separated project roots to index. Codex trap: Codex whitelist-filters the MCP server environment, so an exported shell variable never arrives — set it in `config.toml` under `[mcp_servers.token-savior] env` (or `env_vars`) |
| `PROJECT_ROOT` | — | Single-root alternative to `WORKSPACE_ROOTS` |
| `CLAUDE_PROJECT_ROOT` | — | Deliberate active-project override (Token Savior's own contract — no host sets it). Registered if valid, wins over every other boot signal |
| `TS_STICKY_ACTIVE` | off (*bool*, `on` ok) | Freeze the active project: explicit `project=` hints and absolute path arguments still route each call, but no call repoints the shared default. For parallel agents in sibling worktrees |
| `TOKEN_SAVIOR_PROFILE` | `full` | Tool profile. `optimized` — the value the quickstart config and `ts init` recommend — ships the Pareto manifest, implies thin schemas, and omits the capture tools from the manifest |
| `TS_THIN_SCHEMAS=1` | off (on in `optimized`) | Strip verbose tool schemas from the manifest |
| `TS_AUTO_HOT_K` | `10` | Hot-tool count exposed by the telemetry-driven `auto` profile |
| `TOKEN_SAVIOR_CHAIN_NUDGE` | on | `0`/`false`/`off` disables chained-tool nudges |
| `TS_MEMORY_DISABLE=1` | off | Disable the memory engine (clean-context workloads) |
| `TS_CAPTURE_DISABLED=1` | off | Skip read-side capture sandboxing and drop the capture tools from the manifest (no profile flips this; `optimized` only hides the capture tools) |
| `TS_CODE_MODE_DISABLE=1` | off | Disable code-mode tools |
| `TS_CODE_MODE_NODE` | `node` | Node binary used by the code-mode sandbox |
| `TS_RESOURCES_DISABLED` | off (*bool*) | Don't expose observations as `ts://obs/{id}` MCP resources |
| `TS_WARM_START` | off (*bool*) | Pre-build project slots at startup |
| `TOKEN_SAVIOR_NO_WARMUP` | off (*bool*) | Skip the `ts_search` embedding warm-up |
| `TS_SEARCH_COLD_DELEGATE` | off (*bool*, `on` ok) | Delegate the cold `ts_search` call to a running `ts` daemon |
| `TS_SOCK` | `/tmp/ts.sock` | Unix socket of the `ts` daemon (CLI + cold delegate) |
| `TOKEN_SAVIOR_CLIENT` | auto-detected | Client label (`claude-code`, …) for telemetry/client detection |
| `TOKEN_SAVIOR_SESSION_LABEL` | — | Free-form label attached to session telemetry |

### Indexing

| Var | Default | Purpose |
|---|---|---|
| `INCLUDE_PATTERNS` | built-in list | Colon-separated globs; **replaces** the default include list |
| `EXCLUDE_PATTERNS` | built-in list | Colon-separated globs; **replaces** the default exclude list |
| `EXCLUDE_EXTRA` | — | Colon-separated globs **appended** to the default excludes |
| `TOKEN_SAVIOR_EXCLUDE_PATTERNS` | — | Colon-separated globs appended at the indexer level |
| `TOKEN_SAVIOR_MAX_FILE_SIZE` | `500000` | Max file size (bytes) to index |
| `TOKEN_SAVIOR_MAX_FILES` | `10000` | Max files per project |
| `TOKEN_SAVIOR_WATCHER` | `auto` | File watcher: `auto` / `on` / `off` |
| `TS_WATCHER_FORCE_POLLING` | off | Force the polling watcher backend |

### Claude Code hooks

| Var | Default | Purpose |
|---|---|---|
| `TS_CAPTURE_THRESHOLD_BYTES` | `4096` | Minimum tool-output size to sandbox |
| `TS_CAPTURE_REPLACE=1` | off | Strong-replace: tell the agent to ignore the inline output and `capture_get` the URI |
| `TS_CAPTURE_TTL_DAYS` | `30` | Captures older than this are purged on the next `capture_put`; `0` disables the GC |
| `TS_BASH_COMPACT=1` | off | Enable PostToolUse Bash output compactors |
| `TS_COMPACT_INLINE_THRESHOLD` | `4096` | Hybrid mode: compact-result size above which the full original is also sandboxed |
| `TS_COMPACT_TINY_THRESHOLD` | `256` | Hybrid mode: compact-result size below which the sandbox is always skipped |
| `TS_BASH_REWRITE=1` | off | Enable the PreToolUse Bash command rewriter |
| `TS_BASH_REWRITE_LOG` | — | JSONL audit log of every rewrite |
| `TS_HOOK_MINIMAL=1` | off | SessionStart memory hook emits only the Memory Index block |

### Memory extras

| Var | Default | Purpose |
|---|---|---|
| `TS_VIEWER_PORT` | off | Port for the observation web viewer (unset = disabled) |
| `TS_AUTO_EXTRACT=1` + `TS_API_KEY` | off | LLM auto-extraction of memory observations (Anthropic API key required) |
| `TS_MODEL` | `claude-sonnet-4-6` | Auto-extraction model override |
| `TOKEN_SAVIOR_MEMORY_AUTO_SAVE=1` | off | Auto-save memory observations |
| `TELEGRAM_BOT_TOKEN` + `TELEGRAM_CHAT_ID` | — | Critical-observation feed |

### Storage, dashboard, debugging

| Var | Default | Purpose |
|---|---|---|
| `TOKEN_SAVIOR_STATS_DIR` | `~/.local/share/token-savior` | Telemetry + stats directory |
| `TOKEN_SAVIOR_DASHBOARD_HOST` | `127.0.0.1` | Dashboard bind host |
| `TOKEN_SAVIOR_DASHBOARD_PORT` | `8921` | Dashboard port |
| `TOKEN_SAVIOR_INCLUDE_TMP_PROJECTS` | off (*bool*) | Dashboard also lists projects under temp dirs |
| `TOKEN_SAVIOR_DEBUG=1` | off | Debug logging |
| `TOKEN_SAVIOR_TRACE` | off (*bool*) | MCP request lifecycle tracing |

Not knobs: `CLAUDECODE`, `CLAUDE_CODE_ENTRYPOINT`, `CLAUDE_PROJECT_DIR`,
`CLAUDE_CONTEXT_REMAINING_PCT`, `CODEX_*` and `HERMES_*` are read for host/client
detection and boot-time project hints — the environment sets them, you don't.
(`CLAUDE_PROJECT_DIR` is what Claude Code actually exports; it always names the
main checkout, never the worktree the session works in, so the launch directory
outranks it when that directory is a linked worktree. `CLAUDE_PROJECT_ROOT`
moved up into the knobs table: nothing sets it but you.)

Naming trap: `TS_PROFILE` in the benchmark snippets is **tsbench's** variable;
the server reads `TOKEN_SAVIOR_PROFILE`.

---

## License

MIT
