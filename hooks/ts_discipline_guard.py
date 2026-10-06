#!/usr/bin/env python3
"""PreToolUse guard: enforce Token Savior usage instead of documenting it.

Measured on this repo with `scripts/ts_audit.py`, one-day window:

    get_edit_context: 0 vs 245 edits (GAP)
    edit_without_context: 11
    nudge edit_context: 12 fires

Zero calls against 245 edits. The rule was written in `CLAUDE.md` from the
start and the nudges fired twelve times a day. Compliance was zero. A written
reminder does not constrain anything, however often it is read.

Five rules, each backed by a measured waste rather than a style preference:

1. **Editing a symbol without its context.** `replace_symbol_source` on a
   symbol whose context was never requested edits blind: neither the callers
   nor the impacted tests are known. This is the `edit_without_context` line.
2. **Native `Edit`/`Write` on indexed source.** Bypasses the symbol graph, so
   the edit-impact block never fires.
3. **Native `Read` on indexed source.** Pulls a whole file where
   `get_function_source` returns the symbol.
4. **Native `Grep`/`Glob` targeting indexed code.** `search_codebase` replaces
   it term for term and returns symbols rather than raw lines.
5. **Reading code through the shell.** `grep`/`cat`/`sed`/`awk` on an indexed
   source file bypasses the symbol graph and costs more output.

**Not refusing too much is the hard part**, and it is not a theoretical
concern. The first version of rule 5 was replayed against 9054 real tool calls
from past transcripts (see `scripts/` in the companion tooling): it produced
1036 denials of which **710 were false positives, 68.5%** — `cd` alone
accounted for 395 of them. A guard at that rate gets switched off within a
week, and a guard that is off protects less than no guard at all because it
also grants the illusion of protection. Every exit door below is covered by a
test.

Contract: PreToolUse JSON on stdin, decision on stdout, exit 0, **fail-open**.
Any exception lets the call through. A guard must never be the reason a
session stops.

Opt-in: inert unless `TS_DISCIPLINE_GUARD=1` is set, because it denies calls
and enabling that by default would break existing installs on upgrade. Escape
hatch once enabled: `TS_GUARD_OFF=1`, which wins.
"""
from __future__ import annotations

import json
import os
import re
import sys
import tempfile
import time
from pathlib import Path

# Extensions Token Savior can edit structurally. Everything else (.md, .json,
# .yml, .sql, .env) stays on the native tools by design.
CODE_EXTENSIONS = (".py", ".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs")
INDEX_MARKER = ".token-savior-cache.json"
RACINES_IGNOREES = {"/", os.path.realpath(tempfile.gettempdir())}

# The MCP server registers as `token-savior` or `token-savior-recall`
# depending on the install, hence the loose middle.
CONTEXT_TOOLS = re.compile(
    r"__(get_edit_context|get_full_context|get_function_source|get_class_source"
    r"|ts_execute)$")
# Inside a ts_execute script: `tools.get_edit_context({ name: "X" })`.
SCRIPT_CONTEXT = re.compile(
    r"tools\.(get_edit_context|get_full_context|get_function_source|get_class_source)"
    r"\(\s*\{[^}]*?\b(?:name|symbol_name|symbol)\s*:\s*[\"'`]([^\"'`]+)[\"'`]")
# Rewriting a symbol needs its context. Inserting next to one does not: the
# anchor is only a position, it is not modified. 26 of the 29 refusals of
# insert_near_symbol over 30 days (06/10/2026) were on that anchor.
EDIT_TOOLS = re.compile(
    r"__(replace_symbol_source|add_field_to_model|move_symbol)$")

# A whole-file Read of a short file costs about what read_lines would, and
# Claude Code's Edit requires a prior Read of the file: refusing it only adds
# a round-trip. Measured 06/10/2026 over 30 days: 78 % of refused Reads were
# re-issued identically within five minutes.
LECTURE_LIBRE_LIGNES = int(os.environ.get("TS_GUARD_READ_FREE_LINES", "200"))

