"""Handlers for query-function code-navigation tools and the CSC subsystem.

CSC (Compact Symbol Cache) returns a compact stub for repeat reads of a
symbol whose body has not changed, falling back to the full source when
the body has changed (or when force_full / level>0 is requested).

The 28-entry _QFN_HANDLERS dispatch table is the second half of the
call_tool fan-out (after _META_HANDLERS, _MEMORY_HANDLERS, _SLOT_HANDLERS).
"""

from __future__ import annotations

from typing import Any

import json

from token_savior import memory_db
from token_savior import server_state as state

_BATCH_MAX = 10


def _resolve_batch_names(args: dict[str, Any]) -> list[str] | None:
    """Return list of names if batch mode, else None (single mode)."""
    names = args.get("names")
    if not names:
        return None
    if not isinstance(names, list):
        return None
    return names[:_BATCH_MAX]


def _batch_dispatch(qfns, args: dict[str, Any], single_handler):
    """Run single_handler for each name in batch, return JSON dict."""
    names = _resolve_batch_names(args)
    if names is None:
        return None
    results = {}
    for name in names:
        per_args = {**args, "name": name, "hints": False}
        per_args.pop("names", None)
        try:
            results[name] = single_handler(qfns, per_args)
        except Exception as exc:
            results[name] = f"Error: {exc}"
    return json.dumps(results, indent=2, default=str)


# ---------------------------------------------------------------------------
# CSC subsystem
# ---------------------------------------------------------------------------


def _lookup_symbol_meta(slot, args: dict[str, Any]) -> tuple[str, str, str] | None:
    """Return (kind, body_hash, signature) for a symbol, or None if unresolved."""
    try:
        idx = slot.indexer._project_index
    except AttributeError:
        return None
    name = args.get("name")
    if not name or idx is None:
        return None
    file_path = args.get("file_path")

    def _check(meta, rel_path):
        for func in meta.functions:
            if func.name == name or func.qualified_name == name:
                sig = f"def {func.name}({', '.join(func.parameters)})"
                return ("function", func.body_hash, sig)
        for cls in meta.classes:
            if cls.name == name:
                sig = f"class {cls.name}"
                if cls.base_classes:
                    sig += f"({', '.join(cls.base_classes)})"
                return ("class", cls.body_hash, sig)
            for method in cls.methods:
                if method.qualified_name == name:
                    sig = f"def {method.qualified_name}({', '.join(method.parameters)})"
                    return ("function", method.body_hash, sig)
        return None

    if file_path:
        for rel_path, meta in idx.files.items():
            if rel_path == file_path or rel_path.endswith(file_path):
                got = _check(meta, rel_path)
                if got:
                    return got
        return None
    if name in idx.symbol_table:
        rel_path = idx.symbol_table[name]
        meta = idx.files.get(rel_path)
        if meta is not None:
            got = _check(meta, rel_path)
            if got:
                return got
    for rel_path, meta in idx.files.items():
        got = _check(meta, rel_path)
        if got:
            return got
    return None


def _csc_compact_response(
    name: str,
    signature: str,
    cache_tok: str,
    view_count: int,
    modified: bool,
    diff_preview: str = "",
) -> str:
    """Format a compact CSC hit response."""
    tag = "[MODIFIED]" if modified else ""
    header = f"@sym:{cache_tok} [{name}] {tag}".rstrip()
    lines = [header]
    if signature:
        lines.append(f"Signature: {signature}")
    if modified:
        lines.append(f"Changed body, prior views: {view_count}")
        if diff_preview:
            lines.append("Diff (first lines):")
            lines.append(diff_preview)
    else:
        lines.append(
            f"(body unchanged since last view - {view_count} view{'s' if view_count != 1 else ''} this session)"
        )
        lines.append(
            "Reuse the body you already have. Only re-call with force_full=true if you suspect external mutation."
        )
    return "\n".join(lines)


def _csc_diff_preview(old_full: str, new_full: str, max_lines: int = 5) -> str:
    """Return the first `max_lines` diff hunks between two bodies (trivial line diff)."""
    import difflib

    diff = difflib.unified_diff(
        old_full.splitlines(),
        new_full.splitlines(),
        lineterm="",
        n=1,
    )
    out: list[str] = []
    for line in diff:
        if line.startswith("@@") or line.startswith("---") or line.startswith("+++"):
            continue
        out.append(line)
        if len(out) >= max_lines:
            break
    return "\n".join(out)


