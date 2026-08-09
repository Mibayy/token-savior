"""Rewrite rules for the Bash PreToolUse hook.

Each rule is a small dataclass with ``matches(cmd)`` and ``apply(cmd)``
methods. Rules are pure — they only look at the command string. Order
matters: the first matching rule wins.

Conventions
-----------
* All rules operate on the **stripped** command (no leading/trailing
  whitespace) and expect that :func:`is_unsafe_to_rewrite` already
  filtered out shell-composition cases.
* A rule **must not** alter a command that already carries an opinion
  flag from the user (verbose flags, or rule-specific flags listed in
  the rule's own ``conflict_flags``).
"""
from __future__ import annotations

import re
import shlex
from collections.abc import Callable
from dataclasses import dataclass, field

# Operators that turn a Bash one-liner into a composition. We never
# rewrite a command that contains any of them — too easy to break the
# user's intent.
_UNSAFE_PATTERNS = (
    "|", ">", "<", "&&", "||", ";", "$(", "`",
)

# Standalone ``--`` separator is unsafe too (positional cutoff).
_UNSAFE_TOKEN_DOUBLE_DASH = "--"

# Flags that signal the user wants verbose output. If any of these
# appears as a standalone token, skip every rule.
_VERBOSE_TOKENS = {"-v", "-vv", "-vvv", "--verbose"}


def _tokens(cmd: str) -> list[str]:
    """``shlex.split`` with a graceful fallback for malformed quoting."""
    try:
        return shlex.split(cmd)
    except ValueError:
        return cmd.split()


def is_unsafe_to_rewrite(cmd: str) -> bool:
    """Return ``True`` if the command must be passed through untouched."""
    for pat in _UNSAFE_PATTERNS:
        if pat in cmd:
            return True
    toks = _tokens(cmd)
    if _UNSAFE_TOKEN_DOUBLE_DASH in toks:
        return True
    return any(t in _VERBOSE_TOKENS for t in toks)


def _has_any_flag(toks: list[str]) -> bool:
    """True if any token (after the command word(s)) starts with ``-``."""
    return any(t.startswith("-") for t in toks)


def _has_flag(toks: list[str], *flags: str) -> bool:
    """True if any of ``flags`` is present as a standalone token, or as
    the prefix of a ``--key=value`` token."""
    flagset = set(flags)
    for t in toks:
        if t in flagset:
            return True
        if "=" in t and t.split("=", 1)[0] in flagset:
            return True
    return False


@dataclass
class RewriteRule:
    """A single rewrite rule."""

    name: str
    reason: str
    _match: Callable[[list[str]], bool]
    _apply: Callable[[str, list[str]], str]
    # Documentation-only — what flags would conflict with this rewrite.
    conflict_flags: tuple[str, ...] = field(default_factory=tuple)

    def matches(self, cmd: str) -> bool:
        try:
            return self._match(_tokens(cmd))
        except Exception:
            return False

    def apply(self, cmd: str) -> str:
        try:
            return self._apply(cmd, _tokens(cmd))
        except Exception:
            return cmd


# ---------------------------------------------------------------------------
# Individual rules
# ---------------------------------------------------------------------------

def _starts_with(toks: list[str], *prefix: str) -> bool:
    if len(toks) < len(prefix):
        return False
    return tuple(toks[: len(prefix)]) == prefix


# --- git -------------------------------------------------------------------

def _match_git_status(toks: list[str]) -> bool:
    return _starts_with(toks, "git", "status") and len(toks) == 2


def _apply_git_status(cmd: str, _toks: list[str]) -> str:
    return "git status --porcelain=v2 --branch"


def _match_git_diff(toks: list[str]) -> bool:
    if not _starts_with(toks, "git", "diff"):
        return False
    rest = toks[2:]
    # Leave alone if user already specified any flag.
    return not _has_any_flag(rest)


def _apply_git_diff(cmd: str, toks: list[str]) -> str:
    rest = toks[2:]
    # Only add --no-color. Unlike every other rule, --stat would change WHAT the
    # command produces (a diffstat, not the patch): the hunks the agent asked
    # for would never exist for the PostToolUse layer to recover or sandbox
    # (#97). Output-side compaction is the GitDiffCompactor's job — it sees the
    # real patch and, in hybrid mode, keeps the full diff retrievable.
    if rest:
        tail = " ".join(shlex.quote(t) for t in rest)
        return f"git diff --no-color {tail}"
    return "git diff --no-color"


_GIT_LOG_N = re.compile(r"^(?:-n|--max-count=?)(\d+)?$")


