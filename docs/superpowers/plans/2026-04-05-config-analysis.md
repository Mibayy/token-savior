# Config File Analysis Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add config file parsing (YAML, TOML, INI, ENV, XML, HCL, conf) and an `analyze_config` MCP tool that detects duplicate keys, hardcoded secrets, and orphan config entries.

**Architecture:** New annotators follow the existing pattern (one file per format, returning `StructuralMetadata`). A new `config_analyzer.py` module handles the three analysis checks. One new tool entry in `server.py`. Dependencies kept optional where possible (PyYAML required, pyhcl2 optional).

**Tech Stack:** Python 3.11+, stdlib (`tomllib`, `configparser`, `xml.etree.ElementTree`, `math`, `re`), PyYAML, pyhcl2 (optional)

**Spec:** `docs/superpowers/specs/2026-04-05-config-analysis-design.md`

---

### Task 1: YAML Annotator

**Files:**
- Create: `src/token_savior/yaml_annotator.py`
- Create: `tests/test_markup_yaml.py`

- [ ] **Step 1: Write failing tests**

```python
"""Tests for the YAML annotator."""

import pytest
from token_savior.yaml_annotator import annotate_yaml


class TestYamlBasicKeys:
    def test_simple_keys(self):
        text = "name: test\nversion: '1.0'"
        meta = annotate_yaml(text)
        titles = [s.title for s in meta.sections]
        assert "name" in titles
        assert "version" in titles

    def test_all_sections_level_1(self):
        text = "a: 1\nb: 2\nc: 3"
        meta = annotate_yaml(text)
        assert all(s.level == 1 for s in meta.sections)

    def test_source_name_default(self):
        meta = annotate_yaml("")
        assert meta.source_name == "<yaml>"

    def test_source_name_custom(self):
        meta = annotate_yaml("", source_name="config.yaml")
        assert meta.source_name == "config.yaml"


class TestYamlNested:
    def test_nested_keys_increase_level(self):
        text = "outer:\n  inner: value"
        meta = annotate_yaml(text)
        outer = next(s for s in meta.sections if s.title == "outer")
        inner = next(s for s in meta.sections if s.title == "inner")
        assert outer.level == 1
        assert inner.level == 2

    def test_three_levels(self):
        text = "a:\n  b:\n    c: val"
        meta = annotate_yaml(text)
        levels = {s.title: s.level for s in meta.sections}
        assert levels["a"] == 1
        assert levels["b"] == 2
        assert levels["c"] == 3

    def test_depth_capped_at_4(self):
        text = "a:\n  b:\n    c:\n      d:\n        e: val"
        meta = annotate_yaml(text)
        assert all(s.level <= 4 for s in meta.sections)


class TestYamlArrays:
    def test_named_array_items(self):
        text = "services:\n  - name: web\n    port: 80\n  - name: api\n    port: 8080"
        meta = annotate_yaml(text)
        titles = [s.title for s in meta.sections]
        assert any("web" in t for t in titles)
        assert any("api" in t for t in titles)


class TestYamlInvalid:
    def test_invalid_yaml_fallback(self):
        text = ":\n  - [invalid\nyaml: {{{"
        meta = annotate_yaml(text)
        # Should not crash, fallback to generic
        assert meta.total_lines > 0


class TestYamlLineNumbers:
    def test_line_numbers_populated(self):
        text = "name: test\nversion: '1.0'\ndescription: hello"
        meta = annotate_yaml(text)
        name_section = next(s for s in meta.sections if s.title == "name")
        assert name_section.line_range.start == 1
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd /root/token-savior && python -m pytest tests/test_markup_yaml.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'token_savior.yaml_annotator'`

- [ ] **Step 3: Install PyYAML**

Run: `cd /root/token-savior && pip install PyYAML`

- [ ] **Step 4: Write yaml_annotator.py**

```python
"""YAML config file annotator.

Parses YAML documents and extracts keys as SectionInfo entries.
Keys at each nesting level become sections with level = depth.
Array items with distinguishing fields (name/id/type) get labeled.
Depth capped at 4 to avoid noise.
Falls back to generic annotator on parse failure.
"""

import yaml

from token_savior.generic_annotator import annotate_generic
from token_savior.models import ImportInfo, LineRange, SectionInfo, StructuralMetadata

_MAX_DEPTH = 4
_DISTINGUISHING_FIELDS = ("name", "id", "type", "key", "title")


def _build_line_offsets(lines: list[str]) -> list[int]:
    offsets: list[int] = []
    pos = 0
    for line in lines:
        offsets.append(pos)
        pos += len(line) + 1
    return offsets


def _find_key_line(lines: list[str], key: str, start_from: int) -> int:
    """Find the 1-indexed line number where key appears, searching from start_from."""
    needle = f"{key}:"
    for i in range(start_from, len(lines)):
        stripped = lines[i].lstrip()
        if stripped.startswith(needle) or stripped.startswith(f'"{key}"') or stripped.startswith(f"'{key}'"):
            return i + 1
    # Fallback: search from beginning
    for i in range(min(start_from, len(lines))):
        stripped = lines[i].lstrip()
        if stripped.startswith(needle) or stripped.startswith(f'"{key}"') or stripped.startswith(f"'{key}'"):
            return i + 1
    return start_from + 1


def _walk_structure(
    obj: object,
    lines: list[str],
    path: str,
    depth: int,
    sections: list[SectionInfo],
    line_hint: int,
) -> None:
    if depth > _MAX_DEPTH:
        return

    if isinstance(obj, dict):
        for key, value in obj.items():
            str_key = str(key)
            key_line = _find_key_line(lines, str_key, line_hint)
            sections.append(
                SectionInfo(
                    title=str_key,
                    level=depth,
                    line_range=LineRange(start=key_line, end=key_line),
                )
            )
            _walk_structure(
                value, lines, f"{path}.{str_key}", depth + 1,
                sections, max(0, key_line - 1),
            )
    elif isinstance(obj, list):
        for i, item in enumerate(obj):
            if isinstance(item, dict):
                label = None
                for field in _DISTINGUISHING_FIELDS:
                    if field in item and isinstance(item[field], str):
                        label = item[field]
                        break
                if label is not None:
                    parent_name = path.rsplit(".", 1)[-1] if "." in path else path
                    entry_title = f"{parent_name}[{i}] {label}"
                    entry_line = _find_key_line(lines, label, line_hint)
                    sections.append(
                        SectionInfo(
                            title=entry_title,
                            level=depth,
                            line_range=LineRange(start=entry_line, end=entry_line),
                        )
                    )
                _walk_structure(
                    item, lines, f"{path}[{i}]", depth + 1,
                    sections, line_hint,
                )


def annotate_yaml(text: str, source_name: str = "<yaml>") -> StructuralMetadata:
    """Parse YAML text and extract structural metadata.

    Keys at each nesting level become SectionInfo(title=key, level=depth).
    Depth capped at 4. Invalid YAML falls back to annotate_generic().
    """
    try:
        parsed = yaml.safe_load(text)
    except yaml.YAMLError:
        return annotate_generic(text, source_name)

    if not isinstance(parsed, (dict, list)):
        return annotate_generic(text, source_name)

    lines = text.split("\n")
    total_lines = len(lines)
    total_chars = len(text)
    line_offsets = _build_line_offsets(lines)
    sections: list[SectionInfo] = []

    _walk_structure(parsed, lines, "", 1, sections, 0)

    return StructuralMetadata(
        source_name=source_name,
        total_lines=total_lines,
        total_chars=total_chars,
        lines=lines,
        line_char_offsets=line_offsets,
        sections=sections,
    )
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `cd /root/token-savior && python -m pytest tests/test_markup_yaml.py -v`
Expected: All PASS

- [ ] **Step 6: Commit**

```bash
cd /root/token-savior
git add src/token_savior/yaml_annotator.py tests/test_markup_yaml.py
git commit -m "feat: add YAML config annotator"
```

---

### Task 2: TOML Annotator

**Files:**
- Create: `src/token_savior/toml_annotator.py`
- Create: `tests/test_markup_toml.py`

- [ ] **Step 1: Write failing tests**

```python
"""Tests for the TOML annotator."""

from token_savior.toml_annotator import annotate_toml


class TestTomlBasicKeys:
    def test_simple_keys(self):
        text = 'name = "test"\nversion = "1.0"'
        meta = annotate_toml(text)
        titles = [s.title for s in meta.sections]
        assert "name" in titles
        assert "version" in titles

    def test_source_name_default(self):
        meta = annotate_toml("")
        assert meta.source_name == "<toml>"


class TestTomlTables:
    def test_table_creates_section(self):
        text = '[database]\nhost = "localhost"\nport = 5432'
        meta = annotate_toml(text)
        titles = [s.title for s in meta.sections]
        assert "database" in titles
        assert "host" in titles

    def test_nested_tables(self):
        text = '[server]\nhost = "0.0.0.0"\n\n[server.ssl]\ncert = "/path"'
        meta = annotate_toml(text)
        levels = {s.title: s.level for s in meta.sections}
        assert levels["server"] == 1
        assert levels["ssl"] == 2


class TestTomlInvalid:
    def test_invalid_toml_fallback(self):
        text = "[invalid\ntoml = {{{"
        meta = annotate_toml(text)
        assert meta.total_lines > 0
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd /root/token-savior && python -m pytest tests/test_markup_toml.py -v`
Expected: FAIL

- [ ] **Step 3: Write toml_annotator.py**

```python
"""TOML config file annotator.

Uses stdlib tomllib (Python 3.11+). Keys and tables become SectionInfo entries.
Falls back to generic annotator on parse failure.
"""

import tomllib

from token_savior.generic_annotator import annotate_generic
from token_savior.models import LineRange, SectionInfo, StructuralMetadata

