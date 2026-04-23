"""Library API exposure -- signature + JSDoc/docstring of installed packages.

Motivation: the agent often needs the signature of a library call it is
about to write (`supabase.auth.signInWithOtp`, `useInfiniteQuery`, etc.).
Context7 or raw doc fetches return 3-5k tokens of prose. We already have
the `.d.ts` typings or Python stubs installed in `node_modules/` /
`site-packages/` -- exposing them via a semantic lookup costs ~100 tokens
and is strictly more accurate (reflects the installed version).

Two languages covered:
  - TypeScript: parses `node_modules/<pkg>/**/*.d.ts` with regex,
    extracting export declarations + JSDoc blocks that precede them.
  - Python: uses `importlib.util.find_spec` + `inspect` to resolve the
    symbol path and return signature + docstring.

Lookup semantics:
  - `package`: npm name (`@supabase/supabase-js`) or Python module
    (`pandas.DataFrame`). If the Python module is dotted, the dots that
    resolve to an importable module form the import path, the rest is
    the attribute chain.
  - `symbol_path`: dotted path inside the package. For TS, this matches
    exported names and member-of-interface paths ("createClient",
    "SupabaseAuthClient.signInWithOtp").

This module intentionally degrades to partial results rather than raising.
"""

from __future__ import annotations

import functools
import importlib
import importlib.util
import inspect
import os
import re
from typing import Any


# ---------------------------------------------------------------------------
# TypeScript side: regex over .d.ts files
# ---------------------------------------------------------------------------

# Match a JSDoc block /** ... */ optionally followed by whitespace, then
# capture a declaration. `export` and/or `declare` are both optional so we
# can resolve symbols emitted as bare `declare const foo` and then re-
# exported via `export { foo }` -- the pattern bundlers actually emit.
_TS_EXPORT_RE = re.compile(
    r"(?:(/\*\*[\s\S]*?\*/)\s*)?"
    r"(?:export\s+)?"
    r"(?:declare\s+)?"
    r"(?P<kind>function|const|let|var|class|interface|type|enum)\s+"
    r"(?P<name>[A-Za-z_$][\w$]*)"
    r"(?P<rest>[^\n;{]*)",
    re.MULTILINE,
)

# Match `export { a, b as c, type D };` blocks. We use these purely to
# widen the set of "known exported names" -- the actual signature is
# still resolved from the underlying `declare`.
_TS_EXPORT_BLOCK_RE = re.compile(
    r"export\s*\{([^}]+)\}\s*;",
    re.MULTILINE,
)

# Match members inside a class/interface body. Only captures methods and
# properties that look agent-relevant (public, typed).
_TS_MEMBER_RE = re.compile(
    r"(?:(/\*\*[\s\S]*?\*/)\s*)?"
    r"(?:public\s+|private\s+|protected\s+|readonly\s+|static\s+)*"
    r"(?P<name>[A-Za-z_$][\w$]*)"
    r"\s*(?P<rest>\([^)]*\)[^;\n{]*)",
    re.MULTILINE,
)


def _npm_package_dir(package: str, project_root: str) -> str | None:
    """Resolve an npm package name to its `node_modules/<pkg>` directory.

    Walks upward from project_root so monorepos and nested workspaces work.
    """
    current = os.path.abspath(project_root)
    while True:
        candidate = os.path.join(current, "node_modules", package)
        if os.path.isdir(candidate):
            return candidate
        parent = os.path.dirname(current)
        if parent == current:
            return None
        current = parent


_DTS_SUFFIXES = (".d.ts", ".d.cts", ".d.mts")


