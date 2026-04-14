"""Characterization tests for brace_matcher.find_brace_end_csharp.

These tests freeze the CURRENT behavior of ``find_brace_end_csharp`` before
refactoring. They are not aspirational — each expected value was captured by
running the pre-refactor implementation. Any change in output is a regression.

If you intentionally change behavior, update the expected values *and* document
the reason in the commit.
"""

from __future__ import annotations

import pytest

from token_savior.brace_matcher import (
    find_brace_end_csharp,
    find_brace_end_go,
    find_brace_end_rust,
)


# (name, lines, start_line_0, expected_end_line)
CHARACTERIZATION_CASES: list[tuple[str, list[str], int, int]] = [
    # --- trivial / baseline ---
    ("single_line_empty", ["{}"], 0, 0),
    ("single_line_simple", ["{ x = 1; }"], 0, 0),
    ("multi_line_simple", ["{", "  x = 1;", "}"], 0, 2),
    # --- nesting ---
    ("nested_inline", ["{ { x; } }"], 0, 0),
    ("nested_multiline", ["{", "  { y; }", "}"], 0, 2),
    ("deeply_nested", ["{", "{", "{", "}", "}", "}"], 0, 5),
    # --- regular strings ---
    ("string_with_open_brace", ["{", '  var s = "{";', "}"], 0, 2),
    ("string_with_close_brace", ["{", '  var s = "}";', "}"], 0, 2),
    ("escaped_quote_in_string", ["{", '  var s = "a\\"b";', "}"], 0, 2),
    ("empty_string_then_close", ['{ var s = ""; }'], 0, 0),
    # --- verbatim strings ---
    ("verbatim_string_braces", ["{", '  var s = @"{}";', "}"], 0, 2),
    ("verbatim_escaped_quote", ["{", '  var s = @"a""b";', "}"], 0, 2),
    # Multi-line verbatim string containing "}". The pre-refactor impl used
    # `for idx in range(...)` whose loop variable was rebound each iteration,
    # so advancing `idx` from inside the multi-line verbatim handler was lost
    # and the outer loop revisited lines already consumed by the string body.
    # Result: it mistakenly returned 2 (the `}` inside the verbatim string).
    # The refactor uses a single `while` driver, so the `}` at line 2 is
    # correctly treated as string content and the outer `}` at line 4 closes.
    ("multiline_verbatim", ["{", '  var s = @"', "}", '";', "}"], 0, 4),
    ("verbatim_unterminated_eof", ['{ var s = @"unterminated'], 0, 0),
    ("brace_in_verbatim_body", ['{ var s = @"}"; var x = 1; }'], 0, 0),
    # --- interpolated strings ---
    ("interpolated_string", ["{", '  var s = $"x{v}y";', "}"], 0, 2),
    ("interpolated_verbatim_dollar_at", ["{", '  var s = $@"{{x}}";', "}"], 0, 2),
    ("interpolated_verbatim_at_dollar", ["{", '  var s = @$"a";', "}"], 0, 2),
    # --- char literals ---
    ("char_literal_brace", ["{", "  var c = '{';", "}"], 0, 2),
    ("char_literal_escaped_newline", ["{", "  var c = '\\n';", "}"], 0, 2),
    ("char_no_escape", ["{ var c = 'x'; }"], 0, 0),
    ("char_hex_escape", ["{ var c = '\\x41'; }"], 0, 0),
    # --- comments ---
    ("line_comment_brace", ["{", "  // }", "}"], 0, 2),
    ("block_comment_inline", ["{ /* } */ }"], 0, 0),
    ("block_comment_multiline", ["{", "  /*", "  }", "  */", "}"], 0, 4),
    ("inline_block_comment_midline", ["{ /* c */ var x = 1; }"], 0, 0),
    # --- unterminated / EOF ---
    ("unterminated_no_close", ["{", "  x;"], 0, 1),
    # --- start_line_0 != 0 ---
    (
        "start_at_class_open",
        ["namespace X {", "class Y", "{", "    int m;", "}", "}"],
        2,
        4,
    ),
    (
        "brace_in_line_comment_before_start",
        ["// { unrelated", "{", "}"],
        1,
        2,
    ),
    # --- realistic method body ---
    (
        "method_body",
        [
            "public void M() {",
            "    if (true) { x(); }",
            "    return;",
            "}",
        ],
        0,
        3,
    ),
]


