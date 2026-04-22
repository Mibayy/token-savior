"""Tests for the SQL migrations parser."""

from __future__ import annotations

import os
import textwrap

from token_savior.db_schema import get_db_schema


def _write(tmp_path, rel: str, content: str) -> None:
    path = tmp_path / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(textwrap.dedent(content).lstrip())


class TestAutoDetect:
    def test_no_migrations_dir(self, tmp_path):
        result = get_db_schema(str(tmp_path))
        assert result["ok"] is False
        assert "No migrations directory found" in result["error"]

    def test_auto_detect_supabase(self, tmp_path):
        _write(tmp_path, "supabase/migrations/001_init.sql",
               "CREATE TABLE users (id uuid PRIMARY KEY, email text NOT NULL);")
        result = get_db_schema(str(tmp_path))
        assert result["ok"] is True
        assert result["migrations_dir"] == os.path.join("supabase", "migrations")
        assert "users" in result["tables"]

    def test_explicit_dir(self, tmp_path):
        _write(tmp_path, "custom/001.sql",
               "CREATE TABLE x (id int);")
        result = get_db_schema(str(tmp_path), migrations_dir="custom")
        assert result["ok"] is True
        assert "x" in result["tables"]


class TestCreateTable:
    def test_columns(self, tmp_path):
        _write(tmp_path, "migrations/001.sql", """
            CREATE TABLE users (
              id uuid PRIMARY KEY,
              email text NOT NULL,
              name text,
              created_at timestamptz DEFAULT now()
            );
        """)
        result = get_db_schema(str(tmp_path), migrations_dir="migrations")
        tbl = result["tables"]["users"]
        cols = {c["name"]: c for c in tbl["columns"]}
        assert cols["id"]["type"].lower() == "uuid"
        assert cols["email"]["nullable"] is False
        assert cols["name"]["nullable"] is True
        assert "now()" in (cols["created_at"]["default"] or "").lower()
        assert tbl["primary_key"] == ["id"]

    def test_foreign_key_inline(self, tmp_path):
        _write(tmp_path, "migrations/001.sql", """
            CREATE TABLE posts (
              id uuid PRIMARY KEY,
              author_id uuid REFERENCES users(id)
            );
        """)
        result = get_db_schema(str(tmp_path), migrations_dir="migrations")
        fks = result["tables"]["posts"]["foreign_keys"]
        assert len(fks) == 1
        assert fks[0]["table"] == "users"
        assert fks[0]["cols"] == ["author_id"]

    def test_composite_primary_key(self, tmp_path):
        _write(tmp_path, "migrations/001.sql", """
            CREATE TABLE follows (
              follower uuid,
              followed uuid,
              PRIMARY KEY (follower, followed)
            );
        """)
        result = get_db_schema(str(tmp_path), migrations_dir="migrations")
        assert result["tables"]["follows"]["primary_key"] == ["follower", "followed"]


class TestAlterTable:
    def test_add_column(self, tmp_path):
        _write(tmp_path, "migrations/001.sql", "CREATE TABLE users (id uuid PRIMARY KEY);")
        _write(tmp_path, "migrations/002.sql",
               "ALTER TABLE users ADD COLUMN email text NOT NULL;")
        result = get_db_schema(str(tmp_path), migrations_dir="migrations")
        cols = {c["name"]: c for c in result["tables"]["users"]["columns"]}
        assert "email" in cols
        assert cols["email"]["nullable"] is False

    def test_drop_column(self, tmp_path):
        _write(tmp_path, "migrations/001.sql",
               "CREATE TABLE users (id uuid PRIMARY KEY, legacy text);")
        _write(tmp_path, "migrations/002.sql",
               "ALTER TABLE users DROP COLUMN legacy;")
        result = get_db_schema(str(tmp_path), migrations_dir="migrations")
        cols = [c["name"] for c in result["tables"]["users"]["columns"]]
        assert "legacy" not in cols
        assert "id" in cols

    def test_enable_rls(self, tmp_path):
        _write(tmp_path, "migrations/001.sql",
               "CREATE TABLE docs (id uuid PRIMARY KEY);")
        _write(tmp_path, "migrations/002.sql",
               "ALTER TABLE docs ENABLE ROW LEVEL SECURITY;")
        result = get_db_schema(str(tmp_path), migrations_dir="migrations")
        assert result["tables"]["docs"]["enabled_rls"] is True


class TestIndexAndPolicy:
    def test_create_index(self, tmp_path):
        _write(tmp_path, "migrations/001.sql", """
            CREATE TABLE users (id uuid PRIMARY KEY, email text);
            CREATE UNIQUE INDEX idx_users_email ON users (email);
        """)
        result = get_db_schema(str(tmp_path), migrations_dir="migrations")
        indexes = result["tables"]["users"]["indexes"]
        assert len(indexes) == 1
        assert indexes[0]["unique"] is True
        assert indexes[0]["cols"] == ["email"]

    def test_create_policy(self, tmp_path):
        _write(tmp_path, "migrations/001.sql", """
            CREATE TABLE docs (id uuid PRIMARY KEY);
            CREATE POLICY "docs_read" ON docs FOR SELECT TO authenticated USING (true);
        """)
        result = get_db_schema(str(tmp_path), migrations_dir="migrations")
        policies = result["tables"]["docs"]["rls_policies"]
        assert len(policies) == 1
        assert policies[0]["name"] == "docs_read"
        assert policies[0]["command"] == "SELECT"
        assert "authenticated" in policies[0]["roles"]


class TestFiltering:
    def test_tables_filter(self, tmp_path):
        _write(tmp_path, "migrations/001.sql", """
            CREATE TABLE a (x int);
            CREATE TABLE b (y int);
            CREATE TABLE c (z int);
        """)
        result = get_db_schema(
            str(tmp_path), migrations_dir="migrations", tables=["a", "c"]
        )
        assert set(result["tables"].keys()) == {"a", "c"}


class TestRobustness:
    def test_ignores_comments(self, tmp_path):
        _write(tmp_path, "migrations/001.sql", """
            -- this is a comment
            /* block comment
               CREATE TABLE ghost (id int);
            */
            CREATE TABLE real (id int);
        """)
        result = get_db_schema(str(tmp_path), migrations_dir="migrations")
        assert "real" in result["tables"]
        assert "ghost" not in result["tables"]

    def test_drop_table(self, tmp_path):
        _write(tmp_path, "migrations/001.sql", "CREATE TABLE tmp (id int);")
        _write(tmp_path, "migrations/002.sql", "DROP TABLE tmp;")
        result = get_db_schema(str(tmp_path), migrations_dir="migrations")
        assert "tmp" not in result["tables"]

    def test_quoted_identifiers(self, tmp_path):
        _write(tmp_path, "migrations/001.sql",
               'CREATE TABLE "weird-name" (id int);')
        result = get_db_schema(str(tmp_path), migrations_dir="migrations")
        assert "weird-name" in result["tables"]

    def test_unknown_dialect_warning(self, tmp_path):
        _write(tmp_path, "migrations/001.sql", "CREATE TABLE t (id int);")
        result = get_db_schema(
            str(tmp_path), migrations_dir="migrations", dialect="mysql"
        )
        assert result["ok"] is True
        assert any("dialect" in w for w in result["warnings"])