def _collect_dts_files(pkg_dir: str, max_files: int = 200) -> list[str]:
    """Find candidate declaration files (.d.ts, .d.cts, .d.mts).

    Modern npm packages ship dual typings (`.d.cts` for CommonJS consumers,
    `.d.mts` for ESM) alongside or instead of `.d.ts`. We accept all three.
    Results are biased toward entry-point files.
    """
    entry_hints = {
        "index.d.ts", "index.d.cts", "index.d.mts",
        "main.d.ts", "main.d.cts", "main.d.mts",
    }
    files: list[str] = []
    pkg_dir_abs = os.path.abspath(pkg_dir)
    for dirpath, _dirnames, filenames in os.walk(pkg_dir_abs):
        # Skip *nested* node_modules (peer deps) while keeping the pkg_dir
        # itself even though its ancestors contain "node_modules".
        rel = os.path.relpath(dirpath, pkg_dir_abs)
        if rel != "." and "node_modules" in rel.split(os.sep):
            continue
        for fname in filenames:
            if fname.endswith(_DTS_SUFFIXES):
                files.append(os.path.join(dirpath, fname))
                if len(files) >= max_files:
                    break
        if len(files) >= max_files:
            break
    files.sort(key=lambda p: (0 if os.path.basename(p) in entry_hints else 1, p))
    return files


def _clean_jsdoc(block: str | None) -> str:
    if not block:
        return ""
    lines: list[str] = []
    for raw in block.splitlines():
        line = raw.strip()
        if line.startswith("/**"):
            line = line[3:].strip()
        elif line.startswith("*/"):
            continue
        elif line.startswith("*"):
            line = line[1:].strip()
        if line:
            lines.append(line)
    return "\n".join(lines)


def _ts_lookup(
    pkg_dir: str,
    symbol_path: str,
    *,
    max_files: int,
) -> list[dict[str, Any]]:
    """Scan .d.ts files for symbol_path. Returns a list (possibly empty)."""
    parts = symbol_path.split(".")
    head = parts[0]
    tail = parts[1:]
    results: list[dict[str, Any]] = []
    for path in _collect_dts_files(pkg_dir, max_files=max_files):
        try:
            with open(path, encoding="utf-8") as f:
                text = f.read()
        except (OSError, UnicodeDecodeError):
            continue
        for m in _TS_EXPORT_RE.finditer(text):
            if m.group("name") != head:
                continue
            jsdoc = _clean_jsdoc(m.group(1))
            kind = m.group("kind")
            rest = (m.group("rest") or "").strip()
            signature = f"{kind} {head}{rest}".strip()
            if not tail:
                results.append({
                    "kind": kind,
                    "name": head,
                    "signature": signature,
                    "jsdoc": jsdoc,
                    "file": os.path.relpath(path, pkg_dir),
                    "line": text[:m.start()].count("\n") + 1,
                })
                continue
            # Need to resolve a member. Find the body of the class/interface.
            if kind not in ("class", "interface"):
                continue
            brace_start = text.find("{", m.end())
            if brace_start == -1:
                continue
            # Walk braces to find the matching close.
            depth = 0
            i = brace_start
            end = -1
            while i < len(text):
                ch = text[i]
                if ch == "{":
                    depth += 1
                elif ch == "}":
                    depth -= 1
                    if depth == 0:
                        end = i
                        break
                i += 1
            if end == -1:
                continue
            body = text[brace_start + 1:end]
            member_name = tail[0]
            for mm in _TS_MEMBER_RE.finditer(body):
                if mm.group("name") != member_name:
                    continue
                member_jsdoc = _clean_jsdoc(mm.group(1))
                member_sig = (member_name + (mm.group("rest") or "")).strip()
                results.append({
                    "kind": "method",
                    "name": f"{head}.{member_name}",
                    "signature": member_sig,
                    "jsdoc": member_jsdoc,
                    "file": os.path.relpath(path, pkg_dir),
                    "line": text[:brace_start + 1 + mm.start()].count("\n") + 1,
                    "container": head,
                })
    return results


def _extract_exported_names(text: str) -> set[str]:
    """Extract every name reachable from `export { ... }` blocks."""
    names: set[str] = set()
    for m in _TS_EXPORT_BLOCK_RE.finditer(text):
        for raw in m.group(1).split(","):
            part = raw.strip()
            if not part:
                continue
            # Strip leading `type ` marker on type-only exports.
            if part.startswith("type "):
                part = part[5:].strip()
            # `foo as bar` -> pick the external alias (what consumers import).
            if " as " in part:
                part = part.split(" as ", 1)[1].strip()
            if part:
                names.add(part)
    return names