def _csc_maybe_serve(
    slot,
    kind: str,
    args: dict[str, Any],
    produce_full,
) -> str:
    """Entry point for get_function_source / get_class_source with CSC.

    `produce_full` is a zero-arg callable returning the full formatted source.
    Returns either the compact stub (cache hit, body unchanged) or the full
    source (miss / force_full / modified).
    """

    force_full = bool(args.get("force_full", False))
    level = int(args.get("level", 0) or 0)

    full = produce_full()
    # Skip cache when:
    # - caller asked for non-L0 abstraction (already compact by design)
    # - symbol wasn't resolvable (error messages must pass through verbatim)
    # - force_full is set
    if level > 0 or force_full or full.startswith("Error:"):
        return full

    meta = _lookup_symbol_meta(slot, args)
    if meta is None:
        return full
    _kind, body_hash, signature = meta
    if not body_hash:
        return full

    project_root = getattr(slot, "root", "") or ""
    name = args["name"]
    key = f"{kind}:{project_root}:{name}"
    entry = state._session_symbol_cache.get(key)

    from token_savior.symbol_hash import cache_token

    tok = cache_token(body_hash)

    if entry is None:
        # Miss — return full, record.
        state._session_symbol_cache[key] = {
            "cache_token": tok,
            "body_hash": body_hash,
            "view_count": 1,
            "full_source": full,
            "signature": signature,
        }
        return full

    entry["view_count"] += 1
    prior_full = entry.get("full_source", "")
    if entry["body_hash"] == body_hash:
        compact = _csc_compact_response(
            name=name,
            signature=signature,
            cache_tok=tok,
            view_count=entry["view_count"],
            modified=False,
        )
        saved = max(0, len(full) - len(compact))
        state._csc_hits += 1
        state._csc_tokens_saved += saved // 4
        return compact

    # Modified — return compact with diff preview; refresh cache.
    diff_preview = _csc_diff_preview(prior_full, full)
    compact = _csc_compact_response(
        name=name,
        signature=signature,
        cache_tok=tok,
        view_count=entry["view_count"],
        modified=True,
        diff_preview=diff_preview,
    )
    saved = max(0, len(full) - len(compact))
    state._csc_hits += 1
    state._csc_tokens_saved += saved // 4
    entry.update(
        {
            "cache_token": tok,
            "body_hash": body_hash,
            "full_source": full,
            "signature": signature,
        }
    )
    return compact


# ---------------------------------------------------------------------------
# Query-function handlers (qfns dict + arguments → result)
# ---------------------------------------------------------------------------


def _q_get_class_source(qfns, args: dict[str, Any]) -> str:
    batch = _batch_dispatch(qfns, args, _q_get_class_source)
    if batch is not None:
        return batch
    slot, _ = state._slot_mgr.resolve(args.get("project"))
    explicit_level = "level" in args and args.get("level") is not None
    if explicit_level:
        chosen_level = int(args.get("level") or 0)
        ctx_type = None
    else:
        ctx_type = memory_db._detect_context_type(state._prefetcher.call_sequence)
        chosen_level = memory_db.thompson_sample_level(ctx_type)
    result = _csc_maybe_serve(
        slot,
        "class",
        args,
        lambda: qfns["get_class_source"](
            args["name"],
            args.get("file_path"),
            max_lines=args.get("max_lines", 0),
            level=chosen_level,
        ),
    )
    if ctx_type is not None:
        try:
            success = bool(result and not result.startswith("Error"))
            memory_db.record_lattice_feedback(ctx_type, chosen_level, success)
        except Exception:
            pass
    if args.get("hints", True) and isinstance(result, str) and result and not result.startswith("Error"):
        if _navigation_calls_so_far() >= _OVER_EXPLORATION_THRESHOLD:
            result += f"\n\n{_stop_hint()}"
        else:
            result += (
                f"\n\n→ get_full_context('{args['name']}') "
                "for source + callers + dependencies in one call"
            )
    return result


