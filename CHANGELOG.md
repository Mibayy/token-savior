# Changelog

## v4.6.0 — ts_search cold-start bridge via the warm daemon (2026-07-04)

Delivers the follow-up flagged in v4.5.0: the in-process Nomic model load costs
~5s on a fresh stdio spawn (audit: ts_search p50 5723ms). v4.5.0 removed the
tool-description re-embed; this closes the remaining half -- the query
embedding.

**Cold-start delegation (server.py + daemon_client.py + cli.py).** When
`TS_SEARCH_COLD_DELEGATE=1` and the in-process model is still cold, the first
`ts_search` is delegated over the Unix socket to a running `ts _daemon-serve`,
which keeps the Nomic model warm across sessions (measured ~130ms warm vs
~5700ms in-process cold). The startup warm-up thread keeps loading locally, so
subsequent calls run in-process. Any daemon failure (no socket, timeout, error)
falls through to the unchanged local path -- opt-in and safe by default (most
installs have no daemon).

- `daemon_client.call_daemon()`: minimal length-prefixed-JSON socket client,
  best-effort (returns None on any failure).
- `cli._daemon_serve`: the daemon's `call` handler now routes `ts_search`
  through `_handle_ts_search` (it is special-cased in `call_tool`, not a
  regular dispatched tool, so `_dispatch_tool` returned "unknown tool").

Tests: test_daemon_client.py (real Unix-socket server), test_ts_search_cold_delegate.py
(delegate/fallback matrix). Suite: 1779 passed.

## v4.5.0 — Adoption-gap pass driven by 5.5-week usage audit (2026-07-04)

Audit of ~7 weeks of real usage (tool-calls.json + memory.db `tool_latency`
1414 rows) surfaced four adoption/latency gaps the v4.4 nudges did not close.

**set_project_root churn (server_handlers/project.py + slot_manager.py).**
Measured 51 `set_project_root` calls in 5.5 weeks (≈ as many as switch_project),
p95 1.8s with one 14.6s outlier; `collector-crypt-scanner` reindexed 20x. Root
cause: the in-memory registry was rebuilt from the static `WORKSPACE_ROOTS` env
on every stdio respawn, so a project registered via set_project_root vanished
next session and got fully rebuilt again. Fixes:
- Registered roots now persist to `<stats>/registered_projects.json`
  (`_persist_registered_root` / `_load_registered_roots`, atomic, best-effort).
- `switch_project` resolves an unknown hint against a real directory path or a
  persisted project by basename (`_resolve_unregistered`), registering it
  cache-aware -- the agent no longer needs set_project_root across sessions.
- `set_project_root` is now cache-aware: the non-force path uses `ensure()`
  (reuses the on-disk index when the git ref matches) instead of the
  unconditional `build()` that paid the 14.6s rebuild every session.
  `force=true` still does a full rebuild.

**get_edit_context nudge (server.py, chain-nudge pattern 3).** Audit: 0
`get_edit_context` calls across ~199 edits (replace_symbol_source 156 +
add_field_to_model 28 + insert_near_symbol 15). Editing a symbol without the
pre-edit bundle now prepends a `[NUDGE]` pointing at get_edit_context.

**ts_execute nudge (server.py, chain-nudge pattern 4).** Audit: ts_execute
used only 41x despite thousands of unitary nav calls. When 5 individual
navigation calls land in one 60s window, a `[NUDGE]` suggests folding them into
one Code Mode script. Fires once at the threshold.

**ts_search cold-start (server_handlers/tool_search.py).** p50 still 5723ms
despite the v4.4 warm-up (the thread loses the race in stdio mode). Tool-
description embeddings now persist to `<stats>/tool_embeddings.json`, keyed by a
content+model signature, so cold start skips re-embedding all ~66 descriptions.
(The Nomic model load for the query stays in-process; routing that through the
warm ts-daemon is a documented follow-up.)

**Hook log noise (hooks/memory-session-stop.sh).** A clean no-observations
close no longer prints to stderr -- it had appended 3578 benign lines to
hook-errors.log.

All 6 changes shipped with tests (test_registered_persistence.py,
test_tool_embed_disk_cache.py, TestEditContextNudge/TestTsExecuteNudge in
test_chain_nudge.py). Suite: 1771 passed.

## v4.4.1 — Chain nudge covers get_function_source -> get_full_context (2026-05-26)

Extend the chain-nudge detector to cover the dominant remaining wasteful
pattern: `get_function_source(X)` / `get_class_source(X)` followed by
`get_full_context(X)` within 60s. 9-day usage data showed **187 occurrences**
(vs 42 for the find-then-read pattern already covered in v4.4.0). The first
read is wasted -- `get_full_context` re-fetches the source as part of its
bundle. Nudge fires at top of payload: "start with get_full_context next time."

Test fixture also snapshots `_tool_call_counts` so chain-nudge tests don't
push the global counter past the navigation-overuse threshold (15) and
contaminate `test_query_api::test_navigation_hints_*` in the full suite.

## v4.4.0 — Chain nudges + ts_search warm-up + set_project_root nudge (2026-05-26)

Driven by an audit of 9 days of usage (2026-05-17..26, 869 tool calls).

