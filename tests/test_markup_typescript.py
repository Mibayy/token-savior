"""Tests for the regex-based TypeScript annotator."""

from token_savior.typescript_annotator import annotate_typescript


class TestFunctionDetection:
    """Tests for detecting function declarations."""

    def test_simple_function(self):
        src = "function greet(name: string): string {\n  return `Hello ${name}`;\n}"
        meta = annotate_typescript(src)
        assert len(meta.functions) == 1
        f = meta.functions[0]
        assert f.name == "greet"
        assert f.qualified_name == "greet"
        assert f.is_method is False
        assert f.parent_class is None
        assert "name" in f.parameters
        assert f.line_range.start == 1
        assert f.line_range.end == 3

    def test_export_function(self):
        src = "export function add(a: number, b: number): number {\n  return a + b;\n}"
        meta = annotate_typescript(src)
        assert len(meta.functions) == 1
        assert meta.functions[0].name == "add"
        assert "a" in meta.functions[0].parameters
        assert "b" in meta.functions[0].parameters

    def test_async_function(self):
        src = "export async function fetchData(url: string) {\n  return await fetch(url);\n}"
        meta = annotate_typescript(src)
        assert len(meta.functions) == 1
        assert meta.functions[0].name == "fetchData"

    def test_arrow_function(self):
        src = "const multiply = (x: number, y: number) => {\n  return x * y;\n};"
        meta = annotate_typescript(src)
        assert len(meta.functions) == 1
        f = meta.functions[0]
        assert f.name == "multiply"
        assert f.is_method is False
        assert "x" in f.parameters
        assert "y" in f.parameters

    def test_export_arrow_function(self):
        src = "export const handler = (req: Request, res: Response) => {\n  res.send('ok');\n};"
        meta = annotate_typescript(src)
        assert len(meta.functions) == 1
        assert meta.functions[0].name == "handler"

    def test_multiline_typed_arrow_function_tracks_full_block_range(self):
        src = (
            "export const CTAButton: React.FC<Props> = (\n"
            "  { title }\n"
            ") =>\n"
            "{\n"
            "  return (\n"
            "    <button>{title}</button>\n"
            "  );\n"
            "};"
        )
        meta = annotate_typescript(src)
        assert len(meta.functions) == 1
        func = meta.functions[0]
        assert func.name == "CTAButton"
        assert func.line_range.start == 1
        assert func.line_range.end == 8


class TestClassDetection:
    """Tests for detecting class declarations."""

    def test_simple_class(self):
        src = (
            "class Animal {\n"
            "  name: string;\n"
            "  constructor(name: string) {\n"
            "    this.name = name;\n"
            "  }\n"
            "  speak() {\n"
            "    return this.name;\n"
            "  }\n"
            "}"
        )
        meta = annotate_typescript(src)
        assert len(meta.classes) == 1
        c = meta.classes[0]
        assert c.name == "Animal"
        assert c.line_range.start == 1
        assert c.line_range.end == 9
        # Should have detected methods
        method_names = [m.name for m in c.methods]
        assert "constructor" in method_names
        assert "speak" in method_names

    def test_class_with_extends(self):
        src = "export class Dog extends Animal {\n  bark() {\n    return 'woof';\n  }\n}"
        meta = annotate_typescript(src)
        assert len(meta.classes) == 1
        c = meta.classes[0]
        assert c.name == "Dog"
        assert "Animal" in c.base_classes
        assert len(c.methods) == 1
        assert c.methods[0].name == "bark"
        assert c.methods[0].is_method is True
        assert c.methods[0].parent_class == "Dog"
        assert c.methods[0].qualified_name == "Dog.bark"

    def test_class_with_implements(self):
        src = "class Service extends Base {\n  run() {\n    console.log('running');\n  }\n}"
        meta = annotate_typescript(src)
        assert len(meta.classes) == 1
        assert "Base" in meta.classes[0].base_classes


class TestInterfaceDetection:
    """Interfaces are treated as ClassInfo."""

    def test_simple_interface(self):
        src = "interface User {\n  id: number;\n  name: string;\n}"
        meta = annotate_typescript(src)
        assert len(meta.classes) == 1
        c = meta.classes[0]
        assert c.name == "User"
        assert c.line_range.start == 1
        assert c.line_range.end == 4

    def test_export_interface_extends(self):
        src = "export interface Admin extends User {\n  role: string;\n}"
        meta = annotate_typescript(src)
        assert len(meta.classes) == 1
        c = meta.classes[0]
        assert c.name == "Admin"
        assert "User" in c.base_classes


