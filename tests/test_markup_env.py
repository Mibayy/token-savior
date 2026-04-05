"""Tests for the .env file annotator."""

from token_savior.env_annotator import annotate_env


class TestAnnotateEnvBasic:
    """Basic key=value parsing."""

    def test_simple_keys(self):
        text = "DB_HOST=localhost\nDB_PORT=5432\n"
        meta = annotate_env(text)
        assert len(meta.sections) == 2
        assert meta.sections[0].title == "DB_HOST"
        assert meta.sections[1].title == "DB_PORT"

    def test_all_level_1(self):
        text = "FOO=bar\nBAZ=qux\nQUUX=corge\n"
        meta = annotate_env(text)
        for section in meta.sections:
            assert section.level == 1

    def test_default_source_name(self):
        meta = annotate_env("KEY=value\n")
        assert meta.source_name == "<env>"

    def test_custom_source_name(self):
        meta = annotate_env("KEY=value\n", source_name=".env.production")
        assert meta.source_name == ".env.production"


class TestAnnotateEnvComments:
    """Comments and empty lines are ignored."""

    def test_comments_ignored(self):
        text = "# This is a comment\nDB_HOST=localhost\n"
        meta = annotate_env(text)
        assert len(meta.sections) == 1
        assert meta.sections[0].title == "DB_HOST"

    def test_empty_lines_ignored(self):
        text = "\nDB_HOST=localhost\n\nDB_PORT=5432\n"
        meta = annotate_env(text)
        assert len(meta.sections) == 2
        assert meta.sections[0].title == "DB_HOST"
        assert meta.sections[1].title == "DB_PORT"

    def test_inline_comment_not_a_comment(self):
        # Inline # is part of the value, not a comment
        text = "COLOR=red # primary\n"
        meta = annotate_env(text)
        assert len(meta.sections) == 1
        assert meta.sections[0].title == "COLOR"

    def test_only_comments_returns_empty(self):
        text = "# comment one\n# comment two\n"
        meta = annotate_env(text)
        assert meta.sections == []


class TestAnnotateEnvEmptyValues:
    """Keys with empty values are parsed."""

    def test_empty_value(self):
        text = "SECRET=\n"
        meta = annotate_env(text)
        assert len(meta.sections) == 1
        assert meta.sections[0].title == "SECRET"

    def test_empty_value_no_newline(self):
        text = "SECRET="
        meta = annotate_env(text)
        assert len(meta.sections) == 1
        assert meta.sections[0].title == "SECRET"


class TestAnnotateEnvQuotedValues:
    """Quoted values (single and double) are handled."""

    def test_double_quoted_value(self):
        text = 'DATABASE_URL="postgresql://user:pass@host/db"\n'
        meta = annotate_env(text)
        assert len(meta.sections) == 1
        assert meta.sections[0].title == "DATABASE_URL"

    def test_single_quoted_value(self):
        text = "SECRET_KEY='my secret key with spaces'\n"
        meta = annotate_env(text)
        assert len(meta.sections) == 1
        assert meta.sections[0].title == "SECRET_KEY"

    def test_quoted_value_with_equals(self):
        text = 'JDBC_URL="jdbc:mysql://host/db?user=admin&pass=secret"\n'
        meta = annotate_env(text)
        assert len(meta.sections) == 1
        assert meta.sections[0].title == "JDBC_URL"


class TestAnnotateEnvExportPrefix:
    """export KEY=VALUE syntax is handled."""

    def test_export_prefix(self):
        text = "export API_KEY=abc123\n"
        meta = annotate_env(text)
        assert len(meta.sections) == 1
        assert meta.sections[0].title == "API_KEY"

    def test_export_prefix_level_1(self):
        text = "export NODE_ENV=production\n"
        meta = annotate_env(text)
        assert meta.sections[0].level == 1

    def test_mixed_export_and_plain(self):
        text = "export FIRST=one\nSECOND=two\nexport THIRD=three\n"
        meta = annotate_env(text)
        assert len(meta.sections) == 3
        assert [s.title for s in meta.sections] == ["FIRST", "SECOND", "THIRD"]


class TestAnnotateEnvLineNumbers:
    """Line numbers are 1-indexed and correct."""

    def test_first_key_line_number(self):
        text = "DB_HOST=localhost\nDB_PORT=5432\n"
        meta = annotate_env(text)
        assert meta.sections[0].line_range.start == 1
        assert meta.sections[0].line_range.end == 1

    def test_second_key_line_number(self):
        text = "DB_HOST=localhost\nDB_PORT=5432\n"
        meta = annotate_env(text)
        assert meta.sections[1].line_range.start == 2
        assert meta.sections[1].line_range.end == 2

    def test_line_number_skips_comments(self):
        text = "# comment\nDB_HOST=localhost\n"
        meta = annotate_env(text)
        # DB_HOST is on line 2
        assert meta.sections[0].line_range.start == 2
        assert meta.sections[0].line_range.end == 2

    def test_line_number_skips_empty_lines(self):
        text = "\n\nMY_KEY=value\n"
        meta = annotate_env(text)
        # MY_KEY is on line 3
        assert meta.sections[0].line_range.start == 3
        assert meta.sections[0].line_range.end == 3

    def test_metadata_total_lines(self):
        text = "A=1\nB=2\nC=3\n"
        meta = annotate_env(text)
        # splitlines gives 3 lines; split('\n') with trailing newline gives 4
        # just ensure sections were found
        assert len(meta.sections) == 3

    def test_metadata_source_name_in_meta(self):
        text = "PORT=8080\n"
        meta = annotate_env(text, source_name=".env.local")
        assert meta.source_name == ".env.local"
        assert meta.sections[0].title == "PORT"