SHELL_READERS = re.compile(r"^(cat|head|tail|less|more|grep|rg|sed|awk)$")

# Code extensions expressed as a Grep `glob` or `type` filter.
CODE_FILTER = re.compile(r"\b(py|ts|tsx|js|jsx|python|typescript|javascript)\b")

# Vendored or generated trees: not project symbols, reading them natively is
# the normal thing to do.
TOLERATED_PATHS = re.compile(r"/(node_modules|\.git|dist|build|__pycache__|\.venv)/")


def indexed_root(path: str) -> str | None:
    """Walk up looking for the Token Savior index marker.

    Deliberately not reading `WORKSPACE_ROOTS`: the hook runs in the agent's
    environment, not the MCP server's, where that variable does not exist. The
    marker file at an indexed project's root is local, present exactly where
    the question is asked, and cannot drift from a distant config.

    A marker at `/` or in the temp directory is not a project: on 06/10/2026
    a stray `/tmp/.token-savior-cache.json` (25 MB, written by a nightly job)
    made every file under `/tmp` count as indexed code.
    """
    try:
        p = Path(path).resolve()
    except (OSError, ValueError):
        return None
    p = p if p.is_dir() else p.parent
    for candidate in (p, *p.parents):
        if str(candidate) in RACINES_IGNOREES:
            continue
        if (candidate / INDEX_MARKER).exists():
            return str(candidate)
    return None


def is_indexed_code(path: str) -> bool:
    """Does this path name a real source file of an indexed project?

    Two conditions were added on 2026-07-27, each on a measured false
    positive that blocked real work:

    1. **The name must have a stem.** The shell tokeniser knows nothing about
       globs: `ls dist/index-*.js` splits into `dist/index-` and `.js`. That
       bare `.js` fragment, resolved against the current directory, walked up
       to the indexed project root and got a whole command refused — a command
       that read no source at all. No file is ever literally named `.js`, so
       requiring a stem costs zero detections.

    2. **The file must exist.** A path that names nothing cannot be a read of
       code. This also discards glob fragments, format strings and paths
       merely quoted as examples inside a command.

    The guard exists to make Token Savior the default, not to stop people
    working. An unjustified denial gets the guard switched off, and a guard
    that is switched off protects nothing — that trade-off decides every rule
    in this file.
    """
    if not path or not path.endswith(CODE_EXTENSIONS):
        return False
    if TOLERATED_PATHS.search(path):
        return False
    base = os.path.basename(path)
    if not os.path.splitext(base)[0].strip("."):
        return False
    try:
        if not os.path.isfile(os.path.expanduser(path)):
            return False
    except OSError:
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


STATE_TTL_S = 7 * 86400


def prune_state(dossier: Path) -> None:
    """Drop session files nothing will read again.

    One file per session accumulated forever, in a directory the user never
    sees and no command names. Pruning on write keeps it to a handful of lines
    and needs no new command: the directory stays small precisely because it
    is pruned, so the scan it costs stays cheap.

    Best-effort throughout — a state directory we cannot tidy must never be
    the reason a tool call is refused.
    """
    limite = time.time() - STATE_TTL_S
    try:
        entrees = list(dossier.iterdir())
    except OSError:
        return
    for entree in entrees:
        try:
            if entree.suffix == ".json" and entree.stat().st_mtime < limite:
                entree.unlink()
        except OSError:
            pass


def record_symbols(session_id: str, names: list[str]) -> None:
    seen = seen_symbols(session_id) | {n for n in names if n}
    try:
        chemin = state_file(session_id)
        prune_state(chemin.parent)
        chemin.write_text(json.dumps(sorted(seen)), encoding="utf-8")
    except OSError:
        pass


def _fichier_refus(session_id: str) -> Path:
    """Fichier d'etat des refus deja prononces dans cette session."""
    p = state_file(session_id)
    return p.with_name(p.stem + "-refus.json")


