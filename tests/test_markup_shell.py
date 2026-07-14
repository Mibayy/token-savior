"""Tests for the regex-based shell (POSIX sh / bash) annotator."""

from token_savior.annotator import annotate

try:
    from token_savior.shell_annotator import annotate_shell
except ImportError:
    # (#51) shell_annotator.py doesn't exist yet on the untouched worktree;
    # keeps this file collectible so the dispatch red-proof below can run.
    annotate_shell = None


class TestShellDispatch:
    """Dispatch-level: .sh/.bash route to the shell annotator (red-proof)."""

    def test_sh_extension_dispatches_to_shell_annotator(self):
        src = "pfb_fetch() {\n\techo hi\n}\n"
        meta = annotate(src, "script.sh")
        assert len(meta.functions) == 1
        assert meta.functions[0].name == "pfb_fetch"

    def test_bash_extension_dispatches_to_shell_annotator(self):
        src = "pfb_fetch() {\n\techo hi\n}\n"
        meta = annotate(src, "script.bash")
        assert len(meta.functions) == 1
        assert meta.functions[0].name == "pfb_fetch"


class TestShellFunctionDetection:
    """Tests for detecting function declarations (all spellings)."""

    def test_posix_no_space(self):
        src = "greet() {\n\techo hi\n}\n"
        meta = annotate_shell(src)
        assert len(meta.functions) == 1
        f = meta.functions[0]
        assert f.name == "greet"
        assert f.qualified_name == "greet"
        assert f.is_method is False
        assert f.parent_class is None
        assert f.line_range.start == 1
        assert f.line_range.end == 3

    def test_posix_spaced(self):
        src = "greet () {\n\techo hi\n}\n"
        meta = annotate_shell(src)
        assert len(meta.functions) == 1
        assert meta.functions[0].name == "greet"

    def test_bash_keyword_no_parens(self):
        src = "function greet {\n\techo hi\n}\n"
        meta = annotate_shell(src)
        assert len(meta.functions) == 1
        assert meta.functions[0].name == "greet"

    def test_bash_keyword_with_parens(self):
        src = "function greet() {\n\techo hi\n}\n"
        meta = annotate_shell(src)
        assert len(meta.functions) == 1
        assert meta.functions[0].name == "greet"

    def test_one_liner_start_equals_end(self):
        src = "f() { :; }\n"
        meta = annotate_shell(src)
        assert len(meta.functions) == 1
        f = meta.functions[0]
        assert f.line_range.start == f.line_range.end == 1

    def test_multiline_body_with_nested_braces_and_param_expansion(self):
        src = (
            "foo() {\n"
            '\tif [ "$X" = ${Y:-default} ]; then\n'
            "\t\twhile [ 1 ]; do\n"
            "\t\t\techo hi\n"
            "\t\tdone\n"
            "\tfi\n"
            "}\n"
        )
        meta = annotate_shell(src)
        assert len(meta.functions) == 1
        f = meta.functions[0]
        assert f.name == "foo"
        assert f.line_range.start == 1
        assert f.line_range.end == 7

    def test_subshell_body_function_captured_without_crash(self):
        src = "name() (\n\techo hi\n)\n"
        meta = annotate_shell(src)
        assert len(meta.functions) == 1
        f = meta.functions[0]
        assert f.name == "name"
        assert f.line_range.start == 1
        # ponytail: paren-matching not implemented; body range collapses to
        # the declaration line only (documented simplification).
        assert f.line_range.end == 1

    def test_dash_named_function_not_captured(self):
        # bash allows "my-func() {"; our name grammar is [A-Za-z_]\w*, so
        # dash-named functions are deliberately skipped, not partially matched.
        src = "my-func() {\n\techo hi\n}\n"
        meta = annotate_shell(src)
        assert len(meta.functions) == 0

    def test_parameters_always_empty(self):
        src = "greet() {\n\techo hi\n}\n"
        meta = annotate_shell(src)
        assert meta.functions[0].parameters == []

    def test_multiple_functions(self):
        src = "foo() {\n\t:\n}\n\nbar() {\n\t:\n}\n"
        meta = annotate_shell(src)
        assert len(meta.functions) == 2
        names = [f.name for f in meta.functions]
        assert "foo" in names
        assert "bar" in names