_MAX_DEPTH = 4


def _build_line_offsets(lines: list[str]) -> list[int]:
    offsets: list[int] = []
    pos = 0
    for line in lines:
        offsets.append(pos)
        pos += len(line) + 1
    return offsets


def _find_key_line(lines: list[str], key: str, start_from: int) -> int:
    for i in range(start_from, len(lines)):
        stripped = lines[i].lstrip()
        if stripped.startswith(f"{key} =") or stripped.startswith(f"{key}="):
            return i + 1
        if stripped.startswith(f"[{key}]") or stripped.startswith(f"[") and key in stripped:
            return i + 1
    for i in range(min(start_from, len(lines))):
        stripped = lines[i].lstrip()
        if stripped.startswith(f"{key} =") or stripped.startswith(f"{key}="):
            return i + 1
    return start_from + 1


def _walk_structure(
    obj: object,
    lines: list[str],
    depth: int,
    sections: list[SectionInfo],
    line_hint: int,
) -> None:
    if depth > _MAX_DEPTH:
        return
    if isinstance(obj, dict):
        for key, value in obj.items():
            key_line = _find_key_line(lines, key, line_hint)
            sections.append(
                SectionInfo(
                    title=key,
                    level=depth,
                    line_range=LineRange(start=key_line, end=key_line),
                )
            )
            if isinstance(value, dict):
                _walk_structure(value, lines, depth + 1, sections, max(0, key_line - 1))


def annotate_toml(text: str, source_name: str = "<toml>") -> StructuralMetadata:
    """Parse TOML text and extract structural metadata."""
    try:
        parsed = tomllib.loads(text)
    except (tomllib.TOMLDecodeError, ValueError):
        return annotate_generic(text, source_name)

    lines = text.split("\n")
    line_offsets = _build_line_offsets(lines)
    sections: list[SectionInfo] = []

    _walk_structure(parsed, lines, 1, sections, 0)

    return StructuralMetadata(
        source_name=source_name,
        total_lines=len(lines),
        total_chars=len(text),
        lines=lines,
        line_char_offsets=line_offsets,
        sections=sections,
    )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd /root/token-savior && python -m pytest tests/test_markup_toml.py -v`
Expected: All PASS

- [ ] **Step 5: Commit**

```bash
cd /root/token-savior
git add src/token_savior/toml_annotator.py tests/test_markup_toml.py
git commit -m "feat: add TOML config annotator"
```

---

### Task 3: INI / Properties Annotator

**Files:**
- Create: `src/token_savior/ini_annotator.py`
- Create: `tests/test_markup_ini.py`

- [ ] **Step 1: Write failing tests**

```python
"""Tests for the INI/properties annotator."""

from token_savior.ini_annotator import annotate_ini


class TestIniBasicKeys:
    def test_simple_keys(self):
        text = "[DEFAULT]\nhost = localhost\nport = 5432"
        meta = annotate_ini(text)
        titles = [s.title for s in meta.sections]
        assert "DEFAULT" in titles
        assert "host" in titles

    def test_source_name_default(self):
        meta = annotate_ini("")
        assert meta.source_name == "<ini>"


class TestIniSections:
    def test_section_headers(self):
        text = "[database]\nhost = localhost\n\n[cache]\nttl = 300"
        meta = annotate_ini(text)
        titles = [s.title for s in meta.sections]
        assert "database" in titles
        assert "cache" in titles

    def test_keys_under_sections(self):
        text = "[database]\nhost = localhost\nport = 5432"
        meta = annotate_ini(text)
        section_levels = {s.title: s.level for s in meta.sections}
        assert section_levels["database"] == 1
        assert section_levels["host"] == 2
        assert section_levels["port"] == 2


class TestProperties:
    def test_java_properties(self):
        text = "app.name=MyApp\napp.version=1.0\ndb.host=localhost"
        meta = annotate_ini(text, source_name="app.properties")
        titles = [s.title for s in meta.sections]
        assert "app.name" in titles
        assert "db.host" in titles


class TestIniInvalid:
    def test_empty_file(self):
        meta = annotate_ini("")
        assert meta.total_lines >= 0
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd /root/token-savior && python -m pytest tests/test_markup_ini.py -v`
Expected: FAIL

- [ ] **Step 3: Write ini_annotator.py**

```python
"""INI/CFG/Properties config file annotator.

Handles:
- Standard INI files (configparser)
- Java .properties files (key=value without sections)
Falls back to generic annotator on parse failure.
"""

import configparser
import io
import re

from token_savior.generic_annotator import annotate_generic
from token_savior.models import LineRange, SectionInfo, StructuralMetadata


def _build_line_offsets(lines: list[str]) -> list[int]:
    offsets: list[int] = []
    pos = 0
    for line in lines:
        offsets.append(pos)
        pos += len(line) + 1
    return offsets


def _find_line(lines: list[str], needle: str, start_from: int) -> int:
    for i in range(start_from, len(lines)):
        if needle in lines[i]:
            return i + 1
    for i in range(min(start_from, len(lines))):
        if needle in lines[i]:
            return i + 1
    return start_from + 1


def _is_properties_file(source_name: str) -> bool:
    return source_name.endswith(".properties")


def _parse_properties(text: str, source_name: str) -> StructuralMetadata:
    """Parse Java .properties format (key=value, no sections)."""
    lines = text.split("\n")
    line_offsets = _build_line_offsets(lines)
    sections: list[SectionInfo] = []

    for i, line in enumerate(lines):
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or stripped.startswith("!"):
            continue
        match = re.match(r"^([^=:]+)[=:](.*)", stripped)
        if match:
            key = match.group(1).strip()
            sections.append(
                SectionInfo(title=key, level=1, line_range=LineRange(start=i + 1, end=i + 1))
            )

    return StructuralMetadata(
        source_name=source_name,
        total_lines=len(lines),
        total_chars=len(text),
        lines=lines,
        line_char_offsets=line_offsets,
        sections=sections,
    )


def annotate_ini(text: str, source_name: str = "<ini>") -> StructuralMetadata:
    """Parse INI/CFG text and extract structural metadata."""
    if _is_properties_file(source_name):
        return _parse_properties(text, source_name)

    lines = text.split("\n")
    line_offsets = _build_line_offsets(lines)
    sections: list[SectionInfo] = []

    parser = configparser.ConfigParser(interpolation=None)
    try:
        parser.read_string(text)
    except configparser.Error:
        return annotate_generic(text, source_name)

    for section_name in parser.sections():
        section_line = _find_line(lines, f"[{section_name}]", 0)
        sections.append(
            SectionInfo(title=section_name, level=1, line_range=LineRange(start=section_line, end=section_line))
        )
        for key in parser.options(section_name):
            if key in configparser.DEFAULTSECT.lower():
                continue
            key_line = _find_line(lines, key, section_line - 1)
            sections.append(
                SectionInfo(title=key, level=2, line_range=LineRange(start=key_line, end=key_line))
            )

    # Also capture DEFAULT section if it has keys
    if parser.defaults():
        default_line = _find_line(lines, "[DEFAULT]", 0)
        sections.insert(0,
            SectionInfo(title="DEFAULT", level=1, line_range=LineRange(start=default_line, end=default_line))
        )
        for key in parser.defaults():
            key_line = _find_line(lines, key, default_line - 1)
            sections.insert(len([s for s in sections if s.title == "DEFAULT"]),
                SectionInfo(title=key, level=2, line_range=LineRange(start=key_line, end=key_line))
            )

    return StructuralMetadata(
        source_name=source_name,
        total_lines=len(lines),
        total_chars=len(text),
        lines=lines,
        line_char_offsets=line_offsets,
        sections=sections,
    )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd /root/token-savior && python -m pytest tests/test_markup_ini.py -v`
Expected: All PASS

- [ ] **Step 5: Commit**

```bash
cd /root/token-savior
git add src/token_savior/ini_annotator.py tests/test_markup_ini.py
git commit -m "feat: add INI/properties config annotator"
```

---

### Task 4: ENV Annotator

**Files:**
- Create: `src/token_savior/env_annotator.py`
- Create: `tests/test_markup_env.py`

- [ ] **Step 1: Write failing tests**

```python
"""Tests for the .env annotator."""

from token_savior.env_annotator import annotate_env


class TestEnvBasicKeys:
    def test_simple_keys(self):
        text = "DB_HOST=localhost\nDB_PORT=5432"
        meta = annotate_env(text)
        titles = [s.title for s in meta.sections]
        assert "DB_HOST" in titles
        assert "DB_PORT" in titles

    def test_all_level_1(self):
        text = "A=1\nB=2\nC=3"
        meta = annotate_env(text)
        assert all(s.level == 1 for s in meta.sections)

    def test_source_name_default(self):
        meta = annotate_env("")
        assert meta.source_name == "<env>"

    def test_comments_ignored(self):
        text = "# This is a comment\nDB_HOST=localhost\n# Another comment"
        meta = annotate_env(text)
        titles = [s.title for s in meta.sections]
        assert len(titles) == 1
        assert "DB_HOST" in titles

    def test_empty_values(self):
        text = "EMPTY_VAR=\nNON_EMPTY=hello"
        meta = annotate_env(text)
        titles = [s.title for s in meta.sections]
        assert "EMPTY_VAR" in titles
        assert "NON_EMPTY" in titles

    def test_quoted_values(self):
        text = 'SECRET="my secret value"\nTOKEN=\'another\''
        meta = annotate_env(text)
        titles = [s.title for s in meta.sections]
        assert "SECRET" in titles
        assert "TOKEN" in titles

    def test_export_prefix(self):
        text = "export API_KEY=abc123\nexport DB_URL=postgres://localhost"
        meta = annotate_env(text)
        titles = [s.title for s in meta.sections]
        assert "API_KEY" in titles
        assert "DB_URL" in titles

    def test_line_numbers(self):
        text = "FIRST=1\nSECOND=2\nTHIRD=3"
        meta = annotate_env(text)
        first = next(s for s in meta.sections if s.title == "FIRST")
        third = next(s for s in meta.sections if s.title == "THIRD")
        assert first.line_range.start == 1
        assert third.line_range.start == 3
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd /root/token-savior && python -m pytest tests/test_markup_env.py -v`
Expected: FAIL

- [ ] **Step 3: Write env_annotator.py**

```python
"""Dotenv (.env) file annotator.

Parses KEY=VALUE lines. Handles comments, export prefix, quoted values.
Each key becomes a SectionInfo at level 1.
"""

