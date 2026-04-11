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


def find_brace_end_csharp(lines: list[str], start_line_0: int) -> int:
    """Find the 0-based line where the outermost brace closes,
    skipping strings, verbatim strings, interpolated strings, char literals, and comments."""
    depth = 0
    found_open = False
    in_block_comment = False
    for idx in range(start_line_0, len(lines)):
        line = lines[idx]
        i = 0
        while i < len(line):
            ch = line[i]
            # Block comment handling
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
            # Verbatim/interpolated strings: $@"...", @$"...", @"...", $"..."
            if ch in ("@", "$") and i + 1 < len(line):
                # Check for $@" or @$" (interpolated verbatim)
                if (
                    ch == "$" and line[i + 1] == "@" and i + 2 < len(line) and line[i + 2] == '"'
                ) or (
                    ch == "@" and line[i + 1] == "$" and i + 2 < len(line) and line[i + 2] == '"'
                ):
                    # Interpolated verbatim string — "" for escaped quote
                    i += 3
                    while i < len(line):
                        if line[i] == '"':
                            if i + 1 < len(line) and line[i + 1] == '"':
                                i += 2
                                continue
                            i += 1
                            break
                        i += 1
                    else:
                        # Multi-line verbatim string
                        idx += 1
                        while idx < len(lines):
                            line = lines[idx]
                            i = 0
                            while i < len(line):
                                if line[i] == '"':
                                    if i + 1 < len(line) and line[i + 1] == '"':
                                        i += 2
                                        continue
                                    i += 1
                                    break
                                i += 1
                            else:
                                idx += 1
                                continue
                            break
                        else:
                            return len(lines) - 1
                    continue
                # Verbatim string: @"..."
                if ch == "@" and line[i + 1] == '"':
                    i += 2
                    while i < len(line):
                        if line[i] == '"':
                            if i + 1 < len(line) and line[i + 1] == '"':
                                i += 2
                                continue
                            i += 1
                            break
                        i += 1
                    else:
                        # Multi-line verbatim
                        idx += 1
                        while idx < len(lines):
                            line = lines[idx]
                            i = 0
                            while i < len(line):
                                if line[i] == '"':
                                    if i + 1 < len(line) and line[i + 1] == '"':
                                        i += 2
                                        continue
                                    i += 1
                                    break
                                i += 1
                            else:
                                idx += 1
                                continue
                            break
                        else:
                            return len(lines) - 1
                    continue
                # Interpolated string: $"..."
                if ch == "$" and line[i + 1] == '"':
                    i += 2
                    while i < len(line):
                        if line[i] == "\\":
                            i += 2
                            continue
                        if line[i] == '"':
                            i += 1
                            break
                        i += 1
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
            # Char literal
            if ch == "'" and i + 1 < len(line):
                if i + 2 < len(line) and line[i + 1] == "\\":
                    end = line.find("'", i + 2)
                    if end >= 0 and end <= i + 4:
                        i = end + 1
                        continue
                elif i + 2 < len(line) and line[i + 2] == "'":
                    i += 3
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
