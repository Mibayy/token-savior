# Token Savior — Strategic Audit

**Date** : 2026-04-23
**Branch / commit** : `main` @ `cd9a469`
**Author** : honest review, no flattery.

---

## Phase 0 — Ground truth

### Tool count — the real number

| Source | Claims | Reality |
|---|---|---|
| README.md:17 | "90 tools, cross-language" | — |
| server.json:12 | `"version": "2.6.0"` | stale (pyproject = 2.7.1) |
| CHANGELOG.md:47 | "Tools : 105" (v2.6 stats) | stale |
| src/token_savior/server.py:117 | comment: "106 tools, ~10 950 tokens" | stale |
| src/token_savior/server.py:143 | comment: "106 tools, ~10 950 tokens" | stale |
| tests/test_tool_schemas.py:65 | `assert len(TOOL_SCHEMAS) == 94` | **authoritative** |

**Actual count, measured from `TOOL_SCHEMAS` at runtime: 94 tools**. The `test_server_tools_match_schemas` test confirms the server registers exactly those 94. No discrepancy between the schema dict and what's advertised via `list_tools`.

### Profiles — what actually gets exposed

From `src/token_savior/server.py:155-161`:

| Profile | Tools exposed | Manifest bytes | Est. tokens |
|---|---|---|---|
| `full` | 94 | 56 636 | 14 159 |
| `core` | 56 | 37 005 | 9 251 |
| `nav` | 29 | 18 542 | 4 635 |
| `lean` | 58 | 39 628 | 9 907 |
| `ultra` | 17 + 1 proxy (`ts_extended`) | ~14 160 + catalog | ~3 540 |

`ultra` keeps the 17 hot tools and exposes the rest via a meta tool (`mode=list|describe|call`). The default profile is `full` (`_PROFILE = os.environ.get("TOKEN_SAVIOR_PROFILE", "full").lower()` at line 163). `lean` was declared "bench default" in the v2.6 changelog but nothing currently *sets* the env var to `lean` — running the server without the env hits `full`.

### Top 10 heaviest schemas (bytes of JSON)

| Bytes | Tool |
|---:|---|
| 1759 | `get_related_symbols` |
| 1695 | `search_codebase` |
| 1484 | `find_semantic_duplicates` |
| 1429 | `apply_refactoring` |
| 1307 | `find_library_symbol_by_description` |
| 1246 | `checkpoint` |
| 1225 | `get_stats` |
| 1224 | `memory_save` |
| 1014 | `get_full_context` |
| 1008 | `find_hotspots` |

These 10 alone cost ~3.5k of the ~14k manifest. Bottom 5 bottom out at 160–260 bytes (`list_projects`, `memory_doctor`, etc.).

### Deprecated / removed tools (v1 → v2)

From `tests/test_tool_schemas.py::TestV2HandlersRemoved`, v2.0.0 removed:
- `get_changed_symbols_since_ref` (handler `_h_get_changed_symbols_since_ref`)
- `apply_symbol_change_validate_with_rollback` (handler `_h_apply_symbol_change_validate_with_rollback`)

No currently-advertised tool is marked `deprecated` in its description. The v2.6 changelog mentions **fusions** (stats 9→1, checkpoints 6→1, clustering 4→1, hotspots 3→1) reducing the tool count by ~18, but those removals happened silently — no deprecation warnings were shipped ahead of the fusion.

### Version — what's current

