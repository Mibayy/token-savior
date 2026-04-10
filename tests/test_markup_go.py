"""Tests for the regex-based Go annotator."""

from token_savior.go_annotator import annotate_go


class TestGoFunctionDetection:
    """Tests for detecting function declarations."""

    def test_simple_function(self):
        src = 'func greet(name string) string {\n\treturn "Hello " + name\n}'
        meta = annotate_go(src)
        assert len(meta.functions) == 1
        f = meta.functions[0]
        assert f.name == "greet"
        assert f.qualified_name == "greet"
        assert f.is_method is False
        assert f.parent_class is None
        assert "name" in f.parameters
        assert f.line_range.start == 1
        assert f.line_range.end == 3

    def test_multiple_params(self):
        src = "func add(a int, b int) int {\n\treturn a + b\n}"
        meta = annotate_go(src)
        assert len(meta.functions) == 1
        assert "a" in meta.functions[0].parameters
        assert "b" in meta.functions[0].parameters

    def test_no_params(self):
        src = 'func hello() {\n\tfmt.Println("hello")\n}'
        meta = annotate_go(src)
        assert len(meta.functions) == 1
        assert meta.functions[0].parameters == []

    def test_variadic_params(self):
        src = "func sum(nums ...int) int {\n\ttotal := 0\n\treturn total\n}"
        meta = annotate_go(src)
        assert len(meta.functions) == 1

    def test_multiple_functions(self):
        src = "func foo() {\n}\n\nfunc bar() {\n}\n"
        meta = annotate_go(src)
        assert len(meta.functions) == 2
        names = [f.name for f in meta.functions]
        assert "foo" in names
        assert "bar" in names

    def test_function_line_range(self):
        src = "package main\n\nfunc foo() {\n\tx := 1\n\ty := 2\n\treturn x + y\n}\n"
        meta = annotate_go(src)
        assert len(meta.functions) == 1
        f = meta.functions[0]
        assert f.line_range.start == 3
        assert f.line_range.end == 7

    def test_generic_function(self):
        src = "func Map[T any, U any](s []T, f func(T) U) []U {\n\treturn nil\n}"
        meta = annotate_go(src)
        assert len(meta.functions) == 1
        assert meta.functions[0].name == "Map"


class TestGoMethodDetection:
    """Tests for detecting method declarations."""

    def test_pointer_receiver(self):
        src = "func (s *Server) Start() error {\n\treturn nil\n}"
        meta = annotate_go(src)
        assert len(meta.functions) == 1
        f = meta.functions[0]
        assert f.name == "Start"
        assert f.qualified_name == "Server.Start"
        assert f.is_method is True
        assert f.parent_class == "Server"

    def test_value_receiver(self):
        src = "func (p Point) Distance() float64 {\n\treturn 0.0\n}"
        meta = annotate_go(src)
        assert len(meta.functions) == 1
        f = meta.functions[0]
        assert f.name == "Distance"
        assert f.parent_class == "Point"
        assert f.is_method is True

    def test_method_with_params(self):
        src = "func (s *Server) Handle(path string, handler func()) {\n}"
        meta = annotate_go(src)
        assert len(meta.functions) == 1
        f = meta.functions[0]
        assert f.name == "Handle"
        assert "path" in f.parameters

    def test_methods_attached_to_struct(self):
        src = (
            "type Server struct {\n"
            "\tport int\n"
            "}\n"
            "\n"
            "func (s *Server) Start() {\n"
            "}\n"
            "\n"
            "func (s *Server) Stop() {\n"
            "}\n"
        )
        meta = annotate_go(src)
        assert len(meta.classes) == 1
        c = meta.classes[0]
        assert c.name == "Server"
        method_names = [m.name for m in c.methods]
        assert "Start" in method_names
        assert "Stop" in method_names


class TestGoTypeDetection:
    """Tests for detecting type declarations."""

    def test_simple_struct(self):
        src = "type Point struct {\n\tX float64\n\tY float64\n}"
        meta = annotate_go(src)
        assert len(meta.classes) == 1
        c = meta.classes[0]
        assert c.name == "Point"
        assert c.line_range.start == 1
        assert c.line_range.end == 4

    def test_struct_with_embedding(self):
        src = "type Admin struct {\n\tUser\n\tRole string\n}"
        meta = annotate_go(src)
        assert len(meta.classes) == 1
        c = meta.classes[0]
        assert c.name == "Admin"
        assert "User" in c.base_classes

    def test_simple_interface(self):
        src = "type Reader interface {\n\tRead(p []byte) (int, error)\n}"
        meta = annotate_go(src)
        assert len(meta.classes) == 1
        c = meta.classes[0]
        assert c.name == "Reader"
        assert len(c.methods) == 1
        assert c.methods[0].name == "Read"

    def test_interface_with_embedding(self):
        src = "type ReadWriter interface {\n\tReader\n\tWriter\n}"
        meta = annotate_go(src)
        assert len(meta.classes) == 1
        c = meta.classes[0]
        assert c.name == "ReadWriter"
        assert "Reader" in c.base_classes
        assert "Writer" in c.base_classes

    def test_type_alias(self):
        src = "type MyString = string"
        meta = annotate_go(src)
        # Type alias doesn't use initial-cap target, but it should still be detected
        assert len(meta.classes) == 1
        assert meta.classes[0].name == "MyString"
        assert "string" in meta.classes[0].base_classes

    def test_empty_struct(self):
        src = "type Empty struct {}"
        meta = annotate_go(src)
        assert len(meta.classes) == 1
        assert meta.classes[0].name == "Empty"