def _ts_list(pkg_dir: str, pattern: str | None, max_files: int, limit: int) -> list[dict[str, Any]]:
    """List exported symbols across the package's declaration files.

    Combines bare `declare`/`export <kind>` statements with names that appear
    in `export { ... }` re-export blocks. De-duplicates by name, keeping the
    first declaration we find so consumers get a real file/line anchor.
    """
    compiled = re.compile(pattern, re.IGNORECASE) if pattern else None
    seen: dict[str, dict[str, Any]] = {}
    reexported: set[str] = set()
    for path in _collect_dts_files(pkg_dir, max_files=max_files):
        try:
            with open(path, encoding="utf-8") as f:
                text = f.read()
        except (OSError, UnicodeDecodeError):
            continue
        rel = os.path.relpath(path, pkg_dir)
        reexported |= _extract_exported_names(text)
        for m in _TS_EXPORT_RE.finditer(text):
            name = m.group("name")
            if name in seen:
                continue
            seen[name] = {
                "kind": m.group("kind"),
                "name": name,
                "file": rel,
                "line": text[:m.start()].count("\n") + 1,
            }
    # Filter to the symbols actually exposed by the package (declared then
    # re-exported, or declared with an explicit `export` prefix).
    # Fallback: if no re-export block was seen, expose everything declared.
    out: list[dict[str, Any]] = []
    if reexported:
        for name in sorted(reexported):
            if compiled and not compiled.search(name):
                continue
            item = seen.get(name) or {
                "kind": "reexport",
                "name": name,
                "file": "",
                "line": 0,
            }
            out.append(item)
            if len(out) >= limit:
                break
    else:
        for name, item in seen.items():
            if compiled and not compiled.search(name):
                continue
            out.append(item)
            if len(out) >= limit:
                break
    return out


# ---------------------------------------------------------------------------
# Python side: importlib + inspect
# ---------------------------------------------------------------------------


def _split_python_dotted(dotted: str) -> tuple[str, list[str]]:
    """Split `pandas.DataFrame.head` into (`pandas`, ['DataFrame', 'head']).

    Walks the prefix while it's still an importable module; remaining parts
    become attribute lookups.
    """
    parts = dotted.split(".")
    for i in range(len(parts), 0, -1):
        candidate = ".".join(parts[:i])
        try:
            if importlib.util.find_spec(candidate) is not None:
                return candidate, parts[i:]
        except (ImportError, ValueError):
            continue
    return parts[0], parts[1:]


def _py_symbol(dotted: str) -> dict[str, Any] | None:
    mod_name, attr_path = _split_python_dotted(dotted)
    try:
        mod = importlib.import_module(mod_name)
    except Exception as exc:  # pragma: no cover
        return {"ok": False, "error": f"import {mod_name}: {exc}"}
    obj: Any = mod
    for part in attr_path:
        if not hasattr(obj, part):
            return {"ok": False, "error": f"{mod_name}.{'.'.join(attr_path)}: missing '{part}'"}
        obj = getattr(obj, part)
    # Derive signature
    signature_str: str | None = None
    try:
        sig = inspect.signature(obj)
        signature_str = str(sig)
    except (ValueError, TypeError):
        pass
    # Kind
    if inspect.isclass(obj):
        kind = "class"
    elif inspect.isfunction(obj) or inspect.ismethod(obj):
        kind = "function"
    elif inspect.isbuiltin(obj):
        kind = "builtin"
    elif inspect.ismodule(obj):
        kind = "module"
    else:
        kind = type(obj).__name__
    doc = inspect.getdoc(obj) or ""
    # Truncate very long docstrings to keep output tight.
    if len(doc) > 800:
        doc = doc[:800] + "\n... [truncated]"
    # Source file
    src_file: str | None = None
    src_line: int | None = None
    try:
        src_file = inspect.getsourcefile(obj)
        _src, src_line = inspect.getsourcelines(obj)
    except (TypeError, OSError):
        pass
    return {
        "ok": True,
        "name": dotted,
        "kind": kind,
        "signature": f"{dotted}{signature_str}" if signature_str else dotted,
        "doc": doc,
        "file": src_file,
        "line": src_line,
    }


