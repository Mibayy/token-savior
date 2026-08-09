"""Handlers for query-function code-navigation tools and the CSC subsystem.

CSC (Compact Symbol Cache) returns a compact stub for repeat reads of a
symbol whose body has not changed, falling back to the full source when
the body has changed (or when force_full / level>0 is requested).

The 28-entry _QFN_HANDLERS dispatch table is the second half of the
call_tool fan-out (after _META_HANDLERS, _MEMORY_HANDLERS, _SLOT_HANDLERS).
"""

from __future__ import annotations

import json
import os
import re
from typing import Any

from token_savior import memory_db
from token_savior import server_state as state
from token_savior.query_api import format_signature
from token_savior.tool_schemas import _READ_LINES_CAP, _READ_LINES_SPAN

_BATCH_MAX = 10

# Set TS_NO_HINTS=1 to suppress all _hints / _suggestion blocks in tool
# results. Cuts ~30-50 tokens per tool call at the cost of removing
# next-step routing advice (the agent must rely on its system prompt).
# Recommended for benchmark / cold-start workloads where the prompt
# already encodes routing rules.
#
# The `optimized` profile is the token-minimizing default: it already implies
# thin schemas (server.py), and a usage audit found the per-result hint blocks
# convert poorly (e.g. the ts_execute nudge fired hundreds of times without
# moving adoption). So `optimized` disables them too — the ~30-50 tokens/call
# are a measured overhead there. An explicit TS_NO_HINTS=0 opts back in.
def _hints_off() -> bool:
    override = os.environ.get("TS_NO_HINTS")
    if override is not None:
        return override == "1"
    return os.environ.get("TOKEN_SAVIOR_PROFILE", "").lower() == "optimized"


_HINTS_DISABLED = _hints_off()


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
                sig = format_signature(func, rel_path)
                return ("function", func.body_hash, sig)
        for cls in meta.classes:
            if cls.name == name:
                sig = f"class {cls.name}"
                if cls.base_classes:
                    sig += f"({', '.join(cls.base_classes)})"
                return ("class", cls.body_hash, sig)
            for method in cls.methods:
                if method.qualified_name == name:
                    sig = format_signature(method, rel_path)
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