**Chain nudges (server.py):** Data showed 42 `find_symbol(X) -> get_function_source(X)`
and 26 `find_symbol(X) -> get_full_context(X)` same-symbol chains within 60s,
plus 258 `search_codebase -> get_function_source` chains. Trailing `_hints`
were ignored. Now when `get_function_source`/`get_class_source`/`get_dependents`/
`get_dependencies` is called on a symbol that was passed to `find_symbol`
within the previous 60s, the response is prepended with a `[NUDGE]` block
suggesting `get_full_context(X)`. Top-of-payload so it survives output
compression. Opt out via `TOKEN_SAVIOR_CHAIN_NUDGE=0`.

**ts_search warm-up (server_handlers/tool_search.py + server.py):** Data
showed `ts_search` avg **4867ms** over 19 calls -- the Nomic cold start +
66 tool description embeddings dominate the first call. New `warm_up_async()`
fires a background thread at server startup so the first client `ts_search`
sees a populated cache. Opt out via `TOKEN_SAVIOR_NO_WARMUP=1`.

**set_project_root nudge (server_handlers/project.py):** When the cheap
path fires (project already registered via `WORKSPACE_ROOTS`), the response
now prepends `[NUDGE] Use switch_project('name') next time` so the agent
self-corrects toward the documented entry point.

## v4.3.3 — Fix MCP `CallToolResult` validation regression (#32) (2026-05-26)

Hotfix for a regression introduced in v3.5.0 with the `_compat.py` shim.
Every successful tool call was returning `isError=True` with five
`CallToolResult` pydantic validation errors. Reported by @zinkovsky in #32.

Root cause: `list_tools()` converted shim `ToolDef` -> `mcp.types.Tool` at
the protocol boundary, but `call_tool()` returned shim `TextContent`
instances unconverted. pydantic v2 rejects the shim on `CallToolResult`
validation (same class name, different class object).

Fix: introduce `_to_mcp_content()` in `server.py` that converts shim
items to real `mcp.types.TextContent` at the boundary. Symmetric with
the `list_tools` conversion. Cold-start cost preserved -- the import
stays lazy (server-only path, never hit by the CLI fork-mode consumers
the shim was built for).

Test gap closed: every prior `call_tool` integration test inspected the
returned list directly, never going through the SDK's pydantic
validation step. New `tests/test_issue_32_mcp_textcontent.py` builds a
real `CallToolResult` from the value `call_tool` returns -- catches any
future shim leak on the success path, the error path, and the meta-tool
paths (`ts_search`, `ts_extended`).

## v4.3.2 — `ts init` next-steps hint (2026-05-19)

After a successful `ts init`, the CLI now prints a short "Next steps"
block listing the env vars to add (`TS_BASH_COMPACT=1`,
`TS_BASH_REWRITE=1`, optional `TOKEN_SAVIOR_PROFILE=optimized`) and a
reminder to restart the agent. Without this hint, new users could end
up with hooks merged but the activation gates still off.

## v4.3.1 — Fix `ts init` after vanilla PyPI install (2026-05-19)

Hotfix. v4.3.0 was broken for users installing from PyPI:

- The bundled hook JSON configs (`hooks/*.json`) had hard-coded paths
  pointing to `/root/token-savior/hooks/...` (the dev machine layout).
- The `hooks/` directory was not included in the wheel at all.

Both fixed:

- `pyproject.toml` -- new `[tool.hatch.build.targets.wheel.force-include]`
  rule packages `hooks/` inside the wheel at `token_savior/hooks/`.
- `hooks/*.json` -- hard-coded paths replaced with the `{{TS_HOOKS_DIR}}`
  placeholder.
- `cli_init/__init__.py` -- on load, substitutes the placeholder with the
  actual install path resolved either from the installed package
  (`site-packages/token_savior/hooks/`) or, in editable installs, from
  the repo root. So `ts init --agent claude` now produces correct paths
  for every install method.

Users on v4.3.0 should `pip install --upgrade token-savior-recall` and
re-run `ts init`.

## v4.3.0 — Bench-driven coverage push (2026-05-19)

Real-world bench against 7 days of Louis's transcripts (1130 Bash outputs)
showed v4.2.0 only matched 11.9% of commands. v4.3.0 closes the gaps
identified by the bench. Full suite: **1688 passed, 55 skipped**.

### New

- **F3a — fix `pytest` regex + git/gh extras.** `PytestCompactor` now
  matches `python3 -m pytest`, `python -m pytest`, venv-prefixed forms,
  and `uv/poetry/hatch/pdm/rye run pytest`. Five new git compactors
  (`fetch`, `checkout`, `branch`, `worktree list`, `stash list`). Four new
  gh compactors (`gh repo view`, `gh pr view`, `gh issue view`,
  `gh pr diff` — last reuses `GitDiffCompactor` internals). Existing
  `GitPushPull`/`GitAdd` matchers narrowed to release `fetch`/`checkout`
  to the dedicated compactors.
- **F3b — `grep` + `find` + `cat` compactors.** GrepCompactor groups
  `file:line:rest` hits by filename, 83% savings on a 100-line fixture.
  FindCompactor strips common prefix + head/tail truncation, 96% on a
  300-file fixture. CatCompactor truncates long file dumps, 92% on a
  500-line fixture. All bail on shell composition (pipes, `&&`, `;`).