def _py_list(module: str, pattern: str | None, limit: int) -> list[dict[str, Any]]:
    try:
        mod = importlib.import_module(module)
    except Exception:
        return []
    compiled = re.compile(pattern, re.IGNORECASE) if pattern else None
    out: list[dict[str, Any]] = []
    for name in dir(mod):
        if name.startswith("_"):
            continue
        if compiled and not compiled.search(name):
            continue
        try:
            obj = getattr(mod, name)
        except Exception:
            continue
        if inspect.isclass(obj):
            kind = "class"
        elif inspect.isfunction(obj) or inspect.isbuiltin(obj):
            kind = "function"
        elif inspect.ismodule(obj):
            kind = "module"
        else:
            kind = type(obj).__name__
        out.append({"name": name, "kind": kind})
        if len(out) >= limit:
            break
    return out


# ---------------------------------------------------------------------------
# Dispatch
# ---------------------------------------------------------------------------


def _looks_npm(package: str) -> bool:
    return package.startswith("@") or "/" in package or "-" in package or package.islower()


def get_library_symbol(
    package: str,
    symbol_path: str,
    *,
    project_root: str,
    max_files: int = 200,
) -> dict[str, Any]:
    """Resolve `package.symbol_path` against installed typings/stubs.

    Returns a result dict (never raises). When multiple matches are found
    in TS (e.g. same name in different `.d.ts` files), all are returned.
    """
    # 1) Try npm: look up in node_modules.
    pkg_dir = _npm_package_dir(package, project_root)
    if pkg_dir is not None:
        matches = _ts_lookup(pkg_dir, symbol_path, max_files=max_files)
        if matches:
            return {
                "ok": True,
                "language": "typescript",
                "package": package,
                "symbol_path": symbol_path,
                "package_dir": os.path.relpath(pkg_dir, project_root),
                "matches": matches,
            }
        # Fall through to Python attempt only if the name is clearly not npm.
        if _looks_npm(package):
            return {
                "ok": False,
                "language": "typescript",
                "error": f"no matches for '{symbol_path}' in {package}",
                "package_dir": os.path.relpath(pkg_dir, project_root),
                "matches": [],
            }

    # 2) Try Python.
    combined = f"{package}.{symbol_path}" if symbol_path else package
    py_result = _py_symbol(combined)
    if py_result and py_result.get("ok"):
        return {
            "ok": True,
            "language": "python",
            "package": package,
            "symbol_path": symbol_path,
            **{k: v for k, v in py_result.items() if k != "ok"},
        }

    return {
        "ok": False,
        "error": (
            f"Could not resolve '{package}.{symbol_path}'. "
            f"Tried node_modules search from {project_root} and Python import."
        ),
    }


@functools.lru_cache(maxsize=4096)
def _cached_doc_embed(embed_doc: str) -> tuple[float, ...] | None:
    """Content-addressed cache over ``embed()`` for library symbol docs.

    Library lookups are one-shot but frequently touch the same packages
    across a session (pydantic, fastembed, next/react for TS projects).
    The embed_doc string is deterministic in the symbol's name + sig +
    doc_head, so same content → same embedding → cache hit. A changed
    docstring or signature produces a different key and falls through
    to a fresh ``embed()`` call.

    Returns tuple instead of list so the LRU can hash the return value
    safely; the cosine dot product at the call site works on any
    iterable of floats.
    """
    from token_savior.memory.embeddings import embed
    vec = embed(embed_doc)
    return tuple(vec) if vec is not None else None


