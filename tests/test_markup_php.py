"""Tests for the regex-based PHP annotator (issue #50)."""

from token_savior.annotator import annotate

try:
    from token_savior.php_annotator import annotate_php
except ImportError:  # pragma: no cover - true only before the RED proof is fixed
    annotate_php = None


def _php(src: str, source_name: str = "<source>"):
    """Local wrapper so this file collects even before php_annotator.py
    exists — the RED proof only needs TestPhpDispatch below to be meaningful."""
    if annotate_php is None:
        raise ModuleNotFoundError("token_savior.php_annotator not implemented yet")
    return annotate_php(src, source_name)


class TestPhpDispatch:
    """Dispatch-level tests: only import token_savior.annotator, never
    php_annotator directly, so they stay meaningful (behavioural, not an
    ImportError) both before and after php_annotator.py exists."""

    def test_php_extension_routes_to_php_annotator(self):
        src = "<?php\nfunction f($a) {\n    return $a;\n}\n"
        meta = annotate(src, "example.php")
        assert "f" in [f.name for f in meta.functions]

    def test_inc_extension_routes_to_php_annotator(self):
        src = "<?php\nfunction f($a) { return $a; }\n"
        meta = annotate(src, "example.inc")
        assert "f" in [f.name for f in meta.functions]


class TestPhpFunctionDetection:
    """Tests for detecting top-level function declarations."""

    def test_simple_function(self):
        src = "<?php\nfunction foo($a, $b) {\n    return $a + $b;\n}\n"
        meta = _php(src)
        assert len(meta.functions) == 1
        f = meta.functions[0]
        assert f.name == "foo"
        assert f.parameters == ["$a", "$b"]
        assert f.is_method is False
        assert f.parent_class is None
        assert f.line_range.start == 2
        assert f.line_range.end == 4

    def test_parameter_default(self):
        src = "<?php\nfunction greet($name = 'World') {\n    return $name;\n}\n"
        meta = _php(src)
        assert meta.functions[0].parameters == ["$name"]

    def test_parameter_type_hint(self):
        src = "<?php\nfunction calc(int $a, string $b) {\n    return $a;\n}\n"
        meta = _php(src)
        assert meta.functions[0].parameters == ["$a", "$b"]

    def test_parameter_nullable_type_hint(self):
        src = "<?php\nfunction maybe(?Foo $b) {\n    return $b;\n}\n"
        meta = _php(src)
        assert meta.functions[0].parameters == ["$b"]

    def test_parameter_by_ref(self):
        src = "<?php\nfunction inc(&$x) {\n    $x++;\n}\n"
        meta = _php(src)
        assert meta.functions[0].parameters == ["$x"]

    def test_parameter_variadic(self):
        src = "<?php\nfunction sum(...$xs) {\n    return array_sum($xs);\n}\n"
        meta = _php(src)
        assert meta.functions[0].parameters == ["$xs"]

    def test_return_type(self):
        src = "<?php\nfunction f(): ?string {\n    return null;\n}\n"
        meta = _php(src)
        assert meta.functions[0].return_type == "?string"

    def test_multiline_signature(self):
        src = (
            "<?php\n"
            "function multi(\n"
            "    int $a,\n"
            "    ?string $b = null\n"
            ") {\n"
            "    return $a;\n"
            "}\n"
        )
        meta = _php(src)
        assert len(meta.functions) == 1
        f = meta.functions[0]
        assert f.parameters == ["$a", "$b"]
        assert f.line_range.start == 2
        assert f.line_range.end == 7

    def test_brace_allman_style(self):
        src = "<?php\nfunction allman()\n{\n    return 1;\n}\n"
        meta = _php(src)
        f = meta.functions[0]
        assert f.line_range.start == 2
        assert f.line_range.end == 5

    def test_brace_krstyle(self):
        src = "<?php\nfunction krstyle() {\n    return 1;\n}\n"
        meta = _php(src)
        f = meta.functions[0]
        assert f.line_range.start == 2
        assert f.line_range.end == 4

    def test_case_insensitive_function_keyword(self):
        src = "<?php\nFunction F() {\n    return 1;\n}\n"
        meta = _php(src)
        assert meta.functions[0].name == "F"

    def test_closure_not_captured(self):
        src = "<?php\n$f = function () {\n    return 1;\n};\n"
        meta = _php(src)
        assert meta.functions == []

    def test_arrow_function_not_captured(self):
        src = "<?php\n$f = fn($x) => $x + 1;\n"
        meta = _php(src)
        assert meta.functions == []


