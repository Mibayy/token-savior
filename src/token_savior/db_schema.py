"""SQL migration parser -- condensed schema snapshot for agents.

Motivation: agents working on Supabase/Postgres projects re-read raw
`supabase/migrations/*.sql` (or `migrations/*.sql`) every time they need to
write a query, which burns 2-8k tokens of redundant DDL per session. This
module walks a migrations directory, applies CREATE/ALTER statements in
filename order, and returns a compact JSON view of the resulting schema.

Scope: PostgreSQL-flavored DDL (covers Supabase, RLS policies). We parse
with regex instead of a full SQL grammar because: (1) migrations are
machine-generated and stable, (2) we only need structural facts (not exec
plans), (3) no external dependency.

What we extract per table:
  - columns: name, type, nullable, default
  - primary_key
  - foreign_keys
  - indexes (inline + CREATE INDEX statements)
  - rls_policies (Supabase-specific, filtered per table)
  - enabled_rls (bool)

What we skip: triggers, functions, views, materialized views, DO blocks.
"""

from __future__ import annotations

import os
import re
from typing import Any


# ---------------------------------------------------------------------------
# Regex patterns. All case-insensitive, MULTILINE, DOTALL where relevant.
# ---------------------------------------------------------------------------

_RE_CREATE_TABLE = re.compile(
    r"create\s+table\s+(?:if\s+not\s+exists\s+)?"
    r"(?:\"([^\"]+)\"|([a-zA-Z_][a-zA-Z0-9_.]*))"
    r"\s*\((.*?)\)\s*;",
    re.IGNORECASE | re.DOTALL,
)

_RE_ALTER_ADD_COLUMN = re.compile(
    r"alter\s+table\s+(?:if\s+exists\s+)?"
    r"(?:\"([^\"]+)\"|([a-zA-Z_][a-zA-Z0-9_.]*))"
    r"\s+add\s+(?:column\s+)?(?:if\s+not\s+exists\s+)?"
    r"(?:\"([^\"]+)\"|([a-zA-Z_][a-zA-Z0-9_]*))"
    r"\s+(.*?);",
    re.IGNORECASE | re.DOTALL,
)

_RE_ALTER_ENABLE_RLS = re.compile(
    r"alter\s+table\s+(?:\"([^\"]+)\"|([a-zA-Z_][a-zA-Z0-9_.]*))"
    r"\s+enable\s+row\s+level\s+security\s*;",
    re.IGNORECASE,
)

_RE_ALTER_DROP_COLUMN = re.compile(
    r"alter\s+table\s+(?:if\s+exists\s+)?"
    r"(?:\"([^\"]+)\"|([a-zA-Z_][a-zA-Z0-9_.]*))"
    r"\s+drop\s+(?:column\s+)?(?:if\s+exists\s+)?"
    r"(?:\"([^\"]+)\"|([a-zA-Z_][a-zA-Z0-9_]*))"
    r".*?;",
    re.IGNORECASE | re.DOTALL,
)

_RE_DROP_TABLE = re.compile(
    r"drop\s+table\s+(?:if\s+exists\s+)?"
    r"(?:\"([^\"]+)\"|([a-zA-Z_][a-zA-Z0-9_.]*))"
    r"\s*(?:cascade|restrict)?\s*;",
    re.IGNORECASE,
)

_RE_CREATE_INDEX = re.compile(
    r"create\s+(?:unique\s+)?index\s+(?:concurrently\s+)?"
    r"(?:if\s+not\s+exists\s+)?"
    r"(?:\"([^\"]+)\"|([a-zA-Z_][a-zA-Z0-9_]*))"
    r"\s+on\s+(?:\"([^\"]+)\"|([a-zA-Z_][a-zA-Z0-9_.]*))"
    r"\s*(?:using\s+\w+\s*)?"
    r"\(([^)]*)\)\s*;",
    re.IGNORECASE | re.DOTALL,
)

