"""Shared brace-matching helpers for C-family annotators.

Each function finds the 0-based line where the outermost ``{`` closes,
correctly skipping language-specific strings, char literals, and comments.
The implementations are intentionally separate because the lexical rules
differ significantly between languages (nested comments in Rust, verbatim
strings in C#, backtick raw strings in Go, etc.).
"""

from __future__ import annotations


def find_brace_end_c(lines: list[str], start_line_0: int) -> int:
    """Find the 0-based line where the outermost brace closes,
    skipping strings, char literals, and comments."""
    depth = 0
    found_open = False
    in_block_comment = False
    for idx in range(start_line_0, len(lines)):
        line = lines[idx]
        i = 0
        while i < len(line):
            ch = line[i]
            # Block comment handling (C does NOT nest /* */)
            if in_block_comment:
                if ch == "*" and i + 1 < len(line) and line[i + 1] == "/":
                    in_block_comment = False
                    i += 2
                    continue
                i += 1
                continue
            # Line comment
            if ch == "/" and i + 1 < len(line):
                if line[i + 1] == "/":
                    break  # rest is line comment
                if line[i + 1] == "*":
                    in_block_comment = True
                    i += 2
                    continue
            # String literal
            if ch == '"':
                i += 1
                while i < len(line):
                    if line[i] == "\\":
                        i += 2
                        continue
                    if line[i] == '"':
                        i += 1
                        break
                    i += 1
                continue
            # Char literal
            if ch == "'":
                i += 1
                if i < len(line) and line[i] == "\\":
                    i += 2  # skip escaped char
                elif i < len(line):
                    i += 1  # skip char
                if i < len(line) and line[i] == "'":
                    i += 1
                continue
            if ch == "{":
                depth += 1
                found_open = True
            elif ch == "}":
                depth -= 1
                if found_open and depth == 0:
                    return idx
            i += 1
    return len(lines) - 1


def _csharp_skip_block_comment(lines: list[str], idx: int, i: int) -> tuple[int, int]:
    """Scan until the next ``*/`` and return the position just after it.

    Returns ``(len(lines), 0)`` as an EOF sentinel if the comment never closes.
    """
    while idx < len(lines):
        line = lines[idx]
        while i < len(line):
            if line[i] == "*" and i + 1 < len(line) and line[i + 1] == "/":
                return idx, i + 2
            i += 1
        idx += 1
        i = 0
    return len(lines), 0


def _csharp_skip_regular_string(line: str, i: int) -> int:
    """Consume a regular ``"..."`` string starting at ``line[i] == '"'``.

    Honors ``\\`` escapes. Returns position after the closing quote, or
    ``len(line)`` if the string is not closed on this line.
    """
    i += 1
    while i < len(line):
        if line[i] == "\\":
            i += 2
            continue
        if line[i] == '"':
            return i + 1
        i += 1
    return len(line)


def _csharp_skip_interpolated_string(line: str, i: int) -> int:
    """Consume ``$"..."`` (non-verbatim interpolated) starting at the ``$``.

    Honors ``\\`` escapes. Single-line only. Returns position after the
    closing quote, or ``len(line)`` if unclosed.
    """
    i += 2  # past '$' and '"'
    while i < len(line):
        if line[i] == "\\":
            i += 2
            continue
        if line[i] == '"':
            return i + 1
        i += 1
    return len(line)


def _csharp_skip_verbatim_string(
    lines: list[str], idx: int, i: int
) -> tuple[int, int]:
    """Consume a verbatim ``@"..."`` body starting past the opening ``"``.

    Supports ``""`` as an escaped quote and multi-line bodies. Returns
    ``(idx, i)`` after the closing quote, or ``(len(lines), 0)`` as an
    EOF sentinel if unclosed.
    """
    while idx < len(lines):
        line = lines[idx]
        while i < len(line):
            if line[i] == '"':
                if i + 1 < len(line) and line[i + 1] == '"':
                    i += 2
                    continue
                return idx, i + 1
            i += 1
        idx += 1
        i = 0
    return len(lines), 0


def _csharp_try_skip_prefixed_string(
    lines: list[str], idx: int, line: str, i: int
) -> tuple[int, int] | None:
    """Handle strings starting with ``@`` or ``$`` at ``line[i]``.

    Covers ``$@"..."``, ``@$"..."``, ``@"..."``, ``$"..."``. Returns
    ``(new_idx, new_i)`` after the closing quote, or ``None`` if the
    prefix is not the start of a C# string literal.
    """
    if i + 1 >= len(line):
        return None
    ch, nxt = line[i], line[i + 1]
    if (ch, nxt) in (("$", "@"), ("@", "$")):
        if i + 2 < len(line) and line[i + 2] == '"':
            return _csharp_skip_verbatim_string(lines, idx, i + 3)
        return None
    if ch == "@" and nxt == '"':
        return _csharp_skip_verbatim_string(lines, idx, i + 2)
    if ch == "$" and nxt == '"':
        return idx, _csharp_skip_interpolated_string(line, i)
    return None


def _csharp_try_skip_char_literal(line: str, i: int) -> int | None:
    """If ``line[i:]`` looks like a C# char literal, return the position
    after its closing ``'``. Otherwise return ``None``.
    """
    if i + 2 >= len(line):
        return None
    if line[i + 1] == "\\":
        end = line.find("'", i + 2)
        if 0 <= end - i <= 4:
            return end + 1
        return None
    if line[i + 2] == "'":
        return i + 3
    return None


