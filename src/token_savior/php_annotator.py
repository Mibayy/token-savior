"""Regex-based PHP annotator (best-effort, issue #50).

Strategy: mask everything that is not real PHP statement text — HTML
outside ``<?php ... ?>`` tags, ``//``/``#``/``/* */`` comments, and
heredoc/nowdoc bodies — to whitespace (same length, newlines kept), so
declaration regexes stay simple line-anchored matches and brace/paren
depth-counts on the masked text bound multiline signatures and bodies.
String literals are kept verbatim in the masked text (needed for
``require``/``use`` targets) but are skipped during depth counting.
"""

from __future__ import annotations

import bisect
import re
from typing import Optional

from token_savior.models import (
    ClassInfo,
    FunctionInfo,
    ImportInfo,
    LineRange,
    StructuralMetadata,
    build_line_char_offsets,
)
from token_savior.utils.dependency_graph import build_dependency_graph

# ---------------------------------------------------------------------------
# Keywords / builtins for the dependency graph (PHP keywords are
# case-insensitive at the language level; kept lowercase since callers pass
# already-lowercased identifiers through this exact set via `in`).
# ---------------------------------------------------------------------------

_PHP_KEYWORDS = frozenset({
    "abstract", "and", "array", "as", "break", "callable", "case", "catch",
    "class", "clone", "const", "continue", "declare", "default", "do",
    "echo", "else", "elseif", "empty", "enddeclare", "endfor", "endforeach",
    "endif", "endswitch", "endwhile", "enum", "extends", "final", "finally",
    "fn", "for", "foreach", "function", "global", "goto", "if", "implements",
    "include", "include_once", "instanceof", "insteadof", "interface",
    "isset", "list", "match", "namespace", "new", "or", "print", "private",
    "protected", "public", "readonly", "require", "require_once", "return",
    "static", "switch", "throw", "trait", "try", "unset", "use", "var",
    "while", "xor", "yield", "self", "parent", "true", "false", "null",
    "int", "float", "string", "bool", "void", "mixed", "object", "never",
    "iterable",
})


# ---------------------------------------------------------------------------
# Masking pass: blank HTML, comments, and heredoc/nowdoc bodies to spaces
# (length- and newline-preserving) so structural regexes stay line-anchored.
# ---------------------------------------------------------------------------

_TAG_RE = re.compile(r"<\?php\b|<\?=|\?>")


def _blank(s: str) -> str:
    return "".join(c if c == "\n" else " " for c in s)


def _strip_html(source: str) -> str:
    """Blank everything outside <?php ... ?> / <?= ... ?> tags."""
    out: list[str] = []
    in_php = False
    pos = 0
    for m in _TAG_RE.finditer(source):
        chunk = source[pos : m.start()]
        out.append(chunk if in_php else _blank(chunk))
        out.append(_blank(m.group()))
        in_php = m.group() != "?>"
        pos = m.end()
    tail = source[pos:]
    out.append(tail if in_php else _blank(tail))
    return "".join(out)


_HEREDOC_START_RE = re.compile(r"<<<[ \t]*(?:'(\w+)'|\"(\w+)\"|(\w+))")


def _mask_comments_and_heredocs(text: str) -> str:
    out = list(text)
    n = len(text)
    i = 0
    state = "normal"
    terminator: Optional[re.Pattern[str]] = None
    while i < n:
        ch = text[i]
        if state == "normal":
            if ch == "/" and i + 1 < n and text[i + 1] == "/":
                j = i
                while j < n and text[j] != "\n":
                    out[j] = " "
                    j += 1
                i = j
            elif ch == "#" and not (i + 1 < n and text[i + 1] == "["):
                j = i
                while j < n and text[j] != "\n":
                    out[j] = " "
                    j += 1
                i = j
            elif ch == "/" and i + 1 < n and text[i + 1] == "*":
                out[i] = out[i + 1] = " "
                i += 2
                state = "block_comment"
            elif ch == "'":
                i += 1
                state = "str_single"
            elif ch == '"':
                i += 1
                state = "str_double"
            elif text[i : i + 3] == "<<<":
                m = _HEREDOC_START_RE.match(text, i)
                if m:
                    term = next(g for g in m.groups() if g)
                    for k in range(i, m.end()):
                        if text[k] != "\n":
                            out[k] = " "
                    i = m.end()
                    terminator = re.compile(rf"^[ \t]*{re.escape(term)}\b")
                    state = "heredoc"
                else:
                    i += 1
            else:
                i += 1
        elif state == "block_comment":
            if ch == "*" and i + 1 < n and text[i + 1] == "/":
                out[i] = out[i + 1] = " "
                i += 2
                state = "normal"
            else:
                if ch != "\n":
                    out[i] = " "
                i += 1
        elif state in ("str_single", "str_double"):
            if ch == "\\" and i + 1 < n:
                i += 2
            elif (state == "str_single" and ch == "'") or (state == "str_double" and ch == '"'):
                i += 1
                state = "normal"
            else:
                i += 1
        else:  # state == "heredoc"
            if ch == "\n":
                i += 1
                continue
            line_end = text.find("\n", i)
            if line_end == -1:
                line_end = n
            assert terminator is not None
            if terminator.match(text[i:line_end]):
                state = "normal"
                terminator = None
            for k in range(i, line_end):
                out[k] = " "
            i = line_end
    return "".join(out)