_RE_CREATE_POLICY = re.compile(
    r"create\s+policy\s+(?:\"([^\"]+)\"|([a-zA-Z_][a-zA-Z0-9_]*))"
    r"\s+on\s+(?:\"([^\"]+)\"|([a-zA-Z_][a-zA-Z0-9_.]*))"
    r"\s+(.*?);",
    re.IGNORECASE | re.DOTALL,
)


# ---------------------------------------------------------------------------
# Column parser (for body of CREATE TABLE)
# ---------------------------------------------------------------------------

_COLUMN_CONSTRAINT_WORDS = (
    "primary",
    "unique",
    "references",
    "check",
    "foreign",
    "constraint",
    "exclude",
)


def _split_top_level(body: str) -> list[str]:
    """Split a CREATE TABLE body by commas that sit at paren depth 0."""
    parts: list[str] = []
    depth = 0
    buf: list[str] = []
    in_string = False
    for ch in body:
        if ch == "'" and (not buf or buf[-1] != "\\"):
            in_string = not in_string
        if in_string:
            buf.append(ch)
            continue
        if ch == "(":
            depth += 1
            buf.append(ch)
        elif ch == ")":
            depth -= 1
            buf.append(ch)
        elif ch == "," and depth == 0:
            parts.append("".join(buf).strip())
            buf = []
        else:
            buf.append(ch)
    if buf:
        tail = "".join(buf).strip()
        if tail:
            parts.append(tail)
    return parts


def _strip_comments(sql: str) -> str:
    """Remove -- line comments and /* */ block comments."""
    sql = re.sub(r"--[^\n]*", "", sql)
    sql = re.sub(r"/\*.*?\*/", "", sql, flags=re.DOTALL)
    return sql


def _unquote(s: str) -> str:
    s = s.strip()
    if s.startswith('"') and s.endswith('"'):
        return s[1:-1]
    return s


def _parse_column_line(line: str) -> dict[str, Any] | None:
    """Parse a single column definition line.

    Returns None if the line is a table-level constraint (PRIMARY KEY (...),
    FOREIGN KEY (...), etc.).
    """
    stripped = line.strip()
    if not stripped:
        return None
    lower = stripped.lower()
    # Table-level constraints start with a keyword, not a column name.
    for kw in ("primary key", "foreign key", "unique (", "unique(",
               "constraint ", "check (", "check(", "exclude "):
        if lower.startswith(kw):
            return None
    # First token = column name (may be quoted).
    if stripped.startswith('"'):
        end = stripped.find('"', 1)
        if end == -1:
            return None
        name = stripped[1:end]
        rest = stripped[end + 1:].strip()
    else:
        m = re.match(r"([a-zA-Z_][a-zA-Z0-9_]*)\s+(.*)", stripped, re.DOTALL)
        if not m:
            return None
        name = m.group(1)
        rest = m.group(2)
    # Type = tokens up to first constraint keyword or end.
    type_tokens: list[str] = []
    tokens = re.split(r"(\s+|\([^)]*\))", rest)
    i = 0
    while i < len(tokens):
        tok = tokens[i]
        if tok is None or not tok.strip():
            i += 1
            continue
        low = tok.lower().strip()
        if low in ("not", "null", "default", "primary", "unique", "references",
                   "check", "constraint", "generated", "collate"):
            break
        type_tokens.append(tok.strip())
        i += 1
    col_type = " ".join(t for t in type_tokens if t).strip()
    # Scan remaining tokens for constraints.
    trailing = rest[len(" ".join(type_tokens)):].strip()
    low_trail = trailing.lower()
    nullable = "not null" not in low_trail
    default: str | None = None
    m_def = re.search(r"default\s+([^,]+?)(?:\s+not\s+null|\s+primary|\s+unique|\s+references|\s*$)", trailing, re.IGNORECASE)
    if m_def:
        default = m_def.group(1).strip()
    is_pk = bool(re.search(r"\bprimary\s+key\b", low_trail))
    fk: dict[str, str] | None = None
    m_fk = re.search(
        r"references\s+(?:\"([^\"]+)\"|([a-zA-Z_][a-zA-Z0-9_.]*))\s*"
        r"(?:\(\s*(?:\"([^\"]+)\"|([a-zA-Z_][a-zA-Z0-9_]*))\s*\))?",
        trailing,
        re.IGNORECASE,
    )
    if m_fk:
        ref_table = m_fk.group(1) or m_fk.group(2)
        ref_col = m_fk.group(3) or m_fk.group(4) or ""
        fk = {"table": ref_table, "column": ref_col}
    return {
        "name": name,
        "type": col_type,
        "nullable": nullable,
        "default": default,
        "primary_key": is_pk,
        "fk": fk,
    }


