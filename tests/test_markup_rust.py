"""Tests for the regex-based Rust annotator."""

from token_savior.rust_annotator import annotate_rust


class TestRustFunctionDetection:
    """Tests for detecting function declarations."""

    def test_simple_function(self):
        src = "fn greet(name: &str) -> String {\n    name.to_string()\n}"
        meta = annotate_rust(src)
        assert len(meta.functions) == 1
        f = meta.functions[0]
        assert f.name == "greet"
        assert f.qualified_name == "greet"
        assert f.is_method is False
        assert f.parent_class is None
        assert "name" in f.parameters
        assert f.line_range.start == 1
        assert f.line_range.end == 3

    def test_pub_function(self):
        src = "pub fn add(a: i32, b: i32) -> i32 {\n    a + b\n}"
        meta = annotate_rust(src)
        assert len(meta.functions) == 1
        assert meta.functions[0].name == "add"
        assert "a" in meta.functions[0].parameters
        assert "b" in meta.functions[0].parameters

    def test_pub_crate_function(self):
        src = "pub(crate) fn internal() {\n}\n"
        meta = annotate_rust(src)
        assert len(meta.functions) == 1
        assert meta.functions[0].name == "internal"

    def test_async_function(self):
        src = "async fn fetch(url: &str) -> Result<String, Error> {\n    Ok(String::new())\n}"
        meta = annotate_rust(src)
        assert len(meta.functions) == 1
        assert meta.functions[0].name == "fetch"
        assert "url" in meta.functions[0].parameters

    def test_const_function(self):
        src = "const fn max_size() -> usize {\n    1024\n}"
        meta = annotate_rust(src)
        assert len(meta.functions) == 1
        assert meta.functions[0].name == "max_size"

    def test_unsafe_function(self):
        src = "unsafe fn dangerous(ptr: *const u8) {\n}\n"
        meta = annotate_rust(src)
        assert len(meta.functions) == 1
        assert meta.functions[0].name == "dangerous"
        assert "ptr" in meta.functions[0].parameters

    def test_pub_async_function(self):
        src = "pub async fn serve(port: u16) -> Result<(), Error> {\n    Ok(())\n}"
        meta = annotate_rust(src)
        assert len(meta.functions) == 1
        assert meta.functions[0].name == "serve"

    def test_function_with_lifetime(self):
        src = "fn first<'a>(s: &'a str) -> &'a str {\n    s\n}"
        meta = annotate_rust(src)
        assert len(meta.functions) == 1
        assert meta.functions[0].name == "first"

    def test_function_with_where_clause(self):
        src = (
            "fn process<T>(item: T) -> String\n"
            "where\n"
            "    T: Display + Debug,\n"
            "{\n"
            '    format!("{}", item)\n'
            "}\n"
        )
        meta = annotate_rust(src)
        assert len(meta.functions) == 1
        assert meta.functions[0].name == "process"
        assert "item" in meta.functions[0].parameters

    def test_multiple_functions(self):
        src = "fn foo() {\n}\n\nfn bar() {\n}\n"
        meta = annotate_rust(src)
        func_names = [f.name for f in meta.functions]
        assert "foo" in func_names
        assert "bar" in func_names

    def test_function_with_mut_param(self):
        src = "fn update(mut data: Vec<i32>) -> Vec<i32> {\n    data\n}"
        meta = annotate_rust(src)
        assert len(meta.functions) == 1
        assert "data" in meta.functions[0].parameters


class TestRustStructDetection:
    """Tests for detecting struct declarations."""

    def test_regular_struct(self):
        src = "struct Point {\n    x: f64,\n    y: f64,\n}"
        meta = annotate_rust(src)
        assert len(meta.classes) == 1
        c = meta.classes[0]
        assert c.name == "Point"
        assert c.line_range.start == 1
        assert c.line_range.end == 4

    def test_pub_struct(self):
        src = "pub struct Config {\n    pub name: String,\n}"
        meta = annotate_rust(src)
        assert len(meta.classes) == 1
        assert meta.classes[0].name == "Config"

    def test_tuple_struct(self):
        src = "struct Wrapper(i32);"
        meta = annotate_rust(src)
        assert len(meta.classes) == 1
        assert meta.classes[0].name == "Wrapper"

    def test_unit_struct(self):
        src = "struct Marker;"
        meta = annotate_rust(src)
        assert len(meta.classes) == 1
        assert meta.classes[0].name == "Marker"

    def test_struct_with_derive(self):
        src = "#[derive(Debug, Clone)]\nstruct Item {\n    id: u64,\n}\n"
        meta = annotate_rust(src)
        assert len(meta.classes) == 1
        c = meta.classes[0]
        assert c.name == "Item"
        assert any("derive" in d for d in c.decorators)