- `pyproject.toml` : **2.7.1**
- `server.json` : **2.6.0** (stale — does not reflect v2.7.0 / v2.7.1 / today's work)
- git tags : up to `v2.6.0`, nothing for v2.7.x
- CHANGELOG.md last entry : **v2.6.0** (2026-04-20). Missing v2.7.0, v2.7.1, and today's changes.

### What changed since v2.0 — reconstructed from commits

Commits categorised (not exhaustive):

**v2.0 → v2.6 (Apr 13 → Apr 20)**
- Phase 1+2 memory engine: `<private>` stripper, content_hash dedup, `ts://obs/{id}` URIs, PreToolUse-Read hook, session-end rollups (structured FTS5), narrative/facts/concepts, sqlite-vec hybrid + RRF, web viewer opt-in, LLM auto-extraction, confirmed lean profile as "bench default"
- Tool fusions: stats 9→1, checkpoints 6→1, clustering 4→1, hotspots 3→1 → net −18 tools
- tsbench hits 100 % (180/180) on Opus-TS

**v2.6 → v2.7.1 (Apr 21)**
- 14 bench-driven optimisations: `find_symbol` completeness flag, normalised symbol index (snake/kebab/case-insensitive), skip generated in `search_codebase`, new `search_in_symbols` + `audit_file` (tool count 88 → 90)
- Scaffold-kind detection (`[scaffold: stub]` prefix)
- `get_backward_slice` max_symbol_lines cap
- T0–T3 tiering in `find_hotspots`, `detect_breaking_changes` substring-stable format
- Descriptions tightened (12 371 → 11 657 chars, −6 %)

**v2.7.1 → today (Apr 21 → Apr 23)**
- MiniLM → Nomic-embed-text-v1.5-Q (768 d, fastembed)
- 3 semantic tools shipped in one day: `search_codebase(semantic=True)`, `find_semantic_duplicates(method="embedding")`, `find_library_symbol_by_description`
- Plus 3 library-API tools: `get_library_symbol`, `list_library_symbols`, `get_db_schema`, and `.token-savior/hint.md` auto-injection at `switch_project`
- This audit commit + code_retrieval bench + library_retrieval bench + LRU cache on library embed
- Safety-contract iterations: thresholds tuned, then warnings removed from both `search_symbols_semantic` and `find_library_symbol_by_description` because bench showed 0–12 % warning precision
- Per-cluster sim scores on `find_semantic_duplicates(embedding)`

### Doc ↔ code inconsistencies (living list)

| Location | Says | Reality | Severity |
|---|---|---|---|
| README.md | "90 tools" | 94 | Low (off by 4) |
| server.json | v2.6.0 | 2.7.1 in pyproject | Medium (MCP registry will show wrong version) |
| CHANGELOG.md | last entry v2.6 | 2.7.0, 2.7.1, 2.8-dev unreleased | High (new features invisible) |
| server.py:117 comment | "106 tools, ~10 950 tokens" | 94 tools, ~14 159 tokens | Medium (misleads for profile sizing decisions) |
| server.py:143 comment | "106 tools, ~10 950 tokens" | same | Medium |

**Phase 0 verdict** : the repo ships without a working source of truth for tool count or version. The single test asserting 94 is correct; everything human-readable is stale. Fix before anything else.

---

## Phase 1 — Architecture, dépendances, tailles de réponse

### Architecture 1-page

```
              ┌────────────────────┐
MCP client ──►│ server.py          │
              │  call_tool()       │
              │  _dispatch_tool()  │──┐
              └────────────────────┘  │
                                      ▼
              ┌──────────────────────────────────────┐
              │ 4 handler dicts (94 total)           │
              │  META  (9)  — list/switch/checkpoint │
              │  MEM   (29) — memory_save/search...  │
              │  SLOT  (27) — index+edit+git+test    │
              │  QFN   (29) — pure queries over idx  │
              └──────────────────────────────────────┘
                 │       │          │          │
                 ▼       ▼          ▼          ▼
        ┌──────────┐ ┌────────┐ ┌──────────┐ ┌──────────┐
        │ SlotMgr  │ │ memory │ │ project_ │ │ query_api│
        │ per root │ │_db.py  │ │indexer.py│ │ 3141 LOC │
        │ mtime    │ │ +memory│ │1696 LOC  │ │          │
        │ +git30s  │ │/*.py ×29│ │          │ │          │
        └─────┬────┘ └────────┘ └────┬─────┘ └────┬─────┘
              │                      │            │
              │                      │            └── depends on ProjectIndex
              │                      ▼
              │              ┌─────────────────┐
              │              │ 21 annotator    │
              │              │ files, 6312 LOC │
              │              │ only java_ uses │
              │              │ tree-sitter     │
              │              └─────────────────┘
              │
              ▼
        ┌──────────────────────────┐
        │ .token-savior-cache.json │  (3.1 MB for 243 files)
        │ JSON, human-readable     │  (~12 KB/file)
        └──────────────────────────┘
```

### Key modules

| Module | LOC | Role |
|---|---|---|
| `project_indexer.py` | 1696 | Build `ProjectIndex`. Orchestrates per-language annotators. Handles file walk, exclusions, symbol graph |
| `query_api.py` | 3141 | `ProjectQueryEngine` — the fat class behind every navigation/analysis tool |
| `slot_manager.py` | 413 | Per-project slots, mtime + throttled git checks, incremental reindex, LRU over open slots |
| `memory_db.py` | 92 | SQLite+FTS5+vec open helpers |
| `memory/` (29 files) | ~8000 | Persistent observation engine: save, search, dedup, decay, ROI, distillation, embeddings |
| `git_tracker.py` | 248 | Shell git wrapper (status, head, changed files) |
| `tool_schemas.py` | 1736 | The 94 tool descriptors |
| `server_handlers/*.py` | ~1400 | Thin adapters between MCP and engines |
| `*_annotator.py` × 21 | 6312 | Language parsers (Python AST ✅, Java tree-sitter ✅, 19 others hand-rolled ❌) |

### Git-awareness — measured cost

| Operation | Mean | Notes |
|---|---|---|
| `get_git_status` | 8.7 ms | shell git, not called on hot path |
| `get_head_commit` | 5.5 ms | shell git, 30 s throttle |
| `check_mtime_changes` | **2.1 ms / call** | called on **every tool invocation** |
| Cache parse cold start | 59 ms | 243-file project, JSON parse |
| `find_symbol` full dispatch | 5.9 ms | includes mtime check + lookup |

The prompt's concern "git-awareness check à 1-2 ms avant CHAQUE query" is slightly off on the high side (measured 2.1 ms for mtime, not git) but the critique stands: **mtime stat on every file on every call is pure overhead** on repos where nothing changed. A file watcher (inotify on Linux, FSEvents on macOS, watchfiles cross-platform) would collapse this to ~0 ms.

### The live-editing window

From `slot_manager.py:248-298` : the update path throttles the git check to every 30 s. Between two git checks, if the user writes a new file that isn't already tracked, `check_mtime_changes` won't see it (it only stats files the index already knows about). So a freshly-created file is invisible until either:
- Another query fires 30 s later (git check picks up the new untracked file), or
- Explicit `reindex`.

This is the "live-editing window" the user flagged. Confirmed: mtime change detection works only on known files, git picks up new files at 30 s granularity.

### Response sizes — measured against the token-savior repo itself

| Tool | Bytes | Est. tokens | Status |
|---|---:|---:|---|
| `get_functions` (no filter) | **222 973** | **55 743** | 🔴 runaway — 1 call = ~56 k tokens |
| `get_imports` (no filter) | **99 654** | **24 913** | 🔴 runaway |
| `get_classes` (no filter) | 24 805 | 6 201 | 🟡 large but actionable |
| `list_files` | 8 945 | 2 236 | 🟡 |
| `get_dependencies` | 5 201 | 1 300 | 🟢 |
| `get_function_source` (level=0) | 4 757 | 1 189 | 🟢 |
| `get_full_context` | 4 802 | 1 200 | 🟢 |
| `find_hotspots` | 2 388 | 597 | 🟢 |
| `find_semantic_duplicates` | 1 599 | 399 | 🟢 |
| `analyze_config` | 1 571 | 392 | 🟢 |
| `detect_breaking_changes` | 1 358 | 339 | 🟢 |
| `get_stats` | 1 327 | 331 | 🟢 |
| `find_dead_code` | 1 074 | 268 | 🟢 |
| `find_import_cycles` | 1 075 | 268 | 🟢 |
| `audit_file` | 996 | 249 | 🟢 |
| `get_structure_summary` | 805 | 201 | 🟢 |
| `get_dependents` | 926 | 231 | 🟢 |
| `find_symbol` | 182 | 45 | 🟢 |
| `get_git_status` | 154 | 38 | 🟢 |
| `search_codebase` | 157 | 39 | 🟢 |
| `get_changed_symbols` | 174 | 43 | 🟢 |

**The three outliers cost more tokens than a whole agent session.** The agent has no idea the response will be 56 k until it receives them. No `limit` param documented on the hot path. For a 2 428-function project this breaks the main value promise.

### Handler categories → dispatch routes

`_dispatch_tool` at `server.py:328` routes in order:
1. `META_HANDLERS` (9) — no project needed
2. `MEMORY_HANDLERS` (29) — routed directly, memory engine
3. Resolve `slot` from `project` argument (auto-promote if provided)
4. `SLOT_HANDLERS` (27) — need indexed slot
5. `QFN_HANDLERS` (29) — need `slot.query_fns` (index must exist)

`QFN_HANDLERS` go through a **session result cache** (`_session_result_cache`) keyed by `(name, root, cache_gen, sorted_args)`. `cache_gen` bumps on any incremental reindex. That's solid design.

### Internal tool-to-tool dependencies (shared logic)

- Everything navigational shares **`ProjectQueryEngine`** — `query_api.py` is 3 141 LOC and pulls in most of the other modules.
- `find_hotspots`, `find_dead_code`, `audit_file`, `detect_breaking_changes` share `complexity.py` + `dead_code.py`.
- `search_codebase(semantic=True)`, `find_semantic_duplicates(method=embedding)`, `find_library_symbol_by_description` share `memory/symbol_embeddings.py` + `memory/embeddings.py` (Nomic).
- Memory engine tools share `memory_db.py` + `memory/observations.py` + `memory/search.py`.
- `get_full_context`, `get_edit_context` wrap several other query methods in one call (batch acceleration).
- `apply_refactoring` is a polymorphic front door calling `move_symbol`, `replace_symbol_source`, `add_field_to_model`, or `insert_near_symbol` based on `action` arg — **this already follows the "consolidate polymorphic tool" pattern the user asked about.**

### Parsers — fragility estimate

| Language | Parser | Type | LOC |
|---|---|---|---|
| Python | `python_annotator.py` | `ast` stdlib | 352 |
| Java | `java_annotator.py` | **tree-sitter** | 1228 |
| TypeScript / JSX / TSX | `typescript_annotator.py` | hand-rolled regex+brace | 581 |
| Rust | `rust_annotator.py` | hand-rolled | 738 |
| Go | `go_annotator.py` | hand-rolled | 371 |
| C#, C/C++ | `csharp_annotator.py`, `c_annotator.py` | hand-rolled | 685, 670 |
| Dockerfile, YAML, TOML, JSON, INI, HCL, Gradle, Prisma, env, conf, xml | various | regex mostly | 3000+ combined |

Only Python (AST, robust) and Java (tree-sitter, robust) are structurally parsed. 19 other languages rely on **regex + brace matching**. Fragility predictions:

- TS/JSX: generics (`Foo<Bar<Baz>>`), conditional types, template literals with `<`, JSX closing `<` inside string — regex fails silently on these.
- Rust: `impl Trait<'a> for Foo where T: Bar` multi-line with attributes, macro-generated items — misses completely.
- Go: generics (Go 1.18+), multi-line signatures spanning type params.
- C/C++: preprocessor directives, multi-line macros, class templates, namespaces nested — regex noise.

No bench currently measures parser correctness on these corner cases. We're flying blind on accuracy.

### Memory engine structure — first pass

```
memory_db.py — SQLite (WAL) + FTS5 + sqlite-vec
memory_schema.sql — schema
memory/ (29 files)
  observations.py — CRUD on obs table
  search.py — FTS5 + vec hybrid retrieval, RRF
  dedup.py — exact hash + Jaccard ~0.85
  decay.py — TTL per type, scoring
  roi.py — ROI-based GC (LinUCB-adjacent)
  distillation.py — merge / summarize observations
  consistency.py — contradiction detection
  bus.py / lattice.py — cross-project broadcasting
  auto_extract.py — LLM extraction from PostToolUse
  embeddings.py — Nomic wrapper (swapped from MiniLM 22/04)
  symbol_embeddings.py — NEW: vector index over CODE symbols (not obs)
```

Retrieval strategy:
- FTS5 over observation body — BM25
- Vec similarity via sqlite-vec — cosine on Nomic 768-d
- RRF fusion (`rrf_merge`)
- Layered: `memory_index` (L1, ~15 tok) → `memory_search` (L2, ~60 tok) → `memory_get` (L3, ~200 tok). Cost-aware.

Per-project scoping: yes, `project_root` column on `observations`. But `is_global=True` observations cross projects by design (auto-memory patterns).

### Solide vs bancal — honest cut

**Solide :**
- ProjectIndexer + SlotManager with incremental updates + mtime throttle + 30 s git throttle. Design is sound, the throttle kills the "git every query" fear.
- QFN session-result cache with `cache_gen` invalidation — clean invalidation boundary.
- Memory progressive disclosure (L1/L2/L3) is the correct answer to agent-facing memory cost.
- RRF fusion, FTS5 + sqlite-vec hybrid — industry-standard, well executed.
- Python parsing via `ast` — accurate.
- `apply_refactoring` as a polymorphic front already validates the consolidation pattern.
- Test suite 1360 tests, CI green, bench infra (code_retrieval + library_retrieval) shipped today.

**Bancal :**
- 94 tools manifest = **14 159 tokens** on every session open in `full` profile. The user is right that the "savior" costs before saving. `lean`/`ultra` exist but `full` is the default.
- Three tools (`get_functions`, `get_imports`, `get_classes`) can return **6 k–56 k tokens** per call with no user-facing limit. Silent blowup. The worst offender (`get_functions`) has an optional `limit` but no default — if an agent naively calls it without one, the cost is unbounded.
- 19/21 parsers are hand-rolled regex — no guaranteed correctness on TS generics, Rust macros, Go generics, C/C++ templates. No bench measures parser accuracy.
- Cache is 3.1 MB JSON → 59 ms cold parse. Binary format would help cold start on bigger projects.
- Doc drift everywhere (README 90 / CHANGELOG 105 / comment 106 / actual 94 / server.json v2.6 / code v2.7.1). Nobody can know what they're running from docs alone.
- `mtime_changes` only detects changes to **already-indexed** files. New files created by the agent mid-session are invisible for up to 30 s (until next git poll). Agent edits via `replace_symbol_source` auto-trigger reindex but external edits (another tool, user in editor) do not.
- No currently-installed file watcher. `watchfiles` / inotify would close the live-editing window.
- No global tool-usage telemetry in the repo I can measure — the `get_stats` session view is session-scoped. I can't answer "which 15 tools do 95 % of calls" without running the bench suite end-to-end.
- `AUDIT.md`-style rigor is absent from the repo itself: the README headline "90 tools" and the v2.6 CHANGELOG are the best documentation an adopter has.

---

## Phase 2 — Audit critique

### 2a — Redondances et discriminabilité

**Groupes denses (candidats à consolidation)**

| Préfixe | N | Verdict |
|---|---:|---|
| `get_*` | 29 | Surcharge typologique. Beaucoup de paires jumelles. |
| `memory_*` | 26 | Légitime si on accepte la suite comme un sous-produit à part entière ; pourrait vivre derrière un namespace MCP `memory` distinct |
| `find_*` | 8 | Cohérent (analyse). OK. |
| `list_*` | 3 | OK. |
| `reasoning_*` | 3 | Marginal — peu exercé par le bench. |

**Paires clairement redondantes (tout réduction plausible)**

| Paire | Question de l'agent | Proposition |
|---|---|---|
| `get_dependencies` / `get_dependents` | « qu'appelle X ? » vs « qui appelle X ? » | fusionner : `get_relations(name, direction=both\|incoming\|outgoing\|transitive)` |
| `get_file_dependencies` / `get_file_dependents` | idem au niveau fichier | `get_file_relations(path, direction=…)` |
| `get_function_source` / `get_class_source` | source d'une fonction vs source d'une classe | fusionner : `get_source(name, kind=auto\|function\|class)` — `find_symbol` connaît déjà le kind |
| `get_functions` / `get_classes` / `get_imports` | énumérer un type de symbole | fusionner : `list_symbols(kind)` |
| `find_symbol` / `get_full_context` | localisation seule vs localisation + source + deps | `get_full_context(depth=0)` couvre déjà `find_symbol` — `find_symbol` peut disparaître ou devenir un alias |
| `find_hotspots` / `audit_file` | ranking global vs mega-batch d'un fichier | `audit_file` bat `find_hotspots` scope-fichier ; garder les deux mais clarifier les descriptions |
| `get_duplicate_classes` / `find_semantic_duplicates` | classes vs fonctions | déjà unifiable dans `find_semantic_duplicates(kind=class\|function\|all)` |
| `memory_top` / `memory_why` | top ranké vs explication d'une entrée | lecture complémentaire, garder les deux mais documenter quand l'un versus l'autre |
| `get_dependents` / `get_change_impact` | direct vs transitif | `get_change_impact` couvre le cas transitif, `get_dependents` est un cas trivial — on peut dépréciercs `get_dependents` avec un flag `depth=1` sur `get_change_impact` |
| `memory_save` / `memory_from_bash` | persister un fait vs capturer un résultat bash | `memory_from_bash` est un wrapper — le garder comme helper mais pas un tool MCP séparé : il consomme ~200 bytes de manifest pour rien |

**Gain potentiel** : 10–14 tools retirés sans perte de surface (94 → 80–84). Manifest passe de 14 k à ~12 k tokens en `full`.

**Descriptions discriminabilité** — vérification rapide sur un échantillon de descriptions courtes :

- `find_symbol` : « Locate a symbol (file, line, signature, preview). » ✅ clair.
- `search_codebase` : bien tagué SAFETY, deux modes documentés. ✅
- `get_dependencies` vs `get_dependents` : lisent littéralement pareil au premier coup d'œil — un agent hésitera. ❌
- `get_full_context` vs `get_edit_context` : « edit_context » ajoute callers+siblings+tests ; pas évident quand choisir. ❌
- `find_hotspots` vs `audit_file` : un agent qui lit les deux ne saura pas lequel ouvrir. ❌

**Nommage incohérent** : `find_*` sous-entend recherche, `get_*` sous-entend lookup. Mais on a `find_symbol` (pure lookup) vs `get_change_impact` (analyse lourde). C'est arbitraire.

### 2b — Le problème de surface d'API

**Coût manifeste mesuré** (Phase 0) : `full` = **14 159 tokens** statiques. Pour comparer :
- Un CLAUDE.md projet sérieux fait ~2–5 k tokens.
- Un message système de hook Claude Code fait ~500 tokens.
- 14 k tokens, c'est **l'équivalent d'un fichier de code de ~3500 lignes** lu par l'agent sans rien faire, à chaque ouverture de session.

**Ce que je n'ai pas pu mesurer** : la distribution d'appels réelle sur les 170+ sessions benchmarkées. Le repo ne persiste pas un call counter agrégé par tool (seulement session-scoped via `get_stats`). **Gros trou observationnel** — on ne peut pas dire aujourd'hui "15 tools = 95 % des appels". À instrumenter avant toute décision de suppression agressive.

Un proxy raisonnable : les 17 tools choisis pour `ultra` (`_ULTRA_INCLUDES`) reflètent probablement l'intuition de l'auteur sur les outils hot. Si on y ajoute `get_changed_symbols`, `get_class_source`, `find_hotspots`, `find_dead_code`, `get_dependents`, `get_call_chain`, `detect_breaking_changes`, `replace_symbol_source`, `insert_near_symbol`, `apply_refactoring` → on est à ~27 tools core. Les 67 autres sont probablement < 5 % des appels combinés.

**Patterns MCP non-exploités** (fouille du code) :

| Feature MCP | Exploité ? | Note |
|---|---|---|
| `tools` | ✅ | 94 exposés |
| `resources` | ❌ | Aucun `list_resources` / `read_resource` |
| `prompts` | ❌ | Aucun `list_prompts` |
| `sampling` | ❌ | Pas utilisé |
| `progress notifications` | ❌ | Les ops longues (reindex 2 min Nomic) ne stream pas de progrès |
| `structured content` | ❌ | Tout est `TextContent` — l'agent re-parse des strings |
| `roots` | ⚠️ | `WORKSPACE_ROOTS` env, pas MCP `roots` |
| `elicitation` | ❌ | Pas utilisé |
| `tool filtering via profiles` | ✅ | via env `TOKEN_SAVIOR_PROFILE`, pas standard MCP |

**Le plus important** : `structured content` (MCP 2025-12-11). Actuellement chaque réponse est un string potentiellement JSON-serialisé côté serveur, re-parsé par l'agent. Un mode structured content éliminerait une round-trip de sérialisation et réduirait les tokens gâchés sur la duplication clés.

**Lazy tool loading** : `ultra` + `ts_extended` simule déjà le pattern. La vraie question est : pourquoi n'est-ce pas le **default** ? Un serveur MCP sain en 2026 devrait exposer 15–25 tools max au handshake et charger à la demande.

### 2c — Points techniques (mesures, pas spéculation)

**Git-awareness sur hot path**
- `check_mtime_changes` : 2.1 ms/query (mesuré, Phase 1). Sur 100 queries = 210 ms cumulés. Pas critique mais évitable.
- `get_head_commit` : 5.5 ms, throttlé 30 s. OK.
- **Proposition** : remplacer par `watchfiles` (cross-platform, wraps inotify / FSEvents / ReadDirectoryChangesW). Coût en steady state : 0 ms/query, les changements sont poussés au processus. Léger surcoût mémoire (~1–2 MB par projet watché). Library active, API stable.

**Live-editing window**
- Bug confirmé : un fichier créé par l'agent *sans* passer par `replace_symbol_source` / `insert_near_symbol` n'apparaît pas avant 30 s.
- Fix 1 : même file watcher → fenêtre = 0.
- Fix 2 (sans watcher) : hook le handler `Write`/`Edit` MCP côté agent → force un `reindex_file` sur le chemin touché. Spécifique Claude Code, pas portable.

**Cache JSON (3.1 MB, 59 ms cold parse)**
- JSON est human-readable, debuggable, mais gros. Pour 5000 fichiers = ~60 MB JSON.
- sqlite avec WAL : 1 fichier, lecture partielle possible, serialisation binaire. Trade-off : perte du diff-friendly dans git. Plausible : stocker en sqlite, exposer un `--dump-cache` pour debug.
- msgpack : ~3x plus compact que JSON, ~5x plus rapide à parser. Pas de serveur requis, fichier unique. Plus simple que sqlite pour ce cas.
- **Mesure à faire** : benchmarker les 3 formats sur un repo 2000 fichiers. Pas dans le scope de l'audit.

**Parsers**
- Python `ast` : fiable.
- Java `tree-sitter` : fiable.
- **19 autres sont regex-based, 6312 LOC combined.** Tree-sitter supporte déjà tous les langages listés (Python, TS/JS, Rust, Go, C/C++, C#, Ruby, HCL, YAML, TOML, JSON, XML, Dockerfile, …).
- Impact estimé : passer à tree-sitter partout réduirait les 6312 LOC à ~2000 LOC (queries Scheme + binding Python), et éliminerait les faux-négatifs silencieux sur generics/macros/nested.
- Coût : ajouter `tree-sitter-*` deps (binaires précompilés ~500 kB par langue, ~10 MB total).
- Pas d'urgence de correctness — on n'a pas de bug rapporté sur ces langages — mais la dette est réelle.

**`get_change_impact` et le BFS transitif**
- Code lu (`query_api.py` autour de la méthode) : retourne direct + transitif jusqu'à `max_direct` et `max_transitive`. Pas de scoring.
- Risque : un changement sur un util largement utilisé (ex. `log`) ramène 50 % du projet.
- **Fix proposé** : ranker par distance BFS ascendante puis par PageRank sur le graphe de dépendances. Aider repomap fait exactement ça (voir Phase 3).

**Recall (moteur mémoire) — safety policy**
- Pas de **politique de confiance formalisée** qu'un humain peut auditer aujourd'hui :
  - Qui écrit quoi ? Hooks + LLM auto-extract + appels manuels. Pas de sig.
  - Qui confirme ? `auto_promote` (note ×5 → convention) mais le seuil est une constante.
  - Qui expire ? TTL par type (command=60d, research=90d, note=60d). Pas de contrôle par projet.
- **Risque concret** : une observation mal extraite par le LLM reste valide 60+ jours, contamine les prochaines sessions via injection.
- Pas de tool `memory_diff_vs_code` pour détecter qu'une obs cite `foo()` qui n'existe plus.
- **Stockage par projet** : `project_root` scope-t-il correctement ? Le test rapide dit oui (observations typées par root). Mais `is_global=True` percole partout — politique de promotion au global devrait être stricte et tracée.
- Pas de bench de "memory poisoning" (injection adverse via un outil PostToolUse).

**Redondance `search_codebase(semantic=True)` vs memory FTS5**
- Deux stores vectoriels : `obs_vectors` (mémoire) et `symbol_vectors` (code). Même modèle Nomic, deux tables. OK structurellement, mais l'agent pourrait confondre `memory_search` ("trouve une mémoire") vs `search_codebase(semantic=true)` ("trouve un symbole"). Descriptions clarifient — acceptable.

### Verdict Phase 2

Ce qui marche :
- Dispatch à 4 catégories est propre.
- `apply_refactoring` polymorphe est un bon précédent.
- Progressive disclosure mémoire (L1/L2/L3) est conceptuellement juste.
- Bench code_retrieval + library_retrieval = guard rails corrects pour le retrieval.

Ce qui saigne :
- 94 tools flat = 14 k tokens de manifest en `full` — majorité non exploitée.
- 3 tools (`get_functions/classes/imports`) peuvent renvoyer 6–56 k tokens en un call sans cap agressif.
- 19/21 parsers = regex artisanale, dette silencieuse sur les langages non-ASCII-trivial.
- Aucune télémétrie d'usage persistée → décisions de consolidation à l'aveugle.
- Recall n'a pas de politique de confiance formalisée.
- Live-editing window 30 s + mtime-only = lent sur fichiers nouvellement créés.

---

## Phase 3 — État de l'art (sources citées)

### 3.1 Spec MCP — ce qu'on n'exploite pas

Sources : [Specification 2025-11-25](https://modelcontextprotocol.io/specification/2025-11-25), [One Year of MCP: November 2025 Spec Release](https://blog.modelcontextprotocol.io/posts/2025-11-25-first-mcp-anniversary/), [Update on the Next MCP Protocol Release](https://modelcontextprotocol.info/blog/mcp-next-version-update/).

| Primitive / feature | Token Savior | Valeur loupée |
|---|---|---|
| `tools` | ✅ 94 | — |
| `resources` | ❌ | Exposer `project://summary`, `project://entry-points`, `project://hint` comme resources lues **une fois** par session au lieu d'appels tool répétés |
| `prompts` | ❌ | Un `prompt` MCP peut guider l'agent vers le bon workflow (navigation → lecture → edit) et remplacer la moitié des descriptions duplicates |
| `structured content` | ❌ | Retour JSON natif plutôt que TextContent, avec clés courtes |
| `progress notifications` | ❌ | Le reindex initial Nomic (~2 min) ne stream rien — l'agent ne sait pas si c'est vivant |
| `tasks` (new Nov 2025) | ❌ | Ops longues (reindex complet, corpus_build) devraient être des tasks, pas des calls bloquants |
| `icons` metadata | ❌ | Nice-to-have |

**Impact concret pour TS** : `prompts` + `resources` permettent de sortir les "ambient" observations (project summary, hint) du flot tool → économie ~1–2 k tokens/session + meilleure discoverability.

### 3.2 Aider repomap — ce qu'ils font mieux

Sources : [Building a better repository map with tree sitter | aider](https://aider.chat/2023/10/22/repomap.html), [Repository Mapping System | DeepWiki](https://deepwiki.com/Aider-AI/aider/4.1-repository-mapping), [Repository map | aider](https://aider.chat/docs/repomap.html).

Architecture Aider :
1. **Tree-sitter** pour extraire definitions ET references, pour **40+ langages** via un format unifié.
2. **Graphe dirigé** fichier→fichier avec arêtes = import/ref cross-file.
3. **PageRank personnalisé** (NetworkX) avec personnalization vector biaisé vers les fichiers mentionnés dans la conversation en cours.
4. **Token budget binary search** : sélectionne le top-K définitions qui rentrent dans `--map-tokens` (défaut 1 k).
5. **Elided rendering** : seulement les lignes critiques (signatures + docstrings) des définitions, pas le corps complet.

**Ce qu'on a de comparable** :
- Extraction de symbols : oui mais 19/21 parsers sont regex (cf. Phase 2.2c).
- Graphe import : oui, `get_file_dependents`/`get_file_dependencies`.
- **Ranking** : non — `get_change_impact` fait BFS transitif sans scoring. Pas de PageRank, pas de priorité "symbole central du projet".
- Token budget : partiel — `max_lines` / `level` sur certains outils mais pas une logique globale de "tiens dans N tokens".

**Verdict** : Aider a 3 mécanismes qu'on n'a pas : (a) tree-sitter universel, (b) PageRank personnalisé, (c) budget-driven summarisation. Les trois sont complémentaires, pas en compétition.

### 3.3 Serena MCP — le concurrent direct

Sources : [GitHub oraios/serena](https://github.com/oraios/serena), [a2a-mcp.org/entry/serena-mcp](https://a2a-mcp.org/entry/serena-mcp), [Deconstructing Serena's MCP-Powered Semantic Code Understanding (Medium)](https://medium.com/@souradip1000/deconstructing-serenas-mcp-powered-semantic-code-understanding-architecture-75802515d116).

Architecture Serena :
- **LSP** (Language Server Protocol) **40+ langues** via SolidLSP, une abstraction unifiée au-dessus des language servers existants (pyright, rust-analyzer, gopls, ts-server, clangd…).
- Tools symboliques : find symbol, symbol overview, find references, type hierarchy, find declaration/implementations.
- Refactoring sémantique : rename/move/inline, safe delete.
- Symbolic edit : replace symbol body, insert before/after.

**Trade-offs LSP vs tree-sitter** :
- LSP résout les types, cross-ref multi-fichiers, imports transitifs — chose que tree-sitter ne fait pas seul.
- LSP nécessite un language server par langue (installation, démarrage, mémoire).
- Tree-sitter est O(n) incremental, zero-config, mais CST-only (pas de résolution de types).

**Où Serena nous bat** : Go-to-definition / Find references sont sémantiquement corrects (résout les overrides Python, les traits Rust, etc.). On fait du name-matching, donc ambiguous sur les surcharges.

**Où on bat Serena (potentiellement)** :
- Démarrage : pas de language server à booter → first-query plus rapide sur petits projets.
- Mémoire persistante (Recall) : Serena n'a pas d'équivalent intégré.
- Bench/quality : on a 1360 tests + benches retrieval ; Serena c'est plutôt "works in practice".

**Verdict** : si on reste purement "navigation code", Serena est structurellement supérieur (LSP > regex/ast). Notre angle de différenciation doit passer par la **mémoire persistante** et le **bench/quality** — ou intégrer tree-sitter a minima pour fermer l'écart sur correctness.

### 3.4 Sourcegraph SCIP & LSIF

Sources : [SCIP - a better code indexing format than LSIF | Sourcegraph](https://sourcegraph.com/blog/announcing-scip), [Precise code navigation | Sourcegraph docs](https://sourcegraph.com/docs/code-search/code-navigation/precise_code_navigation), [GitHub sourcegraph/scip](https://github.com/sourcegraph/scip/).

SCIP (Sourcegraph Code Intelligence Protocol) est le successeur de LSIF :
- Format protobuf compact pour stocker les graphes "symbol → definition + references".
- Incremental indexing (update par fichier, pas rebuild complet).
- Cross-language (e.g. protobuf → Java/Go bindings générés).

**Utilité pour nous** : consommer des index SCIP existants (gitlab.com, github.com via Sourcegraph) comme source ground-truth pour les références, au lieu de re-parser. Trop gros pour auto-host mais pourrait être un **output format** — on exporte notre index en SCIP → compatible avec Sourcegraph / GitLab Precise Code Intelligence.

### 3.5 Tree-sitter vs LSP vs hybrid — conclusion 2025/2026

Sources : [Tree-sitter vs. Language Servers | HN](https://news.ycombinator.com/item?id=46719899), [Tree-sitter vs LSP: Why Hybrid IDE Architecture Wins (byteiota)](https://byteiota.com/tree-sitter-vs-lsp-why-hybrid-ide-architecture-wins/), [Explainer: Tree-sitter vs. LSP (Lambda Land, 2026-01)](https://lambdaland.org/posts/2026-01-21_tree-sitter_vs_lsp/), [CodeRLM (HN, 2025)](https://news.ycombinator.com/item?id=46974515).

**Consensus du milieu en 2026** :
- Tree-sitter pour parsing/highlighting/indexing : coût ~constant, portable, incremental.
- LSP pour ref-lookup/go-to-def/rename : sémantique, coûte en démarrage.
- **Les éditeurs modernes (Zed, Helix, Neovim) combinent les deux** : tree-sitter pour la couche vue, LSP pour la couche intelligence.

Pour un **indexer batch** (ce qu'est TS), tree-sitter suffit largement si on ne fait que du symbol-level navigation, ce qui est notre cas à 95 %.

### 3.6 Anthropic — skills, tool search, lazy loading

Sources : [Tool search tool | Claude API](https://platform.claude.com/docs/en/agents-and-tools/tool-use/tool-search-tool), [Advanced tool use | Anthropic Engineering](https://www.anthropic.com/engineering/advanced-tool-use), [Claude Code just got updated (VentureBeat)](https://venturebeat.com/orchestration/claude-code-just-got-updated-with-one-of-the-most-requested-user-features), [Extend Claude with skills | Claude Code Docs](https://code.claude.com/docs/en/skills), [Anthropic brings MCP tool search to Claude Code (tessl.io)](https://tessl.io/blog/anthropic-brings-mcp-tool-search-to-claude-code/).

**Faits vérifiés dans la doc officielle** :
- Deux variantes : `tool_search_tool_regex_20251119`, `tool_search_tool_bm25_20251119`.
- Marquer les tools avec `defer_loading: true` → invisibles au system prompt, chargés à la demande via search.
- Retour : 3 à 5 `tool_reference` par recherche.
- Seuil Claude Code : **>10k tokens de definitions tools = auto-defer**.
- **Tool selection accuracy "degrades significantly once you exceed 30–50 available tools"** — quote directe Anthropic.
- Réduction token Anthropic internal : ~85 %.
- Recommandation : garder **3–5 outils les plus utilisés non-defer** + le reste `defer_loading: true`.

**Implication directe pour TS** :
- `full` manifest = 14 k tokens → **au-delà du seuil 10k**, Claude Code auto-defer tout.
- `ultra` profile = 17 tools / 3 540 tokens → dans la bonne fenêtre mais **fait nous-même ce que Claude Code ferait mieux** (search client-side).
- La bonne architecture en 2026 est : garder 94 tools, mais marquer 80+ comme `defer_loading: true`, en laissant 10–14 critiques non-defer. Ça demande que l'app client supporte `defer_loading` — **Claude Code oui**, autres clients MCP pas encore universellement.

**Skills Claude Code** : un skill est un package Markdown + code qui étend Claude Code. Potentiellement, un subset de TS pourrait être reconditionné en skill "Codebase navigator" (qui compose les bons tools MCP sans les exposer tous flat). Mais le skill ne remplace pas le serveur MCP ; il **l'utilise**.

### 3.7 Moteurs de mémoire persistante — comparaison rapide

Sources : [Best AI Agent Memory Frameworks 2026 (Atlan)](https://atlan.com/know/best-ai-agent-memory-frameworks-2026/), [From Beta to Battle-Tested: Letta, Mem0, Zep (Medium)](https://medium.com/asymptotic-spaghetti-integration/from-beta-to-battle-tested-picking-between-letta-mem0-zep-for-ai-memory-6850ca8703d1), [5 AI Agent Memory Systems Compared (dev.to, 2026)](https://dev.to/varun_pratapbhardwaj_b13/5-ai-agent-memory-systems-compared-mem0-zep-letta-supermemory-superlocalmemory-2026-benchmark-59p3), [Survey of AI Agent Memory Frameworks (Graphlit)](https://www.graphlit.com/blog/survey-of-ai-agent-memory-frameworks).

| Système | Architecture | Ce qu'on en retient |
|---|---|---|
| **Mem0** | Framework-agnostic, passive extract on `add()`, FTS + vec | Plus grosse GitHub (48 k★). Notre `observation_save` + dedup + FTS/vec est l'équivalent fonctionnel |
| **Letta (MemGPT)** | Full runtime, 3-tier (core / archival / recall), OS-inspired | Agents "run inside Letta" — intégration plus invasive. On n'est pas sur ce modèle |
| **Zep / Graphiti** | Hybrid vec+graph, long-running agents | Graph explicite entre observations. On a des `links` (auto-linking), pas de vrai graphe queryable |
| **Cognee** | Graph + vec, extraction auto avancée | Meilleur sur l'extraction, plus complexe à opérer |

**Benchmark LongMemEval 2026** (cité par dev.to) :
- OMEGA 95.4 % (GPT-4.1)
- Mastra Observational 94.87 % (GPT-5-mini)
- Emergence AI 86 % (RAG)
- Zep/Graphiti 71.2 % (GPT-4o)

On n'a pas de score LongMemEval pour TS Recall. Point aveugle : **on ne sait pas si notre mémoire est meilleure ou pire que Zep/Mem0 sur un benchmark standard**. Reproduire LongMemEval sur notre engine serait la seule façon d'asseoir une claim.

**Ce qu'on fait probablement mieux** :
- Progressive disclosure L1/L2/L3 explicite — peu d'équivalents font ça côté prompt engineering.
- ROI-based GC + LinUCB bandit (cf. `memory/roi.py`, `linucb_injector.py`) — apprentissage en ligne de la valeur d'une mémoire.
- Project-scoped + cross-project via `is_global` — granularité fine.

**Ce qu'ils font mieux** :
- Mem0 / Zep ont une API très simple (`add`, `search`) — notre surface mémoire fait 26 tools.
- Zep/Graphiti ont un graphe explicite queryable ; nous avons `links` mais pas de MATCH-style.
- Letta pousse le paradigme "mémoire comme state de l'agent" plus loin.

### 3.8 BM25 + embeddings lightweight

Sources : [Enhance Your LLM Agents with BM25 (towardsai.net)](https://towardsai.net/p/artificial-intelligence/enhance-your-llm-agents-with-bm25-lightweight-retrieval-that-works), [Citation-Grounded Code Comprehension (arxiv 2512.12117)](https://arxiv.org/html/2512.12117v1), [BGE-m3 HuggingFace](https://huggingface.co/BAAI/bge-m3), [GitHub BM25 vs Vector Search for Large-Scale Code Repository (ZenML)](https://www.zenml.io/llmops-database/bm25-vs-vector-search-for-large-scale-code-repository-search).

- Hybrid BM25 + BGE dense outperforms single-mode baselines by **14–18 pp** on architectural queries.
- GitHub Code Search retient BM25 sur vector pur pour le code : plus rapide, explainable, zero-shot.
- BGE-base (768-d) tourne sur 8 CPU / 32 GB RAM, pas de GPU requis.

**Pour TS** : on a déjà FTS5 (~BM25) + Nomic 768-d hybrid RRF pour la mémoire. La littérature valide le choix. Pour le code, on fait actuellement regex (FTS5 non activé sur symbol_vectors). Il manque une couche BM25 **sur les symboles** pour compléter le semantic — éviterait les cas où semantic échoue à cause d'un corpus trop court par symbol_doc.

---

## Phase 4 — Propositions

Format : priorité `[P0/P1/P2]`, effort en personnes·jours, risque `[lo/med/hi]`, gain chiffré ou « estimé ».

### A — À corriger

#### A1 `[P0, 0.5d, lo]` — Réconcilier tool count partout

**Problème** : README dit 90, CHANGELOG 105, 2 comments in-code 106, server.json v2.6, code v2.7.1, test 94. L'adopter ne peut pas savoir ce qu'il run.

**Fix** :
- Générer le count dynamiquement dans `README.md` via un script lancé en pre-commit (ou au minimum, ajouter un `make audit` qui patch).
- Supprimer les 2 comments "106 tools" dans `server.py` (ou les regénérer).
- Bump `server.json` à `2.7.1` au minimum.
- Relancer `CHANGELOG.md` avec v2.7.0 / v2.7.1 / v2.8-dev.

#### A2 `[P0, 0.5d, lo]` — Cap dur sur `get_functions` / `get_imports` / `get_classes`

**Problème mesuré** : respectivement 55 k / 25 k / 6 k tokens sans `limit` explicite. Un agent qui appelle naïvement consomme plus qu'une session entière.

**Fix** : défaut `limit=50` ou `limit=100` avec message explicite "showing first N of M — pass limit=0 for all, or pattern='…' to filter". Zéro casse d'API, juste une borne.

**Gain attendu** : −50 k tokens sur les pires cas d'usage.

#### A3 `[P1, 1d, lo]` — Descriptions discriminables

**Problème** (Phase 2a) : paires de descriptions qui se confondent (`find_hotspots` / `audit_file`, `get_full_context` / `get_edit_context`, `get_dependencies` / `get_dependents`).

**Fix** : réécrire en format **"USE WHEN"** + **"NOT WHEN"** de 2–3 lignes. Pattern :
```
get_dependents — USE WHEN you need "who calls X". NOT WHEN you need
  transitive impact — use get_change_impact for that.
```

**Gain** : réduction empirique de la variance de tool selection (à valider sur une sous-matrice bench).

#### A4 `[P1, 1d, med]` — Formaliser politique de confiance Recall

**Problème** (Phase 2c) : aucune trace de qui écrit / confirme / expire une observation. Risque de memory poisoning.

**Fix** :
- Ajouter `trust: {asserted|confirmed|automated}` sur `observations` (default `automated` pour LLM auto-extract, `asserted` pour user explicite via `memory_save`, `confirmed` après ≥2 accès confirmateurs).
- Tool `memory_diff_vs_code(observation_id)` qui vérifie que les symboles cités existent encore (file+line resolvable).
- Promotion `is_global=True` réservée à `trust=asserted` ou `confirmed` + ≥3 accès cross-project.

**Risque** : moderate, touche le schéma `observations` → migration.

#### A5 `[P2, 0.5d, lo]` — Supprimer les tools jamais appelés (instrumentation d'abord)

**Prérequis** : ajouter un persist-counter dans `_track_call` (déjà existe session-scoped). Agréger sur 2 semaines de sessions, produire le Pareto.

Ensuite deprécier (avec warning dans description) les tools en queue de distribution. **Pas maintenant, pas sans données.**

### B — À optimiser (surface identique)

#### B1 `[P0, 1d, lo]` — Activer `defer_loading: true` sur 80+ tools

**Levier** (Phase 3.6) : Claude Code auto-engage tool search au-delà de 10 k tokens de definitions. Le protocole Anthropic supporte `defer_loading` per-tool. Token Savior peut :
- Marquer tout sauf 10–14 core tools comme `defer_loading: true` dans les definitions MCP.
- Les 10–14 "always visible" = sous-ensemble de `_ULTRA_INCLUDES` + `apply_refactoring`, `get_function_source`, `get_class_source`, `search_codebase`, `find_symbol`, `get_full_context`, `get_structure_summary`, `get_git_status`.

**Gain** : manifest visible 14 k → ~3 k (85 % cité par Anthropic). Et surtout : accuracy stable au-delà de 30–50 tools (quote directe Anthropic doc).

**Risque** : les clients MCP qui n'implémentent pas `defer_loading` affichent tout (dégradation gracieuse). À tester sur Claude Desktop / Cursor.

**Coût** : un flag `"defer_loading": true` à ajouter par tool dans `tool_schemas.py`. ~30 lignes de code. Mais nécessite de vérifier que le SDK MCP Python qu'on utilise (`mcp` package) sérialise bien ce champ — à confirmer.

#### B2 `[P1, 2d, med]` — Tree-sitter pour TS / Rust / Go / C / C++ / C#

**Levier** (Phase 2c + 3.5) : 19/21 parsers sont regex, silent failures sur generics/macros/nested. Tree-sitter est le standard 2026, supporte tous nos langages.

**Fix** : remplacer progressivement `*_annotator.py` par des queries `.scm` tree-sitter. Garder `python_annotator.py` (ast est supérieur) et `java_annotator.py` (déjà tree-sitter).

**Gain** : parser-correctness reproductible, ~4000 LOC supprimées (sur 6312), corner cases fermés. Doit venir avec un bench de parser-accuracy (corpus de 20–30 edge cases par langue).

**Risque** : moderate. Tree-sitter binaries à ajouter en dépendance, ~10 MB total. À tester sur CI matrix.

#### B3 `[P1, 0.5d, lo]` — File watcher au lieu de mtime stat

**Levier** (Phase 2c) : `check_mtime_changes` = 2.1 ms par query. Sur 100 queries = 210 ms de stat inutile quand rien ne change.

**Fix** : `watchfiles` library (wraps inotify/FSEvents/ReadDirectoryChangesW). Thread en background, invalidate `slot.cache_gen` sur change, élimine la stat sur hot path.

**Gain** : 210 ms/100-queries → 0. Plus important : **ferme la live-editing window**. Les fichiers créés hors index apparaissent instantanément.

**Risque** : lo. `watchfiles` est maintenu par Samuel Colvin (pydantic author), stable.

#### B4 `[P1, 1d, med]` — Ranker `get_change_impact` par PageRank

**Levier** (Phase 3.2) : Aider's approach. Actuellement on retourne BFS transitif sans tri. Pour une fonction comme `log()` appelée 200×, on noie l'agent.

**Fix** : calculer PageRank (personnalisé sur le symbol queried) sur le graphe d'appels, ranker les dependants. Sortie capée à top-20 par défaut, avec score visible.

**Gain** : réponse `get_change_impact` passe de "50 deps bruitées" à "20 deps ordonnées par impact réel". Token density nettement meilleure.

**Coût** : `networkx` est déjà potentiellement dans les deps (à vérifier). PageRank sur 2000 symboles = négligeable.

**Risque** : moderate — change la sémantique "tous les dependants" vers "les plus importants". Ajouter `mode=all|ranked|top20` pour préserver l'ancien comportement.

#### B5 `[P2, 1d, lo]` — Cache msgpack au lieu de JSON

**Levier** (Phase 2c) : 3.1 MB JSON, 59 ms cold parse sur 243 fichiers.

**Fix** : sérialiser le cache en msgpack. ~3× plus compact, ~5× plus rapide à parser.

**Gain** : cold-start indexer 59 ms → ~12 ms. Taille cache / 3. Sur un repo 5000 fichiers : parse JSON ~1.2 s → msgpack ~250 ms.

**Trade-off** : perte du diff-git-friendly du cache. On peut conserver un export `--dump-cache-json` pour debug si quelqu'un veut l'auditer.

**Risque** : lo.

#### B6 `[P2, 0.5d, lo]` — Batch `defer_loading` + ranger les 14 tools non-defer par fréquence

Conjugué à B1. Demande que A5 soit fait (télémétrie d'usage) pour identifier les "always visible".

### C — À ajouter / remplacer

#### C1 `[P0, 2d, med]` — Semantic layer BM25 sur symboles (complète Nomic)

**Problème utilisateur** : `search_codebase(semantic=True)` Nomic donne MRR 0.71 sur le bench code_retrieval. La littérature (Phase 3.8, arxiv 2512.12117) montre que BM25+dense hybrid bat dense seul de 14–18 pp.

**Signature proposée** :
```python
search_codebase(pattern, semantic=True, hybrid=True)
```
- `hybrid=True` par défaut quand `semantic=True`.
- FTS5 sur `embed_doc` (name+sig+doc_head) + RRF fusion avec Nomic.

**Pourquoi mieux** : +10–15 pp attendus sur MRR@10 (extrapolation des résultats mémoire RRF + confirmation dans la littérature code-specific).

**Remplace** : rien. Extend le mode semantic actuel.

**POC** (pseudo-code) :
```python
def search_symbols_hybrid(query, project_root, limit=10):
    bm25_hits = symbol_fts5.match(query)[:limit*2]
    nomic_hits = search_symbols_semantic(query, project_root, limit=limit*2)["hits"]
    return rrf_merge(bm25_hits, nomic_hits, limit=limit, k=60)
```

#### C2 `[P1, 3d, hi]` — Tree-sitter migration phase 1 (TS + Go + Rust)

Cf. B2. Commencer par les 3 langages les plus utilisés dans l'audit (estimation), itérer. **Bench parser-accuracy obligatoire avant de déprécier les regex annotators.**

**Risque** : hi — touche le cœur de l'indexer. Parser dual pendant une période (old + new), bench accuracy sur snippets edge, switch derrière `TS_PARSER=tree_sitter|regex` env.

#### C3 `[P1, 2d, med]` — MCP resources pour ambient context

**Problème** : l'agent appelle `get_project_summary` + lit `.token-savior/hint.md` via tool — ça pourrait être des MCP resources lues une fois au handshake.

**Signature** : 3 resources
- `ts://project/summary` — même payload que `get_project_summary`
- `ts://project/hint` — contenu de `.token-savior/hint.md`
- `ts://project/entry-points` — même payload que `get_entry_points`

**Pourquoi mieux** : `resources` sont lues par l'agent "gratuitement" au setup, pas comptées comme tool-call. Moins de tokens dans l'historique.

**Remplace** : pas les tools, mais réduit leur fréquence d'appel.

**Dépendance client** : Claude Code supporte resources (vérifié spec nov 2025).

#### C4 `[P1, 2d, med]` — `explore(intent, target, depth)` polymorphe

**Problème** (Phase 2a) : 5-10 tools jumelés (get_dependencies/dependents, get_function_source/class_source/full_context). Un agent hésite.

**Signature proposée** :
```python
explore(
    intent: Literal["locate", "read", "graph", "rank"],
    target: str,                 # symbol name or file path
    depth: int = 1,              # for graph intent
    kind: Literal["auto","function","class","file"] = "auto",
    direction: Literal["in","out","both"] = "both",   # for graph
    source_level: int = 0,       # for read: 0 full ... 3 oneliner
)
```

Route interne :
- `intent=locate` → `find_symbol`
- `intent=read` → `get_function_source` / `get_class_source` (selon kind)
- `intent=graph, direction=out` → `get_dependencies`
- `intent=graph, direction=in` → `get_dependents`
- `intent=graph, direction=both, depth≥2` → `get_change_impact` ranké
- `intent=rank` → `find_hotspots` scope-limité

**Pourquoi mieux** : 1 tool à apprendre au lieu de 8. Précédent : `apply_refactoring` fonctionne déjà comme ça chez nous.

**Remplace** : `find_symbol`, `get_function_source`, `get_class_source`, `get_dependencies`, `get_dependents`, `get_change_impact` (ou en alias).

**Risque** : medium — grosse réorganisation de description. À faire après A3 (descriptions).

**POC** :
```python
def explore(intent, target, **kw):
    if intent == "locate": return _find_symbol(target)
    if intent == "read":
        kind = _resolve_kind(target, kw.get("kind", "auto"))
        return _get_source(target, kind, level=kw.get("source_level", 0))
    if intent == "graph":
        depth = kw.get("depth", 1)
        direction = kw.get("direction", "both")
        if depth >= 2: return _change_impact_ranked(target, depth, direction)
        if direction == "out": return _get_dependencies(target)
        if direction == "in": return _get_dependents(target)
        return {"in": _deps_in(target), "out": _deps_out(target)}
    if intent == "rank":
        return _find_hotspots(scope=target)
```

#### C5 `[P1, 1d, lo]` — `memory_diff_vs_code(id)` + trust policy

Cf. A4. Trust field + diff tool = le filet de sécurité qui manque à Recall.

#### C6 `[P2, 2d, hi]` — SCIP export / import

Cf. Phase 3.4. Export `project.scip` permet d'utiliser notre index via Sourcegraph CLI ou GitLab Code Intelligence. Import SCIP (depuis pyright, rust-analyzer, gopls) nous donnerait une ground-truth sémantique sans LSP runtime.

**Priorité P2** — nice-to-have, pas critique pour le core UX.

#### C7 `[P2, 3d, hi]` — Reproduire LongMemEval sur Recall

Pour sortir de la "claim-by-feelings" sur la mémoire (Phase 3.7), produire un score LongMemEval officiel. Permettrait de comparer scientifiquement à Mem0/Zep/Letta/OMEGA.

**Gros effort** — dataset à acquérir, harness à monter, bench à tourner sur plusieurs modèles.

### Pistes du brief utilisateur — verdict rapide

| Piste | Verdict | Justification |
|---|---|---|
| Tree-sitter partout | **Accepté** (B2, C2) | 19/21 parsers regex = dette mesurable ; tree-sitter standard 2026 |
| Cache binaire (sqlite WAL / msgpack) | **Accepté msgpack (B5)**, sqlite non | msgpack = win clair ; sqlite = over-engineering pour un cache single-writer |
| Semantic layer optionnel BM25+embeddings | **Accepté, hybrid obligatoire (C1)** | Nomic déjà shipped ; manque BM25 sur symboles, litt. confirme +10–18 pp |
| Consolidation agressive (N→15 + polymorphe `explore`) | **Accepté (C4)**, mais après A5 télémétrie | 15 fixes risqué sans données d'usage. Précédent `apply_refactoring` valide le pattern |
| Profiles intelligents par défaut | **Partiel** — `defer_loading` (B1) bat profiles | Profile = manuel ; defer_loading = auto côté agent. B1 est supérieur |
| File watcher | **Accepté (B3)** | 0 coût runtime, ferme la live-editing window |
| MCP resources / prompts | **Accepté (C3)** | Réduit les roundtrips ambient |
| Structured content | **Accepté** (à folder dans C1/C4) | Réduit tokens de sérialisation clés répétées |
| Politique confiance + diff mémoire | **Accepté (A4, C5)** | Safety filet manquant |

### Ordre d'exécution recommandé

1. **Semaine 1** : A1 (docs) + A2 (caps get_functions) + B1 (defer_loading) + B5 (msgpack) + B3 (watcher)
   → livre un "TS v2.8" propre, token-efficient côté MCP, rapide côté I/O.
2. **Semaine 2** : A3 (descriptions) + A5 instrumentation (télémétrie 2 semaines avant consolidation)
3. **Semaine 3–4** : C1 (BM25 hybrid symboles) + B4 (PageRank change_impact) + A4/C5 (trust memory)
4. **Semaine 5–8** : B2/C2 (tree-sitter migration) + C3 (resources) + C4 (explore polymorphe après télémétrie)
5. **Semaine 9+** : C6/C7 selon appétit.

### Ce que je garde sans toucher

- ProjectIndexer + SlotManager : le design à 4-handler-categories + throttled git + session-result cache est sain.
- Memory progressive disclosure L1/L2/L3 : conceptuellement juste, ne pas casser.
- Bench harness (code_retrieval + library_retrieval + memory_retrieval) : garde-fou indispensable pour toute modification.
- `apply_refactoring` pattern polymorphe : précédent à généraliser pas à abandonner.
- Nomic + sqlite-vec + RRF : stack correct, fraîchement benché.

---

## Sources complètes

- [MCP Specification 2025-11-25](https://modelcontextprotocol.io/specification/2025-11-25)
- [One Year of MCP: November 2025 Spec Release](https://blog.modelcontextprotocol.io/posts/2025-11-25-first-mcp-anniversary/)
- [Update on the Next MCP Protocol Release](https://modelcontextprotocol.info/blog/mcp-next-version-update/)
- [Aider — Building a better repository map with tree sitter](https://aider.chat/2023/10/22/repomap.html)
- [DeepWiki — Aider Repository Mapping System](https://deepwiki.com/Aider-AI/aider/4.1-repository-mapping)
- [Aider — Repository map docs](https://aider.chat/docs/repomap.html)
- [GitHub oraios/serena](https://github.com/oraios/serena)
- [a2a-mcp.org — Serena MCP](https://a2a-mcp.org/entry/serena-mcp)
- [Deconstructing Serena's MCP-Powered Semantic Code Understanding (Medium)](https://medium.com/@souradip1000/deconstructing-serenas-mcp-powered-semantic-code-understanding-architecture-75802515d116)
- [Sourcegraph — SCIP announcement](https://sourcegraph.com/blog/announcing-scip)
- [Sourcegraph — Precise code navigation](https://sourcegraph.com/docs/code-search/code-navigation/precise_code_navigation)
- [GitHub sourcegraph/scip](https://github.com/sourcegraph/scip/)
- [Tree-sitter vs. Language Servers (HN)](https://news.ycombinator.com/item?id=46719899)
- [Tree-sitter vs LSP: Why Hybrid IDE Architecture Wins (byteiota)](https://byteiota.com/tree-sitter-vs-lsp-why-hybrid-ide-architecture-wins/)
- [Explainer: Tree-sitter vs. LSP (Lambda Land, 2026-01)](https://lambdaland.org/posts/2026-01-21_tree-sitter_vs_lsp/)
- [CodeRLM HN thread](https://news.ycombinator.com/item?id=46974515)
- [Anthropic — Tool search tool](https://platform.claude.com/docs/en/agents-and-tools/tool-use/tool-search-tool)
- [Anthropic Engineering — Advanced tool use](https://www.anthropic.com/engineering/advanced-tool-use)
- [Extend Claude with skills — Claude Code Docs](https://code.claude.com/docs/en/skills)
- [Anthropic brings MCP tool search to Claude Code (tessl.io)](https://tessl.io/blog/anthropic-brings-mcp-tool-search-to-claude-code/)
- [Claude Code just got updated (VentureBeat)](https://venturebeat.com/orchestration/claude-code-just-got-updated-with-one-of-the-most-requested-user-features)
- [Best AI Agent Memory Frameworks 2026 (Atlan)](https://atlan.com/know/best-ai-agent-memory-frameworks-2026/)
- [From Beta to Battle-Tested: Letta, Mem0, Zep (Medium)](https://medium.com/asymptotic-spaghetti-integration/from-beta-to-battle-tested-picking-between-letta-mem0-zep-for-ai-memory-6850ca8703d1)
- [5 AI Agent Memory Systems Compared (dev.to 2026)](https://dev.to/varun_pratapbhardwaj_b13/5-ai-agent-memory-systems-compared-mem0-zep-letta-supermemory-superlocalmemory-2026-benchmark-59p3)
- [Survey of AI Agent Memory Frameworks (Graphlit)](https://www.graphlit.com/blog/survey-of-ai-agent-memory-frameworks)
- [Enhance Your LLM Agents with BM25 (towardsai)](https://towardsai.net/p/artificial-intelligence/enhance-your-llm-agents-with-bm25-lightweight-retrieval-that-works)
- [Citation-Grounded Code Comprehension (arxiv 2512.12117)](https://arxiv.org/html/2512.12117v1)
- [BGE-m3 HuggingFace](https://huggingface.co/BAAI/bge-m3)
- [GitHub: BM25 vs Vector Search for Large-Scale Code (ZenML)](https://www.zenml.io/llmops-database/bm25-vs-vector-search-for-large-scale-code-repository-search)