def deja_refuse(session_id: str, cle: str) -> bool:
    try:
        return cle in set(json.loads(
            _fichier_refus(session_id).read_text(encoding="utf-8")))
    except (OSError, ValueError):
        return False


def noter_refus(session_id: str, cle: str) -> None:
    try:
        chemin = _fichier_refus(session_id)
        try:
            deja = set(json.loads(chemin.read_text(encoding="utf-8")))
        except (OSError, ValueError):
            deja = set()
        deja.add(cle)
        prune_state(chemin.parent)
        chemin.write_text(json.dumps(sorted(deja)), encoding="utf-8")
    except OSError:
        pass


def cle_de_refus(tool: str, tool_input: dict) -> str:
    """Identifie l'appel refuse, pour ne pas le refuser deux fois.

    Un refus dit « il existe une meilleure route ». Repete, il ne dit plus
    rien de neuf : il empeche. Le 27/07/2026 trois gardes bloquants ont ete
    retires de ce poste apres avoir bloque quatre fois du travail correct en
    une session, dont deux fois sur un cas que Token Savior ne sait pas
    traiter -- `replace_symbol_source` ne porte que sur les fonctions et les
    classes, pas sur une constante ni sur un dictionnaire de module. Le garde
    interdisait alors la seule voie restante.

    La sortie de secours documentee, `TS_GUARD_OFF=1`, ne repond pas a ce
    cas : elle vit dans l'environnement de la session, on ne la pose pas
    entre deux appels. En pratique elle voulait dire « demande a l'utilisateur
    d'eteindre le garde », c'est-a-dire le mur.

    D'ou cette cle : le premier appel est refuse et enseigne, le second passe.
    Reformuler le meme appel est alors une decision prise en connaissance de
    cause, et non une impasse.
    """
    cible = (
        tool_input.get("file_path")
        or tool_input.get("symbol_name")
        or tool_input.get("name")
        or tool_input.get("path")
        or tool_input.get("command")
        or ""
    )
    return f"{tool}:{str(cible)[:200]}"


def journal_garde() -> Path:
    """Ou s'ecrit le compte des refus. Actif par defaut, exprès.

    Le reste de ce depot met ses journaux derriere une variable
    d'environnement. Pas celui-ci : un garde-fou dont personne ne compte les
    refus derive sans que ca se voie. Le reecriveur de commandes a passe des
    mois installe et inerte sur ce poste sans que rien ne le signale.

    Une ligne JSON par decision, basename uniquement -- de quoi mesurer la
    friction et sa derive, pas de quoi reconstituer une session.
    `TS_GUARD_LOG=0` coupe, `TS_GUARD_LOG=<chemin>` deplace.
    """
    valeur = os.environ.get("TS_GUARD_LOG", "")
    if valeur:
        return Path(valeur)
    base = Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local" / "state"))
    d = base / "token-savior"
    d.mkdir(parents=True, exist_ok=True)
    return d / "discipline-guard.jsonl"


def _cible_journalisable(tool: str, cible: str) -> str:
    """Ce qu'on garde d'une cible : un nom de fichier, jamais une commande.

    Sur `Bash`, la cible est la ligne de commande entiere : `os.path.basename`
    en rendait un fragment arbitraire, heredoc compris. Le journal promet de
    ne pas permettre de reconstituer une session, il doit donc tenir cette
    promesse la ou c'est le plus facile de la trahir.
    """
    texte = str(cible)
    if tool == "Bash":
        premier = texte.strip().split()
        return premier[0][:20] if premier else "bash"
    return os.path.basename(texte)[:60]


def noter_decision(decision: str, verdict: str, tool: str, cible: str) -> None:
    """Best-effort : un journal illisible ne doit jamais refuser un appel."""
    if os.environ.get("TS_GUARD_LOG") == "0":
        return
    try:
        with open(journal_garde(), "a", encoding="utf-8") as f:
            f.write(json.dumps({
                "t": int(time.time()),
                "decision": decision,          # "refus" | "relance"
                "verdict": verdict,
                "outil": tool,
                "cible": _cible_journalisable(tool, cible),
            }) + "\n")
    except OSError:
        pass


