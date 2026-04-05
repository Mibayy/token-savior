"""Tests for the TOML annotator."""

import textwrap

import pytest

from token_savior.toml_annotator import annotate_toml


class TestTomlSimpleKeys:
    """Tests for simple top-level key-value entries."""

    def test_simple_keys_present(self):
        text = textwrap.dedent("""\
            name = "myapp"
            version = "1.0.0"
        """)
        meta = annotate_toml(text)
        titles = [s.title for s in meta.sections]
        assert "name" in titles
        assert "version" in titles

    def test_simple_keys_level_1(self):
        text = textwrap.dedent("""\
            name = "myapp"
            version = "1.0.0"
            debug = true
        """)
        meta = annotate_toml(text)
        assert all(s.level == 1 for s in meta.sections)

    def test_line_numbers_populated(self):
        text = textwrap.dedent("""\
            name = "myapp"
            version = "1.0.0"
        """)
        meta = annotate_toml(text)
        name_sec = next(s for s in meta.sections if s.title == "name")
        assert name_sec.line_range.start == 1  # "name" is on line 1

    def test_version_line_number(self):
        text = textwrap.dedent("""\
            name = "myapp"
            version = "1.0.0"
        """)
        meta = annotate_toml(text)
        ver_sec = next(s for s in meta.sections if s.title == "version")
        assert ver_sec.line_range.start == 2

    def test_source_name_default(self):
        meta = annotate_toml("name = 'x'")
        assert meta.source_name == "<toml>"

    def test_source_name_custom(self):
        meta = annotate_toml("name = 'x'", source_name="pyproject.toml")
        assert meta.source_name == "pyproject.toml"

    def test_total_lines(self):
        text = "a = 1\nb = 2\nc = 3\n"
        meta = annotate_toml(text)
        assert meta.total_lines == 3

    def test_total_chars(self):
        text = "a = 1\n"
        meta = annotate_toml(text)
        assert meta.total_chars == len(text)

    def test_functions_and_classes_empty(self):
        meta = annotate_toml("a = 1\n")
        assert meta.functions == []
        assert meta.classes == []


class TestTomlTables:
    """Tests for TOML table sections ([header])."""

    def test_table_creates_section(self):
        text = textwrap.dedent("""\
            [database]
            host = "localhost"
            port = 5432
        """)
        meta = annotate_toml(text)
        titles = [s.title for s in meta.sections]
        assert "database" in titles

    def test_table_key_level_1(self):
        text = textwrap.dedent("""\
            [database]
            host = "localhost"
        """)
        meta = annotate_toml(text)
        db = next(s for s in meta.sections if s.title == "database")
        assert db.level == 1

    def test_table_children_level_2(self):
        text = textwrap.dedent("""\
            [database]
            host = "localhost"
            port = 5432
        """)
        meta = annotate_toml(text)
        host = next(s for s in meta.sections if s.title == "host")
        assert host.level == 2

    def test_table_line_number(self):
        text = textwrap.dedent("""\
            name = "app"

            [database]
            host = "localhost"
        """)
        meta = annotate_toml(text)
        db = next(s for s in meta.sections if s.title == "database")
        assert db.line_range.start == 3

    def test_table_child_line_number(self):
        text = textwrap.dedent("""\
            [database]
            host = "localhost"
            port = 5432
        """)
        meta = annotate_toml(text)
        host_sec = next(s for s in meta.sections if s.title == "host")
        assert host_sec.line_range.start == 2


class TestTomlNestedTables:
    """Tests for nested TOML tables."""

    def test_nested_table_creates_section(self):
        text = textwrap.dedent("""\
            [server]
            host = "0.0.0.0"

            [server.ssl]
            enabled = true
            cert = "/etc/ssl/cert.pem"
        """)
        meta = annotate_toml(text)
        titles = [s.title for s in meta.sections]
        assert "server" in titles
        assert "ssl" in titles

    def test_nested_table_levels(self):
        text = textwrap.dedent("""\
            [server]
            host = "0.0.0.0"

            [server.ssl]
            enabled = true
        """)
        meta = annotate_toml(text)
        server = next(s for s in meta.sections if s.title == "server")
        ssl = next(s for s in meta.sections if s.title == "ssl")
        assert server.level == 1
        assert ssl.level == 2

    def test_deeply_nested_table_level(self):
        text = textwrap.dedent("""\
            [a]
            x = 1

            [a.b]
            y = 2

            [a.b.c]
            z = 3
        """)
        meta = annotate_toml(text)
        a_sec = next(s for s in meta.sections if s.title == "a")
        b_sec = next(s for s in meta.sections if s.title == "b")
        c_sec = next(s for s in meta.sections if s.title == "c")
        assert a_sec.level == 1
        assert b_sec.level == 2
        assert c_sec.level == 3

    def test_depth_cap_at_4(self):
        """Sections beyond depth 4 should not appear."""
        text = textwrap.dedent("""\
            [a]
            k1 = 1

            [a.b]
            k2 = 2

            [a.b.c]
            k3 = 3

            [a.b.c.d]
            k4 = 4

            [a.b.c.d.e]
            k5 = 5
        """)
        meta = annotate_toml(text)
        levels = [s.level for s in meta.sections]
        assert max(levels) <= 4

    def test_key_within_nested_table_has_correct_level(self):
        text = textwrap.dedent("""\
            [server.ssl]
            enabled = true
        """)
        meta = annotate_toml(text)
        enabled = next(s for s in meta.sections if s.title == "enabled")
        # ssl is at level 2, so enabled should be at level 3
        assert enabled.level == 3


class TestTomlInvalidFallback:
    """Test that invalid TOML falls back to annotate_generic."""

    def test_invalid_toml_returns_metadata(self):
        bad = "this is not = = valid toml ]["
        meta = annotate_toml(bad)
        # Should not raise and should return StructuralMetadata
        assert meta is not None
        assert meta.total_chars == len(bad)

    def test_invalid_toml_source_name_preserved(self):
        bad = "[[invalid"
        meta = annotate_toml(bad, source_name="broken.toml")
        assert meta.source_name == "broken.toml"

    def test_invalid_toml_sections_empty(self):
        """Generic fallback produces no sections."""
        bad = "not valid toml ]["
        meta = annotate_toml(bad)
        assert meta.sections == []
