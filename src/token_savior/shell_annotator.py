"""Regex-based POSIX sh / bash annotator (best-effort).

Handles function declarations (POSIX ``name() {``, bash ``function name {``),
source-imports (``.``/``source``), and doc comments using regex plus a
brace-depth scan. Comments, quoted strings, and heredoc bodies are stripped
before brace counting so quoted/commented ``{``/``}`` never corrupt a
function's line range; declarations are matched against the original text
(minus heredoc bodies) so quoted import paths stay readable.
"""

import re

from token_savior.models import (
    FunctionInfo,
    ImportInfo,
    LineRange,
    StructuralMetadata,
    build_line_char_offsets,
)
from token_savior.utils.dependency_graph import build_dependency_graph

# ---------------------------------------------------------------------------
# Dependency graph helpers
# ---------------------------------------------------------------------------

_SHELL_KEYWORDS = frozenset({
    # Language keywords / control structures
    "if", "then", "elif", "else", "fi", "for", "while", "until", "do",
    "done", "case", "esac", "in", "function", "select", "time", "coproc",
    # Common builtins
    "echo", "printf", "local", "return", "exit", "export", "set", "shift",
    "test", "unset", "readonly", "declare", "typeset", "eval", "exec",
    "trap", "wait", "read", "cd", "pwd", "true", "false", "break",
    "continue", "source", "let", "type", "command", "builtin",
})


# ---------------------------------------------------------------------------
# Comment / quote / heredoc scanning
# ---------------------------------------------------------------------------

_HEREDOC_START_RE = re.compile(
    r"<<(-)?\s*(?:'([A-Za-z_]\w*)'|\"([A-Za-z_]\w*)\"|([A-Za-z_]\w*))"
)


def _scan_shell(lines: list[str]) -> tuple[list[str], list[bool]]:
    """Single pass building a brace-counting-safe copy of ``lines`` plus a
    per-line heredoc-body mask.

    The returned ``clean`` lines have ``#`` comments and quoted-string
    interiors blanked (so a quoted/commented ``{``/``}`` never affects brace
    counting) and heredoc body lines fully blanked. ``in_heredoc_body[i]``
    is True when line ``i`` is heredoc content, not real shell code — used
    to keep declaration detection from matching text inside a heredoc body.
    """
    clean: list[str] = []
    in_heredoc_body = [False] * len(lines)
    heredoc_word: str | None = None
    strip_tabs = False
    idx = 0
    while idx < len(lines):
        line = lines[idx]
        if heredoc_word is not None:
            candidate = line.lstrip("\t") if strip_tabs else line
            if candidate.rstrip() == heredoc_word:
                heredoc_word = None
                clean.append(line)
            else:
                in_heredoc_body[idx] = True
                clean.append("")
            idx += 1
            continue

        out_chars: list[str] = []
        i, n = 0, len(line)
        while i < n:
            ch = line[i]
            if ch == "#" and (i == 0 or line[i - 1].isspace()):
                break
            if ch == "'":
                j = line.find("'", i + 1)
                end = j + 1 if j != -1 else n
                out_chars.append(" " * (end - i))
                i = end
                continue
            if ch == '"':
                j = i + 1
                while j < n:
                    if line[j] == "\\":
                        j += 2
                        continue
                    if line[j] == '"':
                        j += 1
                        break
                    j += 1
                out_chars.append(" " * (j - i))
                i = j
                continue
            if ch == "<" and i + 1 < n and line[i + 1] == "<" and (i + 2 >= n or line[i + 2] != "<"):
                m = _HEREDOC_START_RE.match(line, i)
                if m:
                    heredoc_word = m.group(2) or m.group(3) or m.group(4)
                    strip_tabs = m.group(1) == "-"
                    out_chars.append(line[i:m.end()])
                    i = m.end()
                    continue
            out_chars.append(ch)
            i += 1
        clean.append("".join(out_chars))
        idx += 1
    return clean, in_heredoc_body


def _find_brace_end_shell(clean_lines: list[str], start_line_0: int) -> int:
    """Find the 0-based line where the function's outermost ``{`` closes.

    Operates on already-sanitized lines (see ``_scan_shell``); a balanced
    ``${var}``/compound-block pair inside just nets to zero, so it never
    disturbs the outer count. Unbalanced/truncated input falls back to EOF.
    """
    depth = 0
    found_open = False
    for idx in range(start_line_0, len(clean_lines)):
        for ch in clean_lines[idx]:
            if ch == "{":
                depth += 1
                found_open = True
            elif ch == "}":
                depth -= 1
                if found_open and depth == 0:
                    return idx
    return len(clean_lines) - 1