- **F3c — compound command splitting.** When a command like
  `cd /root/foo && git status` doesn't match any compactor as-is, the
  dispatcher now calls `pick_meaningful_segment()` and re-runs the
  registry against the last meaningful segment. Bails conservatively
  on subshells, heredocs, pipes, loops, unterminated quotes. Pure
  stdlib state-machine parser.

### Tests

+85 tests across the three feature lines. Full suite **1688 passed**.

### Expected real-world impact

Based on the same 7-day bench window, projected savings should rise from
~12 K tokens/week to ~25 K tokens/week (3-4× v4.2.0 baseline). Re-bench
after a few days of live usage to confirm.

## v4.2.0 — Compactor coverage + hybrid mode + ts init (2026-05-19)

Five parallel feature lines on top of v4.1.0, all green (1603 passed,
55 skipped).

### New

- **F1a — test/lint compactors** (`compactors/{jest,vitest,eslint,biome}.py`).
  Savings 58 % (eslint) to 95 % (jest all-green collapses to one line).
- **F1b — cloud/package compactors** (`compactors/{kubectl,aws,pkg_list,curl}.py`).
  12 new compactors: `kubectl get/logs`, `aws sts/ec2/lambda/logs/iam/dynamodb/s3`,
  `npm/yarn/pnpm list`, `pip list/show`, `curl`. Peaks: 91.7 % `aws ec2`,
  89.1 % `npm list`, 87.9 % `aws lambda`. DynamoDB type-tag unwrap so the
  agent gets plain JSON.
- **F2-hybrid — sandbox+compact dual-mode** (`hooks/tool_capture_hook.py`,
  `compactors/base.py`). When a compactor matches but the compact text is
  still bulky (> `TS_COMPACT_INLINE_THRESHOLD`, default 4 KB), the hook
  emits the compact preview AND sandboxes the full original so the model
  can fetch it via `capture_get` if needed. Small results stay inline-only
  (legacy behavior). Tiny results (≤ `TS_COMPACT_TINY_THRESHOLD`, default
  256 B) skip the sandbox path entirely.
- **F3 — `ts init` CLI** (`src/token_savior/cli_init/`). New subcommand:
  `ts init --agent {claude,cursor,gemini,codex} [--global] [--dry-run]
  [--yes]`. Detects agent settings, deep-merges the hook config,
  preserves existing hooks, dedups by `(matcher, command)`, prints a
  unified diff, backs up `settings.json` to `.bak-YYYYMMDD-HHMMSS` (UTC),
  idempotent on re-run.
- **F4-all — `ts_discover` cross-project + adoption mode** (`discover/`,
  `server_handlers/discover.py`, `tool_schemas.py`). Semantic change:
  `project=None` now means "scan ALL transcript projects" (was: active
  only). Each Finding gains `top_projects: dict[str,int]`. New
  `format="adoption"` / `"adoption_json"` reports TS vs native ratios
  per session, overall, with first-half/second-half trend and the 5
  worst-ratio sessions.

### Tests

+75 new tests across the five features. Full suite: **1603 passed**.

## v4.1.0 — RTK-inspired Bash compaction + discover (2026-05-19)

Four parallel feature lines, all green (1528 passed, 55 skipped):

### New

- **F1 — Bash output compactors** (`src/token_savior/compactors/`). 14
  compactors for `git status/diff/log/push/commit/add`, `pytest`, `cargo
  test/build/clippy`, `tsc`, `docker ps/logs`, `gh run list/view`.
  Median savings 63 %, peak 100 % (`pytest -q` all-pass collapses to one
  line). Wired into the existing `tool_capture` PostToolUse hook behind
  `TS_BASH_COMPACT=1` (default off, no impact on existing users).
- **F2 — PreToolUse Bash command rewriter** (`hooks/bash_rewriter_hook.py`,
  `src/token_savior/bash_rewriter/`). Rewrites bare commands into denser
  variants before execution: `git status` → `--porcelain=v2 --branch`,
  `tsc` → `--pretty false`, `pytest` → `-q --tb=line`, etc. 10 safe rules,
  guarded against composition operators and explicit verbose flags.
  Gated on `TS_BASH_REWRITE=1`. Optional audit log via
  `TS_BASH_REWRITE_LOG=/path/to/log.jsonl`.
- **F3 — `get_usage_stats` v2** (`src/token_savior/server_handlers/stats.py`,
  `stats_render.py`). ASCII sparkline (30 d), daily breakdown table (7 d),
  top-tools cumulative (proportional attribution), session-vs-previous
  delta. New kwargs `days`, `daily`, `format` (`text` / `json`). Backward
  compat preserved.
- **F4 — `ts_discover`** (`src/token_savior/server_handlers/discover.py`,
  `src/token_savior/discover/`). New MCP tool that scans
  `~/.claude/projects/*/*.jsonl` transcripts for missed TS opportunities:
  Read→Grep→Read chains, sequential `find_symbol`, edit without
  `get_edit_context`, `memory_search` without prior `memory_index`,
  native shell on code files. Streams JSONL, mtime fast-skip, args pruned
  to load-bearing keys (PII-safe). 30-day scan in ~2.5 s on a 343 MB
  transcript dir.

### Tests

+105 new tests across the four features. Full suite 1528 passed.

## v3.0.0 — PyPI catch-up release (2026-04-30)

First PyPI release since v2.6.0 (2026-04-20). Bundles every accumulated
change from v2.7.0 through today onto the index. PyPI users on
`pip install token-savior-recall` jumping from v2.6.0 will see:

