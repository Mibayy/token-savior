"""Characterization tests for brace_matcher.find_brace_end_csharp.

These tests freeze the CURRENT behavior of ``find_brace_end_csharp`` before
refactoring. They are not aspirational — each expected value was captured by
running the pre-refactor implementation. Any change in output is a regression.

If you intentionally change behavior, update the expected values *and* document
the reason in the commit.
"""

from __future__ import annotations

import pytest

from token_savior.brace_matcher import find_brace_end_csharp


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