import re

from token_savior.models import LineRange, SectionInfo, StructuralMetadata

_ENV_LINE_RE = re.compile(
    r"^(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=(.*)"
)


def _build_line_offsets(lines: list[str]) -> list[int]:
    offsets: list[int] = []
    pos = 0
    for line in lines:
        offsets.append(pos)
        pos += len(line) + 1
    return offsets


def annotate_env(text: str, source_name: str = "<env>") -> StructuralMetadata:
    """Parse .env text and extract keys as SectionInfo entries."""
    lines = text.split("\n")
    line_offsets = _build_line_offsets(lines)
    sections: list[SectionInfo] = []

    for i, line in enumerate(lines):
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        match = _ENV_LINE_RE.match(stripped)
        if match:
            key = match.group(1)
            sections.append(
                SectionInfo(
                    title=key,
                    level=1,
                    line_range=LineRange(start=i + 1, end=i + 1),
                )
            )

    return StructuralMetadata(
        source_name=source_name,
        total_lines=len(lines),
        total_chars=len(text),
        lines=lines,
        line_char_offsets=line_offsets,
        sections=sections,
    )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd /root/token-savior && python -m pytest tests/test_markup_env.py -v`
Expected: All PASS

- [ ] **Step 5: Commit**

```bash
cd /root/token-savior
git add src/token_savior/env_annotator.py tests/test_markup_env.py
git commit -m "feat: add .env config annotator"
```

---

### Task 5: XML Annotator

**Files:**
- Create: `src/token_savior/xml_annotator.py`
- Create: `tests/test_markup_xml.py`

- [ ] **Step 1: Write failing tests**

```python
"""Tests for the XML annotator."""

from token_savior.xml_annotator import annotate_xml


class TestXmlBasicElements:
    def test_simple_elements(self):
        text = "<config>\n  <host>localhost</host>\n  <port>5432</port>\n</config>"
        meta = annotate_xml(text)
        titles = [s.title for s in meta.sections]
        assert "config" in titles
        assert "host" in titles
        assert "port" in titles

    def test_source_name_default(self):
        meta = annotate_xml("<root/>")
        assert meta.source_name == "<xml>"

    def test_nested_levels(self):
        text = "<root>\n  <db>\n    <host>localhost</host>\n  </db>\n</root>"
        meta = annotate_xml(text)
        levels = {s.title: s.level for s in meta.sections}
        assert levels["root"] == 1
        assert levels["db"] == 2
        assert levels["host"] == 3

    def test_depth_capped_at_4(self):
        text = "<a><b><c><d><e>val</e></d></c></b></a>"
        meta = annotate_xml(text)
        assert all(s.level <= 4 for s in meta.sections)


class TestXmlAttributes:
    def test_attributes_in_title(self):
        text = '<server name="web" port="80"/>'
        meta = annotate_xml(text)
        titles = [s.title for s in meta.sections]
        assert any("web" in t for t in titles)


class TestXmlInvalid:
    def test_invalid_xml_fallback(self):
        text = "<invalid><no-close>"
        meta = annotate_xml(text)
        assert meta.total_lines > 0
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd /root/token-savior && python -m pytest tests/test_markup_xml.py -v`
Expected: FAIL

- [ ] **Step 3: Write xml_annotator.py**

```python
"""XML/plist config file annotator.

Uses stdlib xml.etree.ElementTree. Elements become SectionInfo entries.
Attributes with name/id/type get included in the section title.
Depth capped at 4.
Falls back to generic annotator on parse failure.
"""

import xml.etree.ElementTree as ET

from token_savior.generic_annotator import annotate_generic
from token_savior.models import LineRange, SectionInfo, StructuralMetadata

_MAX_DEPTH = 4
_DISTINGUISHING_ATTRS = ("name", "id", "type", "key", "title")


def _build_line_offsets(lines: list[str]) -> list[int]:
    offsets: list[int] = []
    pos = 0
    for line in lines:
        offsets.append(pos)
        pos += len(line) + 1
    return offsets


def _find_tag_line(lines: list[str], tag: str, start_from: int) -> int:
    for i in range(start_from, len(lines)):
        if f"<{tag}" in lines[i] or f"<{tag}>" in lines[i]:
            return i + 1
    return start_from + 1


def _walk_element(
    elem: ET.Element,
    lines: list[str],
    depth: int,
    sections: list[SectionInfo],
    line_hint: int,
) -> None:
    if depth > _MAX_DEPTH:
        return

    tag = elem.tag.split("}")[-1] if "}" in elem.tag else elem.tag  # strip namespace
    label = None
    for attr in _DISTINGUISHING_ATTRS:
        if attr in elem.attrib:
            label = elem.attrib[attr]
            break

    title = f"{tag} {label}" if label else tag
    tag_line = _find_tag_line(lines, tag, line_hint)

    sections.append(
        SectionInfo(title=title, level=depth, line_range=LineRange(start=tag_line, end=tag_line))
    )

    for child in elem:
        _walk_element(child, lines, depth + 1, sections, max(0, tag_line - 1))


def annotate_xml(text: str, source_name: str = "<xml>") -> StructuralMetadata:
    """Parse XML text and extract elements as SectionInfo entries."""
    try:
        root = ET.fromstring(text)
    except ET.ParseError:
        return annotate_generic(text, source_name)

    lines = text.split("\n")
    line_offsets = _build_line_offsets(lines)
    sections: list[SectionInfo] = []

    _walk_element(root, lines, 1, sections, 0)

    return StructuralMetadata(
        source_name=source_name,
        total_lines=len(lines),
        total_chars=len(text),
        lines=lines,
        line_char_offsets=line_offsets,
        sections=sections,
    )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd /root/token-savior && python -m pytest tests/test_markup_xml.py -v`
Expected: All PASS

- [ ] **Step 5: Commit**

```bash
cd /root/token-savior
git add src/token_savior/xml_annotator.py tests/test_markup_xml.py
git commit -m "feat: add XML/plist config annotator"
```

---

### Task 6: HCL Annotator

**Files:**
- Create: `src/token_savior/hcl_annotator.py`
- Create: `tests/test_markup_hcl.py`

- [ ] **Step 1: Write failing tests**

```python
"""Tests for the HCL/Terraform annotator."""

from token_savior.hcl_annotator import annotate_hcl


class TestHclBlocks:
    def test_resource_block(self):
        text = 'resource "aws_instance" "web" {\n  ami = "abc-123"\n  instance_type = "t2.micro"\n}'
        meta = annotate_hcl(text)
        titles = [s.title for s in meta.sections]
        assert any("aws_instance" in t for t in titles)

    def test_variable_block(self):
        text = 'variable "region" {\n  default = "us-east-1"\n}'
        meta = annotate_hcl(text)
        titles = [s.title for s in meta.sections]
        assert any("region" in t for t in titles)

    def test_source_name_default(self):
        meta = annotate_hcl("")
        assert meta.source_name == "<hcl>"

    def test_key_value_pairs(self):
        text = 'resource "aws_instance" "web" {\n  ami = "abc"\n  instance_type = "t2.micro"\n}'
        meta = annotate_hcl(text)
        titles = [s.title for s in meta.sections]
        assert "ami" in titles
        assert "instance_type" in titles


class TestHclNested:
    def test_nested_blocks(self):
        text = 'resource "aws_instance" "web" {\n  provisioner "local-exec" {\n    command = "echo hello"\n  }\n}'
        meta = annotate_hcl(text)
        assert len(meta.sections) >= 2
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd /root/token-savior && python -m pytest tests/test_markup_hcl.py -v`
Expected: FAIL

- [ ] **Step 3: Write hcl_annotator.py**

```python
"""HCL/Terraform config file annotator.

Regex-based parser for HCL block structure. Handles:
- Block declarations: resource "type" "name" { ... }
- Key-value pairs: key = value
- Nested blocks
No external dependency — pure regex.
Falls back to generic annotator if parsing yields nothing.
"""

import re

from token_savior.generic_annotator import annotate_generic
from token_savior.models import LineRange, SectionInfo, StructuralMetadata

_MAX_DEPTH = 4

# Matches: resource "type" "name" {  or  block_type {  or  block_type "label" {
_BLOCK_RE = re.compile(r'^(\s*)(\w+)\s+((?:"[^"]*"\s*)*)\{')
# Matches: key = value
_KV_RE = re.compile(r'^(\s*)(\w[\w-]*)\s*=\s*(.*)')


def _build_line_offsets(lines: list[str]) -> list[int]:
    offsets: list[int] = []
    pos = 0
    for line in lines:
        offsets.append(pos)
        pos += len(line) + 1
    return offsets