@pytest.mark.parametrize(
    "lines,start,expected",
    [(lines, start, expected) for (_name, lines, start, expected) in CHARACTERIZATION_CASES],
    ids=[name for (name, _lines, _start, _expected) in CHARACTERIZATION_CASES],
)
def test_find_brace_end_csharp_characterization(
    lines: list[str], start: int, expected: int
) -> None:
    assert find_brace_end_csharp(lines, start) == expected


# ---------------------------------------------------------------------------
# Rust characterization
# ---------------------------------------------------------------------------

# Each expected value was captured from the pre-refactor implementation.
RUST_CHARACTERIZATION_CASES: list[tuple[str, list[str], int, int]] = [
    # --- baseline / nesting ---
    ("single_line_empty", ["{}"], 0, 0),
    ("single_line_simple", ["{ let x = 1; }"], 0, 0),
    ("multi_line_simple", ["{", "  let x = 1;", "}"], 0, 2),
    ("nested_inline", ["{ { x; } }"], 0, 0),
    ("nested_multiline", ["{", "  { y; }", "}"], 0, 2),
    ("deeply_nested", ["{", "{", "{", "}", "}", "}"], 0, 5),
    # --- regular strings ---
    ("string_open_brace", ["{", '  let s = "{";', "}"], 0, 2),
    ("string_close_brace", ["{", '  let s = "}";', "}"], 0, 2),
    ("escaped_quote", ["{", '  let s = "a\\"b";', "}"], 0, 2),
    ("empty_string", ['{ let s = ""; }'], 0, 0),
    # --- raw strings ---
    ("raw_string_inline", ['{ let s = r#"{"#; }'], 0, 0),
    ("raw_string_double_hash", ['{ let s = r##"{"##; }'], 0, 0),
    ("raw_string_no_hash", ['{ let s = r"hello"; }'], 0, 0),
    # --- byte strings (no special handling, treated as `b` ident + string) ---
    ("byte_string", ['{ let s = b"{"; }'], 0, 0),
    # --- char literals & lifetimes ---
    ("char_brace", ["{", "  let c = '{';", "}"], 0, 2),
    ("char_escaped_newline", ["{", "  let c = '\\n';", "}"], 0, 2),
    ("char_simple", ["{ let c = 'x'; }"], 0, 0),
    ("lifetime", ["{ let x: &'a str = \"hi\"; }"], 0, 0),
    # --- comments ---
    ("line_comment", ["{", "  // }", "}"], 0, 2),
    ("block_comment_inline", ["{ /* } */ }"], 0, 0),
    ("block_comment_nested", ["{", "/* /* } */ */", "}"], 0, 2),
    ("block_comment_multiline", ["{", "  /*", "  }", "  */", "}"], 0, 4),
    # --- unterminated / EOF ---
    ("unterminated", ["{", "  x;"], 0, 1),
    # --- start_line_0 != 0 ---
    ("start_offset", ["fn foo() {", "fn bar()", "{", "    1;", "}", "}"], 2, 4),
    ("brace_in_comment_before_start", ["// { unrelated", "{", "}"], 1, 2),
    # --- realistic fn body ---
    ("fn_body", ["fn m() {", "    if true { x(); }", "    return;", "}"], 0, 3),
]


@pytest.mark.parametrize(
    "lines,start,expected",
    [(lines, start, expected) for (_n, lines, start, expected) in RUST_CHARACTERIZATION_CASES],
    ids=[name for (name, _l, _s, _e) in RUST_CHARACTERIZATION_CASES],
)
def test_find_brace_end_rust_characterization(
    lines: list[str], start: int, expected: int
) -> None:
    assert find_brace_end_rust(lines, start) == expected