class TestTypeAliasDetection:
    """Type aliases are treated as ClassInfo."""

    def test_simple_type_alias(self):
        src = "type ID = string | number;"
        meta = annotate_typescript(src)
        assert len(meta.classes) == 1
        assert meta.classes[0].name == "ID"

    def test_export_type_alias(self):
        src = "export type Status = 'active' | 'inactive';"
        meta = annotate_typescript(src)
        assert len(meta.classes) == 1
        assert meta.classes[0].name == "Status"


class TestImportDetection:
    """Tests for detecting import statements."""

    def test_named_import(self):
        src = "import { Component, useState } from 'react';"
        meta = annotate_typescript(src)
        assert len(meta.imports) == 1
        imp = meta.imports[0]
        assert imp.module == "react"
        assert "Component" in imp.names
        assert "useState" in imp.names
        assert imp.line_number == 1

    def test_default_import(self):
        src = "import React from 'react';"
        meta = annotate_typescript(src)
        assert len(meta.imports) == 1
        assert meta.imports[0].alias == "React"
        assert meta.imports[0].module == "react"

    def test_side_effect_import(self):
        src = "import './styles.css';"
        meta = annotate_typescript(src)
        assert len(meta.imports) == 1
        assert meta.imports[0].module == "./styles.css"

    def test_multiple_imports(self):
        src = "import { A } from 'mod-a';\nimport B from 'mod-b';\nimport './side-effect';"
        meta = annotate_typescript(src)
        assert len(meta.imports) == 3


class TestMetadata:
    """Tests for basic metadata fields."""

    def test_total_lines_and_chars(self):
        src = "const x = 1;\nconst y = 2;"
        meta = annotate_typescript(src)
        assert meta.total_lines == 2
        assert meta.total_chars == len(src)

    def test_source_name_default(self):
        meta = annotate_typescript("const x = 1;")
        assert meta.source_name == "<source>"

    def test_source_name_custom(self):
        meta = annotate_typescript("const x = 1;", source_name="app.ts")
        assert meta.source_name == "app.ts"

    def test_line_offsets(self):
        src = "abc\ndef\nghi"
        meta = annotate_typescript(src)
        assert meta.line_char_offsets == [0, 4, 8]

    def test_sections_empty(self):
        """TypeScript annotator should not populate sections."""
        src = "function foo() {}"
        meta = annotate_typescript(src)
        assert meta.sections == []


class TestComplexFile:
    """Integration-style test with a multi-element file."""

    def test_full_file(self):
        src = (
            "import { Request, Response } from 'express';\n"
            "import logger from './logger';\n"
            "\n"
            "interface Config {\n"
            "  port: number;\n"
            "  host: string;\n"
            "}\n"
            "\n"
            "export class Server extends Base {\n"
            "  private config: Config;\n"
            "\n"
            "  constructor(config: Config) {\n"
            "    this.config = config;\n"
            "  }\n"
            "\n"
            "  start() {\n"
            "    console.log('starting');\n"
            "  }\n"
            "}\n"
            "\n"
            "export function createServer(port: number): Server {\n"
            "  return new Server({ port, host: 'localhost' });\n"
            "}\n"
            "\n"
            "const helper = (x: number) => {\n"
            "  return x * 2;\n"
            "};"
        )
        meta = annotate_typescript(src, source_name="server.ts")

        # Imports
        assert len(meta.imports) == 2

        # Classes (Config interface + Server class)
        class_names = [c.name for c in meta.classes]
        assert "Config" in class_names
        assert "Server" in class_names

        server = next(c for c in meta.classes if c.name == "Server")
        assert "Base" in server.base_classes
        method_names = [m.name for m in server.methods]
        assert "constructor" in method_names
        assert "start" in method_names

        # Top-level functions
        top_funcs = [f for f in meta.functions if not f.is_method]
        top_func_names = [f.name for f in top_funcs]
        assert "createServer" in top_func_names
        assert "helper" in top_func_names

        # Methods should have is_method=True
        methods = [f for f in meta.functions if f.is_method]
        assert len(methods) >= 2
        for m in methods:
            assert m.parent_class == "Server"