# ---------------------------------------------------------------------------
# Character-level helpers operating on absolute offsets into the masked text.
# ---------------------------------------------------------------------------


def _skip_string(text: str, i: int) -> int:
    """Advance past a '...'/"..." literal starting at text[i] (the quote)."""
    quote = text[i]
    i += 1
    n = len(text)
    while i < n:
        if text[i] == "\\" and i + 1 < n:
            i += 2
            continue
        if text[i] == quote:
            return i + 1
        i += 1
    return n


def _find_matching(text: str, open_i: int, open_ch: str, close_ch: str) -> int:
    """Index just past the char matching text[open_i] (== open_ch), skipping
    string literals. Returns len(text) if never closed (best-effort EOF)."""
    depth = 0
    i = open_i
    n = len(text)
    while i < n:
        ch = text[i]
        if ch in ("'", '"'):
            i = _skip_string(text, i)
            continue
        if ch == open_ch:
            depth += 1
        elif ch == close_ch:
            depth -= 1
            if depth == 0:
                return i + 1
        i += 1
    return n


def _find_open_brace(text: str, from_idx: int) -> Optional[int]:
    i = from_idx
    n = len(text)
    while i < n:
        ch = text[i]
        if ch in ("'", '"'):
            i = _skip_string(text, i)
            continue
        if ch == "{":
            return i
        i += 1
    return None


def _find_body_start(text: str, from_idx: int) -> tuple[Optional[int], str]:
    """Scan forward from a closed param list for '{' (body) or ';' (no
    body — abstract/interface signature)."""
    i = from_idx
    n = len(text)
    while i < n:
        ch = text[i]
        if ch in ("'", '"'):
            i = _skip_string(text, i)
            continue
        if ch == "{":
            return i, "{"
        if ch == ";":
            return i, ";"
        i += 1
    return None, ""


def _line_at(offset: int, line_offsets: list[int]) -> int:
    idx = bisect.bisect_right(line_offsets, offset) - 1
    return max(idx, 0) + 1


def _brace_end_line(text: str, open_idx: int, line_offsets: list[int], total_lines: int) -> int:
    """Line of the brace matching text[open_idx] == '{', or the file's last
    line when never closed — mirrors find_brace_end_go's EOF fallback."""
    end_idx = _find_matching(text, open_idx, "{", "}")
    if end_idx >= len(text):
        return total_lines
    return _line_at(end_idx - 1, line_offsets)


# ---------------------------------------------------------------------------
# Declaration detection
# ---------------------------------------------------------------------------

# A named function only — the mandatory name excludes closures
# (`function () {}`) and arrow functions use the `fn` keyword, not `function`.
_FUNC_RE = re.compile(
    r"^[ \t]*(?P<mods>(?:(?:public|private|protected|static|abstract|final)\s+)*)"
    r"function\s+&?\s*(?P<name>[A-Za-z_]\w*)\s*\(",
    re.IGNORECASE | re.MULTILINE,
)

# Anonymous classes (`new class {`) never match: "class" must be the first
# token after optional abstract/final modifiers at the line start.
_CLASS_RE = re.compile(
    r"^[ \t]*(?P<mods>(?:(?:abstract|final)\s+)*)"
    r"(?:class|interface|trait|enum)\s+(?P<name>[A-Za-z_]\w*)"
    r"(?:\s*:\s*[A-Za-z_]\w*)?"
    r"(?P<rest>[^{;\n]*)",
    re.IGNORECASE | re.MULTILINE,
)

_PARAM_NAME_RE = re.compile(r"\$(\w+)")
_RETURN_TYPE_RE = re.compile(r":\s*([^{;]+)")