def find_brace_end_csharp(lines: list[str], start_line_0: int) -> int:
    """Find the 0-based line where the outermost brace closes,
    skipping strings, verbatim strings, interpolated strings, char literals, and comments."""
    depth = 0
    found_open = False
    in_block_comment = False
    idx = start_line_0
    i = 0
    while idx < len(lines):
        line = lines[idx]
        if i >= len(line):
            idx += 1
            i = 0
            continue
        if in_block_comment:
            new_idx, new_i = _csharp_skip_block_comment(lines, idx, i)
            if new_idx >= len(lines):
                return len(lines) - 1
            idx, i, in_block_comment = new_idx, new_i, False
            continue
        ch = line[i]
        if ch == "/" and i + 1 < len(line):
            if line[i + 1] == "/":
                i = len(line)
                continue
            if line[i + 1] == "*":
                in_block_comment = True
                i += 2
                continue
        if ch in ("@", "$"):
            skipped = _csharp_try_skip_prefixed_string(lines, idx, line, i)
            if skipped is not None:
                idx, i = skipped
                if idx >= len(lines):
                    return len(lines) - 1
                continue
        if ch == '"':
            i = _csharp_skip_regular_string(line, i)
            continue
        if ch == "'":
            new_i = _csharp_try_skip_char_literal(line, i)
            if new_i is not None:
                i = new_i
                continue
        if ch == "{":
            depth += 1
            found_open = True
        elif ch == "}":
            depth -= 1
            if found_open and depth == 0:
                return idx
        i += 1
    return len(lines) - 1


def find_brace_end_rust(lines: list[str], start_line_0: int) -> int:
    """Find the 0-based line where the outermost brace closes,
    skipping strings, raw strings, char literals, and comments."""
    depth = 0
    found_open = False
    in_block_comment = 0  # nesting depth for /* */
    for idx in range(start_line_0, len(lines)):
        line = lines[idx]
        i = 0
        while i < len(line):
            ch = line[i]
            # Block comment handling (Rust supports nested /* */)
            if in_block_comment > 0:
                if ch == "/" and i + 1 < len(line) and line[i + 1] == "*":
                    in_block_comment += 1
                    i += 2
                    continue
                if ch == "*" and i + 1 < len(line) and line[i + 1] == "/":
                    in_block_comment -= 1
                    i += 2
                    continue
                i += 1
                continue
            # Line comment
            if ch == "/" and i + 1 < len(line):
                if line[i + 1] == "/":
                    break  # rest is line comment
                if line[i + 1] == "*":
                    in_block_comment += 1
                    i += 2
                    continue
            # Raw string: r#"..."#, r##"..."##, etc.
            if ch == "r" and i + 1 < len(line) and line[i + 1] in ('"', "#"):
                hash_count = 0
                j = i + 1
                while j < len(line) and line[j] == "#":
                    hash_count += 1
                    j += 1
                if j < len(line) and line[j] == '"':
                    j += 1
                    # Find closing "###
                    closing = '"' + "#" * hash_count
                    while True:
                        pos = line.find(closing, j)
                        if pos >= 0:
                            i = pos + len(closing)
                            break
                        # Span to next line
                        idx += 1
                        if idx >= len(lines):
                            return len(lines) - 1
                        line = lines[idx]
                        j = 0
                    continue
            # Regular string
            if ch == '"':
                i += 1
                while i < len(line):
                    if line[i] == "\\":
                        i += 2
                        continue
                    if line[i] == '"':
                        i += 1
                        break
                    i += 1
                continue
            # Char literal (skip 'a', '\n', etc. but not lifetime 'a)
            if ch == "'" and i + 1 < len(line):
                # Lifetime check: 'a where next is alpha and followed by non-'
                # Char literal: 'x' or '\n'
                if i + 2 < len(line) and line[i + 1] == "\\":
                    # Escaped char literal like '\n'
                    end = line.find("'", i + 2)
                    if end >= 0 and end <= i + 4:
                        i = end + 1
                        continue
                elif i + 2 < len(line) and line[i + 2] == "'":
                    # Simple char literal like 'a'
                    i += 3
                    continue
                # Otherwise it's a lifetime, skip
            if ch == "{":
                depth += 1
                found_open = True
            elif ch == "}":
                depth -= 1
                if found_open and depth == 0:
                    return idx
            i += 1
    return len(lines) - 1


def find_brace_end_go(lines: list[str], start_line_0: int) -> int:
    """Find the 0-based line where the outermost brace closes, skipping strings/comments."""
    depth = 0
    found_open = False
    in_block_comment = False
    for idx in range(start_line_0, len(lines)):
        line = lines[idx]
        i = 0
        while i < len(line):
            ch = line[i]
            if in_block_comment:
                if ch == "*" and i + 1 < len(line) and line[i + 1] == "/":
                    in_block_comment = False
                    i += 2
                    continue
                i += 1
                continue
            if ch == "/" and i + 1 < len(line):
                if line[i + 1] == "/":
                    break  # rest is line comment
                if line[i + 1] == "*":
                    in_block_comment = True
                    i += 2
                    continue
            if ch == '"':
                i += 1
                while i < len(line) and line[i] != '"':
                    if line[i] == "\\":
                        i += 1
                    i += 1
                i += 1
                continue
            if ch == "`":
                # raw string can span lines - scan to end
                i += 1
                while True:
                    while i < len(line):
                        if line[i] == "`":
                            i += 1
                            break
                        i += 1
                    else:
                        # continue to next line
                        idx += 1
                        if idx >= len(lines):
                            return len(lines) - 1
                        line = lines[idx]
                        i = 0
                        continue
                    break
                continue
            if ch == "{":
                depth += 1
                found_open = True
            elif ch == "}":
                depth -= 1
                if found_open and depth == 0:
                    return idx
            i += 1
    return len(lines) - 1
