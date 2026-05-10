"""Tests for Vue single-file component annotation."""

from token_savior.annotator import annotate
from token_savior.vue_annotator import annotate_vue


class TestVueAnnotator:
    def test_script_setup_extracts_symbols_with_original_lines(self):
        src = (
            "<template>\n"
            "  <button @click=\"submit\">Save</button>\n"
            "</template>\n"
            "\n"
            "<script setup lang=\"ts\">\n"
            "import WidgetCard from '~/components/WidgetCard.vue';\n"
            "type Props = { label: string };\n"
            "const submit = async (id: string) => {\n"
            "  return id;\n"
            "};\n"
            "</script>\n"
            "\n"
            "<style scoped>\n"
            "button { color: red; }\n"
            "</style>"
        )

        meta = annotate_vue(src, "pages/index.vue")

        assert meta.total_lines == 15
        assert [imp.module for imp in meta.imports] == ["~/components/WidgetCard.vue"]
        assert "Props" in [cls.name for cls in meta.classes]

        submit = next(func for func in meta.functions if func.name == "submit")
        assert submit.line_range.start == 8
        assert submit.line_range.end == 10

    def test_dispatch_routes_vue_extension_to_vue_annotator(self):
        meta = annotate(
            "<script>\nfunction loadThing() {\n  return true;\n}\n</script>",
            source_name="components/Thing.vue",
        )

        assert [func.name for func in meta.functions] == ["loadThing"]
