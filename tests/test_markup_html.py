"""Tests for HTML embedded JavaScript annotation."""

from token_savior.annotator import annotate
from token_savior.html_annotator import annotate_html
from token_savior.project_indexer import ProjectIndexer


class TestHtmlAnnotator:
    def test_script_blocks_extract_symbols_with_original_lines(self):
        src = (
            "<!doctype html>\n"
            "<html>\n"
            "<head>\n"
            "<script type=\"module\">\n"
            "import { render } from './render.js';\n"
            "function bootApp(root) {\n"
            "  return render(root);\n"
            "}\n"
            "</script>\n"
            "</head>\n"
            "<body>\n"
            "<button onclick=\"bootApp(this)\">Boot</button>\n"
            "</body>\n"
            "</html>"
        )

        meta = annotate_html(src, "index.html")

        assert meta.total_lines == 14
        assert [imp.module for imp in meta.imports] == ["./render.js"]
        assert [func.name for func in meta.functions] == ["bootApp"]
        assert meta.functions[0].line_range.start == 6
        assert meta.functions[0].line_range.end == 8

    def test_markup_outside_script_is_not_treated_as_code(self):
        src = (
            "<main>\n"
            "  <div>{ maybeTemplateExpression }</div>\n"
            "  <style>.panel { color: red; }</style>\n"
            "</main>"
        )

        meta = annotate_html(src, "plain.html")

        assert meta.functions == []
        assert meta.classes == []
        assert meta.imports == []

    def test_dispatch_routes_html_extension_to_html_annotator(self):
        meta = annotate(
            "<script>\nconst hydrate = () => {\n  return true;\n};\n</script>",
            source_name="index.html",
        )

        assert [func.name for func in meta.functions] == ["hydrate"]

    def test_project_indexer_resolves_embedded_script_imports(self, tmp_path):
        (tmp_path / "index.html").write_text(
            "<script type=\"module\">\n"
            "import { render } from './render';\n"
            "function bootApp() {\n"
            "  return render();\n"
            "}\n"
            "</script>\n"
        )
        (tmp_path / "render.js").write_text(
            "export function render() {\n"
            "  return true;\n"
            "}\n"
        )

        idx = ProjectIndexer(str(tmp_path)).index()

        assert "index.html" in idx.files
        assert "render.js" in idx.files
        assert "bootApp" in idx.symbol_table
        assert idx.import_graph["index.html"] == {"render.js"}