class TestPhpMethodDetection:
    """Tests for detecting methods inside classes/interfaces."""

    def test_method_is_method_and_qualified_name(self):
        src = "<?php\nclass Foo {\n    public function bar() {\n        return 1;\n    }\n}\n"
        meta = _php(src)
        f = next(fn for fn in meta.functions if fn.name == "bar")
        assert f.is_method is True
        assert f.parent_class == "Foo"
        assert f.qualified_name == "Foo.bar"
        assert "bar" in [m.name for m in meta.classes[0].methods]

    def test_method_visibility_captured(self):
        src = (
            "<?php\n"
            "class Foo {\n"
            "    public function a() {}\n"
            "    private function b() {}\n"
            "    protected function c() {}\n"
            "}\n"
        )
        meta = _php(src)
        vis = {f.name: f.visibility for f in meta.functions}
        assert vis["a"] == "public"
        assert vis["b"] == "private"
        assert vis["c"] == "protected"

    def test_static_final_abstract_modifiers_dont_break_parsing(self):
        src = (
            "<?php\n"
            "abstract class Foo {\n"
            "    public static function a() { return 1; }\n"
            "    final public function b() { return 2; }\n"
            "    abstract public function c();\n"
            "}\n"
        )
        meta = _php(src)
        names = [f.name for f in meta.functions]
        assert "a" in names and "b" in names and "c" in names

    def test_abstract_interface_method_no_body(self):
        src = "<?php\ninterface Foo {\n    public function bar(): void;\n}\n"
        meta = _php(src)
        f = next(fn for fn in meta.functions if fn.name == "bar")
        assert f.is_method is True
        assert f.parent_class == "Foo"
        assert f.return_type == "void"
        assert f.line_range.start == f.line_range.end == 3

    def test_constructor_captured(self):
        src = (
            "<?php\n"
            "class Foo {\n"
            "    public function __construct($x) {\n"
            "        $this->x = $x;\n"
            "    }\n"
            "}\n"
        )
        meta = _php(src)
        assert "__construct" in [f.name for f in meta.functions]


class TestPhpClassDetection:
    """Tests for detecting class/interface/trait/enum declarations."""

    def test_simple_class(self):
        src = "<?php\nclass Foo {}\n"
        meta = _php(src)
        assert len(meta.classes) == 1
        c = meta.classes[0]
        assert c.name == "Foo"
        assert c.line_range.start == c.line_range.end == 2

    def test_class_extends_implements(self):
        # base_classes combines both the extends target and every
        # implements target — no separate "interfaces" field exists.
        src = "<?php\nclass Foo extends Bar implements Baz, Qux {}\n"
        meta = _php(src)
        c = meta.classes[0]
        assert set(c.base_classes) == {"Bar", "Baz", "Qux"}

    def test_abstract_class_prefix(self):
        src = "<?php\nabstract class Foo {}\n"
        meta = _php(src)
        assert meta.classes[0].name == "Foo"

    def test_final_class_prefix(self):
        src = "<?php\nfinal class Foo {}\n"
        meta = _php(src)
        assert meta.classes[0].name == "Foo"

    def test_interface_captured(self):
        src = "<?php\ninterface Foo {}\n"
        meta = _php(src)
        assert meta.classes[0].name == "Foo"

    def test_trait_captured(self):
        src = "<?php\ntrait Foo {}\n"
        meta = _php(src)
        assert meta.classes[0].name == "Foo"

    def test_enum_captured(self):
        src = "<?php\nenum Foo {}\n"
        meta = _php(src)
        assert meta.classes[0].name == "Foo"

    def test_backed_enum_captured(self):
        src = "<?php\nenum Foo: string {}\n"
        meta = _php(src)
        assert meta.classes[0].name == "Foo"

    def test_anonymous_class_not_captured(self):
        src = "<?php\n$obj = new class {\n    public function hello() {}\n};\n"
        meta = _php(src)
        assert meta.classes == []

    def test_methods_populated_on_class(self):
        src = (
            "<?php\n"
            "class Foo {\n"
            "    public function a() {}\n"
            "    public function b() {}\n"
            "}\n"
        )
        meta = _php(src)
        method_names = [m.name for m in meta.classes[0].methods]
        assert method_names == ["a", "b"]


class TestPhpImportsAndModule:
    """Tests for namespace/use/require-include detection."""

    def test_namespace_sets_module_name(self):
        src = "<?php\nnamespace App\\Core;\n"
        meta = _php(src)
        assert meta.module_name == "App\\Core"

    def test_use_import(self):
        src = "<?php\nuse App\\Models\\User;\n"
        meta = _php(src)
        assert len(meta.imports) == 1
        assert meta.imports[0].module == "App\\Models\\User"
        assert meta.imports[0].alias is None

    def test_use_import_with_alias(self):
        src = "<?php\nuse App\\Models\\User as UserModel;\n"
        meta = _php(src)
        assert meta.imports[0].module == "App\\Models\\User"
        assert meta.imports[0].alias == "UserModel"

    def test_group_use_import(self):
        # Group use expands to one ImportInfo per name (not a single entry
        # with a names list) — simplest mapping onto the shared ImportInfo shape.
        src = "<?php\nuse App\\Models\\{User, Post};\n"
        meta = _php(src)
        modules = {imp.module for imp in meta.imports}
        assert modules == {"App\\Models\\User", "App\\Models\\Post"}

    def test_require_once_import(self):
        src = "<?php\nrequire_once 'x.php';\n"
        meta = _php(src)
        assert meta.imports[0].module == "x.php"

    def test_include_import(self):
        src = '<?php\ninclude "y.inc";\n'
        meta = _php(src)
        assert meta.imports[0].module == "y.inc"


