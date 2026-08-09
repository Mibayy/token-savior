# Changelog

## Unreleased — Reading by line number

An audit of 1 047 real code reads done in Bash rather than through this server
found that 93,2 % never tried a tool at all (only 1,4 % were repairs after a
failure), and that among the reads the index *could* have served, the dominant
shape was `sed -n '105,150p' path.py`. The caller held a line number — from a
traceback, a `grep -n` hit, a compiler error — and every read tool on the
surface asked for a symbol name.

- **`read_lines(file_path, start, end)`.** 1-indexed, inclusive, numbered by
  default (`numbers=false` to get raw text back). `end` is optional: with only
  a `start`, it returns a 60-line window, which is the shape a traceback
  gives you. Capped at 400 lines by default (`max_lines`) so a huge `end`
  can't turn back into `cat`, with a marker naming the `start` to resume from.
  Names the enclosing function or class after the excerpt, so a range that
  turns out to be the wrong unit routes to `get_function_source` instead of a
  second blind guess. The engine already had `get_lines` — tested, reachable
  from the Python API, exposed by no tool. Also whitelisted for `ts_execute`
  scripts, where it was equally missing.
- **`TS_STICKY_ACTIVE` now holds for unregistered directories.** The freeze
  lived in `server_state.noter_racine_active`, at the dispatch layer, but
  `SlotManager.resolve` assigned `active_root` itself on one branch: the real
  path nobody had registered. That is exactly the /tmp clone, the export
  folder, the scratch file — 23,4 % of those same Bash reads. So the one
  guarantee meant to stop a parallel agent from repointing the shared default
  failed precisely where the need is most common, and the next hint-less call
  silently answered from the wrong repository. Default behaviour is unchanged
  (promotion still happens with the flag off).

## Unreleased — One server, many worktrees, no stolen calls

Parallel agents in sibling worktrees shared one server and one mutable
`active_root`, and the worktree a session actually worked in was often never
even registered. Verified against the hosts, not their folklore: Claude Code
exports `CLAUDE_PROJECT_DIR` (stable — always the main checkout, never the
worktree); `CLAUDE_PROJECT_ROOT` is set by no host at all; Codex exports no
path variable and whitelist-filters the MCP server environment, so only the
spawn cwd carries its signal.

- **Per-call routing by absolute path.** A call with no `project` hint but an
  absolute `file_path`/`path`/`target_file` now routes to the tree that owns
  the path — nearest marker root, so a worktree nested in a registered repo
  (`repo/.claude/worktrees/wt`) wins over the parent checkout that contains
  it. A linked worktree's `.git` *file* counts as a marker. The owning root
  registers on the fly, and the shared `active_root` is never touched.
- **Path hints resolve to their owner.** `project=` hints that are paths go
  through the same nearest-root routing; a subdirectory of a registered
  project no longer gets registered as a project of its own. Removed a
  literally duplicated reverse-containment/register block in `resolve()`.