def annotate_hcl(text: str, source_name: str = "<hcl>") -> StructuralMetadata:
    """Parse HCL text and extract blocks and keys as SectionInfo entries."""
    lines = text.split("\n")
    line_offsets = _build_line_offsets(lines)
    sections: list[SectionInfo] = []
    depth_stack: list[int] = []  # tracks brace nesting

    current_depth = 1

    for i, line in enumerate(lines):
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or stripped.startswith("//"):
            continue

        # Check for block opening
        block_match = _BLOCK_RE.match(line)
        if block_match:
            block_type = block_match.group(2)
            labels = re.findall(r'"([^"]*)"', block_match.group(3))
            title = f"{block_type} {' '.join(labels)}".strip()
            if current_depth <= _MAX_DEPTH:
                sections.append(
                    SectionInfo(title=title, level=current_depth, line_range=LineRange(start=i + 1, end=i + 1))
                )
            depth_stack.append(current_depth)
            current_depth += 1
            continue

        # Check for key = value
        kv_match = _KV_RE.match(line)
        if kv_match and current_depth <= _MAX_DEPTH:
            key = kv_match.group(2)
            sections.append(
                SectionInfo(title=key, level=current_depth, line_range=LineRange(start=i + 1, end=i + 1))
            )

        # Track closing braces
        if "}" in stripped:
            if depth_stack:
                current_depth = depth_stack.pop()

    if not sections:
        return annotate_generic(text, source_name)

    return StructuralMetadata(
        source_name=source_name,
        total_lines=len(lines),
        total_chars=len(text),
        lines=lines,
        line_char_offsets=line_offsets,
        sections=sections,
    )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd /root/token-savior && python -m pytest tests/test_markup_hcl.py -v`
Expected: All PASS

- [ ] **Step 5: Commit**

```bash
cd /root/token-savior
git add src/token_savior/hcl_annotator.py tests/test_markup_hcl.py
git commit -m "feat: add HCL/Terraform config annotator"
```

---

### Task 7: Conf Annotator

**Files:**
- Create: `src/token_savior/conf_annotator.py`
- Create: `tests/test_markup_conf.py`

- [ ] **Step 1: Write failing tests**

```python
"""Tests for the .conf annotator (best-effort)."""

from token_savior.conf_annotator import annotate_conf


class TestConfKeyValue:
    def test_equals_syntax(self):
        text = "max_connections = 100\ntimeout = 30"
        meta = annotate_conf(text)
        titles = [s.title for s in meta.sections]
        assert "max_connections" in titles
        assert "timeout" in titles

    def test_colon_syntax(self):
        text = "host: localhost\nport: 8080"
        meta = annotate_conf(text)
        titles = [s.title for s in meta.sections]
        assert "host" in titles
        assert "port" in titles

    def test_source_name_default(self):
        meta = annotate_conf("")
        assert meta.source_name == "<conf>"


class TestConfBlocks:
    def test_nginx_style_blocks(self):
        text = "server {\n    listen 80;\n    server_name example.com;\n}"
        meta = annotate_conf(text)
        titles = [s.title for s in meta.sections]
        assert "server" in titles

    def test_comments_ignored(self):
        text = "# comment\nkey = value\n; another comment"
        meta = annotate_conf(text)
        titles = [s.title for s in meta.sections]
        assert "key" in titles
        assert len(titles) == 1
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd /root/token-savior && python -m pytest tests/test_markup_conf.py -v`
Expected: FAIL

- [ ] **Step 3: Write conf_annotator.py**

```python
"""Generic .conf file annotator (best-effort).

Handles common patterns:
- key = value
- key: value
- block_name { ... }
- Nginx-style directives: directive value;
Falls back to generic annotator if nothing is detected.
"""

import re

from token_savior.generic_annotator import annotate_generic
from token_savior.models import LineRange, SectionInfo, StructuralMetadata

_MAX_DEPTH = 4

_KV_RE = re.compile(r"^(\s*)([A-Za-z_][\w.-]*)\s*[=:]\s*(.*)")
_BLOCK_RE = re.compile(r"^(\s*)([A-Za-z_][\w.-]*)\s*\{")


def _build_line_offsets(lines: list[str]) -> list[int]:
    offsets: list[int] = []
    pos = 0
    for line in lines:
        offsets.append(pos)
        pos += len(line) + 1
    return offsets


def annotate_conf(text: str, source_name: str = "<conf>") -> StructuralMetadata:
    """Parse .conf text and extract keys/blocks as SectionInfo entries."""
    lines = text.split("\n")
    line_offsets = _build_line_offsets(lines)
    sections: list[SectionInfo] = []
    depth_stack: list[int] = []
    current_depth = 1

    for i, line in enumerate(lines):
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or stripped.startswith(";") or stripped.startswith("//"):
            continue

        # Block opening
        block_match = _BLOCK_RE.match(line)
        if block_match:
            name = block_match.group(2)
            if current_depth <= _MAX_DEPTH:
                sections.append(
                    SectionInfo(title=name, level=current_depth, line_range=LineRange(start=i + 1, end=i + 1))
                )
            depth_stack.append(current_depth)
            current_depth += 1
            continue

        # Key-value
        kv_match = _KV_RE.match(line)
        if kv_match and current_depth <= _MAX_DEPTH:
            key = kv_match.group(2)
            sections.append(
                SectionInfo(title=key, level=current_depth, line_range=LineRange(start=i + 1, end=i + 1))
            )
            continue

        # Closing brace
        if stripped == "}" or stripped.startswith("}"):
            if depth_stack:
                current_depth = depth_stack.pop()

    if not sections:
        return annotate_generic(text, source_name)

    return StructuralMetadata(
        source_name=source_name,
        total_lines=len(lines),
        total_chars=len(text),
        lines=lines,
        line_char_offsets=line_offsets,
        sections=sections,
    )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd /root/token-savior && python -m pytest tests/test_markup_conf.py -v`
Expected: All PASS

- [ ] **Step 5: Commit**

```bash
cd /root/token-savior
git add src/token_savior/conf_annotator.py tests/test_markup_conf.py
git commit -m "feat: add .conf config annotator (best-effort)"
```

---

### Task 8: Wire All Annotators into Dispatch + Indexer

**Files:**
- Modify: `src/token_savior/annotator.py`
- Modify: `src/token_savior/project_indexer.py`
- Create: `tests/test_config_dispatch.py`

- [ ] **Step 1: Write failing tests**

```python
"""Tests for config file dispatch through the main annotator."""

from token_savior.annotator import annotate


class TestConfigDispatch:
    def test_yaml_dispatch(self):
        meta = annotate("name: test", source_name="config.yaml")
        assert any(s.title == "name" for s in meta.sections)

    def test_yml_dispatch(self):
        meta = annotate("name: test", source_name="config.yml")
        assert any(s.title == "name" for s in meta.sections)

    def test_toml_dispatch(self):
        meta = annotate('name = "test"', source_name="config.toml")
        assert any(s.title == "name" for s in meta.sections)

    def test_ini_dispatch(self):
        meta = annotate("[section]\nkey = val", source_name="config.ini")
        assert any(s.title == "section" for s in meta.sections)

    def test_cfg_dispatch(self):
        meta = annotate("[section]\nkey = val", source_name="config.cfg")
        assert any(s.title == "section" for s in meta.sections)

    def test_properties_dispatch(self):
        meta = annotate("key=val", source_name="app.properties")
        assert any(s.title == "key" for s in meta.sections)

    def test_env_dispatch(self):
        meta = annotate("DB_HOST=localhost", source_name=".env")
        assert any(s.title == "DB_HOST" for s in meta.sections)

    def test_xml_dispatch(self):
        meta = annotate("<root><key>val</key></root>", source_name="config.xml")
        assert any(s.title == "root" for s in meta.sections)

    def test_plist_dispatch(self):
        meta = annotate("<plist><dict/></plist>", source_name="info.plist")
        assert any("plist" in s.title for s in meta.sections)

    def test_hcl_dispatch(self):
        meta = annotate('variable "x" {\n  default = "y"\n}', source_name="main.tf")
        assert len(meta.sections) > 0

    def test_conf_dispatch(self):
        meta = annotate("key = value", source_name="app.conf")
        assert any(s.title == "key" for s in meta.sections)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd /root/token-savior && python -m pytest tests/test_config_dispatch.py -v`
Expected: FAIL (unknown file types fall through to generic)

- [ ] **Step 3: Update annotator.py**

Add imports at the top of `annotator.py` (after existing imports):
```python
from token_savior.conf_annotator import annotate_conf
from token_savior.env_annotator import annotate_env
from token_savior.hcl_annotator import annotate_hcl
from token_savior.ini_annotator import annotate_ini
from token_savior.toml_annotator import annotate_toml
from token_savior.xml_annotator import annotate_xml
from token_savior.yaml_annotator import annotate_yaml
```

Add to `_EXTENSION_MAP`:
```python
".yaml": "yaml",
".yml": "yaml",
".toml": "toml",
".ini": "ini",
".cfg": "ini",
".properties": "ini",
".env": "env",
".xml": "xml",
".plist": "xml",
".hcl": "hcl",
".tf": "hcl",
".conf": "conf",
```

Add dispatch cases in `annotate()` function (after the `json` case):
```python
elif file_type == "yaml":
    return annotate_yaml(text, source_name)
elif file_type == "toml":
    return annotate_toml(text, source_name)
elif file_type == "ini":
    return annotate_ini(text, source_name)
elif file_type == "env":
    return annotate_env(text, source_name)
elif file_type == "xml":
    return annotate_xml(text, source_name)
elif file_type == "hcl":
    return annotate_hcl(text, source_name)
elif file_type == "conf":
    return annotate_conf(text, source_name)
```

- [ ] **Step 4: Update project_indexer.py default include_patterns**

Add to the `include_patterns` list in `__init__`:
```python
"**/*.yaml",
"**/*.yml",
"**/*.toml",
"**/*.ini",
"**/*.cfg",
"**/*.properties",
"**/.env",
"**/.env.*",
"**/*.xml",
"**/*.plist",
"**/*.hcl",
"**/*.tf",
"**/*.conf",
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `cd /root/token-savior && python -m pytest tests/test_config_dispatch.py -v`
Expected: All PASS