def _csc_key(slot, kind: str, name: str, file_path: object = None) -> str:
    """La cle d'une entree du cache de session. Un seul endroit, exprès.

    Le chemin fait partie de l'identite d'un symbole. Sans lui, tous les
    homonymes d'un projet partagent une entree : constate le 06/08/2026 sur un
    projet Next.js, ou chaque fichier de route exporte `POST`. Le rappel
    servait alors un accuse `[MODIFIED]` dont le diff etait calcule contre une
    fonction sans rapport, et le corps demande n'arrivait jamais.

    Cette fonction existe parce que la cle etait construite en deux endroits
    (`_csc_maybe_serve` et la dedup de `get_full_context`). Corriger l'un sans
    l'autre casse la dedup en silence : c'est arrive pendant l'ecriture de ce
    correctif, et seuls les tests l'ont dit.

    Un appel sans `file_path` garde sa propre entree plutot que d'etre
    rapproche d'un appel qui en precise un : deux cles distinctes valent une
    source servie en trop, une cle commune vaut un corps retenu a tort.
    """
    root = getattr(slot, "root", "") or ""
    return f"{kind}:{root}:{file_path or ''!s}:{name}"


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
        # Ce cache est une globale du processus, partagee par une session et
        # tous les sous-agents qu'elle lance. Un agent parallele peut donc
        # recevoir cet accuse pour un corps que seul un autre agent a recu :
        # l'issue de secours doit etre nommee ici, sinon on invite a reutiliser
        # ce qu'on n'a pas -- et un modele a qui l'on affirme qu'il detient un
        # corps est en position de l'inventer.
        lines.append(
            "Reuse the body you have. Never reconstruct it: if you lack it "
            "(parallel sub-agent, compacted context) or suspect mutation, "
            "re-call with force_full=true."
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
        if line.startswith(("@@", "---", "+++")):
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
    effective_level: int | None = None,
) -> str:
    """Entry point for get_function_source / get_class_source with CSC.

    `produce_full` is a zero-arg callable returning the full formatted source.
    Returns either the compact stub (cache hit, body unchanged) or the full
    source (miss / force_full / modified).

    `effective_level` est le niveau reellement servi par l'appelant. Il doit
    etre passe des que ce niveau n'est pas celui des arguments : quand le
    client ne precise pas `level`, les handlers le tirent au sort via le
    bandit (`thompson_sample_level`), et ce tirage peut rendre 2.

    Sans ce parametre, on lisait `args["level"]`, absent donc 0, et on
    enregistrait au cache un abrege L2 comme s'il s'agissait du corps. Le
    rappel suivant, meme avec `level=0` explicite, recevait alors
    "body unchanged since last view - reuse the body you already have"
    alors que l'appelant n'avait jamais recu de corps, et devait passer par
    `force_full=true` pour s'en sortir. Constate le 26/07/2026, deux
    allers-retours perdus par edition.

    L'accuse n'est servi que s'il est reellement plus court que la source.
    Mesure du 27/07/2026 sur `compter_articles`, trois lignes : la source
    faisait 232 caracteres, l'accuse 317. Le mecanisme cense economiser des
    tokens en coutait 85 de plus, et le savait -- `saved` tombait a 0 par le
    `max(0, ...)` ci-dessous, sans que personne n'en tire la consequence. Sur
    un petit symbole, la reponse la moins chere est la source elle-meme.
    """

    force_full = bool(args.get("force_full", False))
    if effective_level is not None:
        level = int(effective_level)
    else:
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

    name = args["name"]
    key = _csc_key(slot, kind, name, args.get("file_path"))
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
        if len(compact) >= len(full):
            return full
        saved = len(full) - len(compact)
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
    entry.update(
        {
            "cache_token": tok,
            "body_hash": body_hash,
            "full_source": full,
            "signature": signature,
        }
    )
    if len(compact) >= len(full):
        return full
    saved = len(full) - len(compact)
    state._csc_hits += 1
    state._csc_tokens_saved += saved // 4
    return compact


# ---------------------------------------------------------------------------
# Query-function handlers (qfns dict + arguments → result)
# ---------------------------------------------------------------------------


def _require_name(args: dict[str, Any], tool: str, *, batch: bool = False) -> str | None:
    """Explicit message when `name` is missing, instead of a raw KeyError.

    Found by auditing all 69 tools one by one: three of them answered
    ``Error: 'name'`` — the repr of a Python ``KeyError``, nothing else. For an
    LLM client that is the worst possible message: it names neither the missing
    argument nor how to obtain it, so the caller retries blind and pays the
    round-trip twice. That is exactly the waste this project exists to remove.
    `find_symbol` already did this properly; these three did not.
    """
    if isinstance(args.get("name"), str) and args["name"]:
        return None
    if batch and isinstance(args.get("names"), list) and args["names"]:
        return None
    expected = "'name=<str>'" + (" or 'names=[<str>, ...]'" if batch else "")
    return (
        f"{tool} requires {expected}.\n"
        f'  Example: {tool}(name="my_function")\n'
        f"  If you do not know the exact name: search_codebase(pattern=...) "
        f"or ts_search(query=...)."
    )