def _match_git_log(toks: list[str]) -> bool:
    if not _starts_with(toks, "git", "log"):
        return False
    rest = toks[2:]
    # Allow either "no flags" or a single ``-n N`` / ``--max-count=N`` pair.
    if not rest:
        return True
    if len(rest) == 2 and rest[0] in ("-n", "--max-count") and rest[1].isdigit():
        return True
    if len(rest) == 1:
        # ``-nN`` or ``--max-count=N``
        m = _GIT_LOG_N.match(rest[0])
        if m and (m.group(1) or "").isdigit():
            return True
    return False


def _apply_git_log(cmd: str, toks: list[str]) -> str:
    rest = toks[2:]
    n = 20
    if len(rest) == 2 and rest[0] in ("-n", "--max-count"):
        n = int(rest[1])
    elif len(rest) == 1:
        m = _GIT_LOG_N.match(rest[0])
        if m and m.group(1):
            n = int(m.group(1))
    return f"git log --oneline --decorate -n {n}"


# --- tsc / npx tsc ---------------------------------------------------------

def _match_tsc(toks: list[str]) -> bool:
    if toks == ["tsc"]:
        return True
    return toks[:2] == ["npx", "tsc"] and len(toks) == 2


def _apply_tsc(cmd: str, _toks: list[str]) -> str:
    return f"{cmd} --pretty false"


# --- pytest ----------------------------------------------------------------

# Task runners that shell out to pytest, and any python executable spelling
# (python, python3, python3.12, or a venv/absolute path to one). Kept in sync
# with the PostToolUse compactor in compactors/pytest_.py, which accepted these
# forms from v4.3.0 while this matcher did not (#41).
_PYTEST_RUNNERS = frozenset({"uv", "poetry", "hatch", "pdm", "rye"})
_PYTHON_EXE_RE = re.compile(r"^(?:\S*/)?python[0-9.]*$")


def _match_pytest(toks: list[str]) -> bool:
    if not toks:
        return False
    rest: list[str] | None = None
    if toks[0] == "pytest":
        rest = toks[1:]
    elif (len(toks) >= 3 and toks[0] in _PYTEST_RUNNERS and toks[1] == "run" and toks[2] == "pytest") or (len(toks) >= 3 and _PYTHON_EXE_RE.match(toks[0]) and toks[1] == "-m" and toks[2] == "pytest"):
        rest = toks[3:]
    if rest is None:
        return False
    # Block if quietness/verbosity already specified
    return not _has_flag(rest, "-q", "--quiet", "-v", "-vv", "--verbose", "--tb")


def _apply_pytest(cmd: str, _toks: list[str]) -> str:
    return f"{cmd} -q --tb=line"


# --- npm test / yarn test --------------------------------------------------

def _match_npm_test(toks: list[str]) -> bool:
    if toks == ["npm", "test"]:
        return True
    if toks == ["yarn", "test"]:
        return True
    return toks == ["pnpm", "test"]


def _apply_npm_test(cmd: str, _toks: list[str]) -> str:
    return f"{cmd} --silent"


# --- cargo test ------------------------------------------------------------

def _match_cargo_test(toks: list[str]) -> bool:
    if not _starts_with(toks, "cargo", "test"):
        return False
    rest = toks[2:]
    return not _has_flag(rest, "-q", "--quiet", "-v", "--verbose")


def _apply_cargo_test(cmd: str, _toks: list[str]) -> str:
    return f"{cmd} --quiet"


# --- gh --------------------------------------------------------------------

def _match_gh_run_watch(toks: list[str]) -> bool:
    if not _starts_with(toks, "gh", "run", "watch"):
        return False
    return not _has_flag(toks, "--exit-status")


def _apply_gh_run_watch(cmd: str, _toks: list[str]) -> str:
    return f"{cmd} --exit-status"


def _match_gh_pr_list(toks: list[str]) -> bool:
    if not _starts_with(toks, "gh", "pr", "list"):
        return False
    return not _has_flag(toks, "-L", "--limit")


def _apply_gh_pr_list(cmd: str, _toks: list[str]) -> str:
    return f"{cmd} --limit 30"


# --- docker ----------------------------------------------------------------

def _match_docker_ps(toks: list[str]) -> bool:
    if not _starts_with(toks, "docker", "ps"):
        return False
    return not _has_flag(toks, "--format", "-q", "--quiet")


def _apply_docker_ps(cmd: str, _toks: list[str]) -> str:
    fmt = '"table {{.Names}}\\t{{.Image}}\\t{{.Status}}"'
    return f"{cmd} --format {fmt}"


# --- le shell utilise comme lecteur de code -------------------------------
# Mesure du 09/08/2026 (`scripts/audit_budget.py`, 400 transcrits) : les
# sorties de commande pesent 30 % des jetons rendus au modele, et `grep` puis
# `cat` sont les deux familles non couvertes les plus lourdes apres les
# scripts jetables. Un compacteur PostToolUse existe pour les deux, mais il
# ne peut rien economiser : a ce moment-la, la sortie est deja partie. Seule
# une borne posee AVANT compte, et elle doit s'ecrire dans le vocabulaire de
# la commande elle-meme -- `is_unsafe_to_rewrite` refuse tout ce qui contient
# un tube, donc « ajouter | head » n'est pas une option ici.