class TestGoImportDetection:
    """Tests for detecting import statements."""

    def test_single_import(self):
        src = 'import "fmt"'
        meta = annotate_go(src)
        assert len(meta.imports) == 1
        imp = meta.imports[0]
        assert imp.module == "fmt"
        assert "fmt" in imp.names

    def test_grouped_imports(self):
        src = 'import (\n\t"fmt"\n\t"os"\n)'
        meta = annotate_go(src)
        assert len(meta.imports) == 2
        modules = [imp.module for imp in meta.imports]
        assert "fmt" in modules
        assert "os" in modules

    def test_aliased_import(self):
        src = 'import myfmt "fmt"'
        meta = annotate_go(src)
        assert len(meta.imports) == 1
        assert meta.imports[0].alias == "myfmt"
        assert meta.imports[0].module == "fmt"

    def test_blank_import(self):
        src = 'import _ "net/http/pprof"'
        meta = annotate_go(src)
        assert len(meta.imports) == 1
        assert meta.imports[0].alias == "_"

    def test_dot_import(self):
        src = 'import (\n\t. "fmt"\n)'
        meta = annotate_go(src)
        assert len(meta.imports) == 1
        assert meta.imports[0].alias == "."

    def test_path_import(self):
        src = 'import "github.com/user/repo/pkg"'
        meta = annotate_go(src)
        assert len(meta.imports) == 1
        assert meta.imports[0].module == "github.com/user/repo/pkg"
        assert "pkg" in meta.imports[0].names


class TestGoDocComments:
    """Tests for doc comment extraction."""

    def test_function_doc_comment(self):
        src = (
            "// Greet returns a greeting message.\n"
            "// It takes a name parameter.\n"
            "func Greet(name string) string {\n"
            '\treturn "Hello " + name\n'
            "}"
        )
        meta = annotate_go(src)
        assert len(meta.functions) == 1
        assert meta.functions[0].docstring is not None
        assert "Greet returns a greeting message" in meta.functions[0].docstring

    def test_struct_doc_comment(self):
        src = "// Server represents an HTTP server.\ntype Server struct {\n\tPort int\n}"
        meta = annotate_go(src)
        assert len(meta.classes) == 1
        assert meta.classes[0].docstring is not None
        assert "Server represents" in meta.classes[0].docstring

    def test_no_doc_comment(self):
        src = "func noDoc() {}"
        meta = annotate_go(src)
        assert len(meta.functions) == 1
        assert meta.functions[0].docstring is None

    def test_non_adjacent_comment_not_doc(self):
        src = "// This is a general comment\n\nfunc foo() {}\n"
        meta = annotate_go(src)
        # The blank line breaks the doc comment chain
        assert meta.functions[0].docstring is None


class TestGoComplexFile:
    """Integration-style test with a multi-element Go file."""

    def test_full_file(self):
        src = (
            "package main\n"
            "\n"
            "import (\n"
            '\t"fmt"\n'
            '\t"net/http"\n'
            ")\n"
            "\n"
            "// Handler defines request handling.\n"
            "type Handler interface {\n"
            "\tServeHTTP(w http.ResponseWriter, r *http.Request)\n"
            "}\n"
            "\n"
            "// App is the main application.\n"
            "type App struct {\n"
            "\tName string\n"
            "\tPort int\n"
            "}\n"
            "\n"
            "// Run starts the application.\n"
            "func (a *App) Run() error {\n"
            '\treturn http.ListenAndServe(fmt.Sprintf(":%d", a.Port), nil)\n'
            "}\n"
            "\n"
            "func main() {\n"
            '\tapp := &App{Name: "myapp", Port: 8080}\n'
            "\tapp.Run()\n"
            "}\n"
        )
        meta = annotate_go(src, source_name="main.go")

        # Imports
        assert len(meta.imports) == 2

        # Classes (Handler interface + App struct)
        class_names = [c.name for c in meta.classes]
        assert "Handler" in class_names
        assert "App" in class_names

        handler = next(c for c in meta.classes if c.name == "Handler")
        assert len(handler.methods) == 1
        assert handler.methods[0].name == "ServeHTTP"

        app = next(c for c in meta.classes if c.name == "App")
        method_names = [m.name for m in app.methods]
        assert "Run" in method_names

        # Functions
        func_names = [f.name for f in meta.functions if not f.is_method]
        assert "main" in func_names

        # Doc comments
        assert app.docstring is not None
        assert "App is the main application" in app.docstring


class TestGoEdgeCases:
    """Edge case tests."""

    def test_backtick_string_with_braces(self):
        src = 'func tmpl() string {\n\treturn `{"key": "value"}`\n}\n'
        meta = annotate_go(src)
        assert len(meta.functions) == 1
        assert meta.functions[0].name == "tmpl"
        assert meta.functions[0].line_range.end == 3

    def test_function_type_param(self):
        src = "func apply(f func(int) int, x int) int {\n\treturn f(x)\n}"
        meta = annotate_go(src)
        assert len(meta.functions) == 1
        assert meta.functions[0].name == "apply"

    def test_empty_source(self):
        meta = annotate_go("")
        assert meta.total_lines == 1
        assert len(meta.functions) == 0
        assert len(meta.classes) == 0
        assert len(meta.imports) == 0

    def test_package_only(self):
        meta = annotate_go("package main\n")
        assert len(meta.functions) == 0
        assert len(meta.classes) == 0

    def test_source_name(self):
        meta = annotate_go("package main", source_name="main.go")
        assert meta.source_name == "main.go"

    def test_line_offsets(self):
        src = "abc\ndef\nghi"
        meta = annotate_go(src)
        assert meta.line_char_offsets == [0, 4, 8]

    def test_multiline_backtick_string(self):
        src = "func query() string {\n\treturn `\nSELECT *\nFROM {\n  table\n}\n`\n}\n"
        meta = annotate_go(src)
        assert len(meta.functions) == 1
        assert meta.functions[0].name == "query"