- [ ] **Step 6: Run ALL existing tests to check for regressions**

Run: `cd /root/token-savior && python -m pytest -v`
Expected: All PASS

- [ ] **Step 7: Commit**

```bash
cd /root/token-savior
git add src/token_savior/annotator.py src/token_savior/project_indexer.py tests/test_config_dispatch.py
git commit -m "feat: wire config annotators into dispatch and indexer"
```

---

### Task 9: ConfigIssue Model + Config Analyzer — Duplicates Check

**Files:**
- Modify: `src/token_savior/models.py` (add `ConfigIssue`)
- Create: `src/token_savior/config_analyzer.py`
- Create: `tests/test_config_analyzer.py`

- [ ] **Step 1: Add ConfigIssue to models.py**

Add at the end of `models.py`:
```python
@dataclass
class ConfigIssue:
    """A single issue found by config analysis."""
    file: str
    key: str
    line: int
    severity: str   # "error" | "warning" | "info"
    check: str      # "duplicate" | "secret" | "orphan"
    message: str
    detail: str | None = None
```

- [ ] **Step 2: Write failing tests for duplicates check**

```python
"""Tests for config_analyzer."""

from token_savior.config_analyzer import check_duplicates
from token_savior.models import LineRange, SectionInfo, StructuralMetadata


def _make_meta(source_name: str, sections: list[SectionInfo], lines: list[str] | None = None) -> StructuralMetadata:
    """Helper to build a minimal StructuralMetadata."""
    if lines is None:
        lines = [""] * (max((s.line_range.end for s in sections), default=0) + 1)
    return StructuralMetadata(
        source_name=source_name,
        total_lines=len(lines),
        total_chars=sum(len(l) for l in lines),
        lines=lines,
        line_char_offsets=[0] * len(lines),
        sections=sections,
    )


class TestCheckDuplicates:
    def test_exact_duplicate_in_same_file(self):
        sections = [
            SectionInfo(title="timeout", level=1, line_range=LineRange(start=1, end=1)),
            SectionInfo(title="timeout", level=1, line_range=LineRange(start=5, end=5)),
        ]
        meta = _make_meta("config.yaml", sections)
        issues = check_duplicates({"config.yaml": meta})
        assert len(issues) >= 1
        assert issues[0].check == "duplicate"
        assert "timeout" in issues[0].key

    def test_similar_keys_typo(self):
        sections = [
            SectionInfo(title="db_host", level=1, line_range=LineRange(start=1, end=1)),
            SectionInfo(title="db_hsot", level=1, line_range=LineRange(start=2, end=2)),
        ]
        meta = _make_meta("config.yaml", sections)
        issues = check_duplicates({"config.yaml": meta})
        assert any("similar" in i.message.lower() or "typo" in i.message.lower() for i in issues)

    def test_cross_file_conflict(self):
        sections1 = [SectionInfo(title="PORT", level=1, line_range=LineRange(start=1, end=1))]
        sections2 = [SectionInfo(title="PORT", level=1, line_range=LineRange(start=1, end=1))]
        lines1 = ["PORT=3000"]
        lines2 = ["PORT=8080"]
        meta1 = _make_meta(".env", sections1, lines1)
        meta2 = _make_meta(".env.production", sections2, lines2)
        issues = check_duplicates({".env": meta1, ".env.production": meta2})
        assert any("different" in i.message.lower() for i in issues)

    def test_no_false_positives_different_keys(self):
        sections = [
            SectionInfo(title="host", level=1, line_range=LineRange(start=1, end=1)),
            SectionInfo(title="port", level=1, line_range=LineRange(start=2, end=2)),
        ]
        meta = _make_meta("config.yaml", sections)
        issues = check_duplicates({"config.yaml": meta})
        assert len(issues) == 0

    def test_similar_keys_different_levels_no_flag(self):
        sections = [
            SectionInfo(title="host", level=1, line_range=LineRange(start=1, end=1)),
            SectionInfo(title="host", level=2, line_range=LineRange(start=3, end=3)),
        ]
        meta = _make_meta("config.yaml", sections)
        issues = check_duplicates({"config.yaml": meta})
        # Same key at different nesting levels is valid (e.g. server.host vs db.host)
        assert len(issues) == 0
```

- [ ] **Step 3: Run tests to verify they fail**

Run: `cd /root/token-savior && python -m pytest tests/test_config_analyzer.py -v`
Expected: FAIL

- [ ] **Step 4: Write config_analyzer.py with check_duplicates**

```python
"""Config file analyzer — duplicates, secrets, orphans.

Operates on indexed StructuralMetadata from config files.
"""

from __future__ import annotations

from token_savior.models import ConfigIssue, StructuralMetadata


def _levenshtein(s1: str, s2: str) -> int:
    """Compute Levenshtein distance between two strings."""
    if len(s1) < len(s2):
        return _levenshtein(s2, s1)
    if len(s2) == 0:
        return len(s1)
    prev_row = list(range(len(s2) + 1))
    for i, c1 in enumerate(s1):
        curr_row = [i + 1]
        for j, c2 in enumerate(s2):
            cost = 0 if c1 == c2 else 1
            curr_row.append(min(curr_row[j] + 1, prev_row[j + 1] + 1, prev_row[j] + cost))
        prev_row = curr_row
    return prev_row[-1]


def check_duplicates(config_files: dict[str, StructuralMetadata]) -> list[ConfigIssue]:
    """Detect duplicate and similar keys in config files.

    Checks:
    1. Exact duplicate keys at the same nesting level within a file
    2. Similar keys (Levenshtein <= 2) at the same level within a file
    3. Same key with different values across config files
    """
    issues: list[ConfigIssue] = []

    # Per-file checks
    for filename, meta in config_files.items():
        # Group sections by level
        by_level: dict[int, list] = {}
        for s in meta.sections:
            by_level.setdefault(s.level, []).append(s)

        for level, sections in by_level.items():
            seen: dict[str, int] = {}  # title -> line
            for s in sections:
                if s.title in seen:
                    issues.append(ConfigIssue(
                        file=filename, key=s.title, line=s.line_range.start,
                        severity="warning", check="duplicate",
                        message=f'KEY "{s.title}" duplicate at line {seen[s.title]} (same level)',
                        detail=None,
                    ))
                else:
                    seen[s.title] = s.line_range.start

            # Similar keys (only check unique keys at this level)
            unique_keys = list(seen.keys())
            for i in range(len(unique_keys)):
                for j in range(i + 1, len(unique_keys)):
                    k1, k2 = unique_keys[i], unique_keys[j]
                    if 0 < _levenshtein(k1, k2) <= 2 and len(k1) > 3 and len(k2) > 3:
                        issues.append(ConfigIssue(
                            file=filename, key=k2, line=seen[k2],
                            severity="info", check="duplicate",
                            message=f'KEY "{k2}" similar to "{k1}" (line {seen[k1]}) -- typo?',
                            detail=None,
                        ))

    # Cross-file checks
    all_keys: dict[str, list[tuple[str, str]]] = {}  # key -> [(file, line_content)]
    for filename, meta in config_files.items():
        for s in meta.sections:
            if s.level == 1 and s.line_range.start - 1 < len(meta.lines):
                line_content = meta.lines[s.line_range.start - 1].strip()
                all_keys.setdefault(s.title, []).append((filename, line_content))

    for key, occurrences in all_keys.items():
        if len(occurrences) < 2:
            continue
        values = set(v for _, v in occurrences)
        if len(values) > 1:
            files_str = ", ".join(f for f, _ in occurrences)
            issues.append(ConfigIssue(
                file=occurrences[0][0], key=key, line=0,
                severity="warning", check="duplicate",
                message=f'KEY "{key}" has different values across files: {files_str}',
                detail=None,
            ))

    return issues
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `cd /root/token-savior && python -m pytest tests/test_config_analyzer.py -v`
Expected: All PASS

- [ ] **Step 6: Commit**

```bash
cd /root/token-savior
git add src/token_savior/models.py src/token_savior/config_analyzer.py tests/test_config_analyzer.py
git commit -m "feat: add ConfigIssue model and duplicates check"
```

---

### Task 10: Config Analyzer — Secrets Check

**Files:**
- Modify: `src/token_savior/config_analyzer.py`
- Modify: `tests/test_config_analyzer.py`

- [ ] **Step 1: Write failing tests**

Append to `tests/test_config_analyzer.py`:

```python
from token_savior.config_analyzer import check_secrets