def _q_get_class_source(qfns, args: dict[str, Any]) -> str:
    batch = _batch_dispatch(qfns, args, _q_get_class_source)
    if batch is not None:
        return batch
    missing = _require_name(args, "get_class_source")
    if missing:
        return missing
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
        effective_level=chosen_level,
    )
    if ctx_type is not None:
        try:
            success = bool(result and not result.startswith("Error"))
            memory_db.record_lattice_feedback(ctx_type, chosen_level, success)
        except Exception:
            pass
    if not _HINTS_DISABLED and args.get("hints", True) and isinstance(result, str) and result and not result.startswith("Error"):
        if _navigation_calls_so_far() >= _OVER_EXPLORATION_THRESHOLD:
            result += f"\n\n{_stop_hint()}"
        else:
            indice = (
                f"\n\n→ get_full_context('{args['name']}') "
                "for callers + deps (source reused; mode='full' to re-fetch)"
            )
            if _indice_supportable(result, indice):
                result += indice
    return result


def _q_get_function_source(qfns, args: dict[str, Any]) -> str:
    batch = _batch_dispatch(qfns, args, _q_get_function_source)
    if batch is not None:
        return batch
    missing = _require_name(args, "get_function_source")
    if missing:
        return missing
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
        effective_level=chosen_level,
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
    if not _HINTS_DISABLED and args.get("hints", True) and isinstance(result, str) and result and not result.startswith("Error"):
        if _navigation_calls_so_far() >= _OVER_EXPLORATION_THRESHOLD:
            result += f"\n\n{_stop_hint()}"
        else:
            indice = (
                f"\n\n→ get_full_context('{args['name']}') "
                "for callers + deps (source reused; mode='full' to re-fetch)"
            )
            if _indice_supportable(result, indice):
                result += indice
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
    # La source complete est deja dans ctx["source"]. Reexpedier ses vingt
    # premieres lignes dans location["source_preview"] revient a l'envoyer
    # deux fois : mesure du 27/07/2026, get_edit_context coutait 459
    # caracteres la ou la chaine qu'il remplace (source + dependances +
    # appelants) en coutait 216. L'outil phare "un appel au lieu de trois"
    # coutait donc plus du double des trois. On copie le dictionnaire plutot
    # que de le modifier : il vient du cache de find_symbol.
    if isinstance(location, dict) and ctx.get("source"):
        location = {c: v for c, v in location.items() if c != "source_preview"}
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
    "get_full_context", "read_lines",
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




def _indice_supportable(reponse: str, indice: str) -> bool:
    """Un indice ne doit pas peser plus du quart de la reponse qu'il suit.

    L'indice "-> get_full_context(...)" fait ~70 caracteres et etait ajoute a
    toutes les reponses, quelle que soit leur taille. Mesure du 27/07/2026 :
    sur une fonction de trois lignes, get_function_source rendait 131
    caracteres la ou lire le fichier entier en coutait 76. L'outil cense
    economiser des tokens en depensait plus que `cat`, et l'ecart etait
    exactement l'indice.

    Une suggestion qui coute le quart de la reponse ne se rentabilise que si
    l'agent la suit ; sur un petit symbole il n'a aucune raison de le faire,
    il a deja ce qu'il cherchait.
    """
    return len(indice) * 4 <= len(reponse)

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


def _dedup_served_source(result: dict, slot) -> None:
    """Kill the read->get_full_context re-fetch: if this symbol's source was
    already served this session (via get_function_source / get_class_source,
    which populates the CSC session cache) AND its body is unchanged since,
    omit the source and leave a recovery pointer instead.

    Matches on body_hash, not the source string: the two code paths format
    source differently, so a string compare would silently never fire. body_hash
    is the CSC's own unchanged-signal (same `_lookup_symbol_meta` source), so a
    changed body no longer matches and the source is returned in full.
    Lossless — the handler runs this in compact mode only; mode='full' bypasses.
    """
    name = result.get("name")
    if not name and isinstance(result.get("symbol"), dict):
        name = result["symbol"].get("name")
    src = result.get("source")
    # Net-positive guard: the pointer is ~90 chars, so on a tiny source the
    # dedup would cost more than it saves. Same logic as the CSC "only if
    # shorter" guard.
    if not name or not isinstance(src, str) or len(src) < 160:
        return
    meta = _lookup_symbol_meta(slot, {"name": name})
    if not meta:
        return
    _, cur_body_hash, _ = meta
    file_path = result.get("file")
    if not file_path and isinstance(result.get("symbol"), dict):
        file_path = result["symbol"].get("file")
    for kind in ("function", "class"):
        # On tente d'abord la cle portant le chemin, puis celle sans : un
        # get_function_source appele sans `file_path` a enregistre la seconde,
        # et la dedup doit reconnaitre les deux.
        entry = None
        for candidat in (_csc_key(slot, kind, name, file_path), _csc_key(slot, kind, name, None)):
            entry = state._session_symbol_cache.get(candidat)
            if entry is not None:
                break
        if entry and entry.get("body_hash") == cur_body_hash:
            result["source"] = (
                f"# source of {name} already served this session (unchanged) — "
                "reuse it; call with mode='full' to re-fetch"
            )
            state._csc_hits = getattr(state, "_csc_hits", 0) + 1
            return