def _q_get_function_source(qfns, args: dict[str, Any]) -> str:
    batch = _batch_dispatch(qfns, args, _q_get_function_source)
    if batch is not None:
        return batch
    from token_savior.server_runtime import _resolve_project_root

    slot, _ = state._slot_mgr.resolve(args.get("project"))
    explicit_level = "level" in args and args.get("level") is not None
    if explicit_level:
        chosen_level = int(args.get("level") or 0)
        ctx_type = None
    else:
        ctx_type = memory_db._detect_context_type(state._prefetcher.call_sequence)
        chosen_level = memory_db.thompson_sample_level(ctx_type)
    result = _csc_maybe_serve(
        slot,
        "function",
        args,
        lambda: qfns["get_function_source"](
            args["name"],
            args.get("file_path"),
            max_lines=args.get("max_lines", 0),
            level=chosen_level,
        ),
    )
    if ctx_type is not None:
        # Optimistic feedback: a non-empty result at the sampled level counts as
        # a success. Subsequent calls that re-request the same symbol at level=0
        # will register failures naturally as the prior corrects itself.
        try:
            success = bool(result and not result.startswith("Error"))
            memory_db.record_lattice_feedback(ctx_type, chosen_level, success)
        except Exception:
            pass
    try:
        project_root = _resolve_project_root(args)
        symbol_name = args["name"]
        file_path = args.get("file_path")
        rows = memory_db.observation_get_by_symbol(
            project_root, symbol_name, file_path=file_path, limit=3
        )
        if rows:
            lines = ["\n───", f"📌 Memory ({len(rows)}):"]
            for r in rows:
                age = r.get("age") or "?"
                stale = "⚠️ " if r.get("stale") else ""
                glob = "🌐 " if r.get("is_global") else ""
                lines.append(f"  #{r['id']}  [{r['type']}]  {stale}{glob}{r['title']}  — {age}")
            result += "\n".join(lines)
    except Exception:
        pass
    try:
        coactives = state._tca_engine.get_coactive_symbols(args["name"], top_k=3)
        if coactives:
            co_lines = ["\n🔄 Often accessed together:"]
            for co_sym, pmi in coactives:
                co_lines.append(f"  {co_sym} (PMI: {pmi:.2f})")
            result += "\n".join(co_lines)
    except Exception:
        pass
    try:
        comm = state._leiden.get_community_for(args["name"])
        if comm and comm["size"] <= 20:
            peers = [m for m in comm["members"] if m != args["name"]][:8]
            if peers:
                result += (
                    f"\n🏘️ Community '{comm['name']}' ({comm['size']} members): "
                    + ", ".join(peers)
                )
    except Exception:
        pass
    if args.get("hints", True) and isinstance(result, str) and result and not result.startswith("Error"):
        if _navigation_calls_so_far() >= _OVER_EXPLORATION_THRESHOLD:
            result += f"\n\n{_stop_hint()}"
        else:
            result += (
                f"\n\n→ get_full_context('{args['name']}') "
                "for source + callers + dependencies in one call"
            )
    return result