def _parse_table_constraints(body: str) -> dict[str, Any]:
    """Extract table-level PRIMARY KEY, FOREIGN KEY, UNIQUE constraints."""
    out: dict[str, Any] = {"pk_cols": [], "fks": [], "uniques": []}
    for line in _split_top_level(body):
        low = line.lower().strip()
        # PRIMARY KEY (col1, col2)
        m_pk = re.match(r"(?:constraint\s+\S+\s+)?primary\s+key\s*\(([^)]+)\)", low)
        if m_pk:
            cols = [c.strip().strip('"') for c in m_pk.group(1).split(",")]
            out["pk_cols"] = cols
            continue
        # FOREIGN KEY (col) REFERENCES other(col)
        m_fk = re.match(
            r"(?:constraint\s+\S+\s+)?foreign\s+key\s*\(([^)]+)\)\s*"
            r"references\s+([a-zA-Z_][a-zA-Z0-9_.]*)\s*(?:\(([^)]+)\))?",
            low,
        )
        if m_fk:
            cols = [c.strip().strip('"') for c in m_fk.group(1).split(",")]
            ref_table = m_fk.group(2)
            ref_cols = [c.strip().strip('"') for c in m_fk.group(3).split(",")] if m_fk.group(3) else []
            out["fks"].append({"cols": cols, "table": ref_table, "ref_cols": ref_cols})
            continue
        # UNIQUE (col1, col2)
        m_u = re.match(r"(?:constraint\s+\S+\s+)?unique\s*\(([^)]+)\)", low)
        if m_u:
            cols = [c.strip().strip('"') for c in m_u.group(1).split(",")]
            out["uniques"].append(cols)
    return out


# ---------------------------------------------------------------------------
# Main parser
# ---------------------------------------------------------------------------