class TestCheckSecrets:
    def test_known_prefix_sk(self):
        lines = ["API_KEY=sk-1234567890abcdef1234567890abcdef"]
        sections = [SectionInfo(title="API_KEY", level=1, line_range=LineRange(start=1, end=1))]
        meta = _make_meta(".env", sections, lines)
        issues = check_secrets({".env": meta})
        assert len(issues) >= 1
        assert issues[0].severity == "error"

    def test_known_prefix_ghp(self):
        lines = ["GITHUB_TOKEN=ghp_abcdefghij1234567890abcdefghij12"]
        sections = [SectionInfo(title="GITHUB_TOKEN", level=1, line_range=LineRange(start=1, end=1))]
        meta = _make_meta(".env", sections, lines)
        issues = check_secrets({".env": meta})
        assert len(issues) >= 1

    def test_suspicious_key_name(self):
        lines = ["password: mysecretpassword123"]
        sections = [SectionInfo(title="password", level=1, line_range=LineRange(start=1, end=1))]
        meta = _make_meta("config.yaml", sections, lines)
        issues = check_secrets({"config.yaml": meta})
        assert len(issues) >= 1

    def test_url_with_credentials(self):
        lines = ["DATABASE_URL=postgres://admin:s3cret@localhost:5432/db"]
        sections = [SectionInfo(title="DATABASE_URL", level=1, line_range=LineRange(start=1, end=1))]
        meta = _make_meta(".env", sections, lines)
        issues = check_secrets({".env": meta})
        assert any("credential" in i.message.lower() for i in issues)

    def test_high_entropy_value(self):
        lines = ["TOKEN=a8Kz9pLm3nQrStUvWxYz1234567890AbCdEf"]
        sections = [SectionInfo(title="TOKEN", level=1, line_range=LineRange(start=1, end=1))]
        meta = _make_meta(".env", sections, lines)
        issues = check_secrets({".env": meta})
        assert len(issues) >= 1

    def test_no_false_positive_on_normal_values(self):
        lines = ["PORT=8080", "HOST=localhost", "DEBUG=true"]
        sections = [
            SectionInfo(title="PORT", level=1, line_range=LineRange(start=1, end=1)),
            SectionInfo(title="HOST", level=1, line_range=LineRange(start=2, end=2)),
            SectionInfo(title="DEBUG", level=1, line_range=LineRange(start=3, end=3)),
        ]
        meta = _make_meta(".env", sections, lines)
        issues = check_secrets({".env": meta})
        assert len(issues) == 0

    def test_begin_private_key(self):
        lines = ['PRIVATE_KEY="-----BEGIN RSA PRIVATE KEY-----"']
        sections = [SectionInfo(title="PRIVATE_KEY", level=1, line_range=LineRange(start=1, end=1))]
        meta = _make_meta(".env", sections, lines)
        issues = check_secrets({".env": meta})
        assert len(issues) >= 1

    def test_masked_value_in_detail(self):
        lines = ["SECRET=sk-abcdefghijklmnop1234567890"]
        sections = [SectionInfo(title="SECRET", level=1, line_range=LineRange(start=1, end=1))]
        meta = _make_meta(".env", sections, lines)
        issues = check_secrets({".env": meta})
        assert issues[0].detail is not None
        assert "sk-" in issues[0].detail
        assert "abcdefghijklmnop1234567890" not in issues[0].detail

    def test_uuid_not_flagged(self):
        lines = ["REQUEST_ID=550e8400-e29b-41d4-a716-446655440000"]
        sections = [SectionInfo(title="REQUEST_ID", level=1, line_range=LineRange(start=1, end=1))]
        meta = _make_meta(".env", sections, lines)
        issues = check_secrets({".env": meta})
        assert len(issues) == 0
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd /root/token-savior && python -m pytest tests/test_config_analyzer.py::TestCheckSecrets -v`
Expected: FAIL

- [ ] **Step 3: Implement check_secrets in config_analyzer.py**

Add to `config_analyzer.py`:

```python
import math
import re

_SECRET_PREFIXES = [
    "sk-", "sk_live_", "sk_test_",
    "ghp_", "gho_", "ghu_", "ghs_",
    "AKIA",
    "-----BEGIN",
    "xox", "xapp-",
    "eyJ",  # JWT
]

_SUSPICIOUS_KEY_NAMES = re.compile(
    r"(password|passwd|secret|token|api_key|apikey|private_key|"
    r"credential|auth|access_key|signing_key|encryption_key)",
    re.IGNORECASE,
)

_URL_WITH_CREDS_RE = re.compile(r"://[^:]+:[^@]+@")

_UUID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$",
    re.IGNORECASE,
)

_SEMVER_RE = re.compile(r"^\d+\.\d+\.\d+")

_HEX_COLOR_RE = re.compile(r"^#[0-9a-fA-F]{3,8}$")


def _shannon_entropy(s: str) -> float:
    """Calculate Shannon entropy of a string."""
    if not s:
        return 0.0
    freq: dict[str, int] = {}
    for c in s:
        freq[c] = freq.get(c, 0) + 1
    length = len(s)
    return -sum((count / length) * math.log2(count / length) for count in freq.values())


def _extract_value(line: str) -> str:
    """Extract the value part from a KEY=VALUE or KEY: VALUE line."""
    for sep in ("=", ":"):
        if sep in line:
            val = line.split(sep, 1)[1].strip()
            # Strip quotes
            if len(val) >= 2 and val[0] in ('"', "'") and val[-1] == val[0]:
                val = val[1:-1]
            return val
    return line.strip()


def _mask_value(value: str) -> str:
    """Mask a secret value, showing first 4 and last 4 chars."""
    if len(value) <= 8:
        return "****"
    return f"{value[:4]}****{value[-4:]}"


def _is_non_secret_pattern(value: str) -> bool:
    """Check if value matches known non-secret patterns."""
    if _UUID_RE.match(value):
        return True
    if _SEMVER_RE.match(value):
        return True
    if _HEX_COLOR_RE.match(value):
        return True
    if value.startswith("/") or value.startswith("./") or value.startswith("~"):
        return True  # file path
    if value.lower() in ("true", "false", "yes", "no", "on", "off", "null", "none"):
        return True
    return False


def check_secrets(config_files: dict[str, StructuralMetadata]) -> list[ConfigIssue]:
    """Detect hardcoded secrets in config files.

    Two engines:
    1. Pattern-based: known prefixes, suspicious key names, URLs with credentials
    2. Entropy-based: Shannon entropy > 4.5 on values >= 16 chars
    """
    issues: list[ConfigIssue] = []

    for filename, meta in config_files.items():
        for s in meta.sections:
            if s.line_range.start - 1 >= len(meta.lines):
                continue
            line = meta.lines[s.line_range.start - 1]
            value = _extract_value(line)

            if not value:
                continue

            # Check known prefixes
            for prefix in _SECRET_PREFIXES:
                if value.startswith(prefix):
                    issues.append(ConfigIssue(
                        file=filename, key=s.title, line=s.line_range.start,
                        severity="error", check="secret",
                        message=f'KEY "{s.title}" matches known secret prefix ({prefix})',
                        detail=f"value: {_mask_value(value)}",
                    ))
                    break
            else:
                # Check URL with embedded credentials
                if _URL_WITH_CREDS_RE.search(value):
                    issues.append(ConfigIssue(
                        file=filename, key=s.title, line=s.line_range.start,
                        severity="warning", check="secret",
                        message=f'KEY "{s.title}" contains embedded credentials in URL',
                        detail=f"value: {_mask_value(value)}",
                    ))
                # Check suspicious key names
                elif _SUSPICIOUS_KEY_NAMES.search(s.title) and len(value) > 3 and value.lower() not in ("true", "false", "none", "null", ""):
                    issues.append(ConfigIssue(
                        file=filename, key=s.title, line=s.line_range.start,
                        severity="warning", check="secret",
                        message=f'KEY "{s.title}" has suspicious name and looks hardcoded',
                        detail=f"value: {_mask_value(value)}",
                    ))
                # Entropy check
                elif len(value) >= 16 and not _is_non_secret_pattern(value):
                    entropy = _shannon_entropy(value)
                    if entropy > 4.5:
                        issues.append(ConfigIssue(
                            file=filename, key=s.title, line=s.line_range.start,
                            severity="warning", check="secret",
                            message=f'KEY "{s.title}" has high entropy ({entropy:.1f})',
                            detail=f"value: {_mask_value(value)}",
                        ))

    return issues
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd /root/token-savior && python -m pytest tests/test_config_analyzer.py -v`
Expected: All PASS

- [ ] **Step 5: Commit**

```bash
cd /root/token-savior
git add src/token_savior/config_analyzer.py tests/test_config_analyzer.py
git commit -m "feat: add secrets detection to config analyzer"
```

---

### Task 11: Config Analyzer — Orphans Check

**Files:**
- Modify: `src/token_savior/config_analyzer.py`
- Modify: `tests/test_config_analyzer.py`

- [ ] **Step 1: Write failing tests**

Append to `tests/test_config_analyzer.py`:

```python
from token_savior.config_analyzer import check_orphans


def _make_code_meta(source_name: str, lines: list[str]) -> StructuralMetadata:
    """Helper for code files — no sections, just lines."""
    return StructuralMetadata(
        source_name=source_name,
        total_lines=len(lines),
        total_chars=sum(len(l) for l in lines),
        lines=lines,
        line_char_offsets=[0] * len(lines),
    )


