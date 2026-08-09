"""Cross-cutting runtime helpers for the Token Savior MCP server.

Holds request-cycle utilities shared between the server entry point and
handler modules: compact-output formatting, slot prep / async pre-warm,
client/workspace detection, stats persistence, naive-cost estimation,
and project-root resolution. State lives in ``server_state``.
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
import threading
import time
from typing import Any

from token_savior import budget_diag as _budget_diag
from token_savior import memory_db
from token_savior import server_state as s
from token_savior._compat import TextContent, types
from token_savior.slot_manager import _ProjectSlot

# ---------------------------------------------------------------------------
# Compact-output formatting (`@F`/`@S`/`@L`/`@T`/`@P` tokens)
# ---------------------------------------------------------------------------


def _fmt_lines(entry: dict[str, Any]) -> str:
    lines = entry.get("lines") or entry.get("line_range")
    if isinstance(lines, (list, tuple)) and len(lines) == 2:
        return f"{lines[0]}-{lines[1]}"
    if isinstance(lines, int):
        return str(lines)
    line = entry.get("line") or entry.get("start_line")
    end = entry.get("end_line")
    if line and end and end != line:
        return f"{line}-{end}"
    if line:
        return str(line)
    return ""


def compress_symbol_output(tool_name: str, result: object) -> str:
    """Compact list-of-dicts / dict output using @F/@S/@L/@T/@P tokens.

    Skips error entries and leaves non-dict/list payloads untouched.
    Returns the compressed string (never raises).
    """
    def _row(tool: str, e: dict[str, Any]) -> str:
        if not isinstance(e, dict) or "error" in e or e.get("truncated"):
            return json.dumps(e, separators=(",", ":"), default=str)
        parts: list[str] = []
        fpath = e.get("file") or e.get("file_path")
        if fpath:
            parts.append(f"@F:{fpath}")
        if tool == "get_imports":
            mod = e.get("module")
            if mod:
                parts.append(f"@S:{mod}")
            names = e.get("names") or []
            if names:
                parts.append(f"@T:{'from' if e.get('is_from_import') else 'import'}")
                parts.append(f"@P:{','.join(str(n) for n in names)}")
        else:
            sym = e.get("name") or e.get("qualified_name") or e.get("symbol")
            if sym:
                parts.append(f"@S:{sym}")
            stype = e.get("type")
            if not stype:
                if "methods" in e or "bases" in e:
                    stype = "class"
                elif "params" in e or e.get("is_method"):
                    stype = "method" if e.get("is_method") else "fn"
            if stype:
                parts.append(f"@T:{stype}")
            params = e.get("params") or e.get("parameters")
            if isinstance(params, list) and params:
                parts.append(f"@P:({','.join(str(p) for p in params)})")
        lines_tok = _fmt_lines(e)
        if lines_tok:
            parts.append(f"@L:{lines_tok}")
        sig = e.get("signature")
        if sig and "@P:" not in " ".join(parts):
            parts.append(f"@P:{sig}")
        return " ".join(parts) if parts else json.dumps(e, separators=(",", ":"), default=str)

    try:
        if isinstance(result, list):
            rows = [_row(tool_name, e) for e in result]
            if len(rows) >= 5:
                types = set()
                for e in result:
                    if isinstance(e, dict):
                        t = e.get("type")
                        if not t:
                            if "methods" in e or "bases" in e:
                                t = "class"
                            elif "params" in e or e.get("is_method"):
                                t = "method" if e.get("is_method") else "fn"
                        types.add(t)
                if len(types) == 1 and (common_type := types.pop()):
                    tag = f" @T:{common_type}"
                    rows = [r.replace(tag, "") for r in rows]
                    rows.insert(0, f"## {common_type}s ({len(rows)} items)")
            return "\n".join(rows)
        if isinstance(result, dict):
            if "error" in result:
                return json.dumps(result, separators=(",", ":"), default=str)
            # get_call_chain returns {"chain": [hop, hop, ...]} — compress hops.
            if tool_name == "get_call_chain" and isinstance(result.get("chain"), list):
                return "\n".join(_row(tool_name, e) for e in result["chain"])
            # get_change_impact returns {"direct": [...], "transitive": [...]}.
            # Compress each list and tag rows with their bucket + depth.
            if tool_name == "get_change_impact" and (
                isinstance(result.get("direct"), list)
                or isinstance(result.get("transitive"), list)
            ):
                lines: list[str] = []
                for bucket in ("direct", "transitive"):
                    entries = result.get(bucket) or []
                    if not entries:
                        continue
                    groups: dict[tuple, list[str]] = {}
                    for e in entries:
                        row = _row(tool_name, e)
                        depth = e.get("depth", "?") if isinstance(e, dict) else "?"
                        conf = f"{e['confidence']:.2f}" if isinstance(e, dict) and "confidence" in e else "?"
                        stype = e.get("type", "") if isinstance(e, dict) else ""
                        key = (bucket, depth, conf, stype)
                        groups.setdefault(key, []).append(row)
                    for (bkt, dep, cnf, st), rows in groups.items():
                        type_tag = f" @T:{st}" if st else ""
                        lines.append(f"## {bkt} — depth={dep} conf={cnf}{type_tag} ({len(rows)} items)")
                        for row in rows:
                            cleaned = row
                            if st:
                                cleaned = cleaned.replace(f" @T:{st}", "")
                            lines.append(cleaned)
                if result.get("truncated"):
                    lines.append(f"[truncated] {result.get('message', '')}")
                return "\n".join(lines)
            return _row(tool_name, result)
        return str(result)
    except Exception:
        return json.dumps(result, separators=(",", ":"), default=str)


# ---------------------------------------------------------------------------
# Client / workspace detection
# ---------------------------------------------------------------------------


def _detect_client_name() -> str:
    """Best-effort client attribution for persisted stats."""
    explicit = os.environ.get("TOKEN_SAVIOR_CLIENT", "").strip()
    if explicit:
        return explicit
    if os.environ.get("HERMES_GATEWAY_URL") or os.environ.get("HERMES_SESSION_ID"):
        return "hermes"
    if os.environ.get("CODEX_HOME") or os.environ.get("CODEX_SANDBOX"):
        return "codex"
    if os.environ.get("CLAUDECODE") or os.environ.get("CLAUDE_CODE_ENTRYPOINT"):
        return "claude-code"
    return "unknown"


_CLIENT_NAME = _detect_client_name()
_SESSION_LABEL = os.environ.get("TOKEN_SAVIOR_SESSION_LABEL", "").strip()


# What makes a directory a project rather than a pile of files.
PROJECT_MARKERS = (".git", "pyproject.toml", "package.json", "Cargo.toml",
                   "go.mod", "pom.xml", "Gemfile", "composer.json")

# Where people keep their work. Scanned one level deep only: two levels turns
# a home directory into a filesystem crawl, and the payoff is not there.
COMMON_PARENTS = ("projects", "dev", "src", "code", "repos", "work", "git",
                  "workspace", "Projects", "Developer")

# Never auto-index these: vendored trees, build output, virtualenvs, caches.
SKIP_DIRS = {"node_modules", ".venv", "venv", "__pycache__", "site-packages",
             "dist", "build", ".cache", ".local", ".cargo", ".rustup", "target"}

# A cap, not a limit anyone should hit. Beyond this the user has an unusual
# layout and should configure WORKSPACE_ROOTS explicitly.
MAX_AUTODISCOVERED = 40


def is_project_dir(path: str) -> bool:
    try:
        return any(os.path.exists(os.path.join(path, m)) for m in PROJECT_MARKERS)
    except OSError:
        return False


def project_root_of(path: str) -> str | None:
    """Walk up from `path` to the nearest directory that looks like a project.

    The skip check looks at **every** component, not just the current one. A
    package inside `node_modules` usually carries its own `package.json`, so
    checking only the directory being examined returns the vendored package as
    a project root before the walk ever reaches `node_modules`. Caught by
    `test_ne_remonte_pas_depuis_une_dependance_vendorisee`, not by reading.
    """
    try:
        cur = os.path.abspath(path)
    except (OSError, ValueError):
        return None
    if not os.path.isdir(cur):
        cur = os.path.dirname(cur)
    if SKIP_DIRS.intersection(cur.split(os.sep)):
        return None
    seen: set[str] = set()
    while cur and cur not in seen and cur != os.path.dirname(cur):
        seen.add(cur)
        if is_project_dir(cur):
            return cur
        cur = os.path.dirname(cur)
    return None


def autodiscover_roots(*, cwd: str | None = None, home: str | None = None) -> list[str]:
    """Find the user's projects without asking them to list them.

    Two sources, in order of confidence:

    1. the project containing the current working directory — if the server
       was started from inside a repository, that is certainly one of them;
    2. the direct children of the usual code folders (`~/projects`, `~/dev`,
       `~/src`...) that carry a project marker.

    Deliberately shallow. Recursing into a home directory to find every `.git`
    is slow, surprising, and picks up vendored clones nobody wants indexed.
    """
    found: list[str] = []

    def add(p: str | None) -> None:
        if not p or p in found or len(found) >= MAX_AUTODISCOVERED:
            return
        if os.path.basename(p) in SKIP_DIRS:
            return
        found.append(p)

    add(project_root_of(cwd or os.getcwd()))

    base = home or os.path.expanduser("~")
    for parent in COMMON_PARENTS:
        d = os.path.join(base, parent)
        if not os.path.isdir(d):
            continue
        try:
            entries = sorted(os.scandir(d), key=lambda e: e.name)
        except OSError:
            continue
        for e in entries:
            if len(found) >= MAX_AUTODISCOVERED:
                break
            if e.is_dir() and not e.name.startswith(".") and is_project_dir(e.path):
                add(e.path)

    return found

def _parse_workspace_roots() -> list[str]:
    """Parse WORKSPACE_ROOTS (comma-separated) or fall back to PROJECT_ROOT.

    Deliberately unchanged in contract: it reads configuration and nothing
    else. Auto-discovery lives in `_register_roots`, on the startup path only.
    Putting it here broke two unrelated tests, because this function is also
    called at runtime to resolve a project and suddenly answered with whatever
    happened to be near the current directory.
    """
    workspace_raw = os.environ.get("WORKSPACE_ROOTS", "").strip()
    if workspace_raw:
        roots = [r.strip() for r in workspace_raw.split(",") if r.strip()]
        return [os.path.abspath(r) for r in roots if os.path.isdir(r)]

    single = os.environ.get("PROJECT_ROOT", "").strip()
    if single and os.path.isdir(single):
        return [os.path.abspath(single)]

    return []


def _is_linked_worktree(root: str) -> bool:
    """A linked git worktree carries a `.git` FILE (gitfile pointer), the main
    checkout a `.git` directory. The distinction is the routing signal: the
    file says "this tree is a sibling copy, not the project's home"."""
    try:
        return os.path.isfile(os.path.join(root, ".git"))
    except OSError:
        return False


# Which env variable chose the boot-time active project. "CLAUDE_PROJECT_ROOT"
# means a human (or wrapper) chose deliberately — nothing else may override.
_active_hint_source = ""


def _boot_env_root_hint() -> tuple[str, str]:
    """(path, variable) of the environment's project hint, or ("", "").

    Two variables, in order of deliberateness — verified against the hosts,
    not their folklore:

    - CLAUDE_PROJECT_ROOT: Token Savior's own contract. NO host sets it
      (it appears nowhere in the Claude Code docs), so when present a human
      or wrapper script chose it — top priority.
    - CLAUDE_PROJECT_DIR: what Claude Code actually exports to stdio MCP
      servers. Stable by design: always the main checkout, never the
      worktree the session may be working in (code.claude.com/docs/en/mcp).
    - Codex exports no path variable at all AND whitelist-filters the MCP
      server environment, so under Codex both are absent and the spawn cwd
      (which Codex does point at the workspace) carries the signal instead.

    A candidate only counts if it exists and looks like a project (marker
    file; a `.git` file — a linked worktree — counts). With both variables
    set, the one that validates wins.
    """
    for var in ("CLAUDE_PROJECT_ROOT", "CLAUDE_PROJECT_DIR"):
        raw = os.environ.get(var, "").strip()
        if not raw:
            continue
        cand = os.path.abspath(raw)
        if cand in s._slot_mgr.projects or (os.path.isdir(cand) and is_project_dir(cand)):
            return cand, var
    return "", ""


def _register_roots(roots: list[str]) -> None:
    """Create slots for each root. Index is built lazily on first use.

    No discovery here. This runs at import time (`server.py` calls it at module
    level), so guessing here would fire inside every unit test that imports the
    server and register whatever projects sit near the test runner. That is
    exactly what happened: two unrelated memory-viewer tests started failing.
    Discovery belongs to `autodiscover_and_register`, called from `main()`.

    Honors the environment's project hint (see _boot_env_root_hint): a valid
    hint is registered if needed and promoted to active. This lets short-lived
    subprocess callers (benchmarks, `claude -p`, scripted flows) skip
    switch_project entirely when the calling environment already knows which
    project is in focus.
    """
    global _active_hint_source
    s._slot_mgr.register_roots(roots)
    hint_abs, hint_src = _boot_env_root_hint()
    if hint_abs:
        # Only the deliberate variable may REGISTER a root here: this runs at
        # import time, and CLAUDE_PROJECT_DIR is set for every child process
        # of a Claude Code session — pytest included. Letting it register
        # would make every test run adopt the developer's own repo. The
        # automatic variable therefore only promotes among roots that
        # explicit config or discovery already registered.
        if hint_abs not in s._slot_mgr.projects and hint_src == "CLAUDE_PROJECT_ROOT":
            s._slot_mgr.register_roots([hint_abs])
        if hint_abs in s._slot_mgr.projects:
            s._slot_mgr.active_root = hint_abs
            _active_hint_source = hint_src

    if os.environ.get("TS_WARM_START", "").strip() in ("1", "true", "yes"):
        targets = [hint_abs] if hint_abs in s._slot_mgr.projects else list(s._slot_mgr.projects)
        for root in targets:
            slot = s._slot_mgr.projects[root]
            try:
                _prep(slot)
            except Exception as e:
                print(f"[token-savior] warm-start failed for {root}: {e}", file=sys.stderr)


def autodiscover_and_register() -> list[str]:
    """Find the user's projects when nothing was configured. Startup only.

    Starting with an empty registry was the single largest source of missed
    adoption: a fresh install indexed nothing until the user hand-wrote every
    project path. Measured on one real workstation, 28% of all code reads went
    to projects that were never listed, one of them a 767-file repository that
    had existed for months.

    Called from `main()` rather than at import, so unit tests never trigger it.
    Set `TOKEN_SAVIOR_AUTODISCOVER=0` to keep the old behaviour.
    """
    if os.environ.get("TOKEN_SAVIOR_AUTODISCOVER", "1") == "0":
        return []

    fresh: list[str] = []
    if not s._slot_mgr.projects:
        roots = autodiscover_roots()
        if roots:
            _register_roots(roots)
            fresh = roots
            print(f"[token-savior] auto-discovered {len(roots)} project(s): "
                  f"{', '.join(os.path.basename(r) for r in roots[:5])}"
                  f"{'...' if len(roots) > 5 else ''}. "
                  f"Set WORKSPACE_ROOTS to pin them explicitly.", file=sys.stderr)

    # The directory the server was actually started in always gets a slot,
    # even when WORKSPACE_ROOTS pinned the registry. A session launched
    # inside a linked worktree of a configured repo otherwise routed every
    # call to the parent checkout — the worktree was never registered at all.
    cwd_root = project_root_of(os.getcwd())
    if cwd_root and cwd_root not in s._slot_mgr.projects:
        s._slot_mgr.register_roots([cwd_root])
        fresh.append(cwd_root)
        print(f"[token-savior] registered launch-directory project: {cwd_root}",
              file=sys.stderr)

    # A cwd that is a linked worktree outranks the environment's stable hint:
    # CLAUDE_PROJECT_DIR deliberately pins the MAIN checkout even when the
    # session works in a worktree (that is its documented contract), and the
    # spawn cwd is the only signal that follows the worktree. Only an
    # explicit CLAUDE_PROJECT_ROOT — set by a human, never by a host — wins
    # over it.
    if (cwd_root and cwd_root in s._slot_mgr.projects
            and s._slot_mgr.active_root != cwd_root
            and _is_linked_worktree(cwd_root)
            and _active_hint_source != "CLAUDE_PROJECT_ROOT"):
        s._slot_mgr.active_root = cwd_root
        print(f"[token-savior] active project follows the launch worktree: "
              f"{cwd_root}", file=sys.stderr)

    return fresh


_client_roots_synced = False


def _uri_to_path(uri: str) -> str | None:
    """`file:///home/u/proj` -> `/home/u/proj`. Anything else is not ours."""
    if not uri:
        return None
    if uri.startswith("file://"):
        from urllib.parse import unquote, urlparse
        return unquote(urlparse(uri).path) or None
    return uri if uri.startswith("/") else None


async def sync_client_roots(session: Any) -> list[str]:
    """Ask the client which folders the user actually has open.

    This is the protocol's own answer to the problem, and it is better than
    everything around it: `roots` is declared by the client, updated when the
    user opens or closes a workspace, and requires no action from the agent.
    Auto-discovery guesses from the filesystem; this one is told.

    Runs once per session, on the first tool call, because that is where a
    session object exists. Clients that do not implement `roots` simply answer
    nothing and we keep the discovered set. Never raises: a server that dies
    because the client lacks an optional capability is worse than one that
    guesses.
    """
    global _client_roots_synced
    if _client_roots_synced or session is None:
        return []
    _client_roots_synced = True
    try:
        import mcp.types as _t

        if not session.check_client_capability(
                _t.ClientCapabilities(roots=_t.RootsCapability())):
            return []
        result = await session.list_roots()
    except Exception:
        return []

    known = set(s._slot_mgr.projects)
    fresh: list[str] = []
    for root in getattr(result, "roots", None) or []:
        path = _uri_to_path(str(getattr(root, "uri", "")))
        if not path or not os.path.isdir(path):
            continue
        path = os.path.abspath(path)
        # A client may hand us a subdirectory; index the project that owns it.
        path = project_root_of(path) or path
        if path not in known and path not in fresh:
            fresh.append(path)
    if not fresh:
        return []
    _register_roots(sorted(known | set(fresh)))
    print(f"[token-savior] client roots: registered {len(fresh)} project(s) "
          f"({', '.join(os.path.basename(p) for p in fresh[:5])})", file=sys.stderr)
    return fresh


# ---------------------------------------------------------------------------
# Stats persistence (writer-side)
# ---------------------------------------------------------------------------


def _get_stats_file(project_root: str) -> str:
    """Return path to the stats JSON file for this project."""
    slug = hashlib.md5(project_root.encode()).hexdigest()[:8]
    name = os.path.basename(project_root.rstrip("/"))
    return os.path.join(s.stats_dir(), f"{name}-{slug}.json")


def _load_cumulative_stats(stats_file: str) -> dict[str, Any]:
    """Load cumulative stats from disk, or return empty structure."""
    if not stats_file or not os.path.exists(stats_file):
        return {
            "total_calls": 0,
            "total_chars_returned": 0,
            "total_naive_chars": 0,
            "sessions": 0,
            "tool_counts": {},
            "client_counts": {},
            "history": [],
        }
    try:
        with open(stats_file) as f:
            payload = json.load(f)
            if "history" not in payload:
                payload["history"] = []
            if "client_counts" not in payload:
                payload["client_counts"] = {}
            return payload
    except Exception:
        return {
            "total_calls": 0,
            "total_chars_returned": 0,
            "total_naive_chars": 0,
            "sessions": 0,
            "tool_counts": {},
            "client_counts": {},
            "history": [],
        }


def _flush_stats(slot: _ProjectSlot, naive_chars: int) -> None:
    """Persist a per-session snapshot and recompute cumulative totals."""
    if not slot.stats_file:
        return
    try:
        os.makedirs(s.stats_dir(), exist_ok=True)
        cum = _load_cumulative_stats(slot.stats_file)
        session_calls = sum(s._tool_call_counts.values()) - s._tool_call_counts.get(
            "get_stats", 0
        )
        cum["project"] = slot.root
        cum["last_session"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        cum["last_client"] = _CLIENT_NAME
        history = [
            entry for entry in cum.get("history", []) if entry.get("session_id") != s._session_id
        ]
        savings_pct = (1 - s._total_chars_returned / naive_chars) * 100 if naive_chars > 0 else 0.0
        session_entry = {
            "session_id": s._session_id,
            "timestamp": cum["last_session"],
            "client_name": _CLIENT_NAME,
            "session_label": _SESSION_LABEL,
            "duration_sec": round(time.time() - s._session_start, 3),
            "query_calls": session_calls,
            "chars_returned": s._total_chars_returned,
            "naive_chars": naive_chars,
            "tokens_used": s._total_chars_returned // 4,
            "tokens_naive": naive_chars // 4,
            "savings_pct": round(savings_pct, 2),
            "tool_counts": {
                tool: count
                for tool, count in s._tool_call_counts.items()
                if tool != "get_stats"
            },
        }
        history.append(session_entry)
        history = history[-s._MAX_SESSION_HISTORY:]
        cum["history"] = history
        cum["sessions"] = len(history)
        cum["total_calls"] = sum(entry.get("query_calls", 0) for entry in history)
        cum["total_chars_returned"] = sum(entry.get("chars_returned", 0) for entry in history)
        cum["total_naive_chars"] = sum(entry.get("naive_chars", 0) for entry in history)
        aggregate_tool_counts: dict[str, int] = {}
        aggregate_client_counts: dict[str, int] = {}
        for entry in history:
            for tool, count in entry.get("tool_counts", {}).items():
                aggregate_tool_counts[tool] = aggregate_tool_counts.get(tool, 0) + count
            client_name = str(entry.get("client_name") or "unknown").strip() or "unknown"
            aggregate_client_counts[client_name] = aggregate_client_counts.get(client_name, 0) + 1
        cum["tool_counts"] = aggregate_tool_counts
        cum["client_counts"] = aggregate_client_counts
        with open(slot.stats_file, "w") as f:
            json.dump(cum, f, indent=2)
    except Exception as e:
        print(f"[token-savior] Failed to flush stats: {e}", file=sys.stderr)


# ---------------------------------------------------------------------------
# Per-call cost model
# ---------------------------------------------------------------------------


# Realistic estimate of what % of codebase you'd need to read without the indexer
_TOOL_COST_MULTIPLIERS: dict[str, float] = {
    "get_project_summary": 0.10,
    "list_files": 0.01,
    "get_structure_summary": 0.05,
    "read_lines": 0.01,
    "get_functions": 0.05,
    "get_classes": 0.05,
    "get_imports": 0.03,
    "get_function_source": 0.02,
    "get_class_source": 0.03,
    "find_symbol": 0.05,
    "get_dependencies": 0.10,
    "get_dependents": 0.15,
    "get_change_impact": 0.30,
    "get_full_context": 0.30,
    "get_call_chain": 0.20,
    "get_edit_context": 0.25,  # source + deps + callers in one call
    "get_file_dependencies": 0.02,
    "get_file_dependents": 0.10,
    "search_codebase": 0.15,
    "get_git_status": 0.03,
    "get_changed_symbols": 0.12,
    "summarize_patch_by_symbol": 0.15,
    "build_commit_summary": 0.18,
    "checkpoint": 0.08,
    "replace_symbol_source": 0.20,
    "insert_near_symbol": 0.10,
    "add_field_to_model": 0.15,
    "move_symbol": 0.30,
    "apply_refactoring": 0.35,
    "find_impacted_test_files": 0.08,
    "run_impacted_tests": 0.18,
    "apply_symbol_change_and_validate": 0.35,
    "discover_project_actions": 0.0,
    "run_project_action": 0.0,
    "reindex": 0.0,
    "set_project_root": 0.0,
    "switch_project": 0.0,
    "list_projects": 0.0,
    # v3
    "get_routes": 0.08,
    "get_env_usage": 0.12,
    "get_components": 0.06,
    "get_feature_files": 0.20,
    "get_entry_points": 0.10,
    "get_related_symbols": 0.12,
    "get_duplicate_classes": 0.05,
}


def _format_result(value: object) -> str:
    """Format a query result as compact text."""
    if isinstance(value, str):
        return value
    if isinstance(value, (dict, list)):
        return json.dumps(value, separators=(",", ":"), default=str)
    return str(value)


def _count_and_wrap_result(
    slot: _ProjectSlot, name: str, arguments: dict[str, Any], result: object
) -> list[types.TextContent]:
    """Update usage counters for a tool result and return it as text content."""
    # Instrument de budget : None dans le cas normal, donc aucun octet change.
    # Voir budget_diag.py — un diagnostic qui decale la sortie invalide toute
    # mesure A/B prise avec lui.
    _diag = _budget_diag.demarrer()

    formatted = _format_result(result)
    _naifs = _estimate_naive_chars_for_call(slot, name, arguments, result)
    s._total_chars_returned += len(formatted)
    s._total_naive_chars += _naifs

    # DCP — stabilize chunk order for cache-prefix-friendly outputs
    if (
        name in s._DCP_ELIGIBLE_TOOLS
        and arguments.get("dcp", True)
        and len(formatted) >= s._DCP_MIN_BYTES
    ):
        try:
            optimized, stable, total = memory_db.optimize_output_order(formatted)
            if total > 0:
                if os.environ.get("TOKEN_SAVIOR_DEBUG") == "1":
                    formatted = f"{optimized}\n[dcp: {stable}/{total} chunks stable]"
                else:
                    formatted = optimized
                s._dcp_calls += 1
                s._dcp_stable_chunks += stable
                s._dcp_total_chunks += total
        except Exception:
            pass

    # Une reponse coupee par sa borne est indiscernable d'une reponse complete :
    # l'appelant qui compte croit mesurer un total et mesure la borne. On le dit.
    # Voir truncation.py pour pourquoi ce n'est pas de la pagination.
    try:
        from token_savior.truncation import notice_de_troncature
        notice = notice_de_troncature(arguments, result)
        if notice:
            formatted = f"{formatted}\n{notice}"
    except Exception:
        pass

    if slot.stats_file:
        _flush_stats(slot, s._total_naive_chars)

    if _diag is not None:
        _bornes = {k: v for k, v in arguments.items() if k.startswith("max_") or k in ("limit", "top_k")}
        _diag.rapporter(_budget_diag.RapportAppel(
            outil=name,
            projet=os.path.basename(getattr(slot, "root", "") or "") if slot is not None else "",
            octets_rendus=len(formatted),
            octets_naifs=_naifs,
            tronque="[tronque]" in formatted or "[truncated]" in formatted,
            raison_troncature="borne" if _bornes else "",
            bornes=_bornes,
        ))

    wrapped = [TextContent(type="text", text=formatted)]
    # A hint-less call rides the shared active_root, which any parallel agent
    # may have repointed. Once this process has seen more than one project,
    # name the one that answered: a wrong source then shows up in the answer
    # rather than three deductions later. Le tag vit ici, seul point de passage
    # commun aux trois retours de `_dispatch_tool` (_SLOT_HANDLERS, cache-hit
    # QFN, QFN normal) : posé côté appelant, il manquait aux outils QFN
    # (find_symbol, search_codebase, get_full_context…), c'est-à-dire à la
    # majorité des appels réels.
    if not arguments.get("project") and slot is not None and s.racines_multiples():
        wrapped.append(
            TextContent(type="text", text=f"[project: {os.path.basename(slot.root)}]")
        )
    return wrapped


def _estimate_naive_chars_for_call(
    slot: _ProjectSlot, tool_name: str, arguments: dict[str, Any], result: object
) -> int:
    """Estimate the naive character cost of one tool call."""
    index = slot.indexer._project_index if slot.indexer else None
    if index is None:
        return 0

    source_chars = sum(meta.total_chars for meta in index.files.values())
    file_sizes = {path: meta.total_chars for path, meta in index.files.items()}

    def size_for(paths: list[str]) -> int:
        total = 0
        for path in paths:
            resolved = (
                path
                if path in file_sizes
                else next((p for p in file_sizes if p.endswith(path) or path.endswith(p)), None)
            )
            if resolved:
                total += file_sizes[resolved]
        return total

    is_ckpt_create = tool_name == "checkpoint" and (arguments.get("op") or "list") == "create"
    if tool_name in {"summarize_patch_by_symbol", "build_commit_summary"} or is_ckpt_create:
        changed_files = arguments.get("changed_files") or arguments.get("file_paths") or []
        return max(size_for(changed_files), len(_format_result(result)))

    if tool_name in {"replace_symbol_source", "insert_near_symbol"} and isinstance(result, dict):
        target_file = result.get("file")
        return max(size_for([target_file]) * 2 if target_file else 0, len(_format_result(result)))

    if tool_name in {"run_impacted_tests", "find_impacted_test_files"} and isinstance(result, dict):
        selection = result.get("selection") or result
        impacted = selection.get("impacted_tests", [])
        changed = selection.get("changed_files", [])
        return max(size_for(impacted + changed), len(_format_result(result)))

    if tool_name == "apply_symbol_change_and_validate" and isinstance(result, dict):
        edit = result.get("edit", {})
        file_path = edit.get("file")
        validation = result.get("validation", {})
        impacted = validation.get("selection", {}).get("impacted_tests", [])
        return max(
            size_for(([file_path] if file_path else []) + impacted) * 2, len(_format_result(result))
        )

    is_ckpt_compare = tool_name == "checkpoint" and (arguments.get("op") or "list") == "compare"
    if (tool_name == "get_changed_symbols" or is_ckpt_compare) and isinstance(result, dict):
        files = [entry.get("file") for entry in result.get("files", []) if entry.get("file")]
        return max(size_for(files), len(_format_result(result)))

    multiplier = _TOOL_COST_MULTIPLIERS.get(tool_name, 0.10)
    return max(int(source_chars * multiplier), len(_format_result(result)))


# ---------------------------------------------------------------------------
# Slot lifecycle helpers
# ---------------------------------------------------------------------------


def _prep(slot: _ProjectSlot) -> None:
    """Ensure slot is indexed and incrementally updated."""
    s._slot_mgr.ensure(slot)
    s._slot_mgr.maybe_update(slot)


def _warm_cache_async(
    predictions: list[tuple[str, float]],
    slot,
    min_prob: float = 0.25,
    tool_name: str = "",
    symbol_name: str = "",
) -> None:
    """Spawn a daemon thread to pre-render likely next responses.

    Uses the Markov prefetcher's ``beam_search_continuations`` to expand
    ``beam_width=3`` × ``max_depth=3`` branches when possible, falling back
    to the flat *predictions* list otherwise. Safe tools listed in
    ``s._PREFETCHABLE_TOOLS`` are executed; results land in ``s._prefetch_cache``
    keyed by state.

    daemon=True is critical: if the MCP server shuts down mid-prefetch, the
    thread is killed with the process instead of holding it open.
    """
    if not predictions and not tool_name:
        return

    def _worker() -> None:

        try:
            qfns = slot.query_fns if slot is not None else None
            # Collect states to warm: beam frontier + direct predictions.
            states_to_warm: list[tuple[str, float]] = []
            seen: set[str] = set()
            if tool_name:
                try:
                    beams = s._prefetcher.beam_search_continuations(
                        tool_name, symbol_name,
                        beam_width=3, max_depth=3, min_prob=0.15,
                    )
                except Exception:
                    beams = []
                for path, joint in beams:
                    s._spec_branches_explored += 1
                    for st in path:
                        if st in seen:
                            continue
                        seen.add(st)
                        states_to_warm.append((st, joint))
            for st, prob in predictions:
                if st not in seen:
                    seen.add(st)
                    states_to_warm.append((st, prob))
            # Leiden community pre-warm: if the current call has a symbol that
            # belongs to a small community (≤20), warm get_function_source for
            # up to 10 peers. They are likely the next user of this session.
            if symbol_name:
                try:
                    comm = s._leiden.get_community_for(symbol_name)
                    if comm and comm["size"] <= 20:
                        peers = [m for m in comm["members"] if m != symbol_name][:10]
                        for peer in peers:
                            st = f"get_function_source:{peer}"
                            if st not in seen:
                                seen.add(st)
                                # Fixed mid-priority probability — peers are
                                # plausible but not Markov-predicted.
                                states_to_warm.append((st, 0.35))
                except Exception:
                    pass

            for state, prob in states_to_warm:
                if prob < min_prob:
                    continue
                if ":" not in state:
                    continue
                next_tool, next_symbol = state.split(":", 1)
                if not next_symbol or qfns is None:
                    continue
                if next_tool not in s._PREFETCHABLE_TOOLS:
                    continue
                with s._prefetch_lock:
                    if state in s._prefetch_cache:
                        continue
                try:
                    result = qfns[next_tool](next_symbol)
                except Exception:
                    continue
                with s._prefetch_lock:
                    s._prefetch_cache[state] = (
                        result if isinstance(result, str) else str(result)
                    )
                    s._spec_branches_warmed += 1
                    if len(s._prefetch_cache) > 64:
                        # Bound memory; drop oldest entries (insertion order).
                        for stale_key in list(s._prefetch_cache)[:32]:
                            s._prefetch_cache.pop(stale_key, None)
        except Exception:
            pass  # never crash the daemon

    t = threading.Thread(target=_worker, daemon=True)
    t.start()


def _recompute_leiden(slot) -> None:
    """Run Leiden on the slot's global dependency graph (best-effort)."""
    try:
        idx = getattr(slot.indexer, "_project_index", None) if slot and slot.indexer else None
        if idx is None:
            return
        graph = getattr(idx, "global_dependency_graph", None) or {}
        if not graph:
            return
        s._leiden.compute(graph, min_size=3, max_size=50)
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Project resolution (used by memory-engine handlers)
# ---------------------------------------------------------------------------


def _resolve_project_root(arguments: dict[str, Any]) -> str:
    project_hint = arguments.get("project")
    slot, _err = s._slot_mgr.resolve(project_hint)
    if slot:
        return slot.root
    roots = _parse_workspace_roots()
    if roots:
        return roots[0]
    return os.path.expanduser("~")
