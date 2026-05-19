"""Compound command splitter for compactor dispatcher (F3c).

Real-world shell commands often chain multiple operations together:

    cd /root/project && git status
    cd X ; echo "===" ; git log --oneline
    PATH=/usr/local/bin command --flag

When the compactor dispatcher sees the *full* command string, none of the
per-tool matchers fire (they expect e.g. a command that *starts with* ``git``).
The output stays unmatched and bloats the transcript.

This module exposes a single pure function::

    pick_meaningful_segment(command) -> str | None

Returning the segment a compactor should be tried against, or ``None`` when
no useful splitting is possible. The function is intentionally conservative:
when in doubt, return ``None`` and let the caller pass through.

Rules
-----
Splittable separators: ``&&``, ``||``, ``;`` (outside quotes/subshells).

Bail entirely (return None) on:
- Pipes ``|`` — the output flows through filters that mutate it.
- Subshells ``$(...)`` or backticks — output may originate inside them.
- Heredocs ``<<EOF`` — multi-line, parser would need state.
- Loops / conditionals / functions (``for``, ``while``, ``if``, ``case``,
  ``function``, ``do``, ``done``, ``then``, ``fi``).

Trivial segments (dropped from consideration):
- Pure navigation: ``cd``, ``pushd``, ``popd``.
- Pure banners: ``echo ...``, ``printf ...``, ``:`` (no-op).
- Comments (segment whose first non-whitespace char is ``#``).
- Pure env-var assignments with no command (``FOO=bar`` alone).

Env-var prefixes on a real command (``PATH=/usr/local/bin git status``) are
kept attached so the downstream matcher still sees the command tokens.

The returned segment is stripped. ``None`` is returned when:
- The command does not contain any of the splittable separators.
- After splitting, only one meaningful segment remains and it equals the
  original input (the caller already tried it).
- After splitting, zero meaningful segments remain.
"""
from __future__ import annotations

# Tokens whose presence anywhere unquoted means "give up" — the output we
# captured cannot be reliably attributed to a single right-hand segment.
_BAILOUT_TOKENS = (
    "|",   # pipe
    "<<",  # heredoc
    "<(",  # process substitution
    ">(",
)

# Shell keywords that signal compound structures we don't try to split.
_BAILOUT_KEYWORDS = frozenset(
    {
        "for",
        "while",
        "until",
        "if",
        "then",
        "else",
        "elif",
        "fi",
        "case",
        "esac",
        "do",
        "done",
        "function",
        "select",
    }
)

# Commands that don't produce meaningful output worth compacting.
_TRIVIAL_COMMANDS = frozenset(
    {
        "cd",
        "pushd",
        "popd",
        "echo",
        "printf",
        ":",
        "true",
        "false",
        "export",
        "unset",
        "alias",
        "unalias",
        "set",
    }
)


def _scan_for_bailouts(command: str) -> bool:
    """Return True if ``command`` contains unquoted bailout constructs."""
    i = 0
    n = len(command)
    while i < n:
        ch = command[i]
        # Skip single-quoted strings entirely (no escapes inside).
        if ch == "'":
            j = command.find("'", i + 1)
            if j == -1:
                return True  # unterminated — bail
            i = j + 1
            continue
        # Double-quoted strings — honor backslash escapes.
        if ch == '"':
            i += 1
            while i < n and command[i] != '"':
                if command[i] == "\\" and i + 1 < n:
                    i += 2
                else:
                    i += 1
            i += 1
            continue
        # Escape outside any quotes.
        if ch == "\\" and i + 1 < n:
            i += 2
            continue
        # Subshell / command substitution.
        if ch == "$" and i + 1 < n and command[i + 1] == "(":
            return True
        if ch == "`":
            return True
        # Logical OR — skip the pair (it's a valid separator, not a pipe).
        if ch == "|" and i + 1 < n and command[i + 1] == "|":
            i += 2
            continue
        # Pipe.
        if ch == "|":
            return True
        # Heredoc / process substitution.
        if ch == "<" and i + 1 < n and command[i + 1] in ("<", "("):
            return True
        if ch == ">" and i + 1 < n and command[i + 1] == "(":
            return True
        i += 1
    return False