def _q_get_edit_context(qfns, args):
    sym_name = args["name"]
    max_deps = args.get("max_deps", 10)
    max_callers = args.get("max_callers", 10)
    ctx: dict[str, Any] = {"symbol": sym_name}
    location = None
    try:
        location = qfns["find_symbol"](sym_name)
    except Exception:
        location = None
    is_class = isinstance(location, dict) and location.get("type") == "class"
    try:
        if is_class:
            ctx["source"] = qfns["get_class_source"](sym_name, max_lines=200)
        else:
            ctx["source"] = qfns["get_function_source"](sym_name, max_lines=200)
    except Exception:
        try:
            if is_class:
                ctx["source"] = qfns["get_function_source"](sym_name, max_lines=200)
            else:
                ctx["source"] = qfns["get_class_source"](sym_name, max_lines=200)
        except Exception:
            ctx["source"] = None
    ctx["location"] = location
    try:
        dependencies = qfns["get_dependencies"](sym_name, max_results=max_deps)
        if is_class:
            class_name = location.get("name") if isinstance(location, dict) else None
            filtered_dependencies = []
            for dep in dependencies:
                dep_name = dep.get("name") if isinstance(dep, dict) else None
                dep_type = dep.get("type") if isinstance(dep, dict) else None
                if dep_name and dep_name.endswith("()"):
                    owner = dep_name.rsplit(".", 1)[0] if "." in dep_name else None
                    method_base = dep_name.rsplit(".", 1)[-1].split("(", 1)[0]
                    owner_simple = owner.rsplit(".", 1)[-1] if owner else None
                    if (
                        owner
                        and class_name == owner
                        and method_base == owner_simple
                        and dep_type in {None, "method"}
                    ):
                        continue
                filtered_dependencies.append(dep)
            dependencies = filtered_dependencies
        ctx["dependencies"] = dependencies
    except Exception:
        ctx["dependencies"] = []
    try:
        ctx["callers"] = qfns["get_dependents"](sym_name, max_results=max_callers)
    except Exception:
        ctx["callers"] = []

    # --- siblings: other symbols in the same file ---
    loc_file = location.get("file") if isinstance(location, dict) else None
    if loc_file:
        siblings = []
        try:
            for fn in qfns["get_functions"](file_path=loc_file, max_results=0):
                fn_name = fn.get("name") if isinstance(fn, dict) else None
                if fn_name and fn_name != sym_name:
                    siblings.append({"name": fn_name, "type": "function",
                                     "line": fn.get("line")})
        except Exception:
            pass
        try:
            for cls in qfns["get_classes"](file_path=loc_file, max_results=0):
                cls_name = cls.get("name") if isinstance(cls, dict) else None
                if cls_name and cls_name != sym_name:
                    siblings.append({"name": cls_name, "type": "class",
                                     "line": cls.get("line")})
        except Exception:
            pass
        ctx["siblings"] = siblings
    else:
        ctx["siblings"] = []

    # --- impacted tests ---
    try:
        impact = qfns["find_impacted_test_files"](symbol_names=[sym_name], max_tests=5)
        ctx["impacted_tests"] = impact.get("impacted_tests", [])
    except Exception:
        ctx["impacted_tests"] = []

    return ctx


# ---------------------------------------------------------------------------
# Navigation hints (gated by `hints: bool = True`)
# ---------------------------------------------------------------------------
#
# Evidence: IMPROVEMENT-SIGNALS.md reports retry storms on the localisation
# tools (TASK-022: 13 distinct get_function_source calls; TASK-043: 13 distinct
# get_functions calls). The agent treated each response as a dead-end. A small
# trailer that explicitly names the next callable breaks the dead-end loop.
#
# Over-exploration guard (tsbench-sonnet-ts TASK-102/103 evidence): when the
# session has already made many navigation calls, the trailer flips from
# "here are next tools" to "you have enough signal — answer now". Threshold
# picked from sonnet-ts losses (7-9 navigation calls for rubric-partial).

_OVER_EXPLORATION_THRESHOLD = 15
_NAV_TOOLS = {
    "get_function_source", "get_class_source", "get_functions", "get_classes",
    "find_symbol", "search_codebase", "list_files", "get_structure_summary",
    "get_routes", "get_entry_points", "get_feature_files", "get_imports",
    "get_full_context",
}


def _navigation_calls_so_far() -> int:
    from token_savior import server_state as state
    return sum(state._tool_call_counts.get(t, 0) for t in _NAV_TOOLS)


def _stop_hint() -> str:
    n = _navigation_calls_so_far()
    return (
        f"You have made {n} navigation calls on this project in this session. "
        "Consider answering with what you have rather than expanding exploration. "
        "If a specific detail is still missing, prefer one targeted call "
        "(get_function_source, get_change_impact) over broad listing. "
        "Never fabricate file paths or line numbers — if you cannot verify a reference, "
        "say so explicitly."
    )


def _hints_for_symbol(name: str, sym_type: str | None) -> list[str]:
    if _navigation_calls_so_far() >= _OVER_EXPLORATION_THRESHOLD:
        return [_stop_hint()]
    source_tool = "get_class_source" if sym_type == "class" else "get_function_source"
    return [
        f"full_context: get_full_context('{name}')",
        f"source:       {source_tool}('{name}')",
        f"callers:      get_dependents('{name}')",
        f"impact:       get_change_impact('{name}')",
    ]