def _split_top_level(text: str) -> list[str]:
    """Split on top-level commas, skipping strings and nested ([{}])."""
    parts: list[str] = []
    depth = 0
    buf: list[str] = []
    i = 0
    n = len(text)
    while i < n:
        ch = text[i]
        if ch in ("'", '"'):
            j = _skip_string(text, i)
            buf.append(text[i:j])
            i = j
            continue
        if ch in "([{":
            depth += 1
        elif ch in ")]}":
            depth -= 1
        if ch == "," and depth == 0:
            parts.append("".join(buf))
            buf = []
            i += 1
            continue
        buf.append(ch)
        i += 1
    if buf or parts:
        parts.append("".join(buf))
    return parts


def _extract_params(raw: str) -> list[str]:
    params: list[str] = []
    for chunk in _split_top_level(raw):
        m = _PARAM_NAME_RE.search(chunk)
        if m:
            params.append("$" + m.group(1))
    return params


def _extract_bases(rest: str) -> list[str]:
    """Names from the extends/implements clause, combined into one list —
    the model has no separate interfaces field."""
    rest_lower = rest.lower()
    names: list[str] = []
    for kw in ("extends", "implements"):
        idx = rest_lower.find(kw)
        if idx == -1:
            continue
        others = [
            p
            for p in (rest_lower.find(o, idx + len(kw)) for o in ("extends", "implements") if o != kw)
            if p != -1
        ]
        end = min(others) if others else len(rest)
        names.extend(n.strip() for n in rest[idx + len(kw) : end].split(",") if n.strip())
    return names


_DOC_LINE_STRIP_RE = re.compile(r"^\s*\*+\s?")


def _collect_doc_comment(raw_lines: list[str], decl_line_0: int) -> Optional[str]:
    """First non-empty content line of a /** ... */ docblock ending on the
    line immediately above decl_line_0 (0-indexed)."""
    end = decl_line_0 - 1
    if end < 0 or "*/" not in raw_lines[end]:
        return None
    start = end
    while start >= 0 and "/**" not in raw_lines[start] and "/*" not in raw_lines[start]:
        start -= 1
    if start < 0:
        return None
    for idx in range(start, end + 1):
        text = raw_lines[idx].replace("/**", "").replace("/*", "").replace("*/", "")
        text = _DOC_LINE_STRIP_RE.sub("", text).strip()
        if text:
            return text
    return None


# ---------------------------------------------------------------------------
# Import / module detection (per-line, on the masked text — comments and
# heredocs never leak in as a false namespace/use/require statement).
# ---------------------------------------------------------------------------

_NAMESPACE_RE = re.compile(r"[ \t]*namespace\s+([A-Za-z_][\w\\]*)\s*;", re.IGNORECASE)
_USE_GROUP_RE = re.compile(r"[ \t]*use\s+([A-Za-z_][\w\\]*?)\\\{([^}]*)\}\s*;", re.IGNORECASE)
_USE_AS_RE = re.compile(r"[ \t]*use\s+([A-Za-z_][\w\\]*)\s+as\s+([A-Za-z_]\w*)\s*;", re.IGNORECASE)
_USE_RE = re.compile(r"[ \t]*use\s+([A-Za-z_][\w\\]*)\s*;", re.IGNORECASE)
_REQUIRE_RE = re.compile(
    r"[ \t]*(?:require_once|require|include_once|include)\s*\(?\s*['\"]([^'\"]+)['\"]",
    re.IGNORECASE,
)


def _extract_module_and_imports(clean_text: str) -> tuple[Optional[str], list[ImportInfo]]:
    module_name: Optional[str] = None
    imports: list[ImportInfo] = []
    for i, line in enumerate(clean_text.split("\n"), start=1):
        m = _NAMESPACE_RE.match(line)
        if m:
            module_name = m.group(1)
            continue
        m = _USE_GROUP_RE.match(line)
        if m:
            base = m.group(1)
            for name in m.group(2).split(","):
                name = name.strip()
                if name:
                    imports.append(ImportInfo(f"{base}\\{name}", [], None, i, False))
            continue
        m = _USE_AS_RE.match(line)
        if m:
            imports.append(ImportInfo(m.group(1), [], m.group(2), i, False))
            continue
        m = _USE_RE.match(line)
        if m:
            imports.append(ImportInfo(m.group(1), [], None, i, False))
            continue
        m = _REQUIRE_RE.match(line)
        if m:
            imports.append(ImportInfo(m.group(1), [], None, i, False))
    return module_name, imports


# ---------------------------------------------------------------------------
# Main annotator
# ---------------------------------------------------------------------------


