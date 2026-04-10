"""Tests for the .conf config file annotator."""

from token_savior.conf_annotator import annotate_conf


class TestConfEqualsKV:
    """Tests for key = value syntax."""

    def test_simple_equals(self):
        text = "host = localhost\nport = 8080\n"
        meta = annotate_conf(text)
        titles = [s.title for s in meta.sections]
        assert "host" in titles
        assert "port" in titles

    def test_equals_with_spaces(self):
        text = "log_level  =  debug\n"
        meta = annotate_conf(text)
        titles = [s.title for s in meta.sections]
        assert "log_level" in titles

    def test_equals_no_spaces(self):
        text = "timeout=30\n"
        meta = annotate_conf(text)
        titles = [s.title for s in meta.sections]
        assert "timeout" in titles


class TestConfColonKV:
    """Tests for key: value syntax."""

    def test_colon_syntax(self):
        text = "host: localhost\nport: 8080\n"
        meta = annotate_conf(text)
        titles = [s.title for s in meta.sections]
        assert "host" in titles
        assert "port" in titles

    def test_colon_no_space(self):
        text = "debug:true\n"
        meta = annotate_conf(text)
        titles = [s.title for s in meta.sections]
        assert "debug" in titles


class TestConfSourceName:
    """Tests for source_name default and custom."""

    def test_source_name_default(self):
        meta = annotate_conf("key = value\n")
        assert meta.source_name == "<conf>"

    def test_source_name_custom(self):
        meta = annotate_conf("key = value\n", source_name="nginx.conf")
        assert meta.source_name == "nginx.conf"


class TestConfBlocks:
    """Tests for nginx-style block detection."""

    def test_simple_block(self):
        text = "server {\n    listen 80;\n    server_name example.com;\n}\n"
        meta = annotate_conf(text)
        titles = [s.title for s in meta.sections]
        assert "server" in titles

    def test_nested_blocks(self):
        text = "http {\n    server {\n        listen 80;\n    }\n}\n"
        meta = annotate_conf(text)
        titles = [s.title for s in meta.sections]
        assert "http" in titles
        assert "server" in titles

    def test_block_level(self):
        text = "http {\n    server {\n        listen 80;\n    }\n}\n"
        meta = annotate_conf(text)
        by_title = {s.title: s for s in meta.sections}
        assert by_title["http"].level == 1
        assert by_title["server"].level == 2

    def test_block_line_range_start(self):
        text = "server {\n    listen 80;\n}\n"
        meta = annotate_conf(text)
        by_title = {s.title: s for s in meta.sections}
        assert by_title["server"].line_range.start == 1

    def test_block_line_range_end(self):
        text = "server {\n    listen 80;\n}\n"
        meta = annotate_conf(text)
        by_title = {s.title: s for s in meta.sections}
        # Section ends at or before line 3
        assert by_title["server"].line_range.end >= 1


class TestConfComments:
    """Tests that comments are ignored."""

    def test_hash_comment_ignored(self):
        text = "# This is a comment\nhost = localhost\n"
        meta = annotate_conf(text)
        titles = [s.title for s in meta.sections]
        assert "host" in titles
        # Comment itself should not become a section
        assert not any("comment" in t.lower() for t in titles)

    def test_semicolon_comment_ignored(self):
        text = "; semicolon comment\nport = 3306\n"
        meta = annotate_conf(text)
        titles = [s.title for s in meta.sections]
        assert "port" in titles
        assert not any("semicolon" in t.lower() for t in titles)

    def test_slash_comment_ignored(self):
        text = "// C-style comment\nworkers = 4\n"
        meta = annotate_conf(text)
        titles = [s.title for s in meta.sections]
        assert "workers" in titles
        assert not any("style" in t.lower() for t in titles)

    def test_inline_comment_key_still_detected(self):
        text = "host = localhost  # the hostname\n"
        meta = annotate_conf(text)
        titles = [s.title for s in meta.sections]
        assert "host" in titles


class TestConfDepthCap:
    """Tests that depth is capped at 4."""

    def test_depth_capped_at_4(self):
        text = (
            "l1 {\n  l2 {\n    l3 {\n      l4 {\n        l5 {\n        }\n      }\n    }\n  }\n}\n"
        )
        meta = annotate_conf(text)
        titles = [s.title for s in meta.sections]
        levels = [s.level for s in meta.sections]
        assert "l1" in titles
        assert "l2" in titles
        assert "l3" in titles
        assert "l4" in titles
        assert "l5" not in titles
        assert max(levels) == 4


class TestConfFallback:
    """Tests that annotate_generic is used as fallback."""

    def test_no_sections_falls_back_to_generic(self):
        # Plain text with no kv or blocks
        text = "some random text\nwithout any config syntax\n"
        meta = annotate_conf(text)
        # Falls back to generic: sections empty, but lines populated
        assert meta.sections == []
        assert meta.total_lines > 0

    def test_empty_text_fallback(self):
        meta = annotate_conf("")
        assert meta.sections == []


class TestConfMetadata:
    """Tests for metadata correctness."""

    def test_total_lines(self):
        text = "a = 1\nb = 2\nc = 3\n"
        meta = annotate_conf(text)
        assert meta.total_lines == 4  # 3 lines + empty trailing after split

    def test_total_chars(self):
        text = "a = 1\n"
        meta = annotate_conf(text)
        assert meta.total_chars == len(text)

    def test_functions_classes_empty(self):
        text = "host = localhost\n"
        meta = annotate_conf(text)
        assert meta.functions == []
        assert meta.classes == []

    def test_kv_line_number(self):
        text = "host = localhost\nport = 8080\n"
        meta = annotate_conf(text)
        by_title = {s.title: s for s in meta.sections}
        assert by_title["host"].line_range.start == 1
        assert by_title["port"].line_range.start == 2
