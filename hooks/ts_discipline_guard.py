#!/usr/bin/env python3
"""PreToolUse guard: enforce Token Savior usage instead of documenting it.

Measured on this repo with `scripts/ts_audit.py`, one-day window:

    get_edit_context: 0 vs 245 edits (GAP)
    edit_without_context: 11
    nudge edit_context: 12 fires

Zero calls against 245 edits. The rule was written in `CLAUDE.md` from the
start and the nudges fired twelve times a day. Compliance was zero. A written
reminder does not constrain anything, however often it is read.

Four rules, each backed by a measured waste rather than a style preference:

1. **Editing a symbol without its context.** `replace_symbol_source` on a
   symbol whose context was never requested edits blind: neither the callers
   nor the impacted tests are known. This is the `edit_without_context` line.
2. **Native `Edit`/`Write` on indexed source.** Bypasses the symbol graph
   entirely, so the edit-impact block never fires.
3. **Native `Read` on indexed source.** Pulls a whole file where
   `get_function_source` returns the symbol.
4. **Reading code through the shell.** `grep`/`cat`/`sed`/`awk` on an indexed
   source file bypasses the symbol graph and costs more output.

The hard part is not refusing, it is not refusing too much. A guard with false
positives gets switched off, and a guard that is off protects less than no
guard at all because it also grants the illusion of protection. Hence the exit
doors below, each one covered by a test.

Contract: PreToolUse JSON on stdin, decision on stdout, exit 0, **fail-open**.
Any exception lets the call through. A guard must never be the reason a
session stops.

Opt-in: the guard is inert unless `TS_DISCIPLINE_GUARD=1` is set, because it
denies calls and enabling that by default would break existing installs on
upgrade. Escape hatch once enabled: `TS_GUARD_OFF=1`, which wins.
"""
from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path

# Extensions Token Savior can edit structurally. Everything else (.md, .json,
# .yml, .sql, .env) stays on the native tools by design.
CODE_EXTENSIONS = (".py", ".ts", ".tsx", ".js", ".jsx")
INDEX_MARKER = ".token-savior-cache.json"

# The MCP server is registered as `token-savior` or `token-savior-recall`
# depending on the install, hence the loose middle.
CONTEXT_TOOLS = re.compile(r"__(get_edit_context|get_full_context)$")
EDIT_TOOLS = re.compile(
    r"__(replace_symbol_source|insert_near_symbol|add_field_to_model|move_symbol)$")

SHELL_READERS = re.compile(r"\b(cat|head|tail|less|more|grep|rg|sed|awk)\b")

# Vendored or generated trees: not project symbols, reading them natively is
# the normal thing to do.
TOLERATED_PATHS = re.compile(r"/(node_modules|\.git|dist|build|__pycache__|\.venv)/")


def indexed_root(path: str) -> str | None:
    """Walk up looking for the Token Savior index marker.

    Deliberately not reading `WORKSPACE_ROOTS`: the hook runs in the agent's
    environment, not the MCP server's, where that variable does not exist. The
    marker file dropped at an indexed project's root is local, present exactly
    where the question is asked, and cannot drift from a distant config.
    """
    try:
        p = Path(path).resolve()
    except (OSError, ValueError):
        return None
    p = p if p.is_dir() else p.parent
    for candidate in (p, *p.parents):
        if (candidate / INDEX_MARKER).exists():
            return str(candidate)
    return None


def is_indexed_code(path: str) -> bool:
    if not path or not path.endswith(CODE_EXTENSIONS):
        return False
    if TOLERATED_PATHS.search(path):
        return False
    return indexed_root(path) is not None


# --- Session state: which symbols already have their context -------------- #

def state_file(session_id: str) -> Path:
    base = Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local" / "state"))
    d = base / "token-savior" / "discipline-guard"
    d.mkdir(parents=True, exist_ok=True)
    safe = re.sub(r"[^A-Za-z0-9_.-]", "_", session_id or "no-session")[:120]
    return d / f"{safe}.json"


def seen_symbols(session_id: str) -> set[str]:
    try:
        return set(json.loads(state_file(session_id).read_text(encoding="utf-8")))
    except (OSError, ValueError):
        return set()


def record_symbols(session_id: str, names: list[str]) -> None:
    seen = seen_symbols(session_id) | {n for n in names if n}
    try:
        state_file(session_id).write_text(json.dumps(sorted(seen)), encoding="utf-8")
    except OSError:
        pass


