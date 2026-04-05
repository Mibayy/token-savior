"""Tests for INI/properties config annotator."""

from token_savior.ini_annotator import annotate_ini


class TestIniBasic:
    def test_simple_keys_with_default_section(self):
        text = "[DEFAULT]\nfoo = bar\nbaz = qux\n"
        meta = annotate_ini(text, "config.ini")
        titles = [s.title for s in meta.sections]
        assert "DEFAULT" in titles
        assert "foo" in titles
        assert "baz" in titles

    def test_source_name_default(self):
        text = "[section]\nkey = value\n"
        meta = annotate_ini(text)
        assert meta.source_name == "<ini>"

    def test_source_name_custom(self):
        text = "[section]\nkey = value\n"
        meta = annotate_ini(text, "myconfig.ini")
        assert meta.source_name == "myconfig.ini"

    def test_section_headers_are_level_1(self):
        text = "[database]\nhost = localhost\n\n[cache]\nbackend = redis\n"
        meta = annotate_ini(text, "app.ini")
        section_headers = [s for s in meta.sections if s.level == 1]
        titles = [s.title for s in section_headers]
        assert "database" in titles
        assert "cache" in titles

    def test_keys_under_sections_are_level_2(self):
        text = "[database]\nhost = localhost\nport = 5432\n"
        meta = annotate_ini(text, "app.ini")
        level2 = [s for s in meta.sections if s.level == 2]
        titles = [s.title for s in level2]
        assert "host" in titles
        assert "port" in titles

    def test_multiple_sections(self):
        text = "[database]\nhost = localhost\n\n[cache]\nbackend = memcached\ntimeout = 30\n"
        meta = annotate_ini(text, "app.ini")
        section_titles = {s.title for s in meta.sections if s.level == 1}
        assert "database" in section_titles
        assert "cache" in section_titles
        key_titles = {s.title for s in meta.sections if s.level == 2}
        assert "host" in key_titles
        assert "backend" in key_titles
        assert "timeout" in key_titles

    def test_line_range_populated(self):
        text = "[section]\nkey = value\n"
        meta = annotate_ini(text, "config.ini")
        for s in meta.sections:
            assert s.line_range.start >= 1
            assert s.line_range.end >= s.line_range.start

    def test_functions_classes_imports_empty(self):
        text = "[section]\nkey = value\n"
        meta = annotate_ini(text, "config.ini")
        assert meta.functions == []
        assert meta.classes == []
        assert meta.imports == []


class TestIniDefaultSection:
    def test_default_section_with_keys(self):
        text = "[DEFAULT]\nlog_level = INFO\nmax_retries = 3\n"
        meta = annotate_ini(text, "config.ini")
        titles = {s.title for s in meta.sections}
        assert "DEFAULT" in titles
        assert "log_level" in titles
        assert "max_retries" in titles

    def test_default_section_level_1(self):
        text = "[DEFAULT]\nlog_level = INFO\n"
        meta = annotate_ini(text, "config.ini")
        default_sections = [s for s in meta.sections if s.title == "DEFAULT"]
        assert len(default_sections) == 1
        assert default_sections[0].level == 1


class TestPropertiesFormat:
    def test_properties_basic(self):
        text = "db.host=localhost\ndb.port=5432\n"
        meta = annotate_ini(text, "app.properties")
        titles = {s.title for s in meta.sections}
        assert "db.host" in titles
        assert "db.port" in titles

    def test_properties_all_level_2(self):
        text = "key1=value1\nkey2=value2\n"
        meta = annotate_ini(text, "app.properties")
        for s in meta.sections:
            assert s.level == 2

    def test_properties_skips_comments_hash(self):
        text = "# This is a comment\nkey=value\n"
        meta = annotate_ini(text, "app.properties")
        titles = {s.title for s in meta.sections}
        assert "key" in titles
        # Comment line should not appear as a section
        assert not any(t.startswith("#") for t in titles)

    def test_properties_skips_comments_exclamation(self):
        text = "! Another comment\nkey=value\n"
        meta = annotate_ini(text, "app.properties")
        titles = {s.title for s in meta.sections}
        assert "key" in titles
        assert not any(t.startswith("!") for t in titles)

    def test_properties_skips_empty_lines(self):
        text = "key1=value1\n\nkey2=value2\n"
        meta = annotate_ini(text, "app.properties")
        titles = {s.title for s in meta.sections}
        assert "key1" in titles
        assert "key2" in titles

    def test_properties_source_name_detection(self):
        text = "key=value\n"
        meta = annotate_ini(text, "config.properties")
        assert meta.source_name == "config.properties"
        # Should be treated as properties format (no section headers at level 1)
        level1 = [s for s in meta.sections if s.level == 1]
        assert level1 == []

    def test_properties_with_equals_in_value(self):
        text = "encoded=a=b=c\n"
        meta = annotate_ini(text, "app.properties")
        titles = {s.title for s in meta.sections}
        assert "encoded" in titles

    def test_properties_line_range(self):
        text = "key1=value1\nkey2=value2\n"
        meta = annotate_ini(text, "app.properties")
        for s in meta.sections:
            assert s.line_range.start >= 1
            assert s.line_range.end >= s.line_range.start


class TestIniEdgeCases:
    def test_empty_file(self):
        meta = annotate_ini("", "config.ini")
        assert meta.total_lines == 0 or meta.total_lines >= 0
        assert meta.sections == []

    def test_empty_properties_file(self):
        meta = annotate_ini("", "app.properties")
        assert meta.sections == []

    def test_invalid_ini_falls_back_to_generic(self):
        # A file that configparser cannot parse at all
        text = "this is not\n\x00\x01\x02valid ini content with null bytes"
        # Should not raise; falls back gracefully
        meta = annotate_ini(text, "broken.ini")
        assert meta.source_name == "broken.ini"

    def test_total_lines(self):
        text = "[section]\nkey = value\n"
        meta = annotate_ini(text, "config.ini")
        assert meta.total_lines == len(text.splitlines())

    def test_total_chars(self):
        text = "[section]\nkey = value\n"
        meta = annotate_ini(text, "config.ini")
        assert meta.total_chars == len(text)