def requested_names(tool, tool_input: dict) -> list[str]:
    """Symbols whose source the caller has just seen.

    `get_full_context` accepts `name` or `names=[...]` in batch mode. Reading
    the source with `get_function_source` / `get_class_source` counts too: the
    refusal only ever asked that the symbol be looked at before being
    rewritten, and the server appends the edit-impact block (callers) to the
    edit itself.

    A `ts_execute` script is the route the instructions recommend for chains,
    so its calls count as well, read from the script text. Measured on 30
    days of transcripts (06/10/2026): of 33 refusals of
    `replace_symbol_source`, most followed a context taken through a route
    this function did not recognise.
    """
    if isinstance(tool, str) and tool.endswith("__ts_execute"):
        script = str(tool_input.get("script") or tool_input.get("code") or "")
        return [m.group(2) for m in SCRIPT_CONTEXT.finditer(script)]
    names: list[str] = []
    for cle in ("name", "symbol_name", "symbol"):
        if isinstance(tool_input.get(cle), str):
            names.append(tool_input[cle])
    batch = tool_input.get("names")
    if isinstance(batch, list):
        names += [n for n in batch if isinstance(n, str)]
    return names


# --- The five verdicts ---------------------------------------------------- #

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
    """Native `Edit`/`Write` on indexed source.

    Goes through `is_indexed_code` like every other verdict. It used to repeat
    the checks inline and drop `TOLERATED_PATHS` on the way, so the same file
    was readable and not editable: `Read` on `node_modules/pkg/mod.py` passed,
    `Edit` on it was refused. The remedy the message proposes cannot work
    there — those trees are excluded from the index, so there is no symbol to
    replace, and the only way out was `TS_GUARD_OFF=1`.

    `is_indexed_code` also requires the file to exist, which keeps the
    "creating a file has no symbol to replace" exit.
    """
    path = str(tool_input.get("file_path") or "")
    if not is_indexed_code(path):
        return None
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
    """Whole-file Read of a long indexed source file.

    A ranged Read (`offset`/`limit`) already is the targeted read this guard
    asks for, and a short file read whole costs no more than its symbols.
    Both pass. Before 06/10/2026 every Read of indexed code was refused once
    and 78 % were re-issued identically: Claude Code's `Edit` demands a prior
    `Read` of the file, so the refusal mostly bought a round-trip.
    """
    path = str(tool_input.get("file_path") or "")
    if not is_indexed_code(path):
        return None
    if tool_input.get("offset") or tool_input.get("limit"):
        return None
    try:
        with open(os.path.expanduser(path), "rb") as f:
            lignes = sum(1 for _ in f)
    except OSError:
        return None
    if lignes <= LECTURE_LIBRE_LIGNES:
        return None
    return (
        f"native Read on {os.path.basename(path)}, an indexed source file "
        f"of {lignes} lines.\n"
        f'  read_lines(file_path, start, end) for a range, get_function_source("<symbol>") '
        f'or get_full_context("<symbol>") for a symbol and its neighbourhood.\n'
        f"  A Read with offset/limit passes too."
    )


def verdict_native_grep(tool_input: dict) -> str | None:
    """Native `Grep`/`Glob` on indexed code.

    Only refuse when the target is explicitly code: either the path names a
    source file, or the `glob`/`type` filter names a code extension. An
    unfiltered Grep inside an indexed project also searches `.md`, `.json` and
    log files, where Token Savior has nothing to offer — refusing that would
    be the false positive that gets a guard switched off.
    """
    path = str(tool_input.get("path") or "")
    if is_indexed_code(path):
        target = os.path.basename(path)
    else:
        filt = f"{tool_input.get('glob') or ''} {tool_input.get('type') or ''}"
        if not CODE_FILTER.search(filt):
            return None
        if not path or not indexed_root(path):
            return None
        target = f"{filt.strip()} in {os.path.basename(path.rstrip('/'))}"
    return (
        f"native Grep on indexed code ({target}).\n"
        f"  search_codebase(pattern) searches the index and returns the "
        f"symbol, not the raw line.\n"
        f"  search_codebase(description, semantic=True) when you are after an "
        f"intent rather than a pattern."
    )