class TestRustEnumDetection:
    """Tests for detecting enum declarations."""

    def test_simple_enum(self):
        src = "enum Color {\n    Red,\n    Green,\n    Blue,\n}"
        meta = annotate_rust(src)
        assert len(meta.classes) == 1
        assert meta.classes[0].name == "Color"

    def test_pub_enum(self):
        src = "pub enum Option<T> {\n    Some(T),\n    None,\n}"
        meta = annotate_rust(src)
        assert len(meta.classes) == 1
        assert meta.classes[0].name == "Option"

    def test_enum_with_data(self):
        src = "enum Message {\n    Quit,\n    Move { x: i32, y: i32 },\n    Write(String),\n}"
        meta = annotate_rust(src)
        assert len(meta.classes) == 1
        assert meta.classes[0].name == "Message"


class TestRustTraitDetection:
    """Tests for detecting trait declarations."""

    def test_simple_trait(self):
        src = "trait Drawable {\n    fn draw(&self);\n}\n"
        meta = annotate_rust(src)
        assert len(meta.classes) == 1
        c = meta.classes[0]
        assert c.name == "Drawable"
        assert len(c.methods) == 1
        assert c.methods[0].name == "draw"

    def test_trait_with_supertrait(self):
        src = "trait Animal: Display + Debug {\n    fn name(&self) -> &str;\n}\n"
        meta = annotate_rust(src)
        assert len(meta.classes) == 1
        c = meta.classes[0]
        assert c.name == "Animal"
        assert "Display" in c.base_classes
        assert "Debug" in c.base_classes

    def test_trait_with_default_method(self):
        src = 'trait Greet {\n    fn hello(&self) {\n        println!("Hello!");\n    }\n}\n'
        meta = annotate_rust(src)
        assert len(meta.classes) == 1
        c = meta.classes[0]
        assert c.name == "Greet"
        assert len(c.methods) == 1
        assert c.methods[0].name == "hello"

    def test_pub_trait(self):
        src = "pub trait Service {\n    fn call(&self, req: Request) -> Response;\n}\n"
        meta = annotate_rust(src)
        assert len(meta.classes) == 1
        assert meta.classes[0].name == "Service"


class TestRustImplBlocks:
    """Tests for detecting impl blocks and extracting methods."""

    def test_inherent_impl(self):
        src = (
            "struct Point {\n"
            "    x: f64,\n"
            "    y: f64,\n"
            "}\n"
            "\n"
            "impl Point {\n"
            "    fn new(x: f64, y: f64) -> Self {\n"
            "        Point { x, y }\n"
            "    }\n"
            "\n"
            "    fn distance(&self) -> f64 {\n"
            "        (self.x * self.x + self.y * self.y).sqrt()\n"
            "    }\n"
            "}\n"
        )
        meta = annotate_rust(src)

        # Point struct should have methods attached
        assert len(meta.classes) == 1
        c = meta.classes[0]
        assert c.name == "Point"
        method_names = [m.name for m in c.methods]
        assert "new" in method_names
        assert "distance" in method_names

        # Methods should be in functions list
        methods = [f for f in meta.functions if f.is_method and f.parent_class == "Point"]
        assert len(methods) == 2
        for m in methods:
            assert m.qualified_name.startswith("Point.")

    def test_trait_impl(self):
        src = (
            "struct Cat;\n"
            "\n"
            "impl Display for Cat {\n"
            "    fn fmt(&self, f: &mut Formatter) -> Result {\n"
            '        write!(f, "Cat")\n'
            "    }\n"
            "}\n"
        )
        meta = annotate_rust(src)

        methods = [f for f in meta.functions if f.is_method and f.parent_class == "Cat"]
        assert len(methods) == 1
        assert methods[0].name == "fmt"
        assert "impl:Display" in methods[0].decorators

    def test_impl_with_self_param(self):
        src = (
            "impl Server {\n"
            "    fn start(&self) {\n"
            "    }\n"
            "\n"
            "    fn stop(&mut self) {\n"
            "    }\n"
            "\n"
            "    fn create() -> Self {\n"
            "    }\n"
            "}\n"
        )
        meta = annotate_rust(src)

        methods = [f for f in meta.functions if f.parent_class == "Server"]
        assert len(methods) == 3
        # Self params should not appear in parameters
        for m in methods:
            assert "self" not in m.parameters

    def test_method_params_extracted(self):
        src = (
            "impl Handler {\n"
            "    fn handle(&self, path: String, method: Method) -> Response {\n"
            "        Response::new()\n"
            "    }\n"
            "}\n"
        )
        meta = annotate_rust(src)
        methods = [f for f in meta.functions if f.parent_class == "Handler"]
        assert len(methods) == 1
        assert "path" in methods[0].parameters
        assert "method" in methods[0].parameters