### Highlights since v2.6.0

- **Bench-driven optimization passes (v2.7.0 / v2.7.1)** — 14
  description/manifest tweaks; mean −13 % active tokens.
- **Audit & telemetry (v2.8.0)** — `audit_file`, watcher, telemetry
  groundwork.
- **Stability (v2.8.1 → v2.8.4)** — USE WHEN / NOT WHEN tool
  descriptions, root-level `_matches_include_patterns` fix,
  fail-loud memory hooks.
- **Defer-loading via `ts_search` + tiny / tiny_plus profiles
  (v2.9.0)** — embedding-based tool routing for thin manifests
  (~1.6 KT for `tiny_plus`, ~85 % manifest cost cut vs `lean`).
- **`get_feature_files` + v3 ergonomics groundwork.**

### New in v3.0.0 itself

- **Issue #26 — Java indexing resilience.** `_annotate_file` and
  `reindex_file` now wrap the dispatcher's `annotate(...)` call in
  an explicit `Exception` handler so a single bad file (parse glitch,
  encoding edge case, missing tree-sitter binding) is logged and
  skipped instead of poisoning the whole index. Adds `TestJavaProject`
  (default `include_patterns` end-to-end) and `TestAnnotatorResilience`
  regression coverage.
- **Issue #27 — MCP request lifecycle logging.** Opt-in
  `TOKEN_SAVIOR_TRACE=1` emits `-> call <name>` /
  `<- ok / err <name> (Nms)` on every `call_tool` invocation, plus
  three startup checkpoints (migrations, stdio open, server.run loop
  entered). Default behaviour unchanged. Helps localise the Windows
  `AbortError` class of issues by giving operators concrete request
  boundaries in stderr.
- **Test-suite bookkeeping.** `test_tool_count` and
  `test_nav_profile_is_subset_of_core` updated for the v2.9 `ts_search`
  addition (66 → 67 tools, `ts_search` legitimately exposed under
  `nav`).

### Compatibility

No deprecations or removals. Drop-in upgrade from any v2.x — including
the v2.6.0 snapshot still on PyPI before this release.

---

## v2.9.0 — Defer-loading via ts_search + capture/hints gating (2026-04-26)

Three additive optimizations targeting agent-side token cost. All changes
are opt-in via env var or new profile; default behavior is unchanged.

### `ts_search` defer-loading router (new tool)

Embedding-based tool routing for thin manifests. The agent passes a
natural-language query and gets the top-K Token Savior tools back —
including each one's full `inputSchema`, ready for the next turn.

```python
ts_search(query="find dependents of update_user", top_k=5)
# → {"matched_tools": [{"name": "get_dependents", "score": 0.68, ...}, ...]}
```

Implementation: cosine similarity over Nomic 768d embeddings of every
TOOL_SCHEMAS entry, computed once and cached in process memory (~200 KB).
Falls back to substring overlap if `VECTOR_SEARCH_AVAILABLE=False`. The
candidate pool is restricted to currently-visible tools, so a `tiny`
session can reach back into the ~60 hidden tools without breaking
profile/env-var gating.