def _split_on_separators(command: str) -> list[str]:
    """Split ``command`` on top-level ``&&``, ``||``, ``;`` separators.

    Quoting is honored. Bailout constructs are NOT checked here — caller
    is expected to have run ``_scan_for_bailouts`` first.
    """
    segments: list[str] = []
    buf: list[str] = []
    i = 0
    n = len(command)
    while i < n:
        ch = command[i]
        if ch == "'":
            j = command.find("'", i + 1)
            if j == -1:
                buf.append(command[i:])
                i = n
                break
            buf.append(command[i : j + 1])
            i = j + 1
            continue
        if ch == '"':
            start = i
            i += 1
            while i < n and command[i] != '"':
                if command[i] == "\\" and i + 1 < n:
                    i += 2
                else:
                    i += 1
            i += 1
            buf.append(command[start:i])
            continue
        if ch == "\\" and i + 1 < n:
            buf.append(command[i : i + 2])
            i += 2
            continue
        # Separators.
        if ch == "&" and i + 1 < n and command[i + 1] == "&":
            segments.append("".join(buf))
            buf = []
            i += 2
            continue
        if ch == "|" and i + 1 < n and command[i + 1] == "|":
            segments.append("".join(buf))
            buf = []
            i += 2
            continue
        if ch == ";":
            segments.append("".join(buf))
            buf = []
            i += 1
            continue
        buf.append(ch)
        i += 1
    segments.append("".join(buf))
    return segments


def _strip_env_prefix(segment: str) -> str:
    """Strip leading ``FOO=bar BAZ=qux`` env assignments from a segment.

    Returns the remainder (may be empty if the segment was env-only).
    """
    s = segment.lstrip()
    while s:
        # Match identifier=...
        idx = 0
        while idx < len(s) and (s[idx].isalnum() or s[idx] == "_"):
            idx += 1
        if idx == 0 or idx >= len(s) or s[idx] != "=":
            break
        if not (s[0].isalpha() or s[0] == "_"):
            break
        # Walk past the value (until whitespace, honoring quotes).
        j = idx + 1
        while j < len(s) and not s[j].isspace():
            if s[j] == "'":
                k = s.find("'", j + 1)
                if k == -1:
                    j = len(s)
                    break
                j = k + 1
            elif s[j] == '"':
                j += 1
                while j < len(s) and s[j] != '"':
                    if s[j] == "\\" and j + 1 < len(s):
                        j += 2
                    else:
                        j += 1
                j += 1
            else:
                j += 1
        s = s[j:].lstrip()
    return s


def _first_token(s: str) -> str:
    s = s.strip()
    if not s:
        return ""
    # First whitespace-delimited token, stripped of any surrounding quotes.
    end = 0
    while end < len(s) and not s[end].isspace():
        end += 1
    tok = s[:end]
    if len(tok) >= 2 and tok[0] == tok[-1] and tok[0] in ("'", '"'):
        tok = tok[1:-1]
    return tok


def _is_meaningful(segment: str) -> bool:
    """Return True if the segment is worth running a compactor on."""
    raw = segment.strip()
    if not raw:
        return False
    if raw.startswith("#"):
        return False
    # Strip env-var prefixes; if nothing remains it's just an assignment.
    after_env = _strip_env_prefix(raw)
    if not after_env:
        return False
    tok = _first_token(after_env)
    if not tok:
        return False
    if tok in _BAILOUT_KEYWORDS:
        # Compound structure — treat as bailout signal at the segment
        # level; the caller will see _scan_for_bailouts already handled
        # most cases, but a stray ``do`` etc. should still be skipped.
        return False
    if tok in _TRIVIAL_COMMANDS:
        return False
    return True


def _meaningful_form(segment: str) -> str:
    """Return the segment trimmed (env-var prefix retained).

    Most compactors match on the first token; keeping ``PATH=foo git status``
    intact lets a ``git status`` matcher still hit because ``startswith('git ')``
    won't, but ``startswith('git')`` after env-strip will — so we hand back
    the env-stripped form for matching purposes.
    """
    raw = segment.strip()
    return _strip_env_prefix(raw) or raw


def pick_meaningful_segment(command: str) -> str | None:
    """Pick the LAST meaningful segment of a compound command.

    Returns ``None`` when no compaction makes sense (single segment, pipes,
    subshells, heredocs, loops, only-trivial segments, etc.).
    """
    if not command or not command.strip():
        return None

    # Whole-line bailouts.
    if _scan_for_bailouts(command):
        return None

    # Check for compound keywords anywhere as whole tokens.
    # We tokenize crudely on whitespace, which is good enough — anything
    # quoted is intentional and not a real keyword.
    bare = command
    for kw in _BAILOUT_KEYWORDS:
        # Quick substring check; refine with whitespace boundaries.
        idx = 0
        while True:
            pos = bare.find(kw, idx)
            if pos == -1:
                break
            left_ok = pos == 0 or bare[pos - 1].isspace() or bare[pos - 1] == ";"
            right = pos + len(kw)
            right_ok = right == len(bare) or bare[right].isspace() or bare[right] == ";"
            if left_ok and right_ok:
                return None
            idx = pos + 1

    segments = _split_on_separators(command)
    if len(segments) <= 1:
        return None

    # Walk from the right, take the first meaningful segment.
    for seg in reversed(segments):
        if _is_meaningful(seg):
            picked = _meaningful_form(seg)
            if not picked or picked == command.strip():
                return None
            return picked
    return None


__all__ = ["pick_meaningful_segment"]