def requested_names(tool_input: dict) -> list[str]:
    """`get_full_context` accepts `name` or `names=[...]` in batch mode."""
    names: list[str] = []
    if isinstance(tool_input.get("name"), str):
        names.append(tool_input["name"])
    batch = tool_input.get("names")
    if isinstance(batch, list):
        names += [n for n in batch if isinstance(n, str)]
    return names


# --- The four verdicts ---------------------------------------------------- #

def verdict_edit_without_context(tool: str, tool_input: dict, session_id: str) -> str | None:
    symbol = tool_input.get("symbol_name") or tool_input.get("name")
    if not isinstance(symbol, str) or not symbol:
        return None
    if symbol in seen_symbols(session_id):
        return None
    short = tool.rsplit("__", 1)[-1]
    return (
        f'{short}("{symbol}") without prior context.\n'
        f'  get_edit_context("{symbol}") first: source, callers, siblings and '
        f"impacted tests in one call.\n"
        f"Editing without context means ignoring the callers and tests the "
        f"change breaks."
    )


def verdict_native_edit(tool_input: dict) -> str | None:
    path = str(tool_input.get("file_path") or "")
    if not path.endswith(CODE_EXTENSIONS):
        return None
    if not os.path.exists(path):
        return None  # creating a file: no symbol to replace
    root = indexed_root(path)
    if not root:
        return None
    return (
        f"native edit of {os.path.basename(path)} inside indexed project "
        f"{os.path.basename(root)}.\n"
        f'  1. get_edit_context("<symbol>")\n'
        f"  2. replace_symbol_source / insert_near_symbol\n"
        f"Native edits bypass the symbol graph, so the edit-impact block never "
        f"fires. If structural editing does not fit here (module constants, "
        f"decorators), re-run with TS_GUARD_OFF=1."
    )


def verdict_native_read(tool_input: dict) -> str | None:
    path = str(tool_input.get("file_path") or "")
    if not is_indexed_code(path):
        return None
    return (
        f"native Read on {os.path.basename(path)}, an indexed source file.\n"
        f'  get_function_source("<symbol>") or get_full_context("<symbol>") '
        f"return the symbol and its neighbourhood instead of the whole file."
    )


def verdict_shell_read(command: str) -> str | None:
    if not SHELL_READERS.search(command):
        return None
    for token in re.findall(r"[\w./~-]+", command):
        if token.endswith(CODE_EXTENSIONS) and is_indexed_code(os.path.expanduser(token)):
            return (
                f"shell read of {os.path.basename(token)}, an indexed source "
                f"file.\n  search_codebase(pattern) replaces grep, "
                f"get_function_source(name) replaces cat.\n"
                f"Bash stays the right tool for builds, tests, git and network."
            )
    return None


def main() -> int:
    try:
        # Opt-in by design. This guard *denies* calls, so switching it on by
        # default would break existing installs on upgrade. Same contract as
        # TS_BASH_COMPACT and TS_BASH_REWRITE.
        if os.environ.get("TS_DISCIPLINE_GUARD") != "1":
            return 0
        if os.environ.get("TS_GUARD_OFF") == "1":
            return 0
        raw = sys.stdin.read()
        if not raw.strip():
            return 0
        data = json.loads(raw)
        tool = str(data.get("tool_name") or "")
        tool_input = data.get("tool_input") or {}
        if not isinstance(tool_input, dict):
            return 0
        session_id = str(data.get("session_id") or "")

        if CONTEXT_TOOLS.search(tool):
            record_symbols(session_id, requested_names(tool_input))
            return 0

        reason = None
        if EDIT_TOOLS.search(tool):
            reason = verdict_edit_without_context(tool, tool_input, session_id)
        elif tool in ("Edit", "Write", "NotebookEdit"):
            reason = verdict_native_edit(tool_input)
        elif tool == "Read":
            reason = verdict_native_read(tool_input)
        elif tool == "Bash":
            reason = verdict_shell_read(str(tool_input.get("command") or ""))

        if reason:
            print(json.dumps({"hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "deny",
                "permissionDecisionReason": f"[ts_discipline_guard] {reason}",
            }}))
    except Exception:
        return 0
    return 0


if __name__ == "__main__":
    sys.exit(main())
