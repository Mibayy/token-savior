"""Tests for the text/markdown annotator."""

from token_savior.text_annotator import annotate_text


class TestMarkdownHeadings:
    """Tests for Markdown # headings."""

    def test_single_h1(self):
        text = "# Introduction\nSome content here.\nMore content."
        meta = annotate_text(text)
        assert len(meta.sections) == 1
        s = meta.sections[0]
        assert s.title == "Introduction"
        assert s.level == 1
        assert s.line_range.start == 1
        assert s.line_range.end == 3

    def test_multiple_headings_same_level(self):
        text = "# First\nContent A\n# Second\nContent B"
        meta = annotate_text(text)
        assert len(meta.sections) == 2
        assert meta.sections[0].title == "First"
        assert meta.sections[0].line_range.start == 1
        # Section ends at line 2 (0-indexed line 2 is '# Second', so end = 2 in 1-indexed)
        assert meta.sections[0].line_range.end == 2
        assert meta.sections[1].title == "Second"
        assert meta.sections[1].line_range.start == 3
        assert meta.sections[1].line_range.end == 4

    def test_nested_headings(self):
        text = "# Top\n## Sub\nContent\n# Next Top"
        meta = annotate_text(text)
        assert len(meta.sections) == 3
        # Top: level 1, extends to line before '# Next Top' (0-indexed 3 -> end=3)
        assert meta.sections[0].title == "Top"
        assert meta.sections[0].level == 1
        assert meta.sections[0].line_range.end == 3
        # Sub: level 2, extends to line before '# Next Top'
        assert meta.sections[1].title == "Sub"
        assert meta.sections[1].level == 2
        assert meta.sections[1].line_range.end == 3

    def test_h3_heading(self):
        text = "### Deep Section\nSome text"
        meta = annotate_text(text)
        assert len(meta.sections) == 1
        assert meta.sections[0].level == 3


class TestUnderlineHeadings:
    """Tests for underline-style headings (=== and ---)."""

    def test_equals_underline_level1(self):
        text = "My Title\n========\nBody text here."
        meta = annotate_text(text)
        assert len(meta.sections) == 1
        s = meta.sections[0]
        assert s.title == "My Title"
        assert s.level == 1
        assert s.line_range.start == 1  # heading line (1-indexed)

    def test_dash_underline_level2(self):
        text = "Subtitle\n--------\nMore text."
        meta = annotate_text(text)
        assert len(meta.sections) == 1
        s = meta.sections[0]
        assert s.title == "Subtitle"
        assert s.level == 2

    def test_mixed_underline_and_markdown(self):
        text = "Title\n=====\n# Also a heading\nContent"
        meta = annotate_text(text)
        assert len(meta.sections) == 2
        assert meta.sections[0].level == 1
        assert meta.sections[1].level == 1


class TestNumberedSections:
    """Tests for numbered section headings."""

    def test_simple_numbered(self):
        text = "1 Introduction\nSome text\n2 Methods\nMore text"
        meta = annotate_text(text)
        assert len(meta.sections) == 2
        assert meta.sections[0].title == "1 Introduction"
        assert meta.sections[0].level == 1
        assert meta.sections[1].title == "2 Methods"
        assert meta.sections[1].level == 1

    def test_nested_numbered(self):
        text = "1 Top\n1.1 Sub\nContent\n2 Next"
        meta = annotate_text(text)
        assert len(meta.sections) == 3
        assert meta.sections[0].level == 1
        assert meta.sections[1].level == 2
        assert meta.sections[1].title == "1.1 Sub"
        assert meta.sections[2].level == 1

    def test_deep_numbering(self):
        text = "1.2.3 Deep Section\nContent here"
        meta = annotate_text(text)
        assert len(meta.sections) == 1
        assert meta.sections[0].level == 3


class TestAllCapsHeadings:
    """Tests for ALL-CAPS headings of 4+ words."""

    def test_caps_heading(self):
        text = "THIS IS A HEADING\nSome body text below."
        meta = annotate_text(text)
        assert len(meta.sections) == 1
        assert meta.sections[0].title == "THIS IS A HEADING"
        assert meta.sections[0].level == 2

    def test_short_caps_not_heading(self):
        """Lines with fewer than 4 words should not be detected as headings."""
        text = "NOT ENOUGH\nSome text."
        meta = annotate_text(text)
        assert len(meta.sections) == 0


class TestEdgeCases:
    """Edge cases and metadata checks."""

    def test_empty_document(self):
        meta = annotate_text("")
        assert meta.total_lines == 1  # split("") gives [""]
        assert meta.sections == []

    def test_no_headings(self):
        text = "Just some plain text.\nWith multiple lines.\nBut no headings."
        meta = annotate_text(text)
        assert meta.sections == []

    def test_source_name_default(self):
        meta = annotate_text("hello")
        assert meta.source_name == "<text>"

    def test_source_name_custom(self):
        meta = annotate_text("hello", source_name="notes.md")
        assert meta.source_name == "notes.md"

    def test_total_chars(self):
        text = "# H\nContent"
        meta = annotate_text(text)
        assert meta.total_chars == len(text)

    def test_line_char_offsets(self):
        text = "abc\ndef\nghi"
        meta = annotate_text(text)
        assert meta.line_char_offsets == [0, 4, 8]

    def test_functions_classes_imports_empty(self):
        text = "# Heading\nContent"
        meta = annotate_text(text)
        assert meta.functions == []
        assert meta.classes == []
        assert meta.imports == []

    def test_mixed_heading_styles(self):
        text = (
            "# Markdown Heading\n"
            "Intro text\n"
            "Underline Title\n"
            "===============\n"
            "Body A\n"
            "1.1 Numbered Sub\n"
            "Body B\n"
            "THIS IS ALL CAPS HEADING\n"
            "Body C"
        )
        meta = annotate_text(text)
        titles = [s.title for s in meta.sections]
        assert "Markdown Heading" in titles
        assert "Underline Title" in titles
        assert "1.1 Numbered Sub" in titles
        assert "THIS IS ALL CAPS HEADING" in titles