def _q_get_full_context(qfns, args: dict[str, Any]):
    batch = _batch_dispatch(qfns, args, _q_get_full_context)
    if batch is not None:
        return batch
    missing = _require_name(args, "get_full_context", batch=True)
    if missing:
        return missing
    result = qfns["get_full_context"](
        args["name"],
        depth=args.get("depth", 1),
        max_lines=args.get("max_lines", 200),
    )
    mode = args.get("mode", "compact")
    if mode == "compact" and isinstance(result, dict):
        slot, _ = state._slot_mgr.resolve(args.get("project"))
        if slot is not None:
            _dedup_served_source(result, slot)
        return _compact_full_context(result, args.get("intent"))
    return result


_INTENT_KEEP = 12


def _rank_by_intent(names: list[str], intent: str, keep: int) -> tuple[list[str], int]:
    """Deterministic lexical relevance rank (no model): count intent terms that
    hit each name by token match or substring. Stable — earlier items win ties.
    Returns (kept_names, dropped_count)."""
    terms = [t for t in re.findall(r"[a-z0-9]+", intent.lower()) if len(t) > 2]
    if not terms:
        return names[:keep], max(0, len(names) - keep)

    def _score(item):
        i, n = item
        low = n.lower()
        toks = set(re.findall(r"[a-z0-9]+", low))
        overlap = sum(1 for t in terms if t in toks)
        substr = sum(1 for t in terms if t in low)
        return (overlap, substr, -i)

    ranked = sorted(enumerate(names), key=_score, reverse=True)
    return [n for _, n in ranked[:keep]], max(0, len(names) - keep)


def _compact_full_context(result: dict, intent: str | None = None) -> dict:
    """Trim heavy fields: deps/dependents -> names only, source -> head 80 lines.

    When `intent` is set, long deps/dependents lists are ranked by lexical
    relevance to it and trimmed to the top _INTENT_KEEP, with a recovery
    pointer appended -- lossless (the rest stays reachable via mode='full').
    """
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
            names = [
                item.get("name") if isinstance(item, dict) else item
                for item in val
            ]
            clean = [n for n in names if n]
            if intent and len(clean) > _INTENT_KEEP:
                kept, dropped = _rank_by_intent(clean, intent, _INTENT_KEEP)
                compact[key] = kept + [f"# +{dropped} ranked out by intent (mode='full' to expand)"]
            else:
                compact[key] = names
        elif val is not None:
            compact[key] = val
    return compact