_LIST_HINTS = [
    "full_context: get_full_context('<name>')",
    "source:       get_function_source('<name>') / get_class_source('<name>')",
    "callers:      get_dependents('<name>')",
    "impact:       get_change_impact('<name>')",
]


def _q_get_full_context(qfns, args: dict[str, Any]):
    batch = _batch_dispatch(qfns, args, _q_get_full_context)
    if batch is not None:
        return batch
    result = qfns["get_full_context"](
        args["name"],
        depth=args.get("depth", 1),
        max_lines=args.get("max_lines", 200),
    )
    mode = args.get("mode", "compact")
    if mode == "compact" and isinstance(result, dict):
        return _compact_full_context(result)
    return result


def _compact_full_context(result: dict) -> dict:
    """Trim heavy fields: deps/dependents → names only, source → head 80 lines."""
    compact: dict[str, Any] = {}
    for key in ("name", "file", "line", "type", "signature", "error", "symbol"):
        if key in result:
            compact[key] = result[key]
    src = result.get("source")
    if isinstance(src, str):
        lines = src.splitlines()
        if len(lines) > 80:
            compact["source"] = "\n".join(lines[:80]) + f"\n# ... +{len(lines) - 80} lines (use mode='full')"
        else:
            compact["source"] = src
    for key in ("dependencies", "dependents", "callers", "callees", "change_impact"):
        val = result.get(key)
        if isinstance(val, list):
            compact[key] = [
                item.get("name") if isinstance(item, dict) else item
                for item in val
            ]
        elif val is not None:
            compact[key] = val
    return compact


def _suggest_if_empty_search(result, pattern: str):
    if isinstance(result, list) and len(result) == 0:
        return {
            "matches": [],
            "_suggestion": (
                f"No matches for pattern='{pattern}'. "
                f"Try a broader regex, list_files(pattern='*{pattern}*'), "
                f"or check non-indexed files (.sql, .graphql, .prisma) with Read/Grep."
            ),
        }
    return result


def _suggest_if_empty_dependents(result, name: str):
    if isinstance(result, list) and len(result) == 0:
        return {
            "dependents": [],
            "_suggestion": (
                f"No dependents found for '{name}'. Likely a leaf / entry-point / "
                f"dead code. Try get_dependencies('{name}') to see what it uses, "
                f"or find_dead_code() for an audit."
            ),
        }
    return result


def _q_find_symbol(qfns, args: dict[str, Any]):
    batch = _batch_dispatch(qfns, args, _q_find_symbol)
    if batch is not None:
        return batch
    name = args["name"]
    result = qfns["find_symbol"](name, level=args.get("level", 0))
    if isinstance(result, dict) and "error" in result:
        result["_suggestion"] = (
            f"Symbol '{name}' not found. "
            f"Try: search_codebase('{name}') for a text search, "
            f"or get_functions() to list all functions."
        )
    elif args.get("hints", True) and isinstance(result, dict):
        result["_hints"] = _hints_for_symbol(
            result.get("name") or name, result.get("type")
        )
    return result


def _q_list_files(qfns, args: dict[str, Any]):
    pattern = args.get("pattern")
    result = qfns["list_files"](pattern, max_results=args.get("max_results", 0))
    if isinstance(result, list) and len(result) == 0 and pattern:
        return {
            "files": [],
            "_suggestion": (
                f"No files found for pattern='{pattern}'. "
                f"Try: list_files() without pattern to see all files, "
                f"or search_codebase('{pattern}') to search file contents."
            ),
        }
    return result


def _q_get_functions(qfns, args: dict[str, Any]):
    # Default 100 matches the query-api cap introduced to stop unbounded
    # project-wide dumps (~56 k tokens on a 2.5 k-function repo, A2).
    result = qfns["get_functions"](
        args.get("file_path"), max_results=args.get("max_results", 100)
    )
    if args.get("hints", True) and isinstance(result, list) and result:
        if not (len(result) == 1 and isinstance(result[0], dict) and "error" in result[0]):
            result = result + [{"_hints": _LIST_HINTS}]
    return result