class TestCheckOrphans:
    def test_orphan_key_not_in_code(self):
        env_lines = ["OLD_API_URL=http://old.example.com"]
        env_sections = [SectionInfo(title="OLD_API_URL", level=1, line_range=LineRange(start=1, end=1))]
        config_files = {".env": _make_meta(".env", env_sections, env_lines)}
        code_files = {"app.py": _make_code_meta("app.py", ['import os', 'db = os.environ["DB_HOST"]'])}
        issues = check_orphans(config_files, code_files)
        assert any(i.key == "OLD_API_URL" and "not found" in i.message for i in issues)

    def test_used_key_not_flagged(self):
        env_lines = ["DB_HOST=localhost"]
        env_sections = [SectionInfo(title="DB_HOST", level=1, line_range=LineRange(start=1, end=1))]
        config_files = {".env": _make_meta(".env", env_sections, env_lines)}
        code_files = {"app.py": _make_code_meta("app.py", ['import os', 'db = os.environ["DB_HOST"]'])}
        issues = check_orphans(config_files, code_files)
        orphan_keys = [i for i in issues if i.key == "DB_HOST" and "not found" in i.message]
        assert len(orphan_keys) == 0

    def test_ghost_key_in_code_not_in_config(self):
        config_files = {".env": _make_meta(".env", [], [""])}
        code_files = {"app.py": _make_code_meta("app.py", ['stripe_key = os.environ["STRIPE_KEY"]'])}
        issues = check_orphans(config_files, code_files)
        assert any(i.key == "STRIPE_KEY" and "no config" in i.message.lower() for i in issues)

    def test_process_env_pattern(self):
        env_lines = ["API_URL=http://localhost"]
        env_sections = [SectionInfo(title="API_URL", level=1, line_range=LineRange(start=1, end=1))]
        config_files = {".env": _make_meta(".env", env_sections, env_lines)}
        code_files = {"index.ts": _make_code_meta("index.ts", ["const url = process.env.API_URL"])}
        issues = check_orphans(config_files, code_files)
        orphan_keys = [i for i in issues if i.key == "API_URL" and "not found" in i.message]
        assert len(orphan_keys) == 0

    def test_orphan_config_file(self):
        env_lines = ["KEY=value"]
        env_sections = [SectionInfo(title="KEY", level=1, line_range=LineRange(start=1, end=1))]
        config_files = {"config/redis.yaml": _make_meta("config/redis.yaml", env_sections, env_lines)}
        code_files = {"app.py": _make_code_meta("app.py", ["print('hello')"])}
        issues = check_orphans(config_files, code_files)
        assert any("redis.yaml" in i.message and "not referenced" in i.message for i in issues)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd /root/token-savior && python -m pytest tests/test_config_analyzer.py::TestCheckOrphans -v`
Expected: FAIL

- [ ] **Step 3: Implement check_orphans in config_analyzer.py**

Add to `config_analyzer.py`:

```python
import os

_ACCESS_PATTERNS: dict[str, list[re.Pattern]] = {
    "python": [
        re.compile(r'os\.environ\[(["\'])(.+?)\1\]'),
        re.compile(r'os\.getenv\((["\'])(.+?)\1'),
        re.compile(r'os\.environ\.get\((["\'])(.+?)\1'),
    ],
    "typescript": [
        re.compile(r'process\.env\.([A-Z_][A-Z0-9_]*)'),
        re.compile(r'process\.env\[(["\'])(.+?)\1\]'),
        re.compile(r'import\.meta\.env\.([A-Z_][A-Z0-9_]*)'),
    ],
    "go": [
        re.compile(r'os\.Getenv\((["\'])(.+?)\1\)'),
        re.compile(r'viper\.\w+\((["\'])(.+?)\1\)'),
    ],
    "rust": [
        re.compile(r'env::var\((["\'])(.+?)\1\)'),
    ],
}

# File types that are "code" (not config)
_CODE_EXTENSIONS = {".py", ".ts", ".tsx", ".js", ".jsx", ".go", ".rs", ".cs"}


def _detect_lang(source_name: str) -> str | None:
    """Detect language from source file name."""
    ext = os.path.splitext(source_name)[1].lower()
    lang_map = {
        ".py": "python",
        ".ts": "typescript", ".tsx": "typescript",
        ".js": "typescript", ".jsx": "typescript",
        ".go": "go",
        ".rs": "rust",
    }
    return lang_map.get(ext)


def _extract_referenced_keys(code_files: dict[str, StructuralMetadata]) -> dict[str, list[tuple[str, int]]]:
    """Extract env/config keys referenced in code files.

    Returns: {key_name: [(file, line_number), ...]}
    """
    referenced: dict[str, list[tuple[str, int]]] = {}

    for filename, meta in code_files.items():
        lang = _detect_lang(filename)
        patterns = []
        if lang and lang in _ACCESS_PATTERNS:
            patterns = _ACCESS_PATTERNS[lang]
        # Also add generic patterns for all languages
        patterns = patterns + [
            re.compile(r'os\.environ\[(["\'])(.+?)\1\]'),
            re.compile(r'process\.env\.([A-Z_][A-Z0-9_]*)'),
        ]

        for i, line in enumerate(meta.lines):
            for pat in patterns:
                for match in pat.finditer(line):
                    # Extract the key name from the match
                    groups = match.groups()
                    key = None
                    for g in reversed(groups):
                        if g and len(g) > 1 and g not in ('"', "'"):
                            key = g
                            break
                    if key:
                        referenced.setdefault(key, []).append((filename, i + 1))

    return referenced


def check_orphans(
    config_files: dict[str, StructuralMetadata],
    code_files: dict[str, StructuralMetadata],
) -> list[ConfigIssue]:
    """Detect orphan config keys and ghost references.

    Checks:
    1. Config keys not referenced in any code file
    2. Code references to keys not defined in any config file
    3. Config files not referenced by name in any code file
    """
    issues: list[ConfigIssue] = []

    # Extract all config keys
    config_keys: dict[str, tuple[str, int]] = {}  # key -> (file, line)
    for filename, meta in config_files.items():
        for s in meta.sections:
            if s.level == 1:  # Only top-level keys
                config_keys[s.title] = (filename, s.line_range.start)

    # Extract all referenced keys from code
    referenced_keys = _extract_referenced_keys(code_files)

    # Also do a simple grep for key names in code
    all_code_text: dict[str, str] = {}
    for filename, meta in code_files.items():
        all_code_text[filename] = "\n".join(meta.lines)
    code_blob = "\n".join(all_code_text.values())

    # Check 1: Orphan config keys (defined but not referenced)
    for key, (filename, line) in config_keys.items():
        if key not in referenced_keys and key not in code_blob:
            issues.append(ConfigIssue(
                file=filename, key=key, line=line,
                severity="warning", check="orphan",
                message=f'KEY "{key}" not found in any source file',
                detail=None,
            ))

    # Check 2: Ghost keys (referenced in code but not in config)
    for key, locations in referenced_keys.items():
        if key not in config_keys:
            first_file, first_line = locations[0]
            issues.append(ConfigIssue(
                file=first_file, key=key, line=first_line,
                severity="warning", check="orphan",
                message=f'Code references "{key}" ({first_file}:{first_line}) but no config file defines it',
                detail=None,
            ))

    # Check 3: Orphan config files (not referenced in code)
    for filename in config_files:
        basename = os.path.basename(filename)
        if basename and not any(basename in text for text in all_code_text.values()):
            issues.append(ConfigIssue(
                file=filename, key="", line=0,
                severity="info", check="orphan",
                message=f'{filename} -- file not referenced in any source file',
                detail=None,
            ))

    return issues
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd /root/token-savior && python -m pytest tests/test_config_analyzer.py -v`
Expected: All PASS

- [ ] **Step 5: Commit**

```bash
cd /root/token-savior
git add src/token_savior/config_analyzer.py tests/test_config_analyzer.py
git commit -m "feat: add orphans detection to config analyzer"
```

---

### Task 12: Main analyze_config Function + Formatter

**Files:**
- Modify: `src/token_savior/config_analyzer.py`
- Modify: `tests/test_config_analyzer.py`

- [ ] **Step 1: Write failing tests**

Append to `tests/test_config_analyzer.py`:

```python
from token_savior.config_analyzer import analyze_config
from token_savior.models import ProjectIndex


def _make_index(files: dict[str, StructuralMetadata]) -> ProjectIndex:
    """Build a minimal ProjectIndex from file metadata."""
    return ProjectIndex(
        root_path="/fake/project",
        file_metadata=files,
        total_files=len(files),
        total_lines=sum(m.total_lines for m in files.values()),
        total_chars=sum(m.total_chars for m in files.values()),
        total_functions=0,
        total_classes=0,
        symbol_table={},
        import_graph={},
        dependency_graph={},
        reverse_dependency_graph={},
    )


CONFIG_FILE_TYPES = {"yaml", "toml", "ini", "env", "xml", "hcl", "conf", "json"}


class TestAnalyzeConfig:
    def test_all_checks_default(self):
        env_lines = ["DB_HOST=localhost", "OLD_KEY=unused_value"]
        env_sections = [
            SectionInfo(title="DB_HOST", level=1, line_range=LineRange(start=1, end=1)),
            SectionInfo(title="OLD_KEY", level=1, line_range=LineRange(start=2, end=2)),
        ]
        code_lines = ['db = os.environ["DB_HOST"]']
        files = {
            ".env": _make_meta(".env", env_sections, env_lines),
            "app.py": _make_code_meta("app.py", code_lines),
        }
        index = _make_index(files)
        result = analyze_config(index)
        assert "Config Analysis" in result

    def test_specific_checks(self):
        env_lines = ["KEY=value"]
        env_sections = [SectionInfo(title="KEY", level=1, line_range=LineRange(start=1, end=1))]
        files = {".env": _make_meta(".env", env_sections, env_lines)}
        index = _make_index(files)
        result = analyze_config(index, checks=["duplicates"])
        assert "duplicates" in result.lower() or "0 issues" in result.lower() or "no issues" in result.lower()

    def test_severity_filter(self):
        env_lines = ["API_SECRET=sk-1234567890abcdef1234567890abcdef"]
        env_sections = [SectionInfo(title="API_SECRET", level=1, line_range=LineRange(start=1, end=1))]
        files = {".env": _make_meta(".env", env_sections, env_lines)}
        index = _make_index(files)
        result_all = analyze_config(index, checks=["secrets"], severity="all")
        result_error = analyze_config(index, checks=["secrets"], severity="error")
        # error filter should still show the error-level issue
        assert "API_SECRET" in result_error

    def test_no_config_files(self):
        code_lines = ["print('hello')"]
        files = {"app.py": _make_code_meta("app.py", code_lines)}
        index = _make_index(files)
        result = analyze_config(index)
        assert "no config" in result.lower() or "0 config" in result.lower()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd /root/token-savior && python -m pytest tests/test_config_analyzer.py::TestAnalyzeConfig -v`
Expected: FAIL

- [ ] **Step 3: Implement analyze_config and formatter**

Add to `config_analyzer.py`:

```python
CONFIG_FILE_TYPES = {"yaml", "toml", "ini", "env", "xml", "hcl", "conf", "json"}