class TestShellDocComments:
    """Tests for doc comment extraction."""

    def test_function_doc_comment(self):
        src = (
            "# fetches the remote feed\n"
            "# retries on failure\n"
            "pfb_fetch() {\n"
            "\t:\n"
            "}\n"
        )
        meta = annotate_shell(src)
        assert len(meta.functions) == 1
        assert meta.functions[0].docstring is not None
        assert "fetches the remote feed" in meta.functions[0].docstring

    def test_no_doc_comment(self):
        src = "nodoc() {\n\t:\n}\n"
        meta = annotate_shell(src)
        assert meta.functions[0].docstring is None


class TestShellImportDetection:
    """Tests for detecting source-imports."""

    def test_dot_import(self):
        src = ". ./lib/common.sh\n"
        meta = annotate_shell(src)
        assert len(meta.imports) == 1
        imp = meta.imports[0]
        assert imp.module == "./lib/common.sh"
        assert imp.is_from_import is False

    def test_source_import(self):
        src = "source lib/common.sh\n"
        meta = annotate_shell(src)
        assert len(meta.imports) == 1
        imp = meta.imports[0]
        assert imp.module == "lib/common.sh"
        # "." and "source" are synonyms (whole-file import, no name list):
        # both are pinned to is_from_import=False (#51).
        assert imp.is_from_import is False

    def test_quoted_variable_path_captured_verbatim(self):
        src = '. "$DIR/x.sh"\n'
        meta = annotate_shell(src)
        assert len(meta.imports) == 1
        # decision: quoted/variable paths are captured verbatim, not resolved
        # or skipped — static resolution of $DIR is out of scope (#51).
        assert meta.imports[0].module == "$DIR/x.sh"


class TestShellClassesAndModuleName:
    def test_classes_always_empty(self):
        src = "foo() {\n\t:\n}\n"
        meta = annotate_shell(src)
        assert meta.classes == []

    def test_module_name_is_none(self):
        src = "foo() {\n\t:\n}\n"
        meta = annotate_shell(src)
        assert meta.module_name is None


class TestShellDependencyGraph:
    def test_function_calling_another_function(self):
        src = "b() {\n\t:\n}\n\na() {\n\tb\n}\n"
        meta = annotate_shell(src)
        assert "b" in meta.dependency_graph["a"]
        assert "a" not in meta.dependency_graph["b"]

    def test_no_self_dependency(self):
        src = "recur() {\n\trecur\n}\n"
        meta = annotate_shell(src)
        assert "recur" not in meta.dependency_graph["recur"]

    def test_shell_keywords_excluded_from_deps(self):
        src = (
            "process() {\n"
            "\tfor i in 1 2 3; do\n"
            "\t\tif [ \"$i\" ]; then\n"
            "\t\t\techo \"$i\"\n"
            "\t\tfi\n"
            "\tdone\n"
            "}\n"
        )
        meta = annotate_shell(src)
        deps = meta.dependency_graph.get("process", [])
        for kw in ("for", "if", "fi", "done", "echo"):
            assert kw not in deps