class SchemaBuilder:
    def __init__(self) -> None:
        self.tables: dict[str, dict[str, Any]] = {}

    def _ensure(self, name: str) -> dict[str, Any]:
        if name not in self.tables:
            self.tables[name] = {
                "columns": [],
                "primary_key": [],
                "foreign_keys": [],
                "uniques": [],
                "indexes": [],
                "rls_policies": [],
                "enabled_rls": False,
            }
        return self.tables[name]

    def apply_create_table(self, name: str, body: str) -> None:
        tbl = self._ensure(name)
        # Re-create fresh: CREATE TABLE on an existing table is rare in
        # migrations, but if present it redefines.
        tbl["columns"] = []
        tbl["primary_key"] = []
        tbl["foreign_keys"] = []
        tbl["uniques"] = []
        for line in _split_top_level(body):
            col = _parse_column_line(line)
            if col is not None:
                tbl["columns"].append({
                    "name": col["name"],
                    "type": col["type"],
                    "nullable": col["nullable"],
                    "default": col["default"],
                })
                if col["primary_key"]:
                    tbl["primary_key"] = [col["name"]]
                if col["fk"]:
                    tbl["foreign_keys"].append({
                        "cols": [col["name"]],
                        "table": col["fk"]["table"],
                        "ref_cols": [col["fk"]["column"]] if col["fk"]["column"] else [],
                    })
        constraints = _parse_table_constraints(body)
        if constraints["pk_cols"]:
            tbl["primary_key"] = constraints["pk_cols"]
        tbl["foreign_keys"].extend(constraints["fks"])
        tbl["uniques"].extend(constraints["uniques"])

    def apply_add_column(self, table: str, col_name: str, col_def: str) -> None:
        tbl = self._ensure(table)
        synthetic = f"{col_name} {col_def}"
        parsed = _parse_column_line(synthetic)
        if parsed is None:
            return
        # Replace if column exists, else append.
        for i, existing in enumerate(tbl["columns"]):
            if existing["name"] == parsed["name"]:
                tbl["columns"][i] = {
                    "name": parsed["name"],
                    "type": parsed["type"],
                    "nullable": parsed["nullable"],
                    "default": parsed["default"],
                }
                break
        else:
            tbl["columns"].append({
                "name": parsed["name"],
                "type": parsed["type"],
                "nullable": parsed["nullable"],
                "default": parsed["default"],
            })
        if parsed["primary_key"]:
            tbl["primary_key"] = [parsed["name"]]
        if parsed["fk"]:
            tbl["foreign_keys"].append({
                "cols": [parsed["name"]],
                "table": parsed["fk"]["table"],
                "ref_cols": [parsed["fk"]["column"]] if parsed["fk"]["column"] else [],
            })

    def apply_drop_column(self, table: str, col_name: str) -> None:
        if table not in self.tables:
            return
        self.tables[table]["columns"] = [
            c for c in self.tables[table]["columns"] if c["name"] != col_name
        ]

    def apply_drop_table(self, name: str) -> None:
        self.tables.pop(name, None)

    def apply_enable_rls(self, table: str) -> None:
        self._ensure(table)["enabled_rls"] = True

    def apply_create_index(self, idx_name: str, table: str, cols_raw: str, unique: bool) -> None:
        tbl = self._ensure(table)
        cols = [c.strip().strip('"') for c in cols_raw.split(",") if c.strip()]
        tbl["indexes"].append({"name": idx_name, "cols": cols, "unique": unique})

    def apply_create_policy(self, policy_name: str, table: str, rest: str) -> None:
        tbl = self._ensure(table)
        rest_low = rest.lower()
        # Extract the command (FOR <cmd>) and role (TO <role>) if present.
        m_cmd = re.search(r"\bfor\s+(select|insert|update|delete|all)\b", rest_low)
        m_to = re.search(r"\bto\s+([a-zA-Z_, ]+?)(?:\s+using|\s+with|$)", rest_low)
        tbl["rls_policies"].append({
            "name": policy_name,
            "command": m_cmd.group(1).upper() if m_cmd else "ALL",
            "roles": [r.strip() for r in m_to.group(1).split(",")] if m_to else ["public"],
        })


def _apply_statements(sql: str, builder: SchemaBuilder) -> None:
    clean = _strip_comments(sql)
    # Normalize: strip schema qualifier like public.table -> table (keep dotted
    # only if it's clearly a cross-schema reference; for now we strip `public.`
    # to match common Supabase convention).
    for m in _RE_CREATE_TABLE.finditer(clean):
        name = (m.group(1) or m.group(2) or "").removeprefix("public.")
        builder.apply_create_table(name, m.group(3))
    for m in _RE_ALTER_ADD_COLUMN.finditer(clean):
        table = (m.group(1) or m.group(2) or "").removeprefix("public.")
        col_name = m.group(3) or m.group(4) or ""
        col_def = m.group(5) or ""
        if table and col_name:
            builder.apply_add_column(table, col_name, col_def)
    for m in _RE_ALTER_DROP_COLUMN.finditer(clean):
        table = (m.group(1) or m.group(2) or "").removeprefix("public.")
        col_name = m.group(3) or m.group(4) or ""
        if table and col_name:
            builder.apply_drop_column(table, col_name)
    for m in _RE_ALTER_ENABLE_RLS.finditer(clean):
        table = (m.group(1) or m.group(2) or "").removeprefix("public.")
        if table:
            builder.apply_enable_rls(table)
    for m in _RE_DROP_TABLE.finditer(clean):
        name = (m.group(1) or m.group(2) or "").removeprefix("public.")
        if name:
            builder.apply_drop_table(name)
    for m in _RE_CREATE_INDEX.finditer(clean):
        idx_name = m.group(1) or m.group(2) or ""
        table = (m.group(3) or m.group(4) or "").removeprefix("public.")
        cols = m.group(5) or ""
        unique = bool(re.match(r"create\s+unique", m.group(0), re.IGNORECASE))
        if table:
            builder.apply_create_index(idx_name, table, cols, unique)
    for m in _RE_CREATE_POLICY.finditer(clean):
        pol_name = m.group(1) or m.group(2) or ""
        table = (m.group(3) or m.group(4) or "").removeprefix("public.")
        rest = m.group(5) or ""
        if table and pol_name:
            builder.apply_create_policy(pol_name, table, rest)