def annotate_php(source: str, source_name: str = "<source>") -> StructuralMetadata:
    """Parse PHP source and extract structural metadata using regex.

    Detects: functions/methods (incl. multiline signatures, Allman/K&R
    braces, abstract/interface no-body signatures), classes/interfaces/
    traits/enums, namespace/use/require imports, and /** */ doc summaries.
    """
    lines = source.split("\n")
    total_lines = len(lines)
    total_chars = len(source)
    line_offsets = build_line_char_offsets(lines)

    php_only = _strip_html(source)
    doc_lines = php_only.split("\n")  # comments intact — needed for docblocks
    clean_text = _mask_comments_and_heredocs(php_only)

    module_name, imports = _extract_module_and_imports(clean_text)

    classes: list[ClassInfo] = []
    class_ranges: list[tuple[int, int, str]] = []
    for m in _CLASS_RE.finditer(clean_text):
        name = m.group("name")
        start_line = _line_at(m.start(), line_offsets)
        docstring = _collect_doc_comment(doc_lines, start_line - 1)
        base_classes = _extract_bases(m.group("rest") or "")
        brace_idx = _find_open_brace(clean_text, m.end())
        if brace_idx is None:
            classes.append(ClassInfo(name, LineRange(start_line, start_line), base_classes, [], [], docstring))
            class_ranges.append((m.start(), m.start(), name))
            continue
        end_idx = _find_matching(clean_text, brace_idx, "{", "}")
        last_char = end_idx - 1 if end_idx < len(clean_text) else len(clean_text) - 1
        end_line = total_lines if end_idx >= len(clean_text) else _line_at(last_char, line_offsets)
        classes.append(ClassInfo(name, LineRange(start_line, end_line), base_classes, [], [], docstring))
        class_ranges.append((m.start(), last_char, name))

    functions: list[FunctionInfo] = []
    for m in _FUNC_RE.finditer(clean_text):
        name = m.group("name")
        start_line = _line_at(m.start(), line_offsets)
        paren_open = m.end() - 1
        paren_close = _find_matching(clean_text, paren_open, "(", ")")
        raw_params = clean_text[paren_open + 1 : max(paren_close - 1, paren_open + 1)]
        params = _extract_params(raw_params)
        body_start, delim = _find_body_start(clean_text, paren_close)

        if body_start is None:
            end_line = total_lines
            return_type = None
        else:
            return_type = _extract_return_type(clean_text[paren_close:body_start])
            if delim == ";":
                end_line = _line_at(body_start, line_offsets)
            else:
                end_line = _brace_end_line(clean_text, body_start, line_offsets, total_lines)

        mods = (m.group("mods") or "").lower()
        visibility = next((v for v in ("public", "private", "protected") if v in mods), None)

        parent: Optional[str] = None
        best_span: Optional[int] = None
        for cs, ce, cname in class_ranges:
            if cs <= m.start() <= ce and (best_span is None or (ce - cs) < best_span):
                best_span = ce - cs
                parent = cname

        docstring = _collect_doc_comment(doc_lines, start_line - 1)
        functions.append(
            FunctionInfo(
                name=name,
                qualified_name=f"{parent}.{name}" if parent else name,
                line_range=LineRange(start_line, end_line),
                parameters=params,
                decorators=[],
                docstring=docstring,
                is_method=parent is not None,
                parent_class=parent,
                visibility=visibility,
                return_type=return_type,
            )
        )

    methods_by_class: dict[str, list[FunctionInfo]] = {}
    for f in functions:
        if f.parent_class:
            methods_by_class.setdefault(f.parent_class, []).append(f)
    classes = [
        ClassInfo(
            name=c.name,
            line_range=c.line_range,
            base_classes=c.base_classes,
            methods=methods_by_class.get(c.name, []),
            decorators=c.decorators,
            docstring=c.docstring,
        )
        for c in classes
    ]

    defined_names = {f.name for f in functions} | {c.name for c in classes}
    dependency_graph = build_dependency_graph(functions, classes, lines, defined_names, _PHP_KEYWORDS)

    return StructuralMetadata(
        source_name=source_name,
        total_lines=total_lines,
        total_chars=total_chars,
        lines=lines,
        line_char_offsets=line_offsets,
        functions=functions,
        classes=classes,
        imports=imports,
        dependency_graph=dependency_graph,
        module_name=module_name,
    )


def _extract_return_type(segment: str) -> Optional[str]:
    m = _RETURN_TYPE_RE.search(segment)
    return m.group(1).strip() or None if m else None
