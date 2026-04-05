# Config File Analysis for Token Savior

**Date:** 2026-04-05
**Status:** Approved

## Problem

Token Savior indexes code symbols (functions, classes, imports) but ignores config files. Ops/DevOps users need to audit config files for schema discovery, duplicate keys, hardcoded secrets, and orphaned config entries.

## Solution

Two additions:
1. **New annotators** for config file formats — parsed as `SectionInfo` entries, integrated into the existing index
2. **`analyze_config` MCP tool** — runs configurable checks (duplicates, secrets, orphans) across indexed config files

---

## Part 1: Config Annotators

One annotator per format family, same pattern as `json_annotator.py`. Keys become `SectionInfo(title=key_path, level=depth)`.

| Annotator | Extensions | Python lib |
|---|---|---|
| `yaml_annotator.py` | `.yaml`, `.yml` | `PyYAML` |
| `toml_annotator.py` | `.toml` | `tomllib` (stdlib 3.11+) |
| `ini_annotator.py` | `.ini`, `.cfg`, `.properties` | `configparser` (stdlib) |
| `env_annotator.py` | `.env` | custom KEY=VALUE parser |
| `xml_annotator.py` | `.xml`, `.plist` | `xml.etree.ElementTree` (stdlib) |
| `hcl_annotator.py` | `.hcl`, `.tf` | `pyhcl2` (optional, regex fallback) |
| `conf_annotator.py` | `.conf` | regex best-effort (key=value / key: value / blocks {}) |

### Changes to existing files

**`annotator.py`** — add extensions to `_EXTENSION_MAP`:
```python
".yaml": "yaml", ".yml": "yaml",
".toml": "toml",
".ini": "ini", ".cfg": "ini", ".properties": "ini",
".env": "env",
".xml": "xml", ".plist": "xml",
".hcl": "hcl", ".tf": "hcl",
".conf": "conf",
```

**`project_indexer.py`** — add to default `include_patterns`:
```python
"**/*.yaml", "**/*.yml", "**/*.toml", "**/*.ini", "**/*.cfg",
"**/*.properties", "**/*.env", "*.env", "**/.env", "**/.env.*",
"**/*.xml", "**/*.plist", "**/*.hcl", "**/*.tf", "**/*.conf",
```

---

## Part 2: `analyze_config` MCP Tool

### Signature

```python
analyze_config(
    checks: list[str] = ["duplicates", "secrets", "orphans"],
    file_path: str | None = None,   # specific file, or whole project
    severity: str = "all",          # "all" | "error" | "warning"
    project: str | None = None
)
```

### Data model

```python
@dataclass
class ConfigIssue:
    file: str
    key: str
    line: int
    severity: str        # "warning" | "error" | "info"
    check: str           # "duplicate" | "secret" | "orphan"
    message: str
    detail: str | None   # masked value, suggestion, etc.
```

### Module: `config_analyzer.py`

```
analyze_config(index, checks, file_path, severity) -> str
  +-- _check_duplicates(index, config_files) -> list[ConfigIssue]
  +-- _check_secrets(index, config_files) -> list[ConfigIssue]
  +-- _check_orphans(index, config_files, code_files) -> list[ConfigIssue]
```

Config file type constant:
```python
CONFIG_FILE_TYPES = {"yaml", "toml", "ini", "env", "xml", "hcl", "conf", "json"}
```

---

### Check: duplicates

- **Exact duplicates** — same key in the same file (YAML/INI allow this silently)
- **Similar keys** — Levenshtein distance <= 2, same nesting level only (e.g. `db_host` vs `db_hsot`)
- **Cross-file conflicts** — same key with different values across config files (e.g. `PORT=3000` in `.env` vs `PORT=8080` in `.env.production`)

### Check: secrets

Two engines combined:

1. **Pattern-based** — regex on:
   - Known prefixes: `sk-`, `ghp_`, `AKIA`, `Bearer`, `-----BEGIN`
   - Suspicious key names: `password`, `secret`, `token`, `api_key`, `private_key`, `credential`
   - URLs with embedded credentials: `://user:pass@`