# ---------------------------------------------------------------------------
# Go characterization
# ---------------------------------------------------------------------------

# Each expected value was captured from the pre-refactor implementation.
GO_CHARACTERIZATION_CASES: list[tuple[str, list[str], int, int]] = [
    # --- baseline / nesting ---
    ("single_line_empty", ["{}"], 0, 0),
    ("single_line_simple", ["{ x := 1 }"], 0, 0),
    ("multi_line_simple", ["{", "  x := 1", "}"], 0, 2),
    ("nested_inline", ["{ { x } }"], 0, 0),
    ("nested_multiline", ["{", "  { y }", "}"], 0, 2),
    ("deeply_nested", ["{", "{", "{", "}", "}", "}"], 0, 5),
    # --- interpreted string literals ---
    ("string_open_brace", ["{", '  s := "{"', "}"], 0, 2),
    ("string_close_brace", ["{", '  s := "}"', "}"], 0, 2),
    ("escaped_quote", ["{", '  s := "a\\"b"', "}"], 0, 2),
    ("empty_string", ['{ s := "" }'], 0, 0),
    # --- raw string literals (backtick, multi-line capable) ---
    ("raw_string_inline_open", ["{", "  s := `{`", "}"], 0, 2),
    ("raw_string_inline_both", ["{ s := `{}` }"], 0, 0),
    # Multi-line raw string containing "}". The pre-refactor impl used a
    # `for idx in range(...)` driver whose loop variable was rebound on each
    # iteration, so advancing `idx` inside the raw-string handler was lost
    # and the outer loop revisited lines already consumed by the string body.
    # Result: it mistakenly returns 2 (the `}` inside the raw string). The
    # refactored impl uses a single `while` driver and correctly returns 4.
    ("raw_string_multiline_close", ["{", "  s := `", "}", "`", "}"], 0, 4),
    # --- rune literals ---
    # Current impl does not special-case runes, so '{' bumps depth and the
    # outer close at line 2 is reached only via the EOF sentinel (len-1=2).
    # The refactored impl handles runes and reaches line 2 directly. Both
    # paths converge on 2, so the test is stable across the refactor.
    ("rune_brace", ["{", "  c := '{'", "}"], 0, 2),
    ("rune_simple", ["{ c := 'x' }"], 0, 0),
    ("rune_escaped_quote", ["{", "  c := '\\''", "}"], 0, 2),
    # --- comments ---
    ("line_comment", ["{", "  // }", "}"], 0, 2),
    ("block_comment_inline", ["{ /* } */ }"], 0, 0),
    ("block_comment_multiline", ["{", "  /*", "  }", "  */", "}"], 0, 4),
    ("inline_block_comment_midline", ["{ /* c */ x := 1 }"], 0, 0),
    # --- unterminated / EOF sentinel ---
    ("unterminated", ["{", "  x"], 0, 1),
    # --- start_line_0 != 0 ---
    (
        "start_offset",
        ["package m", "func f() {", "  x := 1", "}"],
        1,
        3,
    ),
    (
        "brace_in_comment_before_start",
        ["// { unrelated", "{", "}"],
        1,
        2,
    ),
    # --- realistic func body ---
    (
        "func_body",
        [
            "func M() {",
            "    if true { x() }",
            "    return",
            "}",
        ],
        0,
        3,
    ),
]


@pytest.mark.parametrize(
    "lines,start,expected",
    [(lines, start, expected) for (_n, lines, start, expected) in GO_CHARACTERIZATION_CASES],
    ids=[name for (name, _l, _s, _e) in GO_CHARACTERIZATION_CASES],
)
def test_find_brace_end_go_characterization(
    lines: list[str], start: int, expected: int
) -> None:
    assert find_brace_end_go(lines, start) == expected