class TestRustUseStatements:
    """Tests for detecting use statements."""

    def test_simple_use(self):
        src = "use std::io::Read;"
        meta = annotate_rust(src)
        assert len(meta.imports) == 1
        imp = meta.imports[0]
        assert imp.module == "std::io"
        assert "Read" in imp.names

    def test_grouped_use(self):
        src = "use std::collections::{HashMap, HashSet};"
        meta = annotate_rust(src)
        assert len(meta.imports) == 1
        imp = meta.imports[0]
        assert imp.module == "std::collections"
        assert "HashMap" in imp.names
        assert "HashSet" in imp.names

    def test_glob_use(self):
        src = "use std::io::*;"
        meta = annotate_rust(src)
        assert len(meta.imports) == 1
        assert "*" in meta.imports[0].names

    def test_aliased_use(self):
        src = "use std::io::Result as IoResult;"
        meta = annotate_rust(src)
        assert len(meta.imports) == 1
        assert meta.imports[0].alias == "IoResult"

    def test_crate_use(self):
        src = "use crate::models::Config;"
        meta = annotate_rust(src)
        assert len(meta.imports) == 1
        assert meta.imports[0].module == "crate::models"
        assert "Config" in meta.imports[0].names

    def test_pub_use(self):
        src = "pub use crate::error::Error;"
        meta = annotate_rust(src)
        assert len(meta.imports) == 1
        assert "Error" in meta.imports[0].names

    def test_multiple_use_statements(self):
        src = "use std::io;\nuse std::collections::HashMap;\nuse crate::utils::helper;\n"
        meta = annotate_rust(src)
        assert len(meta.imports) == 3

    def test_self_use(self):
        src = "use std::collections::{self, HashMap};"
        meta = annotate_rust(src)
        assert len(meta.imports) == 1
        assert "self" in meta.imports[0].names
        assert "HashMap" in meta.imports[0].names


class TestRustDocComments:
    """Tests for doc comment extraction."""

    def test_function_doc_comment(self):
        src = (
            "/// Adds two numbers.\n"
            "/// Returns their sum.\n"
            "fn add(a: i32, b: i32) -> i32 {\n"
            "    a + b\n"
            "}\n"
        )
        meta = annotate_rust(src)
        assert len(meta.functions) == 1
        assert meta.functions[0].docstring is not None
        assert "Adds two numbers" in meta.functions[0].docstring

    def test_struct_doc_comment(self):
        src = "/// A point in 2D space.\nstruct Point {\n    x: f64,\n    y: f64,\n}\n"
        meta = annotate_rust(src)
        assert len(meta.classes) == 1
        assert meta.classes[0].docstring is not None
        assert "point in 2D space" in meta.classes[0].docstring

    def test_attributes_collected(self):
        src = "#[derive(Debug, Clone)]\n#[cfg(test)]\nstruct TestItem {\n    value: i32,\n}\n"
        meta = annotate_rust(src)
        assert len(meta.classes) == 1
        c = meta.classes[0]
        assert any("derive" in d for d in c.decorators)
        assert "cfg" in c.decorators

    def test_no_doc_comment(self):
        src = "fn bare() {}"
        meta = annotate_rust(src)
        assert len(meta.functions) == 1
        assert meta.functions[0].docstring is None


class TestRustMacroRules:
    """Tests for macro_rules! detection."""

    def test_simple_macro(self):
        src = 'macro_rules! say_hello {\n    () => {\n        println!("Hello!");\n    };\n}\n'
        meta = annotate_rust(src)
        assert len(meta.functions) == 1
        f = meta.functions[0]
        assert f.name == "say_hello"
        assert "macro" in f.decorators
        assert f.is_method is False

    def test_pub_macro(self):
        src = (
            "#[macro_export]\n"
            "macro_rules! my_vec {\n"
            "    ( $( $x:expr ),* ) => {\n"
            "        {\n"
            "            let mut temp = Vec::new();\n"
            "            $( temp.push($x); )*\n"
            "            temp\n"
            "        }\n"
            "    };\n"
            "}\n"
        )
        meta = annotate_rust(src)
        assert len(meta.functions) == 1
        assert meta.functions[0].name == "my_vec"
        assert "macro_export" in meta.functions[0].decorators