_CONFIG_EXTENSIONS = {
    ".yaml", ".yml", ".toml", ".ini", ".cfg", ".properties",
    ".env", ".xml", ".plist", ".hcl", ".tf", ".conf", ".json",
}


def _is_config_file(filename: str) -> bool:
    """Check if a filename looks like a config file."""
    basename = os.path.basename(filename)
    ext = os.path.splitext(filename)[1].lower()
    if ext in _CONFIG_EXTENSIONS:
        return True
    if basename.startswith(".env"):
        return True
    return False


def _is_code_file(filename: str) -> bool:
    ext = os.path.splitext(filename)[1].lower()
    return ext in _CODE_EXTENSIONS


def _format_issues(all_issues: list[ConfigIssue], severity_filter: str) -> str:
    """Format issues into readable output."""
    if severity_filter == "error":
        all_issues = [i for i in all_issues if i.severity == "error"]
    elif severity_filter == "warning":
        all_issues = [i for i in all_issues if i.severity in ("error", "warning")]

    if not all_issues:
        return "Config Analysis -- 0 issues found"

    by_check: dict[str, list[ConfigIssue]] = {}
    for issue in all_issues:
        by_check.setdefault(issue.check, []).append(issue)

    lines = [f"Config Analysis -- {len(all_issues)} issues found"]

    for check_name in ("duplicates", "secrets", "orphans"):
        check_issues = by_check.get(check_name if check_name != "duplicates" else "duplicate", [])
        if not check_issues:
            continue
        lines.append(f"\n-- {check_name} ({len(check_issues)}) --")
        for issue in check_issues:
            severity_tag = {"error": "[error]", "warning": "[warning]", "info": "[info]"}.get(issue.severity, "[info]")
            loc = f"{issue.file}:{issue.line}" if issue.line > 0 else issue.file
            detail_str = f" ({issue.detail})" if issue.detail else ""
            lines.append(f"{severity_tag} {loc} -- {issue.message}{detail_str}")

    return "\n".join(lines)


def analyze_config(
    index: "ProjectIndex",
    checks: list[str] | None = None,
    file_path: str | None = None,
    severity: str = "all",
) -> str:
    """Run config analysis checks and return formatted results.

    Args:
        index: The project index with file metadata
        checks: List of checks to run (default: all)
        file_path: Specific file to analyze (default: all config files)
        severity: Filter by severity (all/error/warning)
    """
    if checks is None:
        checks = ["duplicates", "secrets", "orphans"]

    # Partition files into config and code
    config_files: dict[str, StructuralMetadata] = {}
    code_files: dict[str, StructuralMetadata] = {}

    for fname, meta in index.file_metadata.items():
        if file_path and fname != file_path:
            if "duplicates" in checks or "secrets" in checks:
                # For targeted analysis, skip non-matching files (but keep all for orphans)
                if "orphans" not in checks:
                    continue
        if _is_config_file(fname):
            if file_path is None or fname == file_path:
                config_files[fname] = meta
        elif _is_code_file(fname):
            code_files[fname] = meta

    if not config_files:
        return "Config Analysis -- 0 config files found in project"

    all_issues: list[ConfigIssue] = []

    if "duplicates" in checks:
        all_issues.extend(check_duplicates(config_files))
    if "secrets" in checks:
        all_issues.extend(check_secrets(config_files))
    if "orphans" in checks:
        all_issues.extend(check_orphans(config_files, code_files))

    return _format_issues(all_issues, severity)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd /root/token-savior && python -m pytest tests/test_config_analyzer.py -v`
Expected: All PASS

- [ ] **Step 5: Commit**

```bash
cd /root/token-savior
git add src/token_savior/config_analyzer.py tests/test_config_analyzer.py
git commit -m "feat: add analyze_config main function with formatter"
```

---

### Task 13: Wire analyze_config into server.py

**Files:**
- Modify: `src/token_savior/server.py`

- [ ] **Step 1: Add the Tool definition to TOOLS list**

Add after the last `Tool(...)` entry in the `TOOLS` list (before the closing `]`):

```python
Tool(
    name="analyze_config",
    description=(
        "Analyze config files for issues: duplicate keys, hardcoded secrets, and orphan entries. "
        "Checks can be filtered via the 'checks' parameter."
    ),
    inputSchema={
        "type": "object",
        "properties": {
            "checks": {
                "type": "array",
                "items": {"type": "string", "enum": ["duplicates", "secrets", "orphans"]},
                "description": 'Checks to run (default: all). Options: "duplicates", "secrets", "orphans".',
            },
            "file_path": {
                "type": "string",
                "description": "Specific config file to analyze. Omit to analyze all config files.",
            },
            "severity": {
                "type": "string",
                "enum": ["all", "error", "warning"],
                "description": 'Filter by severity (default: "all").',
            },
            **_PROJECT_PARAM,
        },
    },
),
```

- [ ] **Step 2: Add the import at the top of server.py**

Add with the other imports:
```python
from token_savior.config_analyzer import analyze_config as run_config_analysis
```

- [ ] **Step 3: Add the handler in call_tool()**

Add before the `_ensure_slot(slot)` / `_maybe_incremental_update(slot)` block (after the `run_project_action` handler), alongside the other tools that need slot but not query_fns:

```python
if name == "analyze_config":
    _ensure_slot(slot)
    _maybe_incremental_update(slot)
    result = run_config_analysis(
        slot.indexer._project_index,
        checks=arguments.get("checks"),
        file_path=arguments.get("file_path"),
        severity=arguments.get("severity", "all"),
    )
    return _count_and_wrap_result(slot, name, arguments, result)
```

- [ ] **Step 4: Run all tests**

Run: `cd /root/token-savior && python -m pytest -v`
Expected: All PASS

- [ ] **Step 5: Commit**

```bash
cd /root/token-savior
git add src/token_savior/server.py
git commit -m "feat: wire analyze_config tool into MCP server"
```

---

### Task 14: Add PyYAML to dependencies + update version

**Files:**
- Modify: `pyproject.toml`

- [ ] **Step 1: Update pyproject.toml**

Change `dependencies = []` to:
```toml
dependencies = ["PyYAML>=6.0"]
```

Update version from `"0.7.1"` to `"0.8.0"` (new feature).

- [ ] **Step 2: Reinstall package**

Run: `cd /root/token-savior && pip install -e ".[mcp,dev]"`

- [ ] **Step 3: Run full test suite**

Run: `cd /root/token-savior && python -m pytest -v`
Expected: All PASS

- [ ] **Step 4: Commit**

```bash
cd /root/token-savior
git add pyproject.toml
git commit -m "chore: add PyYAML dependency, bump to v0.8.0"
```

---

### Task 15: Integration Test — End-to-End

**Files:**
- Create: `tests/test_config_integration.py`

- [ ] **Step 1: Write integration test**

```python
"""Integration test: index a project with config files, run analyze_config."""

import os
import textwrap

from token_savior.project_indexer import ProjectIndexer
from token_savior.config_analyzer import analyze_config


class TestConfigIntegration:
    def test_full_pipeline(self, tmp_path):
        """Create a mini project with code + config, index it, run analysis."""
        # Create config files
        (tmp_path / ".env").write_text("DB_HOST=localhost\nDB_PORT=5432\nOLD_KEY=unused\nSECRET=sk-abcdefghijklmnop1234567890\n")
        (tmp_path / "config.yaml").write_text("database:\n  host: localhost\n  port: 5432\ntimeout: 30\ntimeotu: 30\n")

        # Create code file
        (tmp_path / "app.py").write_text(textwrap.dedent("""\
            import os
            db_host = os.environ["DB_HOST"]
            db_port = os.getenv("DB_PORT")
            missing = os.environ["STRIPE_KEY"]
        """))

        # Index
        indexer = ProjectIndexer(
            str(tmp_path),
            include_patterns=["**/*.py", "**/*.yaml", "**/.env", "**/.env.*"],
        )
        index = indexer.index()

        # Run analysis
        result = analyze_config(index)

        # Should find issues
        assert "Config Analysis" in result
        # Duplicate/similar: timeotu vs timeout
        assert "timeotu" in result or "similar" in result.lower()
        # Secret: sk- prefix
        assert "SECRET" in result or "sk-" in result
        # Orphan: OLD_KEY not used
        assert "OLD_KEY" in result
        # Ghost: STRIPE_KEY not in config
        assert "STRIPE_KEY" in result

    def test_no_config_files(self, tmp_path):
        """Project with only code files — should report 0 config files."""
        (tmp_path / "app.py").write_text("print('hello')\n")
        indexer = ProjectIndexer(str(tmp_path), include_patterns=["**/*.py"])
        index = indexer.index()
        result = analyze_config(index)
        assert "0 config" in result.lower()
```

- [ ] **Step 2: Run integration tests**

Run: `cd /root/token-savior && python -m pytest tests/test_config_integration.py -v`
Expected: All PASS

- [ ] **Step 3: Run full test suite one final time**

Run: `cd /root/token-savior && python -m pytest -v`
Expected: All PASS

- [ ] **Step 4: Commit**

```bash
cd /root/token-savior
git add tests/test_config_integration.py
git commit -m "test: add config analysis integration tests"
```

---

### Task 16: Reindex Token Savior + Smoke Test

- [ ] **Step 1: Reindex the token-savior project itself**

Run: `reindex` via MCP tool to pick up new files.

- [ ] **Step 2: Smoke test with MCP**

Call `analyze_config` on the token-savior project itself to verify it works end-to-end via MCP.

- [ ] **Step 3: Verify the new tool appears in list_tools**

Call `list_tools` and verify `analyze_config` is in the output.
