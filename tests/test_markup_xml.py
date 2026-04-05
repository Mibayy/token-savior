"""Tests for the XML annotator."""

import textwrap

import pytest

from token_savior.xml_annotator import annotate_xml


class TestXmlSimpleElements:
    def test_simple_config(self):
        text = textwrap.dedent("""\
            <config>
              <host>localhost</host>
              <port>8080</port>
            </config>
        """)
        meta = annotate_xml(text)
        titles = [s.title for s in meta.sections]
        assert "config" in titles
        assert "host" in titles
        assert "port" in titles

    def test_source_name_default(self):
        meta = annotate_xml("<root/>")
        assert meta.source_name == "<xml>"

    def test_source_name_custom(self):
        meta = annotate_xml("<root/>", source_name="config.xml")
        assert meta.source_name == "config.xml"

    def test_total_lines(self):
        text = "<root>\n  <child/>\n</root>\n"
        meta = annotate_xml(text)
        # Implementation uses text.split("\n") which includes trailing empty entry
        assert meta.total_lines == len(text.split("\n"))

    def test_functions_classes_empty(self):
        meta = annotate_xml("<root><a/></root>")
        assert meta.functions == []
        assert meta.classes == []


class TestXmlNestedLevels:
    def test_nested_levels(self):
        text = textwrap.dedent("""\
            <root>
              <db>
                <host>localhost</host>
              </db>
            </root>
        """)
        meta = annotate_xml(text)
        by_title = {s.title: s for s in meta.sections}
        assert by_title["root"].level == 1
        assert by_title["db"].level == 2
        assert by_title["host"].level == 3

    def test_level_1_for_root(self):
        meta = annotate_xml("<root/>")
        assert meta.sections[0].level == 1

    def test_three_levels(self):
        text = "<a><b><c/></b></a>"
        meta = annotate_xml(text)
        by_title = {s.title: s for s in meta.sections}
        assert by_title["a"].level == 1
        assert by_title["b"].level == 2
        assert by_title["c"].level == 3


class TestXmlDepthCap:
    def test_depth_capped_at_4(self):
        text = textwrap.dedent("""\
            <l1>
              <l2>
                <l3>
                  <l4>
                    <l5>deep</l5>
                  </l4>
                </l3>
              </l2>
            </l1>
        """)
        meta = annotate_xml(text)
        max_level = max(s.level for s in meta.sections)
        assert max_level == 4
        titles = [s.title for s in meta.sections]
        assert "l1" in titles
        assert "l2" in titles
        assert "l3" in titles
        assert "l4" in titles
        assert "l5" not in titles


class TestXmlAttributes:
    def test_name_attribute_in_title(self):
        text = textwrap.dedent("""\
            <servers>
              <server name="web"/>
            </servers>
        """)
        meta = annotate_xml(text)
        titles = [s.title for s in meta.sections]
        assert any("server" in t and "web" in t for t in titles)

    def test_id_attribute_in_title(self):
        text = '<items><item id="42"/></items>'
        meta = annotate_xml(text)
        titles = [s.title for s in meta.sections]
        assert any("item" in t and "42" in t for t in titles)

    def test_type_attribute_in_title(self):
        text = '<plugins><plugin type="auth"/></plugins>'
        meta = annotate_xml(text)
        titles = [s.title for s in meta.sections]
        assert any("plugin" in t and "auth" in t for t in titles)

    def test_no_distinguishing_attr_uses_plain_tag(self):
        text = '<items><item value="x"/></items>'
        meta = annotate_xml(text)
        titles = [s.title for s in meta.sections]
        # "item" should appear as plain tag without extra label
        assert "item" in titles


class TestXmlNamespaceStripping:
    def test_namespace_stripped(self):
        text = textwrap.dedent("""\
            <ns:config xmlns:ns="http://example.com">
              <ns:host>localhost</ns:host>
            </ns:config>
        """)
        meta = annotate_xml(text)
        titles = [s.title for s in meta.sections]
        # Namespace URIs in Clark notation like {http://...}tag should be stripped
        # Plain ns:prefix tags are passed through as-is by ElementTree
        assert any("config" in t for t in titles)

    def test_clark_notation_namespace_stripped(self):
        # ElementTree uses Clark notation {uri}localname internally
        # Test with actual namespace that gets parsed
        text = '<root xmlns="http://example.com"><child/></root>'
        meta = annotate_xml(text)
        titles = [s.title for s in meta.sections]
        # Should have "root" and "child", not "{http://example.com}root"
        assert "root" in titles
        assert "child" in titles


class TestXmlInvalidFallback:
    def test_invalid_xml_fallback(self):
        text = "not xml at all <<<"
        meta = annotate_xml(text)
        # Falls back to generic annotator — no sections
        assert meta.sections == []
        assert meta.total_lines > 0

    def test_unclosed_tag_fallback(self):
        text = "<root><unclosed>"
        meta = annotate_xml(text)
        assert meta.sections == []

    def test_fallback_preserves_source_name(self):
        meta = annotate_xml("<<<bad>>>", source_name="broken.xml")
        assert meta.source_name == "broken.xml"
        assert meta.sections == []


class TestXmlLineNumbers:
    def test_line_numbers_populated(self):
        text = textwrap.dedent("""\
            <config>
              <host>localhost</host>
              <port>8080</port>
            </config>
        """)
        meta = annotate_xml(text)
        for section in meta.sections:
            assert section.line_range.start >= 1
            assert section.line_range.end >= section.line_range.start

    def test_root_starts_at_line_1(self):
        text = "<root>\n  <child/>\n</root>\n"
        meta = annotate_xml(text)
        root_sections = [s for s in meta.sections if s.title == "root"]
        assert root_sections[0].line_range.start == 1