class TestShellHostileInput:
    """Adversarial inputs that must never crash and must never false-positive."""

    def test_function_pattern_inside_double_quotes_not_captured(self):
        src = '"function fake() {"\n'
        meta = annotate_shell(src)
        assert len(meta.functions) == 0

    def test_function_pattern_inside_single_quotes_not_captured(self):
        src = "'f() {'\n"
        meta = annotate_shell(src)
        assert len(meta.functions) == 0

    def test_heredoc_unquoted_terminator_body_not_captured(self):
        src = (
            "setup() {\n"
            "\tcat <<EOF\n"
            "looks_like_fn() {\n"
            "\techo hi\n"
            "}\n"
            "EOF\n"
            "\techo done\n"
            "}\n"
        )
        meta = annotate_shell(src)
        names = [f.name for f in meta.functions]
        assert names == ["setup"]
        assert meta.functions[0].line_range.end == 8

    def test_heredoc_quoted_terminator_body_not_captured(self):
        src = (
            "setup() {\n"
            "\tcat <<'EOF'\n"
            "looks_like_fn() {\n"
            "\techo hi\n"
            "}\n"
            "EOF\n"
            "}\n"
        )
        meta = annotate_shell(src)
        names = [f.name for f in meta.functions]
        assert names == ["setup"]
        assert meta.functions[0].line_range.end == 7

    def test_comment_line_with_function_pattern_not_captured(self):
        src = "# name() {\nreal() {\n\t:\n}\n"
        meta = annotate_shell(src)
        names = [f.name for f in meta.functions]
        assert names == ["real"]

    def test_case_arm_word_paren_not_captured_as_function(self):
        src = (
            "dispatch() {\n"
            "\tcase \"$1\" in\n"
            "\tstart) do_thing ;;\n"
            "\t*.sh) do_other ;;\n"
            "\tesac\n"
            "}\n"
        )
        meta = annotate_shell(src)
        names = [f.name for f in meta.functions]
        assert names == ["dispatch"]

    def test_awk_inline_braces_do_not_corrupt_function_range(self):
        src = "run() {\n\tawk '{ print $1 }' file\n}\n"
        meta = annotate_shell(src)
        assert len(meta.functions) == 1
        assert meta.functions[0].line_range.end == 3

    def test_command_and_arithmetic_substitution_parens_do_not_crash(self):
        src = "compute() {\n\tx=$(( 1 + 2 ))\n\ty=$(echo hi)\n}\n"
        meta = annotate_shell(src)
        assert len(meta.functions) == 1
        assert meta.functions[0].line_range.end == 4

    def test_unbalanced_brace_truncated_eof_no_exception(self):
        src = "broken() {\n\techo hi\n"
        meta = annotate_shell(src)
        assert len(meta.functions) == 1
        assert meta.functions[0].line_range.end == meta.total_lines

    def test_empty_string_returns_empty_metadata(self):
        meta = annotate_shell("")
        assert meta.functions == []
        assert meta.classes == []
        assert meta.imports == []

    def test_whitespace_only_returns_empty_metadata(self):
        meta = annotate_shell("   \n\t\n  ")
        assert meta.functions == []
        assert meta.classes == []
        assert meta.imports == []

    def test_shebang_line_ignored_cleanly(self):
        src = "#!/bin/sh\nfoo() {\n\t:\n}\n"
        meta = annotate_shell(src)
        assert len(meta.functions) == 1
        assert meta.functions[0].name == "foo"
        assert meta.imports == []


class TestShellComplexFile:
    """Integration-style test with a multi-element shell file."""

    def test_full_file(self):
        src = (
            "#!/bin/sh\n"
            "# pfblockerng helper library (#51)\n"
            "\n"
            ". ./lib/common.sh\n"
            "source lib/net.sh\n"
            "\n"
            "# Fetches a feed and retries once on failure.\n"
            "pfb_fetch() {\n"
            "\tif [ \"$1\" ]; then\n"
            "\t\tpfb_retry\n"
            "\tfi\n"
            "}\n"
            "\n"
            "pfb_retry() {\n"
            "\t:\n"
            "}\n"
        )
        meta = annotate_shell(src, source_name="pfblockerng.sh")

        assert meta.source_name == "pfblockerng.sh"
        assert len(meta.imports) == 2
        modules = [imp.module for imp in meta.imports]
        assert "./lib/common.sh" in modules
        assert "lib/net.sh" in modules

        names = [f.name for f in meta.functions]
        assert "pfb_fetch" in names
        assert "pfb_retry" in names

        fetch = next(f for f in meta.functions if f.name == "pfb_fetch")
        assert fetch.docstring is not None
        assert "Fetches a feed" in fetch.docstring
        assert "pfb_retry" in meta.dependency_graph["pfb_fetch"]

        assert meta.classes == []
        assert meta.module_name is None
