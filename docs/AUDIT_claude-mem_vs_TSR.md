# AUDIT — claude-mem vs Token Savior Recall

Comparative audit of [thedotmack/claude-mem](https://github.com/thedotmack/claude-mem) (v6.5.0, TypeScript/Bun, 60K⭐) against Token Savior Recall (TSR, Python/SQLite).

Source refs: `claude-mem` read via `gh api` on commit `main` (2026-04-17). TSR read via Token Savior MCP on `/root/token-savior`.

Goal: identify what TSR is missing, what is worth porting, and what to skip — under the strong constraint "SQLite-only, no heavyweight runtime extras".

---

## 1. Architecture at a glance

| Axis | claude-mem | Token Savior Recall |
|------|-----------|---------------------|
| Language | TypeScript / Bun | Python 3 / stdlib |
| Storage | SQLite (`bun:sqlite`) + Chroma (vector DB) | SQLite (stdlib `sqlite3`, WAL + FTS5) |
| Runtime footprint | Node ≥18, Bun (auto-install), `uv` (for Chroma), SQLite | Python venv (`/root/.local/token-savior-venv`) |
| Install | `npx claude-mem install` (plugin marketplace) | pip/uv + `switch_project` via MCP |
| Worker | Long-running HTTP service on `:37777` | Stdio MCP server, no background daemon |
| UI | Web viewer at `http://localhost:37777` (SPA bundle) | CLI only (injection into Claude context) |
| MCP tools | 4 (`search`, `timeline`, `get_observations` + 1) | 26 `_mh_*` handlers (`memory_save/search/get/why/distill/...`) |
| Hooks | 7 (Setup, SessionStart, UserPromptSubmit, PostToolUse, PreToolUse on Read, Stop, SessionEnd) | 8 (SessionStart, Stop, SessionEnd, PreCompact, 2×PreToolUse, UserPromptSubmit, PostToolUse) |
| Tables | `sdk_sessions`, `observations`, `session_summaries` (+FTS mirrors) | `observations` (+FTS), `consistency_scores`, `adaptive_lattice`, `sessions`, `user_prompts`, `reasoning`, `memory_bus`, `corpora`, ROI/decay/lattice tables |

---

## 2. Observation schema — side-by-side

**claude-mem `observations`**

```
id, memory_session_id, project, text, type, created_at, created_at_epoch,
title, subtitle, narrative, facts, concepts, content_hash
```

**TSR `observations`**

```
id, session_id, project_root, type, title, content, why, how_to_apply,
symbol, file_path, context, tags, importance, is_global, agent_id,
archived, decay_immune, last_accessed_epoch, ttl_days, expires_at_epoch,
created_at, created_at_epoch, private
```

Read: **claude-mem optimizes for narrative compression** (subtitle + narrative + facts + concepts). **TSR optimizes for operational rules** (why + how_to_apply + symbol + file_path + importance + TTL + quarantine).

Neither is strictly a superset. claude-mem's richer narrative fields would strengthen TSR's `memory_why` and distillation output; TSR's `symbol`/`file_path`/`why`/`how_to_apply` have no equivalent in claude-mem.

---

## 3. Gap-by-gap comparison

| # | Capability | claude-mem | TSR | Gap direction |
|---|-----------|-----------|-----|---------------|
| 1 | Vector / semantic search | Chroma (primary) + FTS5 (back-compat) | FTS5 only | claude-mem ahead |
| 2 | Auto-capture of significant events | PostToolUse on all tools → LLM-extracted observations | Pattern-matched Bash (`systemctl`, `crontab`, `chmod`), WebFetch hints, UserPromptSubmit trigger phrases | claude-mem broader, TSR more deterministic |
| 3 | Progressive disclosure search | `search` → `timeline` → `get_observations` (3 tools, ~10× token savings) | `memory_index` → `memory_search` → `memory_get` (equivalent pattern, less formalized) | Parity, TSR under-documented |
| 4 | Web viewer UI | SPA at :37777, real-time observation stream, settings, version switch | None | claude-mem ahead |
| 5 | Hybrid search (keyword + semantic) | ✅ via Chroma + structured filters | ❌ FTS5 + LIKE only | claude-mem ahead |
| 6 | Timeline (chronological context) | `timeline` MCP tool | `memory_timeline` + `get_timeline_around(obs_id, window_hours)` | Parity |
| 7 | Multi-project isolation | `project` field per observation | `project_root` + `is_global` flag (shared observations across projects) | **TSR ahead** (global layer) |
| 8 | Export / portability | Worker HTTP API, `/api/observation/{id}` citations | DB file only, `memory_distill` for summaries | claude-mem ahead |
| 9 | Observation type taxonomy | Free-form `type` string | Opinionated: `guardrail`, `convention`, `warning`, `decision`, `infra`, `research`, ... | TSR ahead (tied to injection priority) |
| 10 | Scoring / ranking | FTS rank + vector similarity | FTS rank + **LRU + importance + Bayesian validity + decay + quarantine** | **TSR ahead** |
| 11 | Consistency / staleness detection | — | `symbol_staleness`, `detect_contradictions`, `quarantine` on Bayesian validity <40% | **TSR unique** |
| 12 | Dedup | Content hash (`content_hash` column) | Global hash + semantic Jaccard (`semantic_dedup_check`) | TSR ahead |
| 13 | Decay / TTL | — | `expires_at_epoch`, `decay_immune`, `ttl_days` | **TSR unique** |
| 14 | Task-filter modes | 30+ **language / personality** modes (code, code--fr, code--chill, law-study, meme-tokens) — shape summarization tone | `code` / `debug` / `infra` / `research` — shape *what gets injected* (auto_capture_types filter) | Different aims; TSR's is more functional, claude-mem's more aesthetic |
| 15 | Privacy | `<private>` inline tag, content stripped before storage | `private` column flag, but no inline-tag UX | claude-mem more ergonomic |
| 16 | Citations by ID | HTTP URL addressable (`/api/observation/{id}`) | `memory_get(ids)` via MCP | Parity (different surfaces) |
| 17 | Session-end rollup | `session_summaries` with `request / investigated / learned / completed / next_steps / notes` (each FTS-indexed) | `session_end(summary, symbols_changed, files_changed)` — flat summary only | claude-mem ahead |
| 18 | Hooks coverage | 7 (+ PreToolUse on Read for file-context injection) | 8 (+ PreCompact) | TSR has more but **lacks file-context injection on read** |
| 19 | Distillation / compression | — | MDL-based `run_mdl_distillation` (cluster + compress) | **TSR unique** |
| 20 | ROI / usage tracking | — | `memory_roi_stats`, `memory_top`, `memory_why` (explain why an obs was injected) | **TSR unique** |
| 21 | Beta / experimental modes | Endless Mode (biomimetic memory) toggle | — | claude-mem has channel |
| 22 | Third-party feeds | OpenClaw → Telegram/Discord/Slack live observation stream | — | claude-mem ahead (niche) |
| 23 | i18n | Modes translated into 30+ languages | English / French mix | claude-mem ahead (marketing) |

---

## 4. Classification: PORT / ADAPT / SKIP

### 4.1 — PORT (verbatim, small effort)

| # | Feature | Effort | Where it lands |
|---|--------|--------|----------------|
| P1 | `<private>` inline-tag stripper in UserPromptSubmit hook — regex-strip `<private>…</private>` blocks before the prompt is injected, and before observations derived from it are saved | **1 h** | `hooks/memory-userprompt.sh` |
| P2 | PreToolUse-on-Read → file-context injection — when Claude reads a file, emit any past observations tagged with that `file_path` or with a `symbol` belonging to that file | **2–3 h** | new `hooks/memory-pretooluse-read.sh`, extends existing `observation_get_by_symbol(file_path=...)` |
| P3 | Citation URL convention — render observations in injection output as `ts://obs/{id}` so the user (or another tool) can resolve them deterministically. Reuse the existing `memory_get` handler | **1 h** | `memory/index.py::get_recent_index` formatted output |
| P4 | Structured session-end rollup — extend `session_end()` to accept `request / investigated / learned / completed / next_steps` fields; FTS-index them | **4 h** | `memory/sessions.py`, add FTS table, migration in `db_core.py` |
| P5 | Observation `content_hash` column — current TSR dedup uses in-memory hash; persisting it enables O(1) dedup + retroactive dedup sweeps | **2 h** | `memory/dedup.py`, migration |

**Quick-win batch total: ~10–12 h**, all inside `src/token_savior/memory/` and `hooks/`.

### 4.2 — ADAPT (re-implement under SQLite-only constraint)

| # | Feature | Adaptation | Effort |
|---|--------|-----------|--------|
| A1 | Hybrid semantic search | **Use `sqlite-vec` (loadable SQLite extension)** instead of Chroma. Keeps single-file DB, no extra daemon, no `uv`. Fallback to FTS when extension isn't loadable (Bun-on-Windows–style graceful degradation, already a claude-mem pattern). Alternative: `sqlite-vss` or in-process `hnswlib` + cached vectors in a blob column. Embedding source: sentence-transformers locally, or OpenAI/Anthropic embeddings endpoint gated behind env var | **1–2 days** |
| A2 | Web viewer UI | Minimal FastAPI (or stdlib `http.server`) bound to `127.0.0.1:${TS_VIEWER_PORT:-0}` (0 = off by default). Endpoints: `/obs/{id}`, `/search`, SSE stream. Single-file HTML + htmx — no SPA bundle. Stays optional, not on the install critical path | **2–3 days** |
| A3 | LLM-based auto-observation extraction from PostToolUse | Optional, opt-in via `TS_AUTO_EXTRACT=1`. Runs async (non-blocking hook), calls the configured Claude/OpenAI endpoint with a tight extraction prompt. Without the env var, TSR keeps today's deterministic pattern matching | **2 days** |
| A4 | Progressive-disclosure API formalization | Already implemented via `memory_index` + `memory_search` + `memory_get`, but the 3-layer contract is not explicit. Rename/alias to `memory.search / memory.timeline / memory.get_observations` and document the token-cost table in `docs/progressive-disclosure.md` (copy claude-mem's figures as a starting point) | **3–4 h** (docs-heavy) |
| A5 | Narrative fields in observations (`subtitle`, `narrative`, `facts`, `concepts`) | TSR's `content / why / how_to_apply` is close but not the same. Add `narrative TEXT` (optional) and FTS-index it — lets `memory_distill` produce nicer output without breaking today's schema | **3 h** |

### 4.3 — SKIP (not worth it for TSR)

| # | Feature | Why skip |
|---|--------|---------|
| S1 | Chroma vector DB | Heavyweight dep (Python + `uv` just for vectors) — `sqlite-vec` covers 95% of the value for 5% of the footprint |
| S2 | Bun runtime / npm install | TSR's stack is Python + pip; matching Bun would double install complexity |
| S3 | 30+ language / personality modes | Aesthetic only. TSR's task-filter modes (`code` / `debug` / `infra` / `research`) solve a different and more useful problem; don't dilute |
| S4 | Always-on HTTP worker service | Adds a daemon to manage, port conflicts, systemd units. MCP stdio is already the delivery channel |
| S5 | OpenClaw gateway / Telegram-Discord live feed | Niche, ties TSR to a specific deployment product |
| S6 | Endless Mode (biomimetic memory) | Beta in claude-mem, unclear ROI, strong coupling to worker architecture |
| S7 | Plugin marketplace auto-install | TSR is self-hosted infra; `switch_project` + pip is the right install contract |
| S8 | Translating docs to 30 languages | Not a capability gap — a marketing choice |

---

## 5. Implementation order (quick wins first)

```
Phase 1 — dockable now (1 week, ~12 h of actual coding)
  P1  <private> tag stripper                      1 h
  P3  ts://obs/{id} citation format               1 h
  P5  content_hash column + retroactive dedup     2 h
  P2  PreToolUse-on-Read file-context hook        3 h
  P4  structured session-end rollup (FTS)         4 h
  A4  progressive-disclosure docs + aliases       3 h

Phase 2 — high-value ADAPT (1–2 sprints)
  A5  narrative field on observations             3 h
  A1  sqlite-vec hybrid search (with FTS fallback) 1–2 d
  A2  minimal web viewer (opt-in, 127.0.0.1:0)    2–3 d

Phase 3 — optional
  A3  LLM auto-extraction (behind TS_AUTO_EXTRACT) 2 d
```

Phase 1 closes seven of the gaps with less than two days of work and **no new runtime dependency**. Phase 2 introduces exactly one optional dep (`sqlite-vec`) and keeps the "SQLite file on disk = entire state" invariant.

---

## 6. What TSR already does better (keep and lean into)

These are unique to TSR and should stay front-and-center in positioning against claude-mem:

- **Bayesian validity + quarantine** — observations lose weight as they fail to predict future behavior, and drop out of injection when they go stale
- **Symbol staleness detection** — observations tied to a `symbol` that no longer exists get flagged
- **Contradiction detection** at save time — new observations that contradict existing high-confidence ones are surfaced
- **Decay + TTL + `decay_immune`** — lets the user distinguish ephemeral work state from durable guardrails
- **Task-filter modes** — `code` / `debug` / `infra` / `research` change *what gets injected*, not the prose style
- **`is_global` global-shared observations** — single-source guardrails that cut across projects
- **ROI tracking** (`memory_roi_stats`, `memory_top`, `memory_why`) — measurable memory value, not just volume
- **MDL-based distillation** — cluster + compress, not just summarize
- **26 MCP handlers** vs 4 — finer-grained control for power users

claude-mem's pitch is *"Claude remembers, via narrative summaries"*. TSR's pitch should be *"Claude remembers **and the memory self-prunes, self-contradicts, and self-scores**"*. Porting Phase 1 + 2 closes the surface-area gap without compromising that differentiation.

---

*Generated 2026-04-17 by Claude Code on VPS (tsbench session). Refresh with `gh api /repos/thedotmack/claude-mem` for later claude-mem versions.*