Mirrors the [Tool Attention paper](https://arxiv.org/html/2604.21816v1)
(47.3k → 2.4k tokens / turn at 120 tools, −95 % prefix).

### New profile: `tiny`

```
TOKEN_SAVIOR_PROFILE=tiny → 6 tools advertised, ~1 090 tokens manifest
```

Exposes only `switch_project`, `find_symbol`, `get_function_source`,
`get_full_context`, `search_codebase`, `ts_search`. Other 60+ tools are
reachable just-in-time via `ts_search`. Adds 1 round-trip per turn for
non-hot tool usage but cuts the manifest cost ~85 % vs `lean`.

### New profile: `tiny_plus`

```
TOKEN_SAVIOR_PROFILE=tiny_plus → 10 tools advertised, ~1 592 tokens manifest
```

`tiny` + 4 tools that the 26/04 bench showed agents abandon when missing
(`find_dead_code`, `get_call_chain`, `analyze_config`, `get_git_status`).
Closes the score gap of `tiny` (91.7 % → 97.2 % on tsbench-90) while
keeping the manifest under 2 K tokens.

Bench tsbench-90 with Opus 4.7 / Claude Code 2.1.119:

| Profile     | Tools | Manifest | Score   | Active mean | Δ vs lean  |
|-------------|------:|---------:|---------|------------:|-----------:|
| `tiny`      |     6 |   1.1 KT | 91.7 %  |       3 805 | -57 % active, -8.3 pp score |
| `tiny_plus` |    10 |   1.6 KT | 97.2 %  |       6 550 | -27 % active, -2.8 pp score |
| `ultra`     |    33 |   4.6 KT | 98.3 %  |      10 260 | +15 % active, -1.7 pp score |
| `lean`      |    52 |   7.1 KT | 99.4 %  |      11 302 |  baseline (current degraded) |

### `TS_CAPTURE_DISABLED=1` now gates the manifest too

Previously the env var only short-circuited the PostToolUse hook. The
agent still discovered `capture_get` / `capture_search` / `capture_*` in
the manifest and burned turns calling them on an empty sandbox table.

Now the server drops all 6 capture tools from `tools/list` when the env
var is set. Measured impact: the regression from 11 070 → 15 915 active
mean tokens observed on 2026-04-26 morning (TASK-039 alone went from
9 913 → 56 479) is fully recovered.

### `TS_NO_HINTS=1` suppresses `_hints` / `_suggestion` blocks

Six injection sites in `code_nav.py` (all empty-result fallbacks plus
the next-step routing hints attached to `find_symbol` / `get_functions`
/ `get_classes`) become no-ops. Saves 30–50 tokens per tool result.
On a 96-task tsbench run with avg 2.5 tool calls/task, that's ~7-12 KB
cumulative cache_creation.

### Empirical impact (tsbench, 90 tasks, Claude Opus 4.7)

| Configuration                                          | Active mean | Score  |
| ------------------------------------------------------ | ----------: | :----: |
| Plain agent (Read/Grep/Bash, baseline)                 |     17 221  | 78.3 % |
| `lean` profile (default since v2.9)                    |      8 928  | 100 %  |
| `lean` + `TS_*_DISABLE` + `TS_NO_HINTS`                |     ~5 500  | 100 %  |
| `tiny` + `TS_*_DISABLE` (defer-loading via ts_search)  |   *TBD*     | *TBD*  |

### Internal

- `src/token_savior/server_handlers/tool_search.py` (new, 140 lines)
- `src/token_savior/server.py`: `ts_search` dispatch, `_TINY_INCLUDES`,
  `_CAPTURE_GATED` filter
- `src/token_savior/server_handlers/code_nav.py`: `_HINTS_DISABLED`
  guard at 6 injection sites
- `src/token_savior/tool_schemas.py`: `ts_search` schema entry

## v2.8.4 — Fail-loud on memory-hook errors (closes #15) (2026-04-23)

Non-breaking. The 6 memory hooks (`hooks/memory-*.sh`) used to pipe
every Python and `claude -p` sub-shell stderr through `2>/dev/null`,
swallowing real failures (missing venv, broken migration, corrupt DB,
typo in payload parser). A user updating token-savior and forgetting
to run `memory_db.run_migrations()` would see memory injection silently
die for weeks.

Changes:

- All 6 hooks gain an `ERR_LOG` variable pointing at
  `${XDG_STATE_HOME:-$HOME/.local/state}/token-savior/hook-errors.log`.
  Directory auto-created. Log self-rotates at 2 MB (truncates to last
  1 MB) so it can't fill the disk.
- `2>/dev/null` replaced with `2>>"$ERR_LOG"` on **32 of 33**
  Python / `claude -p` sub-shell sites. Remaining site is a legitimate
  `cat "$FLAG" 2>/dev/null || echo 0` first-run-missing fallback — kept.
- Hooks still `exit 0` — a failing sub-shell cannot block Claude Code.

Triage tip: after updating, `tail -f ~/.local/state/token-savior/hook-errors.log`
surfaces import errors, missing migrations, or a broken interpreter
path within seconds of the first hook firing.

1381 tests pass.

Closes [#15](https://github.com/Mibayy/token-savior/issues/15).

## v2.8.3 — Migration docs aligned with empirical measurements (2026-04-23)

Non-breaking docs patch. `docs/migration/v3.md` was written before the
description rewrite of v2.8.1 shifted the manifest tokenization.
Updated with empirical numbers (`full` ~16 000 t, `lean` ~11 700 t,
`ultra` ~3 900 t) and the post-spike-1 `lean` tool count (61, not 58).

Also adds the "Quick rollback" block at the top of the migration guide
and clarifies why `memory_save` and the
`discover_project_actions` / `run_project_action` pair are kept in
`lean` despite being atypical relative to the pure call-volume cut.

No code changes; docs only.

## v2.8.2 — Fix `_matches_include_patterns` on root-level files (2026-04-23)

Non-breaking bug fix surfaced during v2.8.1 validation on hermes-agent
(1704 files). A file created at project root (e.g. `foo.py`) was being
silently filtered out of incremental updates because Python's
`fnmatch` treats `**` as a single `*` (no globstar), so the default
`**/*.py` include pattern doesn't match a bare `foo.py`. The watcher
(B3) fires the add event correctly, but `maybe_update` then drops it
before calling `reindex_file`.

Fix: `_matches_include_patterns` in `slot_manager.py` now also tries
each `**/`-prefixed pattern with the `**/` stripped. Root-level files
matching the bare form now pass through.

Bug pre-dates v2.8.0 — same filter was used by the git-detected
incremental update path since forever. Only became visible after B3
made "new file at root" a common scenario.

1381 tests pass.

## v2.8.1 — Tool descriptions rewritten in USE WHEN / NOT WHEN format (2026-04-23)

Non-breaking patch. All 94 tool descriptions rewritten with explicit
USE WHEN / NOT WHEN clauses citing the nearest alternative tool when
one exists. No API change, no behavioural change — purely a
manifest-quality improvement aimed at tool-selection accuracy.

Why: Anthropic's engineering notes that accuracy degrades past 30–50
visible tools (see AUDIT.md Phase 3.6). Explicit routing hints in each
description give the agent a denser signal than prose alone.

What changed:

- 94 descriptions re-written in a 2–4 line format:
  - Line 1: verb + object (what the tool does).
  - Line 2: `USE WHEN:` — intent-level trigger.
  - Line 3: `NOT WHEN:` — alternative tool cited by name when applicable.
  - Line 4 (optional): safety/behavior/pedagogy — NOT schema duplication.
- Sweep `line-4 = schema duplication` removed from 15 descriptions
  (params/enum/return shape that the JSON inputSchema already carries).
  Saves 238 tokens with zero info loss.
- Reciprocal citations verified: `get_dependencies` ↔ `get_dependents`
  ↔ `get_change_impact` (trio, 6/6), library trio
  `get_library_symbol` ↔ `list_library_symbols` ↔
  `find_library_symbol_by_description` (6/6), plus 4 pairs.
- Client-agnostic: no NOT WHEN cites a non-TS tool name (Read,
  edit_file, etc.). Only `your client's file-read tool` generic.
- Memory_* allégé: 28 of the 33 hors-lean tools use a 2-line
  `<title>. USE WHEN:` form since agents in `full` don't need intra-
  ecosystem disambiguation. 5 cite a lean alternative when confusion
  with the `lean` default is plausible.

Manifest measurements (empirical, tiktoken cl100k_base proxy):

| Profile | Pre-rewrite | Post-rewrite | Δ       |
|---------|-------------|--------------|---------|
| full    | 14 245 t    | 15 986 t     | +12.2 % |
| lean    | 10 507 t    | 11 663 t    | +11.0 % |
| ultra   |  3 540 t    |  3 852 t     |  +8.8 % |

In zone PR review (+5 – 15 %), within projection, well below the +15 %
stop threshold. Net cost of the format is the price of discriminating
tool selection — validated over tsbench + VPS telemetry data (Spike 1).

1381 tests pass; ruff clean.

## v2.8.0 — Audit, watcher, telemetry, v3 prep (2026-04-23)

Non-breaking release. Consolidates the strategic audit + B3 file watcher +
A5 persistent call counter + B1a `mcp_toolset.example.json` + A1/A2 docs
reconcile. Also announces the v3.0 default-profile flip via a one-line
stderr warning at boot so users notice the change before it ships.

Key content (full detail in the `v2.8.0-dev` working log below; this
release crystallises that set):

- **Semantic code tools** : `search_codebase(semantic=True)`, `find_semantic_duplicates(method="embedding")`, `find_library_symbol_by_description` shipped (Nomic-embed-text-v1.5-Q, 768 d, fastembed). Safety contract: per-cluster `sim=min..mean` tags on embedding duplicates; no low-confidence warning (bench showed 0–12 % precision — absolute score doesn't discriminate correct vs wrong on code).
- **Library tooling** : `get_library_symbol`, `list_library_symbols`, `get_db_schema`, per-project `.token-savior/hint.md` auto-injected at `switch_project`.
- **Benchmarks** : `tests/benchmarks/code_retrieval` (30 queries, semantic +87 % MRR vs keyword), `tests/benchmarks/library_retrieval` (15 queries stdlib, MRR 0.84, Recall@10 1.00). CI gate via `scripts/check_bench_gates.py`.
- **Perf** : LRU cache on library embed (P95 cold→warm : 2548 ms → 236 ms, 10×).
- **Docs reconcile** : tool count aligned to actual 94 across README, `server.json`, `server.py` comments. Test count bumped 1318 → 1360. Earlier docs drift (README said 90, comments said 106) resolved.
- **Listing caps** (A2) : `get_functions`, `get_classes`, `get_imports` default to 100-row limit with explicit truncation marker. Passing `max_results=0` restores unlimited behavior.
- **B3 file watcher** (`src/token_savior/watcher.py`) : watchfiles-backed added/modified/deleted stream with mtime fallback. Flag `TOKEN_SAVIOR_WATCHER=on|off|auto` (default `auto`). Closes the 30 s live-editing window and the 2.1 ms/query mtime stat.
- **A5 persistent telemetry** (`src/token_savior/telemetry.py`) : `$TOKEN_SAVIOR_STATS_DIR/tool-calls.json` counter scoped by `(tool_name, TOKEN_SAVIOR_CLIENT)`. Silent on failure, surfaced via `telemetry_health()`.
- **B1a `mcp_toolset.example.json`** + `docs/migration/v3.md` : recommended Anthropic API config with 17 non-deferred tools; migration guide with Quick-rollback in 3 lines.
- **v3 deprecation warning** : `[token-savior] default profile will change from 'full' to 'lean' in v3.0.0 — see docs/migration/v3.md` fires once at boot when `TOKEN_SAVIOR_PROFILE` is unset; silent otherwise.
- **`_LEAN_EXCLUDES` spike-1 update** : `memory_save` and the atomic `discover_project_actions` / `run_project_action` pair kept in `lean` after measuring that dropping them would break (respectively) the user-facing "nothing forgotten" contract and a paired workflow. `lean` now = 61 tools / 10 507 est. tokens (narrowly above Claude Code's 10k auto-defer).
- **AUDIT.md** at repo root — full strategic review (869 lines, Phases 0–4, sourced).
- **GitHub issue #15** open for the `2>/dev/null` hook swallow (fix scheduled post-v2.8).

Tests: 1360 → 1381 passing (+21 : watcher, telemetry, listing caps, bench gates).

## v2.7.1 — Description retightening after v2.7.0 regression signal (2026-04-21)

- Reduce 5 heaviest tool descriptions by 47 % (1 525 → 811 chars) while preserving keyword signal (`BATCH`, `USE THIS instead`, `TERMINAL`, `ignore_generated`). Mean active_tokens delta on bench rerun: unchanged gains on heavy tasks, small regressions on single-tool tasks halved.
- `search_symbols_semantic` / `find_library_symbol_by_description` thresholds tuned (0.75 → 0.60 floor, 0.02 → 0.01 gap) then warnings removed entirely after bench showed distributions overlap.
- Tests : 1318 → 1360 passing after safety rework.

## v2.7.0 — 14 bench-driven optimisations (2026-04-21)

Sample haiku-ts v2.7 (12 tasks) — mean Δ active_tokens = **−13.2 %**. Winners: heavy-read −44 %, navigation −19.5 %, edit −13.9 %.

**Navigation / lookup**
- `find_symbol` returns `complete: true` + `scanned_files: N` (no follow-up exploration needed).
- `_resolve_symbol_info` fallback normalised (snake/kebab/case-insensitive) via `normalized_symbol_index`.
- `search_codebase` skips generated/minified files by default (`.generated.`, `.min.`, `.pb.`, `dist/`, `build/`, `.next/`, `node_modules/`, `.proto`).
- New `search_in_symbols` : content search + enclosing function/class.
- New `audit_file` : mega-batch dead_code + hotspots + semantic duplicates scoped to one file.

**Context / edit**
- `get_full_context` : new `brief=False` default (cap 12 deps, 4 000 chars).
- `get_class_source` : auto-downgrade level 2 when > 300 lines.
- `get_function_source` : prefix `[scaffold: stub]` via AST detection (`pass` / `Ellipsis` / docstring-only / `return None` / `raise NotImplementedError`).
- `get_routes` : `stub: true` flag on empty handlers.

**Analyse**
- `get_backward_slice` : `max_symbol_lines=500` cap.
- `find_hotspots` : T0-T3 tiers (actionability-ranked).
- `detect_breaking_changes` : `BREAKING: [T0] (N)` format (substring-stable for regression tests).
- `_graph_based_test_candidates` : transitive BFS on `reverse_import_graph`.
- `get_community` : `max_members=50` cap.

**Session**
- `_hm_switch_project` : session stickiness (no re-index if slot already active).

**Stats**
- Tool count: 88 → 90 (+ `search_in_symbols`, `audit_file`).
- Description total: 12 371 → 11 657 chars (−6 %).

## v2.6.0 — Memory Engine Phase 1+2 + tsbench 100% (2026-04-20)

### tsbench (90 paired tasks, Opus 4.7) — 180/180 (100.0%) vs 141/180 (78.3%)

- Active tokens: 1,549,915 → 803,531 (−48.2%)
- Wall time: 165.9min → 35.1min (−78.9%)
- Context chars: 473,752 → 258,329 (−45.5%)
- Wins/Ties/Losses: 25 / 65 / 0 (zero losses)
- Also on Sonnet 4.6: ts 170/180 (94.4%) vs base 156/180 (86.7%)

### Bench-driven fixes

- `CLAUDE_PROJECT_ROOT` env auto-promotes active project at boot (no `switch_project` round trip)
- Explicit `project=` hint auto-promotes active project on first call
- `TS_WARM_START=1` pre-builds index at server start
- `get_full_context` defaults to compact mode: source head 80 lines + names-only deps
- Empty-result `_suggestion` on `search_codebase` and `get_dependents`
- Lower defaults on noisy analyses (`analyze_config`, `find_dead_code`, `find_semantic_duplicates`)
- `lean` profile (59 tools) confirmed as bench default
- App-factory detection in `get_entry_points` (`create_app`, `make_app`, `build_app`, factory in `main.py`/`app.py`/`__init__.py`)
- Infra-tech surfacing in `get_project_summary` — flags top-level `infra/` / `deploy/` / `k8s/` and detected techs (docker, terraform, k8s)

### Phase 1 — Gap closure
- P1: `<private>` tag stripper (UserPromptSubmit hook)
- P2: content_hash persisté, dedup O(1) + backfill
- P3: `ts://obs/{id}` citation URIs dans injection output
- P4: PreToolUse-Read hook — file-context injection
- P5: session-end rollup structuré (FTS5, 6 champs)

### Phase 2 — Feature parity + differentiation
- A4: Progressive disclosure formalisé (Layer 1/2/3, cost table)
- A5: narrative / facts / concepts fields sur observations
- A1: sqlite-vec hybrid search + RRF fusion (FTS fallback graceful)
- A2: Web viewer opt-in `127.0.0.1:$TS_VIEWER_PORT` (htmx + SSE)
- A3: LLM auto-extraction PostToolUse (opt-in `TS_AUTO_EXTRACT=1`)

### Stats
- Tools : 105
- Tests : 1318/1318
- Vector search : `sqlite-vec` + `sentence-transformers/all-MiniLM-L6-v2`

## v2.0.0 — Token Savior Recall (2026-04-13)

### Memory Engine (new)

- SQLite WAL + FTS5: cross-session persistent memory
- 21 memory tools: save, search, get, delete, index, timeline, status, why, top
- 8 Claude Code lifecycle hooks: SessionStart, Stop, SessionEnd, PreCompact,
  PreToolUse ×2, UserPromptSubmit, PostToolUse
- LRU scoring: `0.4 × recency + 0.3 × access + 0.3 × type_priority`
- Delta injection: only the diff since last session is re-injected at start
- Explicit TTL per observation type (command 60d, research 90d, note 60d)
- Semantic dedup: exact hash + Jaccard (~0.85 threshold)
- Auto-promotion: note × 5 accesses → convention, warning × 5 → guardrail
- Contradiction detection at save time
- Auto-linking between observations (symbol, context, tags)
- Telegram feed for critical observations (warning / guardrail / error_pattern)
- Mode system: `code`, `review`, `debug`, `infra`, `silent` with auto-detection
- Thematic corpus Q&A
- Versioned markdown export (git-tracked)
- CLI: `ts memory {status,list,search,get,save,delete,top,why,doctor,relink}`
- Dashboard Memory tab
- 12 observation types: `bugfix`, `decision`, `convention`, `warning`,
  `guardrail`, `error_pattern`, `note`, `command`, `research`, `infra`,
  `config`, `idea`

### Manifest optimizations

- 80 → 69 tools (−11)
- 42,251 → 36,153 chars manifest (−14%)
- ~1,524 tokens saved per session on MCP manifest alone

### Cleanup

- Removed DEPRECATED tools (`apply_symbol_change_validate_with_rollback`,
  `get_changed_symbols_since_ref`)
- Fused 10 memory tools → 5 (`memory_mode`, `memory_archive`,
  `memory_maintain`, `memory_set_global`, `memory_prompts`)

### Core Token Savior (unchanged)

- 69 MCP tools total (53 core + 16 memory)
- 97% token savings measured across 170 real sessions
- ~$609 estimated cost saved
- 17 indexed projects
- Annotators: Python, TypeScript/JS, Rust, Go, C/C++, C#, JSON, YAML,
  TOML, XML, INI, ENV, HCL, Dockerfile, Markdown

### Rename

- Project renamed: **Token Savior → Token Savior Recall**
- MCP server identifier: `token-savior` → `token-savior-recall`
- PyPI package: `token-savior` → `token-savior-recall`

---

## v1.0.0 (2026-04-11)

### Architecture

- **ProjectQueryEngine**: Refactored 705-line closure `create_project_query_functions` into a class with one method per query tool. `as_dict()` preserves backward compatibility.
- **CacheManager**: Extracted cache persistence logic from `server.py` into `src/token_savior/cache_ops.py`.
- **SlotManager**: Extracted project slot management from `server.py` into `src/token_savior/slot_manager.py`.
- **Tool schemas**: Extracted all 53 MCP tool schemas from `server.py` into `src/token_savior/tool_schemas.py`. Server reduced from 2,439 to 990 lines.
- **Brace matcher**: Factored `_find_brace_end` from 4 annotators into `src/token_savior/brace_matcher.py` with per-language variants.
- **Annotator refactoring**: Table-driven dispatch in `annotate_rust` and `annotate_csharp` to reduce complexity below 150.
- **AnnotatorProtocol**: Added `typing.Protocol` for annotator type safety in `models.py`.

### Performance

- **LazyLines**: File lines are lazy-loaded from disk on demand instead of stored in cache. Cache size reduced by ~57%, idle RAM reduced proportionally.
- **Manual serialization**: Replaced `dataclasses.asdict()` in cache persistence with zero-copy field-by-field serialization.
- **scandir batching**: `_check_mtime_changes` uses `os.scandir()` per directory instead of individual `os.path.getmtime()` calls.
- **Regex cache**: Module-level `_WORD_BOUNDARY_CACHE` avoids recompiling patterns on every call.
- **File limits**: `ProjectIndexer` gains `max_files` param (env: `TOKEN_SAVIOR_MAX_FILES`, default 10,000).

### Bug fixes

- **Path traversal**: `create_checkpoint` validates file paths with `os.path.commonpath` to prevent `../../../etc/passwd` attacks.
- **Triple save**: `_maybe_incremental_update` uses `_dirty` flag pattern to call `_save_cache` at most once per execution path.
- **Output truncation**: `get_dependents` and `get_change_impact` gained `max_total_chars` (default 50,000) to prevent oversized responses.

### Tool fusions

- **get_changed_symbols**: Unified with `get_changed_symbols_since_ref` via optional `ref` parameter.
- **apply_symbol_change_and_validate**: Unified with rollback variant via `rollback_on_failure` parameter.

### Deprecated (removal planned for v1.1.0)

- **get_changed_symbols_since_ref**: Use `get_changed_symbols(ref=...)` instead.
- **apply_symbol_change_validate_with_rollback**: Use `apply_symbol_change_and_validate(rollback_on_failure=true)` instead.

Both deprecated tools inject a `_deprecated` field in their response with migration instructions. Their schemas are marked with `"deprecated": true` in `tool_schemas.py`.

### Tests

- `tests/test_cache_ops.py` (12 tests)
- `tests/test_slot_manager.py` (13 tests)
- `tests/test_server_integration.py` (5 end-to-end tests)
- `tests/test_annotator_protocol.py` (4 tests)
- `tests/test_tool_schemas.py` (7 tests)

### Benchmarks

- `benchmarks/run_benchmarks.py`: Automated benchmarks on FastAPI + CPython measuring index time, RAM, query response time, and cache size.
- `.github/workflows/benchmark.yml`: GitHub Action for release benchmarks.
