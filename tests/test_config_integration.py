"""End-to-end integration tests for the config analysis pipeline.

These tests exercise the full stack: real files on disk → ProjectIndexer →
analyze_config → formatted report string.
"""

import textwrap

import pytest

from token_savior.project_indexer import ProjectIndexer
from token_savior.config_analyzer import analyze_config


class TestFullPipeline:
    """test_full_pipeline: mini project with .env, config.yaml, and app.py."""

    @pytest.fixture
    def mini_project(self, tmp_path):
        (tmp_path / ".env").write_text(
            "DB_HOST=localhost\nDB_PORT=5432\nOLD_KEY=unused\nSECRET=sk-abcdefghijklmnop1234567890\n"
        )
        (tmp_path / "config.yaml").write_text(
            "database:\n  host: localhost\n  port: 5432\ntimeout: 30\ntimeotu: 30\n"
        )
        (tmp_path / "app.py").write_text(
            textwrap.dedent("""\
                import os
                db_host = os.environ["DB_HOST"]
                db_port = os.getenv("DB_PORT")
                missing = os.environ["STRIPE_KEY"]
            """)
        )
        return tmp_path

    def test_full_pipeline(self, mini_project):
        index = ProjectIndexer(
            str(mini_project),
            include_patterns=["**/*.py", "**/*.yaml", "**/.env", "**/.env.*"],
        ).index()

        result = analyze_config(index)

        # Header is always present when config files exist
        assert "Config Analysis" in result

        # Duplicate/typo detection: "timeotu" is a near-duplicate of "timeout"
        assert "timeotu" in result or "similar" in result.lower()

        # Secret detection: SECRET key has an sk- prefixed value
        assert "SECRET" in result or "sk-" in result

        # Orphan detection: OLD_KEY is defined in .env but never used in code
        assert "OLD_KEY" in result

        # Ghost key detection: STRIPE_KEY is used in code but not in any config
        assert "STRIPE_KEY" in result


class TestNoConfigFiles:
    """test_no_config_files: only a .py file, no config files indexed."""

    def test_no_config_files(self, tmp_path):
        (tmp_path / "app.py").write_text("print('hello')\n")

        index = ProjectIndexer(
            str(tmp_path),
            include_patterns=["**/*.py"],
        ).index()

        result = analyze_config(index)

        assert "0 config" in result.lower()