class TestRustComplexFile:
    """Integration-style test with a multi-element Rust file."""

    def test_full_file(self):
        src = (
            "use std::fmt;\n"
            "use std::collections::HashMap;\n"
            "\n"
            "/// A server configuration.\n"
            "#[derive(Debug, Clone)]\n"
            "pub struct Config {\n"
            "    pub host: String,\n"
            "    pub port: u16,\n"
            "}\n"
            "\n"
            "/// Error types.\n"
            "pub enum AppError {\n"
            "    NotFound,\n"
            "    Internal(String),\n"
            "}\n"
            "\n"
            "/// Displayable trait.\n"
            "pub trait Displayable: fmt::Display {\n"
            "    fn summary(&self) -> String;\n"
            "}\n"
            "\n"
            "impl Config {\n"
            "    pub fn new(host: String, port: u16) -> Self {\n"
            "        Config { host, port }\n"
            "    }\n"
            "\n"
            "    pub fn address(&self) -> String {\n"
            '        format!("{}:{}", self.host, self.port)\n'
            "    }\n"
            "}\n"
            "\n"
            "impl fmt::Display for Config {\n"
            "    fn fmt(&self, f: &mut fmt::Formatter) -> fmt::Result {\n"
            '        write!(f, "{}:{}", self.host, self.port)\n'
            "    }\n"
            "}\n"
            "\n"
            "/// Create a default config.\n"
            "pub fn default_config() -> Config {\n"
            '    Config::new("localhost".into(), 8080)\n'
            "}\n"
        )
        meta = annotate_rust(src, source_name="config.rs")

        # Imports
        assert len(meta.imports) == 2

        # Classes
        class_names = [c.name for c in meta.classes]
        assert "Config" in class_names
        assert "AppError" in class_names
        assert "Displayable" in class_names

        config = next(c for c in meta.classes if c.name == "Config")
        method_names = [m.name for m in config.methods]
        assert "new" in method_names
        assert "address" in method_names
        assert config.docstring is not None

        displayable = next(c for c in meta.classes if c.name == "Displayable")
        assert len(displayable.methods) >= 1
        assert displayable.methods[0].name == "summary"

        # Top-level function
        top_funcs = [f for f in meta.functions if not f.is_method]
        top_func_names = [f.name for f in top_funcs]
        assert "default_config" in top_func_names

        # trait impl methods should have impl:Display decorator
        fmt_methods = [f for f in meta.functions if f.name == "fmt" and f.parent_class == "Config"]
        assert len(fmt_methods) == 1
        assert any("Display" in d for d in fmt_methods[0].decorators)


class TestRustEdgeCases:
    """Edge case tests."""

    def test_braces_in_string(self):
        src = 'fn template() -> String {\n    let s = "{ not a block }";\n    s.to_string()\n}\n'
        meta = annotate_rust(src)
        assert len(meta.functions) == 1
        assert meta.functions[0].name == "template"
        assert meta.functions[0].line_range.end == 4

    def test_raw_string_with_braces(self):
        src = 'fn raw() -> &str {\n    r#"{"key": "value"}"#\n}\n'
        meta = annotate_rust(src)
        assert len(meta.functions) == 1
        assert meta.functions[0].name == "raw"
        assert meta.functions[0].line_range.end == 3

    def test_nested_braces(self):
        src = "fn nested() {\n    if true {\n        if false {\n        }\n    }\n}\n"
        meta = annotate_rust(src)
        assert len(meta.functions) == 1
        assert meta.functions[0].line_range.start == 1
        assert meta.functions[0].line_range.end == 6

    def test_empty_source(self):
        meta = annotate_rust("")
        assert meta.total_lines == 1
        assert len(meta.functions) == 0
        assert len(meta.classes) == 0

    def test_source_name(self):
        meta = annotate_rust("fn main() {}", source_name="main.rs")
        assert meta.source_name == "main.rs"

    def test_line_offsets(self):
        src = "abc\ndef\nghi"
        meta = annotate_rust(src)
        assert meta.line_char_offsets == [0, 4, 8]

    def test_block_comment_with_braces(self):
        src = "fn commented() {\n    /* { this is a comment } */\n    let x = 1;\n}\n"
        meta = annotate_rust(src)
        assert len(meta.functions) == 1
        assert meta.functions[0].line_range.end == 4

    def test_extern_fn(self):
        src = 'extern "C" fn callback(data: *const u8) {\n}\n'
        meta = annotate_rust(src)
        assert len(meta.functions) == 1
        assert meta.functions[0].name == "callback"