def verdict_shell_read(command: str) -> str | None:
    """Shell read of code: the file must be an argument of the reader.

    The first version was measured at **68.5% false positives** over 1036
    replayed denials. It required a reader *somewhere* in the command and a
    code file *somewhere*, without checking the link between the two. A
    compound command such as `cd project && pytest tests/x.py | grep passed`
    satisfied both conditions without reading a single line of code; `cd`
    alone triggered 395 denials.

    So we split into sub-commands and only accuse the one whose **head** is a
    reader and which **cites** an indexed source file.

    The split ignores quoting, which is the right trade-off here: a separator
    inside quotes costs a missed detection, never the reverse. A false
    negative costs one suboptimal read; a false positive costs the guard.
    """
    for chunk in re.split(r"&&|\|\||\||;|\n", command):
        c = chunk.strip()
        if not c:
            continue
        head = re.match(r"([\w.-]+)", c)
        if not head or not SHELL_READERS.match(head.group(1)):
            continue
        for token in re.findall(r"[\w./~-]+", c):
            if token.endswith(CODE_EXTENSIONS) and is_indexed_code(
                    os.path.expanduser(token)):
                return (
                    f"shell read of {os.path.basename(token)}, an indexed "
                    f"source file.\n  search_codebase(pattern) replaces grep, "
                    f"get_function_source(name) replaces cat.\n"
                    f"Bash stays the right tool for builds, tests, git and "
                    f"network."
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
            record_symbols(session_id, requested_names(tool, tool_input))
            return 0

        reason = None
        if EDIT_TOOLS.search(tool):
            reason = verdict_edit_without_context(tool, tool_input, session_id)
            if reason is None:
                # Edited once with its context: later edits of the same
                # symbol in this session need no second context call.
                record_symbols(session_id, requested_names(tool, tool_input))
        elif tool in ("Edit", "Write", "NotebookEdit"):
            # Advice only since 06/10/2026: 303 of 305 refusals over 30 days
            # were re-issued identically within five minutes. A refusal that
            # is always overridden teaches nothing and costs a round-trip.
            conseil = verdict_native_edit(tool_input)
            if conseil:
                noter_decision("conseil", conseil.split("\n", 1)[0][:60], tool,
                               str(tool_input.get("file_path") or ""))
            return 0
        elif tool == "Read":
            reason = verdict_native_read(tool_input)
        elif tool in ("Grep", "Glob"):
            reason = verdict_native_grep(tool_input)
        elif tool == "Bash":
            reason = verdict_shell_read(str(tool_input.get("command") or ""))

        if reason:
            cle = cle_de_refus(tool, tool_input)
            court = reason.split("\n", 1)[0][:60]
            if deja_refuse(session_id, cle):
                # Deuxieme fois sur le meme appel : l'enseignement est passe,
                # insister n'apprend plus rien et devient un mur.
                noter_decision("relance", court, tool, cle.split(":", 1)[-1])
                return 0
            noter_refus(session_id, cle)
            noter_decision("refus", court, tool, cle.split(":", 1)[-1])
            print(json.dumps({"hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "deny",
                "permissionDecisionReason": (
                    f"[ts_discipline_guard] {reason}\n"
                    f"Si cette route est la bonne malgre tout, relancez le meme "
                    f"appel : il passera."
                ),
            }}))
    except Exception:
        return 0
    return 0


if __name__ == "__main__":
    sys.exit(main())