# ---------------------------------------------------------------------------
# Discovery + public entry point
# ---------------------------------------------------------------------------


_DEFAULT_MIGRATION_DIRS = (
    "supabase/migrations",
    "migrations",
    "db/migrations",
    "prisma/migrations",
)


def _find_migrations_dir(project_root: str) -> str | None:
    for rel in _DEFAULT_MIGRATION_DIRS:
        candidate = os.path.join(project_root, rel)
        if os.path.isdir(candidate):
            return candidate
    return None


def _collect_sql_files(migrations_dir: str) -> list[str]:
    files: list[str] = []
    for dirpath, _dirnames, filenames in os.walk(migrations_dir):
        for fname in filenames:
            if fname.endswith(".sql"):
                files.append(os.path.join(dirpath, fname))
    files.sort()
    return files


def get_db_schema(
    project_root: str,
    *,
    migrations_dir: str | None = None,
    dialect: str = "postgres",
    tables: list[str] | None = None,
) -> dict[str, Any]:
    """Parse SQL migrations and return a condensed schema snapshot.

    Args:
        project_root: Absolute path to the project.
        migrations_dir: Relative or absolute path to the migrations dir.
            If None, auto-detect among common conventions.
        dialect: Reserved for future multi-dialect support. Only 'postgres'
            is implemented.
        tables: Optional filter -- if provided, only these tables are in
            the response (indexes/policies still scoped to them).

    Returns:
        {
            "ok": bool,
            "migrations_dir": str | None,
            "files_scanned": int,
            "tables": {table_name: {...}},
            "warnings": [str, ...]  # e.g. unknown dialect, no dir found
        }
    """
    warnings: list[str] = []
    if dialect != "postgres":
        warnings.append(f"dialect '{dialect}' not implemented, treating as postgres")

    if migrations_dir is None:
        mig_dir = _find_migrations_dir(project_root)
    else:
        mig_dir = (
            migrations_dir
            if os.path.isabs(migrations_dir)
            else os.path.join(project_root, migrations_dir)
        )
        if not os.path.isdir(mig_dir):
            return {
                "ok": False,
                "error": f"migrations_dir not found: {mig_dir}",
                "migrations_dir": None,
                "files_scanned": 0,
                "tables": {},
                "warnings": warnings,
            }

    if mig_dir is None:
        return {
            "ok": False,
            "error": (
                "No migrations directory found. Tried: "
                + ", ".join(_DEFAULT_MIGRATION_DIRS)
                + ". Pass migrations_dir='<rel path>' explicitly."
            ),
            "migrations_dir": None,
            "files_scanned": 0,
            "tables": {},
            "warnings": warnings,
        }

    files = _collect_sql_files(mig_dir)
    builder = SchemaBuilder()
    for path in files:
        try:
            with open(path, encoding="utf-8") as f:
                _apply_statements(f.read(), builder)
        except (OSError, UnicodeDecodeError) as exc:
            warnings.append(f"skip {os.path.basename(path)}: {exc}")

    tables_out = builder.tables
    if tables is not None:
        keep = set(tables)
        tables_out = {k: v for k, v in tables_out.items() if k in keep}

    return {
        "ok": True,
        "migrations_dir": os.path.relpath(mig_dir, project_root),
        "files_scanned": len(files),
        "tables": tables_out,
        "warnings": warnings,
    }