def _suggest_if_empty_search(result, pattern: str):
    if isinstance(result, list) and len(result) == 0:
        if _HINTS_DISABLED:
            return {"matches": []}
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
        if _HINTS_DISABLED:
            return {"dependents": []}
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
    name = args.get("name")
    if not name:
        # Common failure mode: agent forgot to pass any name. The MCP
        # validator already raises a generic "Input validation error" but
        # that doesn't tell the agent what to do next. Return a structured
        # hint so the next call goes through.
        return {
            "error": "find_symbol requires either 'name=<str>' or 'names=[<str>, ...]'.",
            "_hints": {
                "single": "find_symbol(name='create_user')",
                "batch": "find_symbol(names=['create_user', 'update_user', 'delete_user'])  # max 10",
                "alternatives": [
                    "search_codebase(pattern='...') if you only have keywords",
                    "get_structure_summary(file_path='...') for a file's TOC",
                ],
            },
        }
    # Only forward `kinds` when the caller set it — the query engine derives
    # its own default, and an unconditional kwarg would break older engines.
    extra = {"kinds": args["kinds"]} if args.get("kinds") else {}
    result = qfns["find_symbol"](name, level=args.get("level", 0), **extra)
    if isinstance(result, dict) and "error" in result:
        if not _HINTS_DISABLED:
            retry_with = result.get("retry_with")
            result["_suggestion"] = (
                # An indexed-but-unsearched kind is the likeliest reason for a
                # miss, so name that retry before sending the caller to grep.
                f"Symbol '{name}' not found in {', '.join(result.get('searched', []))}. "
                f"Try: {retry_with}"
                if retry_with
                else (
                    f"Symbol '{name}' not found. "
                    f"Try: search_codebase('{name}') for a text search, "
                    f"or get_functions() to list all functions."
                )
            )
    elif args.get("hints", True) and not _HINTS_DISABLED and isinstance(result, dict):
        result["_hints"] = _hints_for_symbol(
            result.get("name") or name, result.get("type")
        )
    return result


def _q_list_files(qfns, args: dict[str, Any]):
    pattern = args.get("pattern")
    result = qfns["list_files"](pattern, max_results=args.get("max_results", 0))
    if isinstance(result, list) and len(result) == 0 and pattern:
        if _HINTS_DISABLED:
            return {"files": []}
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
    if (
        args.get("hints", True)
        and not _HINTS_DISABLED
        and isinstance(result, list)
        and result
        and not (len(result) == 1 and isinstance(result[0], dict) and "error" in result[0])
    ):
        result = result + [{"_hints": _LIST_HINTS}]
    return result


def _q_get_classes(qfns, args: dict[str, Any]):
    result = qfns["get_classes"](
        args.get("file_path"), max_results=args.get("max_results", 100)
    )
    if (
        args.get("hints", True)
        and not _HINTS_DISABLED
        and isinstance(result, list)
        and result
        and not (len(result) == 1 and isinstance(result[0], dict) and "error" in result[0])
    ):
        result = result + [{"_hints": _LIST_HINTS}]
    return result

def _read_lines_enclosing(qfns, file_path: str, start: int, end: int) -> str | None:
    """Nom du symbole qui contient la plage, ou None.

    Sert l'unique reproche qu'on peut faire a une lecture par plage : elle ne
    dit pas de quoi elle est un morceau. Repondre "ces lignes sont dans
    Foo.bar (98-160)" evite la relecture a l'aveugle de la plage suivante.
    """
    try:
        symboles = qfns["get_functions"](file_path, max_results=0)
    except Exception:
        return None
    if not isinstance(symboles, list):
        return None
    meilleur = None
    for sym in symboles:
        if not isinstance(sym, dict):
            continue
        bornes = sym.get("lines")
        if not (isinstance(bornes, (list, tuple)) and len(bornes) == 2):
            continue
        debut, fin = bornes
        if not (isinstance(debut, int) and isinstance(fin, int)):
            continue
        # Le plus serre gagne : une methode plutot que la classe qui la porte.
        if (
            debut <= start
            and end <= fin
            and (meilleur is None or (fin - debut) < (meilleur[2] - meilleur[1]))
        ):
            meilleur = (sym.get("qualified_name") or sym.get("name"), debut, fin)
    if meilleur is None or not meilleur[0]:
        return None
    return f"{meilleur[0]} (lines {meilleur[1]}-{meilleur[2]})"