def find_library_symbol_by_description(
    package: str,
    description: str,
    *,
    project_root: str,
    limit: int = 10,
    max_files: int = 100,
    candidate_pool: int = 200,
) -> dict[str, Any]:
    """Rank package exports by embedding cosine similarity to a
    natural-language description.

    Short-lived, on-the-fly index: lists every export via
    ``list_library_symbols``, enriches each (Python path only — uses
    ``inspect`` for docstrings / signatures; TS stays on the raw
    declared name + kind), embeds the pool via a process-level LRU
    cache (``_cached_doc_embed``, maxsize=4096, content-addressed by
    embed_doc), then cosine-ranks against ``embed(description,
    as_query=True)``. Nothing is persisted to disk — library calls are
    one-shot lookups so an on-disk index would be dead weight, but the
    in-process cache collapses the ~500ms first-call cost to ~5ms on
    subsequent calls touching the same package.

    SAFETY — the same caveats as ``search_codebase(semantic=True)``
    apply: the result is a ranked suggestion list, never a trusted
    single answer. Verify via ``get_library_symbol(package, exact_name)``
    before acting on a hit. No low-confidence warning is emitted (see
    tests/benchmarks/library_retrieval for the empirical justification).

    Returns a dict shaped like::

        {"ok": True, "package": str, "description": str,
         "candidates_scanned": int, "hits": [{
             "name": str, "kind": str, "score": float,
             "doc_preview": str, "file": str|None, "line": int|None,
         }, ...],
         "warning": None}
    """
    try:
        from token_savior.memory.embeddings import embed, is_available
    except ImportError as exc:
        return {"ok": False, "error": f"embedding stack unavailable: {exc}"}
    if not is_available():
        return {"ok": False, "error": "fastembed model not loadable"}

    listing = list_library_symbols(
        package, project_root=project_root,
        max_files=max_files, limit=candidate_pool,
    )
    if not listing.get("ok"):
        return listing
    language = listing.get("language")
    items = listing.get("items", [])
    if not items:
        return {"ok": False, "error": f"no exports enumerated for {package}"}

    qvec = embed(description, as_query=True)
    if qvec is None:
        return {"ok": False, "error": "query embedding failed"}

    candidates: list[tuple[dict, tuple[float, ...]]] = []
    for item in items:
        name = item.get("name", "")
        kind = item.get("kind", "symbol")
        doc = ""
        sig = ""
        file_ = item.get("file")
        line = item.get("line")
        if language == "python":
            dotted = f"{package}.{name}" if "." not in name else name
            detailed = _py_symbol(dotted)
            if detailed and detailed.get("ok"):
                doc = (detailed.get("doc") or "").strip()
                sig = detailed.get("signature") or ""
                file_ = detailed.get("file") or file_
                line = detailed.get("line") or line
        doc_head = "\n".join(doc.splitlines()[:3])
        embed_doc = (
            f"{kind} {name}\n"
            f"{sig}\n"
            f"{doc_head}"
        ).strip()
        vec = _cached_doc_embed(embed_doc)
        if vec is None:
            continue
        candidates.append((
            {
                "name": name, "kind": kind,
                "doc_preview": doc_head[:240],
                "file": file_, "line": line,
            },
            vec,
        ))

    def _cos(a, b) -> float:
        return sum(x * y for x, y in zip(a, b, strict=False))

    scored = sorted(
        ((cand, _cos(qvec, v)) for cand, v in candidates),
        key=lambda t: t[1], reverse=True,
    )
    hits = []
    for cand, score in scored[:limit]:
        cand["score"] = round(max(0.0, score), 4)
        hits.append(cand)

    # No low-confidence warning. tests/benchmarks/library_retrieval (15
    # queries on json/pathlib/re) showed 0/9 warning precision: the
    # threshold fires on 60% of queries but 100% of them are actually
    # correct retrievals (Recall@10 = 1.00 on the bench). Same failure
    # mode as search_symbols_semantic — absolute cosine scores on short
    # symbol docs don't discriminate correct vs wrong. The `warning`
    # key stays in the shape as None for callers that do
    # ``result.get("warning")``.
    return {
        "ok": True,
        "language": language,
        "package": package,
        "description": description,
        "candidates_scanned": len(candidates),
        "hits": hits,
        "warning": None,
    }


def list_library_symbols(
    package: str,
    *,
    project_root: str,
    pattern: str | None = None,
    max_files: int = 100,
    limit: int = 100,
) -> dict[str, Any]:
    """List top-level exports of a package, optionally filtered by regex."""
    pkg_dir = _npm_package_dir(package, project_root)
    if pkg_dir is not None:
        items = _ts_list(pkg_dir, pattern, max_files=max_files, limit=limit)
        return {
            "ok": True,
            "language": "typescript",
            "package": package,
            "package_dir": os.path.relpath(pkg_dir, project_root),
            "count": len(items),
            "items": items,
        }
    # Python fallback
    py_items = _py_list(package, pattern, limit)
    if py_items:
        return {
            "ok": True,
            "language": "python",
            "package": package,
            "count": len(py_items),
            "items": py_items,
        }
    return {
        "ok": False,
        "error": f"Could not locate '{package}' in node_modules or as a Python module.",
    }