- **Boot signals, validated and ranked.** `CLAUDE_PROJECT_ROOT` (deliberate,
  ours: may register, always wins) → launch-directory root (the only signal
  that follows worktrees and the only one Codex has; when it is a linked
  worktree it takes active) → `CLAUDE_PROJECT_DIR` (automatic: promotes among
  registered roots, never registers — pytest under Claude Code would adopt
  the developer's repo at import otherwise). A candidate counts only if it
  exists and carries a project marker. The launch directory now always gets
  a slot even when `WORKSPACE_ROOTS` pinned the registry.
- **`TS_STICKY_ACTIVE=1`** freezes `active_root` for multi-agent sessions:
  explicit hints and path arguments still route every call, but no call
  repoints the default the other agents fall back to.
- Hooks and the standalone CLI now fall back `CLAUDE_PROJECT_ROOT` →
  `CLAUDE_PROJECT_DIR` → `$PWD`; the session-start memory hook previously
  read only the first, which no host sets, and ran with an empty project.

## v4.21.0 — The server stops keeping what it knows to itself (2026-07-28)

Three things the server knew and never told anyone.

**It knew which tools only read, and said the opposite.** MCP defaults
`readOnlyHint` to false and `destructiveHint` to true, so every one of the 69
tools reached the client looking potentially destructive — `get_function_source`
included. Any consumer wanting a safe subset had to hard-code its own list and
keep it in sync by hand; that list drifts silently the day a tool is added. The
classification now lives in `tool_annotations.py`, `list_tools()` ships the four
hints, and `read_only_tool_names()` is exported so a judge loop derives the safe
set from the server instead of copying it.

A classification is worth what its guard is worth.
`test_no_unclassified_writer_slips_through` greps for the **absence** of
classification: any tool whose name carries a write verb and is missing from
`MUTATING_TOOLS` fails CI, with an explicit derogation list for the three
readers whose names look like writers. Verified by mutation — dropping
`replace_symbol_source` from the set breaks three tests.

**It knew it had cut a result, and stayed quiet.** 41 tools accept a bound and
honour it. None said it truncated, so a response holding exactly `max_results`
items was indistinguishable from a complete one. A caller that counts those
items believes it measured a total and actually measured the bound — two counts
that both saturate the limit read as two equal values when they are two
truncations. The notice is added at the single point where a raw handler result
is wrapped.

It also separates two things the schemas conflate: bounds that govern a *number*
of elements, comparable to a list length, and bounds that govern a *size* in
bytes or lines, comparable to nothing. `max_symbols_per_file` sits with the size
bounds for a third reason — it caps symbols within *each file*, so comparing it
to the overall total would be wrong in both directions. Multiple bounds in one
call are all considered: `get_change_impact` carries `max_direct` **and**
`max_transitive`, and keeping only the first would miss the truncation governed
by the other. The coverage test found fourteen bounds a hand inventory had
missed.

This is deliberately not pagination. The handler has already cut by the time we
reach that seam, so `total_count` and `next_offset` are not knowable there. It
closes the "I don't know that I don't have everything" class and leaves the
per-handler work for later.

**It paid for its own bookkeeping on every call.** `record_tool_call` re-reads
and re-writes the counter file under an inter-process flock; paid synchronously
inside the dispatch path, that cost sat on every single tool call. The obvious
fix — one daemon thread per call — is wrong: a daemon thread is killed abruptly
at interpreter exit and can die mid read-modify-write, leaving truncated JSON.
A single worker now drains the queue and **aggregates**, so everything enqueued
while the previous write was in flight lands in the same locked write and the
cost stops scaling with the number of calls. `atexit` flushes what is still
queued — without it, short sessions would be systematically under-counted, which
is a bias, not just a loss. `record_tool_call` stays synchronous and durable for
its direct callers. Measured on an uncontended path: 0.489 ms per call down to
0.005 ms.


## v4.20.0 — What the tools promise, now measured (2026-07-27)

Fourteen defects, found by exercising all 69 tools on a real machine rather
than by running the suite again — the suite was green at 2369 tests while
every one of them was live. They shipped as four hurried patch releases the
same night; this single release replaces them, and 4.19.1 through 4.19.3 are
yanked.

**Two of them made the product unusable for its own stated purpose.**

- A `search_codebase(semantic=True)` held a write lock on the shared memory
  database for **more than 25 minutes**: the reindex runs inside the request,
  embedding is sequential by design, and the commit only came at the end of
  the loop. While it ran, nothing else could write, the WAL grew from 8 to
  15 MB, and **a second client could not start at all** — `run_migrations()`
  failed on `database is locked`. The multi-client story shipped in v4.11
  collapsed the moment a semantic search was running. A longer busy timeout
  fixed nothing, verified at 30 s. Committing per batch does.
- `detect_breaking_changes(ref=...)` **silently discarded the ref**. The
  schema exposes `ref`, the CLI sends `ref`, the docs say `ref="v1"` — the
  handler only read `since_ref`, which nobody sends. `HEAD~1`, `HEAD~3`,
  `HEAD~6` and a release tag all analysed `HEAD~1`. The project's own rule is
  "run it before a commit or PR": every check against a tag was comparing the
  last commit, and answering reassuringly.

**Two silent data-integrity defects, both answering `ok: true`.**

- **A project copied after indexing had its edits written into the original.**
  `cp -r` carries `.token-savior-cache.json`, which holds the original's
  absolute paths; loaded from the copy it matched on the git ref, so
  `replace_symbol_source(project=<copy>)` modified the *original* file and
  left the copy untouched. A cache whose recorded root does not match where it
  was loaded from is now ignored.
- **`replace_symbol_source` deleted decorators.** A symbol's indexed range
  starts at the first line of its block, decorators included, while
  `get_function_source` never shows them — so a caller who reads then replaces
  cannot restore them. The same defect ate a `@pytest.fixture` and a
  `@pytest.mark.parametrize` while this release was being written, breaking 73
  tests at once.

**Four tools cost more than they saved.** The claim this project rests on was
verified nowhere, so it was measured across 43 of the 69 tools:

- `get_function_source` on a three-line function returned 131 characters where
  reading the whole file cost 76 — the gap was exactly the
  `→ get_full_context(...)` hint, appended to *every* response regardless of
  size. Now proportionate: 54 characters.
- `get_edit_context` shipped the source twice, in full under `source` and
  again as `location.source_preview`. 459 characters against 216 for the chain
  it replaces; now 375.
- The cache acknowledgement was longer than the source it replaced on small
  symbols — 317 against 232 — and the code knew it, computing a saving of zero
  through a `max(0, ...)` without drawing the consequence.
- `capture_list` returned **29 836 characters** by default, twenty-four times
  the entire source of the audited project. Rows are bounded and the default
  limit is 20: 6 860 characters, down 77%.

**And six smaller ones**: `ts_execute` discarded every tool result already
served when a script timed out; `capture_put` with no arguments wrote an empty
capture attributed to "unknown"; `capture_search` without a query answered
`{"count": 0}`; a missing required argument returned a raw `KeyError`; an
abstract picked by the sampling bandit was cached as if it were the body; and
subprocess transports outlived their event loop, killing the process with
SIGSEGV at exit — 8 times out of 8 as soon as a second project was registered.

**The discipline guard no longer blocks legitimate work.** `ls dist/index-*.js`
splits into `dist/index-` and `.js`; that bare fragment resolved against the
current directory, walked up to the project root, and got the whole command
refused. A path must now have a stem and name a file that exists. The guard
still refuses native reads of indexed code — it is the noise that went, not
the rule. It remains opt-in via `TS_DISCIPLINE_GUARD=1`, so no install is
affected on upgrade.

**Verification is now part of the suite.** `tests/maison/` holds 320 tests
that exercise all 69 tools the way an agent calls them, on a project built to
contain what the tools claim to find. A coverage test fails if any tool in
`TOOL_SCHEMAS` is never exercised, and an economy test fails if any tool
starts costing more than reading the file it answers about.

2728 tests, ruff clean, zero warnings.

## v4.19.0 — A memory that always answers is not a memory (2026-07-26)

**Vector search had no distance floor.** A k-NN always returns k results, however
far they are. Any query — including one with no relation to anything stored —
came back with the least-distant observations, presented as relevant memories
and injected into the model as if it had lived them.

Distances measured on French observations, as returned by sqlite-vec:

```
relevant neighbours      0.85 – 0.99
unrelated neighbours     0.97 – 1.07
```

The bands overlap almost entirely. Worse: `redemarrer le serveur web` ranked
`Certificat SSL expire` (0.928) **ahead of** `Redemarrer nginx` (0.989), and a
nonsense query scored better (0.973) than the correct answer to a legitimate
one. The vector layer only contributes when it is clearly confident; elsewhere
lexical search is the one that knows. The floor is a measured value, and a test
guards it against being relaxed without re-measuring.

**Three more defects around the memory store, all found by trying to use it:**

- `memory_delete` returned `True` and `memory_get` still returned the
  observation. Deletion archives, and `observation_get` did not filter archived
  rows while `observation_search` did. A "deleted" memory could still reach the
  model. Now excluded by default, `include_archived=True` to audit or undelete.
- `capture_get(range="10-12")` returned the **entire** capture instead of three
  lines. Only `line:10-12` was recognised, and any unrecognised form silently
  meant "everything" — in a tool whose purpose is to save tokens. Natural ranges
  are accepted; an unknown one is refused with the accepted forms.
- The memory database ignored `TOKEN_SAVIOR_DATA_DIR` and `XDG_DATA_HOME`, the
  convention every other data path follows. Moving your data left captures and
  state in the new place and memory in the old one, silently.

**And the test suite was writing into the user's real database.** 284 test
observations were found in it. `conftest.py` now redirects the memory DB for the
whole suite: a per-file guarantee does not hold, since one future test forgetting
to patch is enough.

## v4.18.1 — Two clients on one repository, now covered by tests (2026-07-26)

A Claude Code session and a Codex session open on the same repository is not an
exotic setup, and it was never tested. Each client keeps its own in-memory
index: the moment one writes, the other's line ranges are stale, and its next
edit targets lines that have moved. That is precisely the corruption seen
during development.

The pre-edit reindex shipped in v4.15.2 already handles it. This release adds
the tests that prove it and stop it regressing: crossed edits from two
independent clients leave one definition, the neighbours above and below
untouched, and a file that still compiles.

No behaviour change — a verification that was missing.

## v4.18.0 — Recall found 4 queries out of 6 (2026-07-26)

"Recall" is in the product name and its quality had never been measured, only
its absence of errors. First measurement: **4 out of 6 queries returned the
right observation.**

SQLite FTS5 applies an implicit AND across the words of a bare query. A
natural-language sentence — exactly how an agent phrases things — therefore
required *every* word to appear in the observation, function words included.
`supprimer des donnees en prod` returned nothing against an observation
containing supprimer, donnees and prod, because it did not contain `des`.

Three passes now, narrowest first, each attempted only if the previous returned
nothing — so a query that worked before returns byte-identical results:

1. the query as written (unchanged behaviour);
2. an `OR` over the meaningful terms, function words dropped;
3. prefix matching for word families — FTS5 does not stem French, so `nommer`
   never joined `Nommage`, nor `branche` its plural. Reserved to terms of four
   letters or more; below that a prefix returns half the database.

Malformed queries (stray punctuation, misplaced operators) return a list rather
than raising `OperationalError` at the caller.

Measured on the same set after the change: **8 out of 8**, and a genuinely
absent query still returns nothing. Widening a search must not turn it into a
noise generator, so `tests/test_memory_recall_quality.py` pins both directions.

## v4.17.1 — `min_lines` looked broken, and the cache answered for another threshold (2026-07-26)

Two defects in `find_semantic_duplicates`, both found by demanding a known
answer instead of the absence of an error.

**A hidden floor.** On top of `min_lines`, an undocumented `len(source) < 50`
discarded short bodies. A caller explicitly passing `min_lines=1` saw the
parameter do nothing and concluded, reasonably from where they stood, that the
tool was broken. The floor still applies at the default threshold — without it
every one-line getter collides with every other — but asking for `min_lines=1`
now lifts it, because that is an explicit request to see small functions.

**A stale cache.** The hash cache was built once, with the `min_lines` of the
**first** call, then reused as-is. Any later call with a different threshold
received a result computed for the old one, with nothing to signal it. That is
the most expensive family of defect: an answer that is plausible, wrong, and
stable across retries. The cache is now keyed by threshold.

`tests/test_semantic_duplicates_seuil.py` pins both, including a test that runs
the two thresholds in either order — a result that depends on call order is not
a result.

## v4.17.0 — `get_routes` answered `[]` on FastAPI and Flask (2026-07-26)

It knew Next.js App Router and Spring. On the two most common Python web
frameworks it returned an empty list, which a caller reads as "this project has
no routes". A silence indistinguishable from an answer is worse than an error:
nothing signals that you should look elsewhere.

FastAPI, Flask, Starlette and Sanic are now detected: `@app.get("/x")`,
`@router.delete("/x/{id}")`, and `@app.route("/x", methods=[...])` with its
method list, falling back to `GET`. Recognising only an object literally named
`app` would miss half of any structured project, so any `<object>.<verb>`
decorator counts.

**And the reason this was found at all.** Of ~292 checks in a full audit, only
16 verified content; the other 281 verified that no error came back. A tool
answering confidently wrong passed. Two releases shipped the same day did
exactly that while passing 2253 then 2267 green tests.

`tests/test_analysis_correctness.py` inverts that: a project built so every
answer is known in advance — dead code, an exact AST duplicate, an import
cycle, routes, a model, an orphan variable in `.env`, an API break between two
git tags — and each test demands the expected content. Including what must
*not* be reported: a used function is not dead code, and a search with no
result invents nothing.

## v4.16.1 — A Java signature no longer starts with `def` (2026-07-26)

Three call sites built `def name(params)` regardless of language. On Java that
produced `def totalPour(quantite)`: a keyword that does not exist in the
language, and worse, **the parameter types dropped** even though
`qualified_name` carries them (`boutique.Tarif.totalPour(int)`). An agent
reading that signature can write a call that does not compile, with nothing to
warn it.

Signatures are now rendered in the file's own language:

| | before | after |
|---|---|---|
| Java | `def totalPour(quantite)` | `int totalPour(int quantite)` |
| TypeScript | `def totalPanier(lignes, prixUnitaire)` | `totalPanier(lignes, prixUnitaire)` |
| Ruby, Python | `def total_pour(quantite)` | unchanged, `def` is correct there |

Types are re-paired with names only when the counts match. A `(int,int)` for a
single name means the parser was imprecise, so the names are returned alone
rather than inventing a pairing — a wrong type is worse than a missing one.

## v4.16.0 — A class defined in two languages returned one of them, silently (2026-07-26)

Found by exercising Java and Ruby for the first time. `tree-sitter-java` and
`tree-sitter-ruby` have shipped for a long time and **no test had ever run
them**.

Functions collected their candidates and reported ambiguity. Classes returned
the **first** match and stopped. On a polyglot project — a `Tarif` class in
Java and another in Ruby — asking for `Tarif` returned the Java one, presented
as the only one, with nothing to suggest otherwise. That is the worst category
of defect: a wrong answer delivered with confidence, indistinguishable from a
right one.

Both resolution paths are fixed (the `symbol_table` shortcut and the
whole-index fallback), and the message names the files:

```
class 'Panier' is ambiguous; defined in 3 files:
app/panier.py, java/src/main/java/boutique/Panier.java, ruby/lib/panier.rb
```

An ambiguity that says where to look costs one more call. A silent one costs
three, plus whatever was built on the wrong answer. A class defined once still
answers directly, and `file_path` still settles it.

`tests/test_polyglot_class_ambiguity.py` covers Python, Java and Ruby together
— the first test in this repo to index all three.

## v4.15.2 — `move_symbol` failed every single time (2026-07-26)

Found by an adversarial audit of all 69 tools against a purpose-built project,
after 500-call audits on real recorded calls had missed it entirely: **no test
anywhere exercised this tool.**

`_h_move_symbol` called `slot.indexer.reindex()`, a method `ProjectIndexer`
does not have. Every successful move raised `AttributeError` *after* rewriting
both files: the work was done, the tool reported an error, and the caller
believed it had failed.

Fixing that surfaced a second defect immediately behind it. The result keys are
`from_file` and `to_file`; reindexing anything else left the index announcing
the symbol at its old location, so `find_symbol` lied right after a successful
move. Both files are now reindexed, and `tests/test_move_symbol_reindex.py`
covers the source file, the target file, and the index — the last one through
the query API rather than the index internals, because that is what the caller
actually sees.

Audit result on the fixed build: 281/281 tool calls answered correctly, 11/11
adversarial checks passed, **69/69 tools exercised** — including the five that
consume an identifier produced by another call (`capture_get`,
`capture_aggregate`, `memory_get`, `memory_delete`, `run_project_action`),
which no generic invocation could reach.

## v4.15.1 — The alias fix that did nothing, and the edits that overwrote themselves (2026-07-26)

**v4.15.0 shipped an argument-alias feature that never ran.** It translated
aliases just before dispatch, passed 2253 tests, and changed nothing in
production: the MCP SDK validates arguments against the advertised schema
*before* calling the handler, so the call was rejected before reaching the
translation. The tests called the helper directly and saw a function that works
perfectly, on a path nobody takes. A green suite that does not cross the real
path proves nothing.

Aliases are now declared **in the schema**: each becomes a property, and a
canonical `required` becomes an `anyOf` accepting either name. New
`tests/test_protocol_end_to_end.py` speaks the protocol over stdio and knows
nothing about the implementation — it fails on v4.15.0 and passes here, which
is the only reason to trust it.

**Structural edits now reindex the target file first.** They only reindexed
*after* writing. As long as the disk changes solely through Token Savior the
in-memory ranges stay correct; the moment anything else touches it — a `git
checkout`, a script, another tool — the recorded line ranges point elsewhere
and the write lands on the wrong lines. Observed repeatedly in one session:
earlier versions of functions resurrected, whole definitions duplicated, and no
signal at all. One file reparse costs nothing against silent corruption.

**Project resolution stops refusing what it can resolve.** Fuzzy matching only
looked for the hint *inside* a project name, so `scribe-transcription` never
found `scribe`; both directions are tried now, longest name first so `api`
cannot steal `api-client`, names under four characters ignored. A real path
nobody registered gets registered. A name matching an existing but unindexed
directory now says where it is instead of listing every known project.

**A file missing from the current index now says which project holds it.**

Audit result, replaying 200 recorded calls in chronological order against a
clean install: **192 answered correctly**, every remaining failure being one
call to a project that no longer exists on disk. `search_codebase` 44/44,
`replace_symbol_source` 31/31, `get_structure_summary` 22/22,
`get_function_source` 15/15.

## v4.15.0 — Accept what we can resolve (2026-07-26)

The audit that produced v4.14.1 dismissed seven of its eight failures as "not
defects". That was wrong on three of them, and the measurement says so.

**Argument names.** Of 295 real recorded calls, 9 used a parameter that does
not exist — and every one of them was the name a *sibling tool* uses for the
same thing. `query` comes from `ts_search`, `source` from
`replace_symbol_source`, `path` from half the others. The same concept carried
three names depending on the tool: `name` / `project` / `symbol_name` to point
at a target, `pattern` / `query` to search, `content` / `new_source` to pass
code. The caller was guessing because the API made them guess, and a wrong
guess costs a full round-trip.

Known aliases are now translated to the canonical name before dispatch. The
schema still advertises the canonical one: aliases catch, they do not replace.
A value already supplied under the right name always wins.

**Project resolution.** `scribe-transcription` never found the registered
`scribe` project: fuzzy matching only looked for the hint *inside* the project
name, never the reverse. It now tries both, longest name first so `api` cannot
steal what belongs to `api-client`, and ignores names under four characters
which would match half of everything.

**A real path that nobody registered is now registered.** Refusing it sent the
caller to `set_project_root` for no reason — the path was known, it existed,
and registering it was exactly the intent.

Ambiguity is still refused. Silently switching to the wrong project costs more
than an error does.
## v4.14.1 — A missing argument now says what to provide (2026-07-26)

Found by auditing all 69 tools one by one, replaying 100 real calls taken from
recorded sessions plus synthesised calls for the 53 tools no session had ever
used.

92 of the 100 real calls answered correctly. Of the eight that did not, seven
were not defects: four referenced projects or files absent from the registry,
and three were malformed calls from past sessions that the schema correctly
rejected — `switch_project(project=...)` instead of `name`, `search_codebase
(query=...)` instead of `pattern`, `insert_near_symbol(source=...)` instead of
`content`. Validation doing its job.

The eighth was real. **Three tools answered `Error: 'name'`** — the repr of a
Python `KeyError`, nothing else: `get_function_source`, `get_class_source`,
`get_full_context`, three of the most used tools in the set. For an LLM client
that is the worst possible message, naming neither the missing argument nor how
to obtain it, so the caller retries blind and pays the round-trip twice.

They now say what is missing, show an example, and point at `search_codebase`
or `ts_search` for when the exact name is unknown — matching what `find_symbol`
already did. `get_full_context` keeps accepting `names=[...]`; requiring `name`
would have broken batch mode.

Audit result for the rest: of the 34 tools with no `required` field in their
schema, 27 legitimately take no mandatory argument and 4 already returned an
explicit message. Only these three were wrong.

## v4.14.0 — The client tells us which projects are open (2026-07-26)

v4.13.0 guessed the user's projects from the filesystem. That closed most of
the gap but left one: a project opened *after* startup, or living outside the
usual folders, stayed invisible until someone called `switch_project`.

The protocol already answers this. **MCP `roots`** are declared by the client,
updated when the user opens or closes a workspace, and require no action from
the agent. Guessing is a fallback; being told is the mechanism.

On the first tool call of a session the server now asks the client for its
roots and registers any it does not already know, walking up to the owning
project when the client hands over a subdirectory. Clients that do not
implement the capability answer nothing and the discovered set stands — it is
optional in the specification, and not having it is not an error.

Ordering, from most to least authoritative:

1. `WORKSPACE_ROOTS` / `PROJECT_ROOT` — explicit configuration always wins;
2. MCP `roots` — what the client says the user has open;
3. auto-discovery — the cwd's project plus the usual code folders;
4. `switch_project("/absolute/path")` — registers and indexes on the spot.

Never raises: a server that dies because a client lacks an optional capability
is worse than one that guesses. Transport errors, malformed responses, unusable
URIs and missing sessions are all silent no-ops, each covered by a test.

## v4.13.0 — It finds your projects by itself (2026-07-26)

**A fresh install indexed nothing.** With no `WORKSPACE_ROOTS` and no
`PROJECT_ROOT`, the server started with an empty registry and stayed there
until the user hand-wrote a comma-separated list of every project they own.
Nothing failed, nothing warned — the tools simply had nothing to look at.

Measured on one real workstation over 26 days: **28% of all code reads went to
projects that were never listed**, one of them a 767-file repository that had
existed for months. That is not a documentation problem, it is a default.

Now, when nothing is configured, the server looks for projects instead of
starting blind:

1. the project containing the current working directory;
2. the direct children of the usual code folders — `~/projects`, `~/dev`,
   `~/src`, `~/code`, `~/repos`, `~/work`, `~/git`, `~/workspace`.

A project is a directory carrying `.git`, `pyproject.toml`, `package.json`,
`Cargo.toml`, `go.mod`, `pom.xml`, `Gemfile` or `composer.json`. Vendored
trees, virtualenvs, build output and hidden directories are skipped, the scan
is one level deep, and it stops at 40 projects. Set
`TOKEN_SAVIOR_AUTODISCOVER=0` to restore the previous behaviour; an explicit
`WORKSPACE_ROOTS` always wins and is never merged with discovery.

Verified end to end on a fresh virtualenv with a throwaway `$HOME`: three
projects found with no configuration at all, then `switch_project` and
`find_symbol` answering on them.

**Projects created later attach on demand.** `switch_project` accepts a full
path and registers an unknown project on the spot, indexing it immediately.

Two implementation notes, both learned the hard way:

- Discovery runs from `main()`, not from `_register_roots`. The latter is
  called at module import, so guessing there fired inside every unit test that
  imported the server and registered whatever sat near the test runner — two
  unrelated memory-viewer tests started failing.
- `project_root_of` checks **every** path component against the skip list, not
  just the directory being examined. A package inside `node_modules` usually
  carries its own `package.json`, so the walk returned the vendored package as
  a project root before it ever reached `node_modules`. Caught by a test, not
  by reading.

**Also:** the shipped hook scripts no longer contain machine-specific paths.
61 occurrences of a single developer's virtualenv, source checkout and data
directory were baked into eight `.sh` files that `ts init` has installed for
everyone since v4.11.0 — they could not work on any other machine, silently.
Paths now resolve at run time from `$HOME`, `$XDG_DATA_HOME` and the
interpreter that can import `token_savior`.

## v4.12.2 — `pip install token-savior-recall` produced a server that could not start (2026-07-26)

**The MCP server did not start on a default install.** `mcp` was declared as
an *optional* dependency, so `pip install token-savior-recall` gave you a
package whose main entry point died immediately:

```
ModuleNotFoundError: No module named 'mcp'
```

The README always said `pip install "token-savior-recall[mcp]"`, so anyone
following it was fine. But **`server.json` — the file the MCP registry serves
to clients for automatic installation — carried the plain identifier with no
extra**. Every client installing from the registry got a server that could not
run. This package is an MCP server; it now depends on `mcp` outright. The
`[mcp]` extra is kept so published install commands keep working.

Found by installing the published wheel into a clean virtualenv and speaking
the protocol to it — initialize, tools/list, then real tool calls — rather than
by reading the packaging. The same smoke test now passes end to end: 69 tools
listed, `find_symbol`, `get_function_source`, `search_codebase` and
`get_full_context` all answering on a throwaway project.

**Also in this release**, the discipline guard shipped in v4.12.0 was replaced
by its corrected version. The one published an hour earlier had a shell rule
measured at **68.5% false positives** when replayed against 9054 real tool
calls — `cd` alone accounted for 395 of them — because it required a reader
*somewhere* in the command and a code file *somewhere*, without checking the
link between them. It now splits into sub-commands and only accuses the one
whose head is a reader and which cites an indexed source file: 0% false
positives on the same replay, true detections preserved. A `Grep`/`Glob` rule
was added at the same time, with the tests it was missing.

## v4.12.1 — Registry identifier names the repository (2026-07-26)

`io.github.<account>/<name>` is a GitHub *repository* convention. The PyPI
package name had been copied into it, so the registry advertised
`io.github.Mibayy/token-savior-recall` — a repository that does not exist and
returns 404. The registry had accepted it because it validates the account,
not the repository.

Fixed to `io.github.Mibayy/token-savior`. The PyPI package keeps its name,
`token-savior-recall`, carried by `packages[].identifier`.

This is a patch release because the registry validates package ownership
against the `mcp-name:` comment in the README **as published on PyPI**, not in
the repository — so the corrected identifier only takes effect once PyPI
carries the updated README. Its error message is worth quoting for anyone
hitting it: the token must be followed by a space, a newline, an HTML tag or a
comment close, otherwise a longer name starting with the same prefix silently
fails to match.

## v4.12.0 — Discipline guard: enforce the rules instead of documenting them (2026-07-26)

Measured on this repo with `scripts/ts_audit.py`, one-day window:

```
get_edit_context: 0 vs 245 edits (GAP)
edit_without_context: 11
nudge edit_context: 12 fires
```

Zero calls against 245 edits. The rule had been written in `CLAUDE.md` from the
start, and the adoption nudges fired twelve times a day. Compliance was zero. A
written reminder does not constrain anything, however often it is read — so the
rules are now checked where they happen rather than restated.

**Added — `hooks/ts_discipline_guard.py`** (PreToolUse), four rules, each backed
by a measured waste rather than a style preference:

1. Editing a symbol whose context was never requested (`edit_without_context`).
2. Native `Edit`/`Write` on indexed source, which bypasses the symbol graph so
   the edit-impact block never fires.
3. Native `Read` on indexed source, pulling a whole file where
   `get_function_source` returns the symbol.
4. `grep`/`cat`/`sed`/`awk` on indexed source through the shell.

The non-obvious part is that requesting context unlocks **that symbol and no
other**. A single call at session start would otherwise open every subsequent
edit, making the guard a ceremony rather than a check.

**Opt-in.** The guard denies calls, so enabling it by default would break
existing installs on upgrade. It is inert unless `TS_DISCIPLINE_GUARD=1` is
set — same contract as `TS_BASH_COMPACT` and `TS_BASH_REWRITE`. Once enabled,
`TS_GUARD_OFF=1` wins, for the cases where structural editing genuinely does
not fit (module constants, decorators).

**Not refusing too much is the hard part.** A guard with false positives gets
switched off, and a guard that is off protects less than no guard at all
because it also grants the illusion of protection. Every exit door is covered
by a test: non-code files, files outside any indexed project, vendored trees,
file creation, and real Bash usage (tests, git, npm, systemctl, network).

Shipped for Claude Code and Codex. Codex exposes no `Edit`/`Write` tools, so
rule 2 does not apply there, and its bundle carries second-based timeouts.
Gemini names its shell tool `run_shell_command` rather than `Bash`, which the
guard does not yet recognise — not shipped there.

## v4.11.0 — Memory engine ships to every agent (2026-07-26)

The memory engine has shipped in `hooks/` since v4.2.0 and `ts init` never
installed it — for any agent, Claude Code included. `memory-hooks-config.json`
existed but was referenced by no bundle, so every user was expected to wire it
by hand. `SessionStart` carries the recall; without it the rest is decorative.

**Added**

- `ts init --agent openclaw`. OpenClaw loads *hook packs* — a directory with
  `HOOK.md` plus `handler.js` — rather than per-event shell commands, so the
  bundle declares a directory instead. New pack under
  `hooks/openclaw/token-savior-memory/`. Its handler imports nothing from
  OpenClaw: the internal modules are hash-named per version
  (`internal-hooks-D52pUqod.js`) and would break on the next update.
- Memory engine bundles for Codex (`memory-codex.json`) and Gemini CLI
  (`memory-gemini.json`), and the Claude bundle now actually includes
  `memory-hooks-config.json`.
- `AGENTS.md`, the convention read by roughly thirty agents.

**Three hook models, not one format to translate**

Every fact below was read from the shipped binary rather than from its
documentation, because both classes of mistake are silent — a hook registered
on an unknown event raises nothing at all, it simply never fires.

| | Claude Code | Codex | Gemini | OpenClaw |
|---|---|---|---|---|
| Model | command per event | command per event | command per event | hook pack |
| Timeouts | milliseconds | **seconds** | milliseconds | n/a |
| Shell tool | `Bash` | `Bash` | `run_shell_command` | no tool events |

Codex exposes no `Stop`, `StopFailure` or `ConfigChange`. OpenClaw exposes no
tool events whatsoever, so neither `tool_capture` nor the Bash rewriter can run
there; its context injection mutates `context.bootstrapFiles` during
`agent:bootstrap` instead of reading the hook's stdout. Gemini has neither
`UserPromptSubmit` nor `PreCompact`, and names its shell tool
`run_shell_command`.

Hermes (Nous Research) needs nothing written — it is a pure MCP client. Note
its `mcp_<server>_<tool>` prefix, single underscore, against
`mcp__<server>__<tool>` everywhere else: a matcher written for Claude Code
silently misses every Hermes tool.

**Tests**

`test_codex_hook_bundles.py`, `test_gemini_hook_bundles.py` and
`test_openclaw_hook_pack.py` lock the two silent failure modes per agent:
events absent from the binary, and timeouts carrying the wrong unit. Five
existing tests unpacked a fixed number of bundles (`(capture, rewriter) = ...`)
so any addition broke unrelated tests; they now select by content.

## v4.10.0 — Community fixes, `ts gain`, `compact-only` (2026-07-25)

Contributor pass: every open pull request from @andrebrait applied, plus the
remaining reported bugs. His branches predated the history rewrite that purged
a leaked credential from this repo, so they were cherry-picked onto the new
`main` rather than merged — merging would have pushed the purged commits back
in. Authorship is preserved on every commit.

**Fixes**

- `tool_capture_hook` no longer crashes with `AttributeError` when
  `tool_response` arrives as a list of content blocks, which is what MCP tools
  deliver (#48).
- `TOKEN_SAVIOR_MAX_FILE_SIZE` / `TOKEN_SAVIOR_MAX_FILES` are reachable again:
  the parameter defaults were truthy, so the env fallback was dead in the MCP
  server path and files over 500 KB were silently skipped (#49).
- The index cache is keyed by the configuration that built it, not just a
  version int. Changing `INCLUDE_PATTERNS` or upgrading the package no longer
  serves a stale index that quietly answers from the old file set (#61).
- Git compactors stop misparsing the Bash rewriter's own machine formats. With
  both enabled, a dirty tree was reported as `clean` and rewritten diffs
  rendered empty with a false ~100% saving (#46).
- The PreToolUse pytest rewriter accepts `uv run pytest`, `poetry`/`hatch`/
  `pdm`/`rye run pytest`, `python3 -m pytest` and venv-prefixed forms. The
  PostToolUse compactor learned these in v4.3.0; the rewriter had not (#41).
- The startup profile banner is opt-in via `TOKEN_SAVIOR_BANNER=1`. It went to
  stderr on every start, and PowerShell plus several MCP clients on Windows
  surface stderr as an error, so a healthy server looked broken (#44).
- `ts init` writes portable hook commands and a real Codex `hooks.json` schema,
  and honours `CLAUDE_CONFIG_DIR` for global scope and transcript discovery.
- Daemon-client tests keep their sockets under the `AF_UNIX` path limit.

**Features**

- `ts gain [--project ROOT] [--format text|json|compact]` reports savings
  without the dashboard. `compact` is the statusline badge, `[TS 372.8M↓]`.
  Scripts previously had to parse the stats JSON by hand (#63, #39).
- `TOKEN_SAVIOR_PROFILE=compact-only` advertises `ts_discover` and nothing
  else, for setups already running symbol navigation and memory elsewhere.
  1 200 chars of manifest against 5 833 for `optimized` (#42).
- Shell annotator, `.sh`/`.bash` indexed by default (#51).
- PHP annotator, `.php`/`.inc` indexed by default (#50).
- `find_symbol` reports the kinds it searched and indexes module/class
  variables.

**Docs**

- Complete environment-variable reference in the README, and the
  `TS_PROFILE` (tsbench) versus `TOKEN_SAVIOR_PROFILE` (server) trap is called
  out explicitly (#56).

## v4.9.0 — Edit-impact block replaces the get_edit_context nudge (2026-07-15)

Usage audit over 425 real sessions: `get_edit_context` called **0 times across
219 edits**, and the `[NUDGE]` pointing at it fired 12 times and converted 0.
A post-hoc advisory asking the agent to ADD a pre-edit call never lands. So we
stop nudging and fold the value into the edit result instead.

- `server.py`: after a successful `replace_symbol_source` / `insert_near_symbol`
  / `add_field_to_model` / `move_symbol`, `_edit_impact_notice()` appends a
  compact `[EDIT IMPACT]` block listing the edited symbol's callers + impacted
  tests (reuses the same `get_dependents` + `find_impacted_test_files` query
  functions as `get_edit_context`). The safety value ("did you break a caller
  you never saw?") is now delivered by default -- no habit to adopt. Opt out
  with `TOKEN_SAVIOR_EDIT_IMPACT=0`.
- `_detect_chain_nudge`: Pattern 3 (edit-without-context nudge) retired, now
  superseded by the edit-impact block.
- Best-effort: any query failure yields no block rather than disturbing the
  edit result. `tests/test_edit_impact.py` (7 tests) + updated chain-nudge tests.

## v4.8.0 — Observations as MCP resources (2026-07-04)

Formalises the `ts://obs/{id}` scheme (already printed by memory_index) as real
MCP resources, so clients that support resource `@`-mentions (Claude Code) can
pull a specific stored memory without a tool round-trip.

- `server_handlers/resources.py`: `list_observation_resources()` (bounded, ranked
  by the memory_index score) and `read_observation_resource(uri)`.
- Wired in `server.main()` via `list_resources`/`read_resource` handlers,
  opt-out with `TS_RESOURCES_DISABLED=1`. Read-only and additive -- the tool
  dispatch path is untouched.

Tests: test_resources.py (read roundtrip, bad/missing URIs, list scoping).
Suite: 1792 passed.

## v4.7.0 — Self-audit, nudge telemetry, TCA revival, warm-daemon delegation (2026-07-04)

Closes the loop the v4.4→v4.6 passes exposed: fixes were shipped but never
measured (and, it turned out, never even deployed -- see v4.6 notes). This adds
the instrumentation to know whether they work, and fixes a silently-dead ML path.

**Automated usage audit (scripts/ts_audit.py).** Reproduces the manual
memory.db + tool-calls.json dig as a one-shot report: per-tool latency p50/p95,
wasteful chains (tool-level, 60s), adoption gaps (edits without get_edit_context,
nav bursts vs ts_execute, set_project_root churn), nudge fires, and ML liveness.
Re-run after a deploy to see whether behaviour moved. Baseline 2026-07-04
confirmed get_edit_context 0/207, TCA dead, ts_search p50 4564ms.

**Persistent nudge telemetry (telemetry.py + server.py).** Each chain-nudge
fire is now counted by kind in `nudge-stats.json` (`record_nudge`/`nudge_counts`).
Effectiveness = nudge fires here vs the target tool's rise in tool-calls.json
over successive audits.

**TCA co-activation revival (server.py + tca_engine.py).** The audit found TCA
`session_count` stuck at 0 for the whole deployment: `record_activation` filled
the in-session buffer but `flush_session` was never called, so the co-activation
tensor stayed empty and `get_coactive_symbols` always returned []. Now flushed at
each switch_project boundary and via an atexit hook. Not dead code to cut -- a
broken feature now made live.

**Warm-daemon ts_search delegation, actually enabled (ts-daemon.service).** v4.6
built the delegation but the daemon ran system python without fastembed
(substring only). The unit now launches the venv python (fastembed present), so
delegated ts_search returns embedding quality warm: measured 1505ms cold model
load then **23ms warm** (vs ~1.5-5.7s in-process per client spawn).

Tests: test_nudge_telemetry.py, test_tca_flush_wiring.py. Suite: 1786 passed.

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

Real-world bench against 7 days of live transcripts (1130 Bash outputs)
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

Non-breaking bug fix surfaced during v2.8.1 validation on a
1704-file workspace. A file created at project root (e.g. `foo.py`) was being
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