def _q_get_classes(qfns, args: dict[str, Any]):
    result = qfns["get_classes"](
        args.get("file_path"), max_results=args.get("max_results", 100)
    )
    if args.get("hints", True) and isinstance(result, list) and result:
        if not (len(result) == 1 and isinstance(result[0], dict) and "error" in result[0]):
            result = result + [{"_hints": _LIST_HINTS}]
    return result


# Dispatch table: tool name → handler(qfns, arguments) → result
QFN_HANDLERS: dict[str, object] = {
    "get_project_summary": lambda q, a: q["get_project_summary"](),
    "list_files": _q_list_files,
    "get_structure_summary": lambda q, a: q["get_structure_summary"](a.get("file_path")),
    "get_function_source": _q_get_function_source,
    "get_class_source": _q_get_class_source,
    "get_functions": _q_get_functions,
    "get_classes": _q_get_classes,
    "get_imports": lambda q, a: q["get_imports"](
        a.get("file_path"), max_results=a.get("max_results", 100)
    ),
    "find_symbol": _q_find_symbol,
    "get_dependencies": lambda q, a: q["get_dependencies"](
        a["name"], max_results=a.get("max_results", 0), depth=a.get("depth", 1)
    ),
    "get_dependents": lambda q, a: _suggest_if_empty_dependents(
        q["get_dependents"](
            a["name"], max_results=a.get("max_results", 0),
            max_total_chars=a.get("max_total_chars", 50_000),
        ),
        a["name"],
    ),
    "get_change_impact": lambda q, a: q["get_change_impact"](
        a["name"], max_direct=a.get("max_direct", 0), max_transitive=a.get("max_transitive", 0),
        max_total_chars=a.get("max_total_chars", 50_000),
    ),
    "get_full_context": _q_get_full_context,
    "get_call_chain": lambda q, a: q["get_call_chain"](
        a["from_name"], a["to_name"], level=a.get("level", 2)
    ),
    "get_edit_context": _q_get_edit_context,
    "get_file_dependencies": lambda q, a: q["get_file_dependencies"](
        a["file_path"], max_results=a.get("max_results", 0)
    ),
    "get_file_dependents": lambda q, a: q["get_file_dependents"](
        a["file_path"], max_results=a.get("max_results", 0)
    ),
    "search_codebase": lambda q, a: _suggest_if_empty_search(
        q["search_codebase"](
            a["pattern"],
            max_results=a.get("max_results", 100),
            ignore_generated=a.get("ignore_generated", True),
            semantic=a.get("semantic", False),
        ),
        a["pattern"],
    ),
    "search_in_symbols": lambda q, a: _suggest_if_empty_search(
        q["search_in_symbols"](a["pattern"], max_results=a.get("max_results", 100)),
        a["pattern"],
    ),
    "get_routes": lambda q, a: q["get_routes"](max_results=a.get("max_results", 0)),
    "get_env_usage": lambda q, a: q["get_env_usage"](
        a["var_name"], max_results=a.get("max_results", 0)
    ),
    "get_components": lambda q, a: q["get_components"](
        file_path=a.get("file_path"), max_results=a.get("max_results", 0)
    ),
    "get_feature_files": lambda q, a: q["get_feature_files"](
        a["keyword"], max_results=a.get("max_results", 0)
    ),
    "get_entry_points": lambda q, a: q["get_entry_points"](max_results=a.get("max_results", 20)),
    "get_backward_slice": lambda q, a: q["get_backward_slice"](
        a["name"], a["variable"], a["line"], file_path=a.get("file_path")
    ),
    "pack_context": lambda q, a: q["pack_context"](
        a["query"],
        budget_tokens=a.get("budget_tokens", 4000),
        max_symbols=a.get("max_symbols", 20),
    ),
    "find_semantic_duplicates": lambda q, a: q["find_semantic_duplicates"](
        min_lines=a.get("min_lines", 2),
        max_groups=a.get("max_groups", 10),
        method=a.get("method", "ast"),
        min_similarity=a.get("min_similarity", 0.90),
    ),
    "find_import_cycles": lambda q, a: q["find_import_cycles"](
        max_cycles=a.get("max_cycles", 20),
    ),
    "get_duplicate_classes": lambda q, a: q["get_duplicate_classes"](
        a.get("name"),
        max_results=a.get("max_results", 0),
        simple_name_mode=a.get("simple_name_mode", False),
    ),
}