# ---------------------------------------------------------------------------
# Declaration detection
# ---------------------------------------------------------------------------

# POSIX form; dash-named functions (bash-only) fall outside [A-Za-z_]\w* and
# are deliberately not captured (#51).
_FUNC_POSIX_RE = re.compile(r"^([A-Za-z_]\w*)\s*\(\)\s*\{")
_FUNC_KEYWORD_RE = re.compile(r"^function\s+([A-Za-z_]\w*)\s*(?:\(\))?\s*\{")
_FUNC_SUBSHELL_RE = re.compile(r"^([A-Za-z_]\w*)\s*\(\)\s*\(")

_IMPORT_RE = re.compile(r'^(\.|source)\s+(?:"([^"]*)"|\'([^\']*)\'|(\S+))')


def _collect_doc_comment(lines: list[str], decl_line_0: int) -> str | None:
    """Collect consecutive ``#`` comment lines immediately before decl_line_0."""
    doc_lines: list[str] = []
    j = decl_line_0 - 1
    while j >= 0:
        stripped = lines[j].strip()
        if stripped.startswith("#"):
            doc_lines.insert(0, stripped[1:].strip())
            j -= 1
        else:
            break
    return "\n".join(doc_lines) if doc_lines else None


# ---------------------------------------------------------------------------
# Main annotator
# ---------------------------------------------------------------------------


def annotate_shell(source: str, source_name: str = "<source>") -> StructuralMetadata:
    """Parse a POSIX sh / bash script and extract structural metadata.

    Detects:
      - function declarations (``name() {``, ``name () {``, ``function name
        {``, ``function name() {``, one-liners, and a best-effort
        subshell-body ``name() ( ... )`` — start line only, no crash)
      - source-imports (``.`` and ``source``; quoted/variable paths captured
        verbatim; both treated as whole-file imports, ``is_from_import=False``)
      - doc comments (consecutive ``#`` lines before a declaration)

    Shell has no classes, so ``classes`` is always empty.
    """
    lines = source.split("\n")
    total_lines = len(lines)
    total_chars = len(source)
    line_offsets = build_line_char_offsets(lines)

    clean_lines, heredoc_mask = _scan_shell(lines)

    functions: list[FunctionInfo] = []
    imports: list[ImportInfo] = []

    i = 0
    while i < total_lines:
        if heredoc_mask[i]:
            i += 1
            continue

        stripped = lines[i].strip()
        if not stripped or stripped.startswith("#"):
            i += 1
            continue

        im = _IMPORT_RE.match(stripped)
        if im:
            module = im.group(2) if im.group(2) is not None else im.group(3)
            if module is None:
                module = im.group(4)
            imports.append(
                ImportInfo(
                    module=module,
                    names=[],
                    alias=None,
                    line_number=i + 1,
                    is_from_import=False,
                )
            )
            i += 1
            continue

        fm = _FUNC_POSIX_RE.match(stripped) or _FUNC_KEYWORD_RE.match(stripped)
        if fm:
            name = fm.group(1)
            docstring = _collect_doc_comment(lines, i)
            end_0 = _find_brace_end_shell(clean_lines, i)
            functions.append(
                FunctionInfo(
                    name=name,
                    qualified_name=name,
                    line_range=LineRange(start=i + 1, end=end_0 + 1),
                    parameters=[],
                    decorators=[],
                    docstring=docstring,
                    is_method=False,
                    parent_class=None,
                )
            )
            i = end_0 + 1
            continue

        sm = _FUNC_SUBSHELL_RE.match(stripped)
        if sm:
            name = sm.group(1)
            docstring = _collect_doc_comment(lines, i)
            # ponytail: subshell-body functions skip paren-matching; the
            # body range collapses to the declaration line only (#51).
            functions.append(
                FunctionInfo(
                    name=name,
                    qualified_name=name,
                    line_range=LineRange(start=i + 1, end=i + 1),
                    parameters=[],
                    decorators=[],
                    docstring=docstring,
                    is_method=False,
                    parent_class=None,
                )
            )
            i += 1
            continue

        i += 1

    defined_names = {f.name for f in functions}
    dependency_graph = build_dependency_graph(functions, [], lines, defined_names, _SHELL_KEYWORDS)

    return StructuralMetadata(
        source_name=source_name,
        total_lines=total_lines,
        total_chars=total_chars,
        lines=lines,
        line_char_offsets=line_offsets,
        functions=functions,
        classes=[],
        imports=imports,
        dependency_graph=dependency_graph,
    )