def _q_read_lines(qfns, args: dict[str, Any]):
    """Lecture par plage de lignes — le cas ou l'appelant tient un numero.

    Ajoute le 09/08/2026. Le moteur savait deja le faire (`get_lines` dans
    ProjectQueryEngine, teste), mais aucun outil MCP ne l'exposait : un agent
    qui tenait un numero de ligne (trace d'erreur, `grep -n`, erreur de
    compilation) n'avait que `sed`/`cat` en Bash, parce que tout le reste de
    la surface demande un nom de symbole.
    """
    file_path = args.get("file_path")
    start_brut = args.get("start")
    if not file_path or start_brut is None:
        return (
            "Error: read_lines requires 'file_path' and 'start' "
            "(e.g. read_lines(file_path='src/app.py', start=105, end=150)). "
            "Pass a symbol name to get_function_source instead if you have one."
        )
    try:
        start = int(start_brut)
        end = int(args.get("end") or 0)
    except (TypeError, ValueError):
        return "Error: 'start' and 'end' must be integers (1-indexed, inclusive)."
    if end <= 0:
        end = start + _READ_LINES_SPAN - 1
    if end < start:
        return f"Error: start ({start}) > end ({end})."

    cap = args.get("max_lines", _READ_LINES_CAP)
    try:
        cap = int(cap)
    except (TypeError, ValueError):
        cap = _READ_LINES_CAP
    tronque = False
    if cap > 0 and (end - start + 1) > cap:
        end = start + cap - 1
        tronque = True

    brut = qfns["get_lines"](file_path, start, end)
    if not isinstance(brut, str):
        return brut
    if brut.startswith("Error:"):
        if "not found in index" in brut:
            return (
                f"{brut}. Only indexed files are readable: check the path with "
                f"list_files(pattern='*{file_path.rsplit('/', 1)[-1]}'), or "
                f"switch_project(name='<root>') if the file belongs elsewhere."
            )
        return brut

    lignes = brut.split("\n")
    if args.get("numbers", True):
        largeur = len(str(start + len(lignes) - 1))
        corps = "\n".join(
            f"{start + i:>{largeur}}  {ligne}" for i, ligne in enumerate(lignes)
        )
    else:
        corps = brut

    suffixes: list[str] = []
    if tronque:
        suffixes.append(
            f"[tronque a {cap} lignes — relance avec start={end + 1}, "
            f"ou max_lines=0 pour lever le plafond]"
        )
    if args.get("hints", True) and not _HINTS_DISABLED:
        englobant = _read_lines_enclosing(qfns, file_path, start, start + len(lignes) - 1)
        if englobant:
            nom = englobant.split(" (")[0]
            suffixes.append(f"→ inside {englobant} — get_function_source('{nom}') for the whole body")
    if suffixes:
        corps += "\n\n" + "\n".join(suffixes)
    return corps



# Dispatch table: tool name → handler(qfns, arguments) → result
QFN_HANDLERS: dict[str, object] = {
    "get_project_summary": lambda q, a: q["get_project_summary"](),
    "list_files": _q_list_files,
    "get_structure_summary": lambda q, a: q["get_structure_summary"](a.get("file_path")),
    "read_lines": _q_read_lines,
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
    "get_feature_files": lambda q, a: q["get_feature_files"](
        a["keyword"], max_results=a.get("max_results", 0)
    ),
    "get_entry_points": lambda q, a: q["get_entry_points"](max_results=a.get("max_results", 20)),
    "find_semantic_duplicates": lambda q, a: q["find_semantic_duplicates"](
        min_lines=a.get("min_lines", 2),
        max_groups=a.get("max_groups", 10),
        method=a.get("method", "ast"),
        min_similarity=a.get("min_similarity", 0.90),
    ),
    "find_import_cycles": lambda q, a: q["find_import_cycles"](
        max_cycles=a.get("max_cycles", 20),
    ),
}
