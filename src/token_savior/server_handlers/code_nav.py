"""Handlers for query-function code-navigation tools and the CSC subsystem.

CSC (Compact Symbol Cache) returns a compact stub for repeat reads of a
symbol whose body has not changed, falling back to the full source when
the body has changed (or when force_full / level>0 is requested).

The 28-entry _QFN_HANDLERS dispatch table is the second half of the
call_tool fan-out (after _META_HANDLERS, _MEMORY_HANDLERS, _SLOT_HANDLERS).
"""

from __future__ import annotations

from typing import Any

from token_savior import memory_db
from token_savior import server_state as state


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
        "Use force_full=true to bypass the session cache and get the full body."
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
    return result


def _q_get_function_source(qfns, args: dict[str, Any]) -> str:
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
    return ctx


# Dispatch table: tool name → handler(qfns, arguments) → result
QFN_HANDLERS: dict[str, object] = {
    "get_project_summary": lambda q, a: q["get_project_summary"](),
    "list_files": lambda q, a: q["list_files"](
        a.get("pattern"), max_results=a.get("max_results", 0)
    ),
    "get_structure_summary": lambda q, a: q["get_structure_summary"](a.get("file_path")),
    "get_function_source": _q_get_function_source,
    "get_class_source": _q_get_class_source,
    "get_functions": lambda q, a: q["get_functions"](
        a.get("file_path"), max_results=a.get("max_results", 0)
    ),
    "get_classes": lambda q, a: q["get_classes"](
        a.get("file_path"), max_results=a.get("max_results", 0)
    ),
    "get_imports": lambda q, a: q["get_imports"](
        a.get("file_path"), max_results=a.get("max_results", 0)
    ),
    "find_symbol": lambda q, a: q["find_symbol"](a["name"], level=a.get("level", 0)),
    "get_dependencies": lambda q, a: q["get_dependencies"](
        a["name"], max_results=a.get("max_results", 0)
    ),
    "get_dependents": lambda q, a: q["get_dependents"](
        a["name"], max_results=a.get("max_results", 0),
        max_total_chars=a.get("max_total_chars", 50_000),
    ),
    "get_change_impact": lambda q, a: q["get_change_impact"](
        a["name"], max_direct=a.get("max_direct", 0), max_transitive=a.get("max_transitive", 0),
        max_total_chars=a.get("max_total_chars", 50_000),
    ),
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
    "search_codebase": lambda q, a: q["search_codebase"](
        a["pattern"], max_results=a.get("max_results", 100)
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
    "get_symbol_cluster": lambda q, a: q["get_symbol_cluster"](
        a["name"], max_members=a.get("max_members", 30)
    ),
    "get_backward_slice": lambda q, a: q["get_backward_slice"](
        a["name"], a["variable"], a["line"], file_path=a.get("file_path")
    ),
    "pack_context": lambda q, a: q["pack_context"](
        a["query"],
        budget_tokens=a.get("budget_tokens", 4000),
        max_symbols=a.get("max_symbols", 20),
    ),
    "get_relevance_cluster": lambda q, a: q["get_relevance_cluster"](
        a["name"],
        budget=a.get("budget", 10),
        include_reverse=a.get("include_reverse", True),
    ),
    "find_semantic_duplicates": lambda q, a: q["find_semantic_duplicates"](
        min_lines=a.get("min_lines", 4)
    ),
    "get_duplicate_classes": lambda q, a: q["get_duplicate_classes"](
        a.get("name"),
        max_results=a.get("max_results", 0),
        simple_name_mode=a.get("simple_name_mode", False),
    ),
}