_GREP_CAP = 20


def _lettres_courtes(toks: list[str]) -> set[str]:
    """Lettres de tous les drapeaux courts, groupes compris.

    `grep -rl motif` porte `-l` sans jamais l'ecrire comme un jeton a lui
    seul : `_has_flag` ne le voyait pas, et la regle bornait une commande qui
    ne rend que des noms de fichiers. Attrape par son test avant d'etre
    livre.
    """
    lettres: set[str] = set()
    for t in toks:
        if t.startswith("-") and not t.startswith("--") and len(t) > 1:
            for c in t[1:]:
                if c.isalpha():
                    lettres.add(c)
                else:
                    break  # `-m20` : la valeur est collee, on arrete
    return lettres


def _match_grep(toks: list[str]) -> bool:
    if not toks or toks[0] not in {"grep", "egrep", "fgrep", "rg"}:
        return False
    # Ces drapeaux disent deja que l'appelant borne ou compte lui-meme.
    if _lettres_courtes(toks) & {"m", "c", "l", "L", "q", "o"}:
        return False
    return not _has_flag(
        toks, "--max-count", "--count", "--files-with-matches",
        "--files-without-match", "--quiet", "--only-matching",
    )


def _apply_grep(cmd: str, _toks: list[str]) -> str:
    return f"{cmd} -m {_GREP_CAP}"


_CAT_CAP = 400


def _match_cat(toks: list[str]) -> bool:
    # Un seul fichier, aucun drapeau : c'est une lecture integrale deguisee.
    # `cat a b c` concatene volontairement, on n'y touche pas.
    return len(toks) == 2 and toks[0] == "cat" and not toks[1].startswith("-")


def _apply_cat(cmd: str, toks: list[str]) -> str:
    return f"head -n {_CAT_CAP} {toks[1]}"



# ---------------------------------------------------------------------------
# Rule registry — first match wins.
# ---------------------------------------------------------------------------

RULES: list[RewriteRule] = [
    RewriteRule(
        name="git-status",
        reason="git status: porcelain v2 is denser than the human format",
        _match=_match_git_status,
        _apply=_apply_git_status,
    ),
    RewriteRule(
        name="git-diff",
        reason="git diff: cap with --stat and strip ANSI colour",
        _match=_match_git_diff,
        _apply=_apply_git_diff,
    ),
    RewriteRule(
        name="git-log",
        reason="git log: one line per commit, default -n 20",
        _match=_match_git_log,
        _apply=_apply_git_log,
    ),
    RewriteRule(
        name="tsc",
        reason="tsc: --pretty false strips ANSI + frames",
        _match=_match_tsc,
        _apply=_apply_tsc,
    ),
    RewriteRule(
        name="pytest",
        reason="pytest: -q + --tb=line shrinks failure noise",
        _match=_match_pytest,
        _apply=_apply_pytest,
    ),
    RewriteRule(
        name="npm-test",
        reason="JS test runner: --silent drops setup spam",
        _match=_match_npm_test,
        _apply=_apply_npm_test,
    ),
    RewriteRule(
        name="cargo-test",
        reason="cargo test: --quiet trims status frames",
        _match=_match_cargo_test,
        _apply=_apply_cargo_test,
    ),
    RewriteRule(
        name="gh-run-watch",
        reason="gh run watch: --exit-status enables scripting",
        _match=_match_gh_run_watch,
        _apply=_apply_gh_run_watch,
    ),
    RewriteRule(
        name="gh-pr-list",
        reason="gh pr list: cap to 30 to avoid runaway lists",
        _match=_match_gh_pr_list,
        _apply=_apply_gh_pr_list,
    ),
    RewriteRule(
        name="docker-ps",
        reason="docker ps: tabular format drops port/created columns",
        _match=_match_docker_ps,
        _apply=_apply_docker_ps,
    ),
    RewriteRule(
        name="grep-cap",
        reason=(
            f"grep: -m {_GREP_CAP} borne le nombre de correspondances par "
            f"fichier. Pour chercher dans du code indexe, search_codebase "
            f"rend le symbole englobant au lieu de la ligne nue"
        ),
        _match=_match_grep,
        _apply=_apply_grep,
        conflict_flags=("-m", "--max-count", "-c", "-l", "-L", "-q", "-o"),
    ),
    RewriteRule(
        name="cat-cap",
        reason=(
            f"cat d'un fichier entier -> head -n {_CAT_CAP}. Si vous tenez un "
            f"numero de ligne, read_lines(file_path, start, end) ; si vous "
            f"tenez un nom, get_function_source(name)"
        ),
        _match=_match_cat,
        _apply=_apply_cat,
    ),
]