class TestPhpDocComments:
    """Tests for /** ... */ docblock summary extraction."""

    def test_function_docblock_summary(self):
        src = (
            "<?php\n"
            "/**\n"
            " * Adds two numbers together.\n"
            " */\n"
            "function add($a, $b) {\n"
            "    return $a + $b;\n"
            "}\n"
        )
        meta = _php(src)
        assert meta.functions[0].docstring is not None
        assert "Adds two numbers together" in meta.functions[0].docstring

    def test_class_docblock_summary(self):
        src = "<?php\n/**\n * Represents a widget.\n */\nclass Widget {}\n"
        meta = _php(src)
        assert meta.classes[0].docstring is not None
        assert "Represents a widget" in meta.classes[0].docstring

    def test_no_doc_comment(self):
        src = "<?php\nfunction noDoc() {}\n"
        meta = _php(src)
        assert meta.functions[0].docstring is None


class TestPhpDependencyGraph:
    """Tests for the intra-file caller -> callee dependency graph."""

    def test_caller_callee_edge(self):
        src = (
            "<?php\n"
            "function helper() {\n"
            "    return 1;\n"
            "}\n"
            "\n"
            "function caller() {\n"
            "    return helper();\n"
            "}\n"
        )
        meta = _php(src)
        assert "helper" in meta.dependency_graph["caller"]
        assert "caller" not in meta.dependency_graph["helper"]


class TestPhpHostileInputs:
    """Adversarial inputs: none may crash, and none may false-positive."""

    def test_single_quoted_fake_declaration_not_captured(self):
        src = "<?php\n$s = 'function fake() {';\n"
        meta = _php(src)
        assert meta.functions == []

    def test_double_quoted_fake_declaration_not_captured(self):
        src = '<?php\n$s = "function fake() {";\n'
        meta = _php(src)
        assert meta.functions == []

    def test_heredoc_fake_declaration_not_captured(self):
        src = "<?php\n$s = <<<EOT\nfunction fake() {}\nEOT;\n"
        meta = _php(src)
        assert meta.functions == []

    def test_nowdoc_fake_declaration_not_captured(self):
        src = "<?php\n$s = <<<'EOT'\nfunction fake() {}\nEOT;\n"
        meta = _php(src)
        assert meta.functions == []

    def test_slash_comment_fake_declaration_not_captured(self):
        src = "<?php\n// function fake() {}\n"
        meta = _php(src)
        assert meta.functions == []

    def test_hash_comment_fake_declaration_not_captured(self):
        src = "<?php\n# function fake() {}\n"
        meta = _php(src)
        assert meta.functions == []

    def test_block_comment_fake_declaration_not_captured(self):
        src = "<?php\n/*\nfunction fake() {}\n*/\n"
        meta = _php(src)
        assert meta.functions == []

    def test_html_mixed_single_line_function_found(self):
        src = "<html><?php function real() {} ?></html>"
        meta = _php(src)
        assert "real" in [f.name for f in meta.functions]

    def test_multiple_php_blocks_separated_by_html(self):
        src = (
            "<html>\n"
            "<?php\n"
            "function a() {}\n"
            "?>\n"
            "<p>text</p>\n"
            "<?php\n"
            "function b() {}\n"
            "?>\n"
            "</html>\n"
        )
        meta = _php(src)
        names = [f.name for f in meta.functions]
        assert "a" in names
        assert "b" in names

    def test_leading_php_tag_no_closing_tag(self):
        src = "<?php\nfunction f() {\n    return 1;\n}\n"
        meta = _php(src)
        assert "f" in [f.name for f in meta.functions]

    def test_unbalanced_brace_at_eof_no_crash(self):
        src = "<?php\nfunction broken() {\n    echo 'test';\n"
        meta = _php(src)
        f = meta.functions[0]
        assert f.name == "broken"
        assert f.line_range.end == meta.total_lines

    def test_empty_string_input(self):
        meta = _php("")
        assert meta.functions == []
        assert meta.classes == []
        assert meta.imports == []
        assert meta.total_lines == 1

    def test_whitespace_only_input(self):
        meta = _php("   \n\t\n  ")
        assert meta.functions == []
        assert meta.classes == []
        assert meta.imports == []

    def test_very_long_single_line_no_crash(self):
        long_line = "$x = 1; " * 1500
        src = "<?php\n" + long_line + "\nfunction tail() {\n    return 1;\n}\n"
        meta = _php(src)
        assert "tail" in [f.name for f in meta.functions]
