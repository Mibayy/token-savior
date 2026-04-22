"""Tests for the library API exposure module."""

from __future__ import annotations

import textwrap

from token_savior.library_api import (
    get_library_symbol,
    list_library_symbols,
)


def _write_npm_dts(tmp_path, pkg: str, rel: str, content: str) -> None:
    path = tmp_path / "node_modules" / pkg / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(textwrap.dedent(content).lstrip())


class TestTypescriptLookup:
    def test_top_level_function(self, tmp_path):
        _write_npm_dts(tmp_path, "widgetlib", "index.d.ts", """
            /**
             * Creates a new widget.
             * @param name widget name
             */
            export declare function createWidget(name: string): Widget;
        """)
        result = get_library_symbol(
            "widgetlib", "createWidget", project_root=str(tmp_path)
        )
        assert result["ok"] is True
        assert result["language"] == "typescript"
        matches = result["matches"]
        assert len(matches) == 1
        assert matches[0]["kind"] == "function"
        assert "createWidget(name: string)" in matches[0]["signature"]
        assert "Creates a new widget" in matches[0]["jsdoc"]

    def test_class_method(self, tmp_path):
        _write_npm_dts(tmp_path, "widgetlib", "index.d.ts", """
            export declare class WidgetClient {
              /**
               * Fetch widget by id.
               */
              get(id: string): Promise<Widget>;
              close(): void;
            }
        """)
        result = get_library_symbol(
            "widgetlib", "WidgetClient.get", project_root=str(tmp_path)
        )
        assert result["ok"] is True
        matches = result["matches"]
        assert any(
            m["name"] == "WidgetClient.get" and "Fetch widget by id" in m["jsdoc"]
            for m in matches
        )

    def test_not_found(self, tmp_path):
        _write_npm_dts(tmp_path, "widgetlib", "index.d.ts",
                       "export declare function hello(): void;")
        result = get_library_symbol(
            "widgetlib", "nonexistent", project_root=str(tmp_path)
        )
        assert result["ok"] is False

    def test_list_symbols(self, tmp_path):
        _write_npm_dts(tmp_path, "widgetlib", "index.d.ts", """
            export declare function foo(): void;
            export declare function bar(): void;
            export declare class Baz {}
            export declare const qux: number;
        """)
        result = list_library_symbols("widgetlib", project_root=str(tmp_path))
        names = [i["name"] for i in result["items"]]
        assert set(["foo", "bar", "Baz", "qux"]).issubset(set(names))

    def test_list_with_pattern(self, tmp_path):
        _write_npm_dts(tmp_path, "widgetlib", "index.d.ts", """
            export declare function fetchUser(): void;
            export declare function fetchPost(): void;
            export declare function unrelated(): void;
        """)
        result = list_library_symbols(
            "widgetlib", project_root=str(tmp_path), pattern="^fetch"
        )
        names = [i["name"] for i in result["items"]]
        assert "fetchUser" in names
        assert "fetchPost" in names
        assert "unrelated" not in names

    def test_missing_package(self, tmp_path):
        result = get_library_symbol(
            "nonexistent-pkg-name", "foo", project_root=str(tmp_path)
        )
        assert result["ok"] is False


class TestPythonLookup:
    def test_stdlib_function(self, tmp_path):
        result = get_library_symbol(
            "json", "dumps", project_root=str(tmp_path)
        )
        assert result["ok"] is True
        assert result["language"] == "python"
        assert "dumps" in result["signature"]
        # json.dumps has a docstring
        assert len(result["doc"]) > 0

    def test_stdlib_class(self, tmp_path):
        result = get_library_symbol(
            "pathlib", "Path", project_root=str(tmp_path)
        )
        assert result["ok"] is True
        assert result["language"] == "python"
        assert result["kind"] == "class"

    def test_list_stdlib(self, tmp_path):
        result = list_library_symbols(
            "json", project_root=str(tmp_path), pattern="^dump"
        )
        assert result["ok"] is True
        names = [i["name"] for i in result["items"]]
        assert "dumps" in names
        assert "dump" in names