2. **Entropy-based** — Shannon entropy > 4.5 on string values of length >= 16, excluding:
   - UUIDs
   - Build hashes
   - File paths
   - Semver strings
   - Known non-secret patterns (hex color codes, etc.)

Output includes masked values (`sk-****...****`).

### Check: orphans

Bidirectional code<->config analysis:

**Step 1: Extract keys referenced in code** via language-specific access patterns:

```python
ACCESS_PATTERNS = {
    "python": [
        r'os\.environ\[(["\'])(.+?)\1\]',
        r'os\.getenv\((["\'])(.+?)\1',
        r'os\.environ\.get\((["\'])(.+?)\1',
        r'config\[(["\'])(.+?)\1\]',
        r'settings\.(\w+)',
    ],
    "typescript": [
        r'process\.env\.(\w+)',
        r'process\.env\[(["\'])(.+?)\1\]',
        r'import\.meta\.env\.(\w+)',
    ],
    "go": [
        r'os\.Getenv\((["\'])(.+?)\1\)',
        r'viper\.\w+\((["\'])(.+?)\1\)',
    ],
    "rust": [
        r'std::env::var\((["\'])(.+?)\1\)',
        r'env::var\((["\'])(.+?)\1\)',
    ],
}
```

**Step 2: Extract keys defined in config files** from the indexed SectionInfo entries.

**Step 3: Diff both directions:**
- **Orphan keys** = defined in config but never referenced in code
- **Ghost keys** = referenced in code but absent from all config files
- **Orphan files** = config file basename not found in any source file

**Known limitations (documented, not bugs):**
- Dynamic access (`config[variable]`) is not detectable
- Exact key name matching only, no fuzzy for orphans

---

## Output Format

Compact, actionable text grouped by check:

```
Config Analysis -- 12 issues found

-- duplicates (3) --
[warning] .env:5 -- KEY "DB_HOST" also defined at .env.production:3 (different values)
[warning] config.yaml:12 -- KEY "timeout" duplicate at line 45 (same value)
[info] config.yaml:8 -- KEY "db_hsot" similar to "db_host" (line 6) -- typo?

-- secrets (4) --
[error] .env:8 -- KEY "API_SECRET" matches pattern (high entropy: 5.2, value: sk-****...****)
[error] config.yaml:15 -- KEY "private_key" matches pattern (prefix: -----BEGIN)
[warning] .env:12 -- KEY "WEBHOOK_URL" contains embedded credentials (://user:***@)
[warning] docker-compose.yml:22 -- KEY "POSTGRES_PASSWORD" value looks hardcoded

-- orphans (5) --
[warning] .env:3 -- KEY "OLD_API_URL" not found in any source file
[warning] .env:7 -- KEY "LEGACY_TOKEN" not found in any source file
[info] config/redis.yaml -- file not referenced in any source file
[warning] Code references "STRIPE_KEY" (app/billing.py:14) but no config file defines it
[warning] Code references "SENTRY_DSN" (lib/monitoring.ts:3) but no config file defines it
```

---

## Dependencies

- `PyYAML` — yaml_annotator (new dependency)
- `pyhcl2` — hcl_annotator (optional, fallback to regex if not installed)
- All others use stdlib

## Integration

- `server.py`: one new tool entry in `call_tool()` for `analyze_config`
- `annotator.py`: new dispatch entries in `_EXTENSION_MAP` + imports
- `project_indexer.py`: new globs in default `include_patterns`
- `models.py`: new `ConfigIssue` dataclass

## Design Decisions

- **Single `ConfigIssue` type** rather than per-check dataclasses — keeps it simple, all issues are displayed the same way
- **Levenshtein restricted to same nesting level** — avoids noise from unrelated keys at different depths
- **Entropy threshold 4.5 with length >= 16** — tuned to catch real secrets while excluding UUIDs and build hashes
- **HCL optional dependency** — rather than making pyhcl2 required, fall back to regex for basic key=value extraction
- **`.conf` best-effort** — no standard format, regex for common patterns (key=value, key: value, blocks {}), graceful fallback to generic annotator
